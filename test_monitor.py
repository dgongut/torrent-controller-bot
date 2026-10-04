"""Test battery for the torrent monitor. Run with: python3 test_monitor.py

The monitor is the part of the bot nobody looks at: it runs in the background
and its only output is a message that arrives or does not. A bug there shows up
as silence, or as the same notification every 30 seconds, and both look like
"it works" until it is too late. One of them was already there and only came out
when the external add detection was tested: a torrent that dropped out of the
list for a poll was announced as completed again when it came back.

The bot is loaded against a fake torrent client whose torrents change between
polls (they progress, fail, vanish, get renamed...), so every case is a short
story told poll by poll. Unlike test_callbacks.py, this fake client is mutable
on purpose: what is under test here is how the bot reacts to change."""

import copy
import importlib.util
import itertools
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

os.environ.setdefault("TELEGRAM_TOKEN", "0:fake")
os.environ.setdefault("TELEGRAM_ADMIN", "1")
os.environ.setdefault("LOCALE_PATH", os.path.join(REPO, "locale"))

import config
config.CONFIG_PATH = tempfile.mkdtemp()  # Never touch the real /config

import torrent_clients
from torrent_clients import TorrentClientError, TorrentStatus
from torrent_clients.base import TorrentInfo

FAILED = []
CHECKS = [0]


def check(description, actual, expected):
	CHECKS[0] += 1
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


# ---------------------------------------------------------------------------
# FAKE TORRENT CLIENT (mutable: the torrents change between polls)
# ---------------------------------------------------------------------------

class FakeClient:
	supports_alt_speed = True
	supports_rename = True
	supports_verify = True
	supports_categories = False

	def __init__(self):
		self.reset()

	def reset(self):
		self.torrents = {}  # id -> TorrentInfo, in insertion order
		self.calls = []
		self.fail_list = None  # Exception raised by get_torrents
		self.fail_get = None  # Exception raised by get_torrent
		self.fail_rename = None  # Exception raised by every rename
		self.hide_from_get = set()  # Ids get_torrent answers None for
		self.on_add = None  # Replaces add_torrent

	def put(self, torrent):
		self.torrents[torrent.id] = torrent
		return torrent

	def test_connection(self):
		return "Fake 1.0"

	def get_torrents(self, status=None, query=None):
		if self.fail_list:
			raise self.fail_list
		# Real managers list torrents without their files: the bot has to ask
		# for the full torrent before renaming anything
		light = []
		for torrent in self.torrents.values():
			item = copy.copy(torrent)
			item.files = []
			light.append(item)
		return light

	def get_torrent(self, torrent_id):
		self.calls.append(("get_torrent", torrent_id))
		if self.fail_get:
			raise self.fail_get
		if torrent_id in self.hide_from_get:
			return None
		torrent = self.torrents.get(torrent_id)
		return copy.deepcopy(torrent) if torrent else None

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None, category=None):
		self.calls.append(("add_torrent", magnet, download_dir, category))
		if self.on_add:
			return self.on_add()
		raise AssertionError("add_torrent called without on_add")

	def get_free_space(self, path):
		return 1024 ** 4

	def get_default_download_dir(self):
		return "/downloads"

	def get_download_dirs(self):
		return ["/downloads"]

	def get_categories(self):
		return []

	def get_settings(self):
		return {"version": "Fake 1.0", "alt_speed_enabled": False, "alt_speed_down": 0, "alt_speed_up": 0,
				"speed_limit_down": 0, "speed_limit_down_enabled": False,
				"speed_limit_up": 0, "speed_limit_up_enabled": False, "download_dir": "/downloads"}

	def _renamed(self):
		if self.fail_rename:
			raise self.fail_rename

	def rename_torrent(self, torrent_id, new_name):
		self.calls.append(("rename_torrent", torrent_id, new_name))
		self._renamed()
		torrent = self.torrents[torrent_id]
		old = torrent.name
		torrent.name = new_name
		# The top folder (or the single file) follows the torrent name
		torrent.files = [((new_name + p[len(old):]) if p == old or p.startswith(old + "/") else p, s, c)
						for p, s, c in torrent.files]

	def rename_file(self, torrent_id, old_path, new_name):
		self.calls.append(("rename_file", torrent_id, old_path, new_name))
		self._renamed()
		torrent = self.torrents[torrent_id]
		folder = old_path.rsplit("/", 1)[0] + "/" if "/" in old_path else ""
		torrent.files = [(folder + new_name if p == old_path else p, s, c) for p, s, c in torrent.files]

	def rename_folder(self, torrent_id, old_path, new_name):
		self.calls.append(("rename_folder", torrent_id, old_path, new_name))
		self._renamed()
		torrent = self.torrents[torrent_id]
		parent = old_path.rsplit("/", 1)[0] + "/" if "/" in old_path else ""
		new_path = parent + new_name
		torrent.files = [(new_path + p[len(old_path):] if p.startswith(old_path + "/") else p, s, c)
						for p, s, c in torrent.files]


CLIENT = FakeClient()
torrent_clients.create_client = lambda config_module: CLIENT

_spec = importlib.util.spec_from_file_location("tcbot", os.path.join(REPO, "torrent-controller-bot.py"))
bot = importlib.util.module_from_spec(_spec)
sys.modules["tcbot"] = bot
_spec.loader.exec_module(bot)
bot_settings = bot.bot_settings

real_sleep = time.sleep
time.sleep = lambda seconds: None  # The deferred rename waits between checks

NOTIFIED = []  # (text, chat_id, thread_id)
bot.notify = lambda text, chat_id=None, thread_id=None: NOTIFIED.append((text, chat_id, thread_id))
WARNINGS = []
_real_warning = bot.warning
bot.warning = lambda message: WARNINGS.append(message)
EDITS = []
bot.bot.edit_message_text = lambda text, chat_id, message_id, **kw: EDITS.append((text, kw.get("reply_markup"))) or True
bot.bot.send_message = lambda chat_id, text, **kw: EDITS.append((text, kw.get("reply_markup")))
DEFERRED = []  # Deferred renames started: (torrent_id, original_name, chat_id, thread_id)
_real_deferred = bot.deferred_auto_rename


def _record_deferred(torrent_id, original_name, chat_id=None, thread_id=None):
	DEFERRED.append((torrent_id, original_name, chat_id, thread_id))


PENDING = []  # Real deferred renames started by the bot, run by flush()


class _InlineThread(threading.Thread):
	"""The bot starts the deferred rename in a thread. Recording it is run
	inline; the real one is held until flush(), the way it really runs: in
	the background, after the poll that started it has told what it had to"""
	def start(self):
		if self._target is _record_deferred:
			self._target(*self._args)
		elif self._target is _real_deferred:
			PENDING.append((self._target, self._args))
		else:
			super().start()


def flush():
	while PENDING:
		target, args = PENDING.pop(0)
		target(*args)


bot.threading = type("ThreadingProxy", (), {"Thread": _InlineThread, "Lock": threading.Lock})


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

_ids = itertools.count(1)
MOVIE_NAME = "Some.Movie.2030.1080p.BluRay.x264.mkv"
MOVIE_RENAMED = "Some Movie (2030) - 1080p.mkv"
SERIES_NAME = "Show.Name.S02.1080p.WEB-DL"
SERIES_RENAMED = "T2 - Show Name - 1080p"
MAGNET_NAME = "Magnet.Movie.2012.1080p"
MAGNET_RENAMED = "Magnet Movie (2012) - 1080p"
PLAIN_NAME = "xyzzy"  # Nothing to suggest for it


def new_id():
	return f"{next(_ids):040x}"


def movie(name=MOVIE_NAME, progress=10.0, error="", size=1000, torrent_id=None):
	return TorrentInfo(id=torrent_id or new_id(), name=name, status=TorrentStatus.DOWNLOADING, progress=progress,
					total_size=size, download_dir="/downloads", error_message=error,
					files=[(name, size, progress >= 100)])


def series(name=SERIES_NAME, progress=10.0, error="", torrent_id=None):
	files = [(f"{name}/Show.Name.S02E01.1080p.mkv", 400, False),
			(f"{name}/Show.Name.S02E01.1080p.es.srt", 1, False),
			(f"{name}/Show.Name.S02E02.1080p.mkv", 400, False)]
	return TorrentInfo(id=torrent_id or new_id(), name=name, status=TorrentStatus.DOWNLOADING, progress=progress,
					total_size=801, download_dir="/downloads", error_message=error, files=files)


def magnet(name=MAGNET_NAME, torrent_id=None):
	# qBittorrent reports -1 as the size of a magnet still without metadata
	return TorrentInfo(id=torrent_id or new_id(), name=name, status=TorrentStatus.DOWNLOADING, progress=0.0,
					total_size=-1, download_dir="/downloads", files=[])


def plain(torrent_id=None):
	return movie(name=PLAIN_NAME, torrent_id=torrent_id)


KINDS = {"movie": movie, "series": series, "magnet": magnet, "plain": plain}


def configure(notify_completed=True, notify_errors=True, notify_external=False, auto_rename=False,
			rename_files=False, rename_external=False, supports_rename=True):
	bot_settings.set("notify_completed", notify_completed)
	bot_settings.set("notify_errors", notify_errors)
	bot_settings.set("notify_external_added", notify_external)
	bot_settings.set("auto_rename", auto_rename)
	bot_settings.set("auto_rename_files", rename_files)
	bot_settings.set("auto_rename_external", rename_external)
	FakeClient.supports_rename = supports_rename


def fresh(**settings):
	"""A clean client, no notifications, no registered adds and the given settings"""
	CLIENT.reset()
	configure(**settings)
	NOTIFIED.clear()
	WARNINGS.clear()
	DEFERRED.clear()
	PENDING.clear()
	bot._bot_added_ids.clear()
	bot.deferred_auto_rename = _record_deferred


def texts():
	return [text for text, _, _ in NOTIFIED]


def external_header(torrent):
	return bot.get_text("NOTIFY_EXTERNAL_ADDED", bot.html.escape(torrent.name), bot.html.escape(torrent.download_dir))


def completed_text(name):
	return bot.get_text("NOTIFY_COMPLETED", bot.html.escape(name))


def error_text(name, message):
	return bot.get_text("NOTIFY_TORRENT_ERROR", bot.html.escape(name), bot.html.escape(message))


def count_external():
	header = bot.get_text("NOTIFY_EXTERNAL_ADDED", "", "").split("\n")[0]
	return sum(header in t for t in texts())


def renames():
	return [c for c in CLIENT.calls if c[0].startswith("rename")]


def bot_add(torrent):
	"""Adds a torrent the way the bot does, registering it"""
	CLIENT.on_add = lambda: CLIENT.put(torrent)
	return bot.add_torrent_from_bot(magnet="magnet:?xt=urn:btih:x")


def poll(states):
	return bot.poll_torrents(states)


# ---------------------------------------------------------------------------
# 1. BASELINE: the first poll only learns what is there
# ---------------------------------------------------------------------------

fresh(notify_external=True, auto_rename=True, rename_files=True, rename_external=True)
done = CLIENT.put(movie(progress=100.0))
broken = CLIENT.put(movie(name="Broken.Movie.2001.mkv", error="Tracker gave HTTP 404"))
CLIENT.put(series())
CLIENT.put(magnet())
states = poll(None)
check("the first poll notifies nothing, whatever there is", NOTIFIED, [])
check("the first poll renames nothing", renames(), [])
check("the first poll starts no deferred rename", DEFERRED, [])
check("the first poll learns every torrent", sorted(states), sorted(CLIENT.torrents))
check("the first poll never asks for a full torrent", [c for c in CLIENT.calls if c[0] == "get_torrent"], [])
states = poll(states)
check("a second poll with nothing new notifies nothing", NOTIFIED, [])

fresh(notify_external=True)
states = poll(None)
check("a manager with no torrents is a valid baseline", states, {})
CLIENT.put(movie())
states = poll(states)
check("the first torrent after an empty baseline is external", count_external(), 1)

# ---------------------------------------------------------------------------
# 2. COMPLETION
# ---------------------------------------------------------------------------

fresh()
t = CLIENT.put(movie())
states = poll(None)
t.progress = 50.0
states = poll(states)
check("a download in progress notifies nothing", NOTIFIED, [])
t.progress = 100.0
states = poll(states)
check("a download that finishes is notified", texts(), [completed_text(t.name)])
states = poll(states)
states = poll(states)
check("a finished download is notified only once", texts(), [completed_text(t.name)])

NOTIFIED.clear()
t.progress = 99.0  # A recheck that found missing pieces
states = poll(states)
check("going back from finished notifies nothing", NOTIFIED, [])
t.progress = 100.0
states = poll(states)
check("finishing again after a recheck is notified again", texts(), [completed_text(t.name)])

fresh(notify_completed=False)
t = CLIENT.put(movie())
states = poll(None)
t.progress = 100.0
states = poll(states)
check("with the completion setting off nothing is told", NOTIFIED, [])
bot_settings.set("notify_completed", True)
states = poll(states)
check("turning it on later does not tell what already finished", NOTIFIED, [])

fresh()
states = poll(None)
t = bot_add(movie(progress=100.0))
states = poll(states)
check("a torrent added by the bot that finished between polls is told as completed",
	texts(), [completed_text(t.name)])

fresh(notify_external=True)
states = poll(None)
t = CLIENT.put(movie(progress=100.0))
states = poll(states)
check("an external torrent already finished is told as added and as completed", len(NOTIFIED), 2)
check("and the add goes before the completion",
	[external_header(t) in NOTIFIED[0][0], NOTIFIED[1][0]], [True, completed_text(t.name)])

fresh()
states = poll(None)
CLIENT.put(movie(progress=100.0))
states = poll(states)
check("with the external setting off a finished external torrent is only told as completed",
	count_external(), 0)
check("but its completion is still told", len(NOTIFIED), 1)

# Several finishing in the same poll
fresh()
many = [CLIENT.put(movie(name=f"Movie.{n}.2000.mkv")) for n in range(5)]
states = poll(None)
for torrent in many:
	torrent.progress = 100.0
states = poll(states)
check("several downloads finishing at once are each told once",
	sorted(texts()), sorted(completed_text(t.name) for t in many))

# ---------------------------------------------------------------------------
# 3. ERRORS
# ---------------------------------------------------------------------------

fresh()
t = CLIENT.put(movie())
states = poll(None)
t.error_message = "No space left on device"
states = poll(states)
check("a new error is told", texts(), [error_text(t.name, "No space left on device")])
states = poll(states)
check("the same error is told only once", len(NOTIFIED), 1)
t.error_message = "Tracker gave HTTP 404"
states = poll(states)
check("a different error is told again", texts()[-1], error_text(t.name, "Tracker gave HTTP 404"))
t.error_message = ""
states = poll(states)
check("an error that clears tells nothing", len(NOTIFIED), 2)
t.error_message = "Tracker gave HTTP 404"
states = poll(states)
check("an error that comes back after clearing is told again", len(NOTIFIED), 3)

fresh(notify_errors=False)
t = CLIENT.put(movie())
states = poll(None)
t.error_message = "boom"
states = poll(states)
check("with the error setting off nothing is told", NOTIFIED, [])

fresh()
t = CLIENT.put(movie(error="Already broken"))
states = poll(None)
states = poll(states)
check("an error already there at startup is not told", NOTIFIED, [])

fresh(notify_external=True)
states = poll(None)
t = CLIENT.put(movie(error="Bad torrent"))
states = poll(states)
check("an external torrent that comes broken is told as added and as broken",
	[external_header(t) in NOTIFIED[0][0], NOTIFIED[1][0]], [True, error_text(t.name, "Bad torrent")])

fresh()
t = CLIENT.put(movie())
states = poll(None)
t.progress = 100.0
t.error_message = "Unregistered torrent"
states = poll(states)
check("finishing and failing in the same poll tells both",
	texts(), [completed_text(t.name), error_text(t.name, "Unregistered torrent")])

# ---------------------------------------------------------------------------
# 4. TORRENTS THAT DROP OUT OF THE LIST AND COME BACK
# ---------------------------------------------------------------------------

# A manager restarting (or a list request that returns early) can leave out
# torrents for a poll. None of them is new, and none of them finished again.

fresh(notify_external=True, auto_rename=True, rename_external=True)
everything = [CLIENT.put(movie(name=f"Old.Movie.{n}.2000.mkv", progress=100.0 if n % 2 else 30.0)) for n in range(6)]
states = poll(None)
saved = dict(CLIENT.torrents)
CLIENT.torrents = {k: v for k, v in list(saved.items())[:2]}
states = poll(states)
check("torrents leaving the list tell nothing", NOTIFIED, [])
CLIENT.torrents = saved
states = poll(states)
check("torrents coming back are neither external nor completed again", NOTIFIED, [])
check("torrents coming back are never renamed", renames(), [])

CLIENT.torrents = {}
states = poll(states)
CLIENT.torrents = saved
states = poll(states)
check("a manager that lists nothing for a poll tells nothing when it comes back", NOTIFIED, [])

# But what happened while it was out of sight still counts
fresh()
t = CLIENT.put(movie(progress=40.0))
states = poll(None)
hidden = CLIENT.torrents.pop(t.id)
states = poll(states)
hidden.progress = 100.0
CLIENT.put(hidden)
states = poll(states)
check("a torrent that finished while out of the list is told as completed", texts(), [completed_text(t.name)])

fresh()
t = CLIENT.put(movie())
states = poll(None)
hidden = CLIENT.torrents.pop(t.id)
states = poll(states)
hidden.error_message = "Missing files"
CLIENT.put(hidden)
states = poll(states)
check("a torrent that broke while out of the list is told as broken", texts(), [error_text(t.name, "Missing files")])

# ---------------------------------------------------------------------------
# 5. EXTERNAL ADDS: every combination of settings and kind of torrent
# ---------------------------------------------------------------------------

# Five switches and four kinds of torrent: 128 situations, each one checked
# against what it should do, worked out independently of the bot's own code.

EXPECTED_NAME = {"movie": MOVIE_RENAMED, "series": SERIES_RENAMED, "magnet": None, "plain": None}

combinations = 0
for notify_external, auto_rename, rename_files, rename_external, supports_rename in itertools.product((False, True), repeat=5):
	for kind, factory in KINDS.items():
		combinations += 1
		label = (f"[notify={notify_external} rename={auto_rename} files={rename_files} "
				f"external={rename_external} supported={supports_rename} {kind}]")
		fresh(notify_external=notify_external, auto_rename=auto_rename, rename_files=rename_files,
			rename_external=rename_external, supports_rename=supports_rename)
		states = poll(None)
		t = CLIENT.put(factory())
		original = t.name
		states = poll(states)

		renaming = supports_rename and auto_rename and rename_external
		expected_name = EXPECTED_NAME[kind] if renaming else None
		files_renamed = renaming and rename_files and kind == "series"
		deferred = renaming and kind == "magnet"
		told = notify_external or bool(expected_name) or files_renamed

		check(f"{label} it is told only when asked or renamed", count_external(), 1 if told else 0)
		check(f"{label} nothing else is told", len(NOTIFIED), 1 if told else 0)
		check(f"{label} the torrent name", CLIENT.torrents[t.id].name, expected_name or original)
		check(f"{label} the files are renamed only with both scopes on",
			any(c[0] == "rename_file" for c in renames()), files_renamed)
		check(f"{label} the deferred rename waits for the metadata", [d[0] for d in DEFERRED], [t.id] if deferred else [])
		if deferred:
			check(f"{label} the deferred rename tells the default destination", DEFERRED[0][2:], (None, None))
		if told:
			text, chat_id, thread_id = NOTIFIED[0]
			check(f"{label} it goes to the configured destination", (chat_id, thread_id), (None, None))
			check(f"{label} it names the torrent as it was added", external_header(movie(name=original)) in text, True)
			check(f"{label} it tells the new name",
				bot.get_text("ADD_AUTO_RENAMED", expected_name) in text if expected_name else "✏️" not in text, True)
			check(f"{label} it tells the pending rename only for a magnet being renamed",
				bot.get_text("ADD_AUTO_RENAME_PENDING") in text, deferred)
			check(f"{label} a magnet has no size to tell", "💾" in text, kind != "magnet")
			check(f"{label} it tells the renamed contents only when renamed",
				"🪄" in text, files_renamed)
		states = poll(states)
		check(f"{label} a later poll repeats nothing", count_external(), 1 if told else 0)
		check(f"{label} a later poll renames nothing more",
			len(renames()), len([c for c in CLIENT.calls if c[0].startswith("rename")]))

check("every combination was exercised", combinations, 128)

# The contents of a series end up where they should
fresh(auto_rename=True, rename_files=True, rename_external=True)
states = poll(None)
t = CLIENT.put(series())
states = poll(states)
check("the episodes of an external series are renamed",
	sorted(p for p, _, _ in CLIENT.torrents[t.id].files),
	sorted([f"{SERIES_RENAMED}/2x01 - Show Name - 1080p.mkv", f"{SERIES_RENAMED}/2x01 - Show Name - 1080p.es.srt",
			f"{SERIES_RENAMED}/2x02 - Show Name - 1080p.mkv"]))
check("the contents go before the torrent", renames()[-1][0], "rename_torrent")

# ---------------------------------------------------------------------------
# 6. BOT ADDS ARE NEVER EXTERNAL
# ---------------------------------------------------------------------------

for kind, factory in KINDS.items():
	fresh(notify_external=True, auto_rename=True, rename_files=True, rename_external=True)
	states = poll(None)
	t = bot_add(factory())
	states = poll(states)
	check(f"a {kind} added by the bot is never told as external", count_external(), 0)
	check(f"a {kind} added by the bot is not renamed by the monitor", renames(), [])
	check(f"a {kind} added by the bot is not given a deferred rename by the monitor", DEFERRED, [])
	check(f"the bot forgets a {kind} once the monitor has seen it", t.id in bot._bot_added_ids, False)

fresh()
states = poll(None)
t = bot_add(movie())
states = poll(states)
check("with every setting off the bot still forgets what the monitor saw", bot._bot_added_ids, {})

fresh(notify_external=True)
states = poll(None)
mine = [bot_add(movie(name=f"Mine.{n}.2000.mkv")) for n in range(3)]
theirs = [CLIENT.put(movie(name=f"Theirs.{n}.2000.mkv")) for n in range(4)]
states = poll(states)
check("in a mixed poll only the external ones are told", count_external(), 4)
check("each external one is told by name",
	sorted(t.name for t in theirs if any(external_header(t) in x for x in texts())), sorted(t.name for t in theirs))

fresh(notify_external=True)
states = poll(None)
burst = [CLIENT.put(movie(name=f"Burst.{n}.2000.mkv")) for n in range(40)]
states = poll(states)
check("a burst of external adds is told one by one", count_external(), 40)
states = poll(states)
check("a burst of external adds is told only once", count_external(), 40)

# An id that comes back as a number: the registry and the monitor must agree
fresh(notify_external=True)
states = poll(None)
numeric = movie(torrent_id=None)
numeric.id = 4242
bot_add(numeric)
states = poll(states)
check("a numeric id added by the bot is not external", count_external(), 0)

# Ids nobody ever sees again do not pile up forever
fresh()
bot._bot_added_ids["stale"] = time.time() - bot.BOT_ADDED_TTL - 1
bot._bot_added_ids["recent"] = time.time()
CLIENT.on_add = lambda: CLIENT.put(movie())
bot.add_torrent_from_bot(magnet="m")
check("a registered id older than the TTL is dropped", "stale" in bot._bot_added_ids, False)
check("a recent registered id is kept", "recent" in bot._bot_added_ids, True)

# A failed add registers nothing and never leaves the lock taken
fresh(notify_external=True)
states = poll(None)
def _failing_add():
	raise TorrentClientError("duplicate")
CLIENT.on_add = _failing_add
try:
	bot.add_torrent_from_bot(magnet="m")
	check("a failed add raises", False, True)
except TorrentClientError:
	pass
check("a failed add registers nothing", bot._bot_added_ids, {})
check("a failed add releases the lock", bot._bot_adds_lock.acquire(timeout=1), True)
bot._bot_adds_lock.release()

# The add through the bot still says what it said before
fresh(auto_rename=True, rename_files=True)
CLIENT.on_add = lambda: CLIENT.put(series())
text = bot.perform_add_torrent({"magnet": "m", "data": None}, "/downloads")
lines = text.split("\n")
check("the bot add tells the rename", bot.get_text("ADD_AUTO_RENAMED", SERIES_RENAMED) in lines, True)
check("the bot add tells the rename before the contents",
	lines.index(bot.get_text("ADD_AUTO_RENAMED", SERIES_RENAMED)) < next(i for i, l in enumerate(lines) if l.startswith("🪄")), True)
check("the bot add tells the size", bot.get_text("ADD_OK_SIZE", bot.sizeof_fmt(801)) in lines, True)

for size in (0, -1, None):
	fresh()
	CLIENT.on_add = lambda: CLIENT.put(magnet())
	torrent_size = size
	def _add_sized():
		t = CLIENT.put(magnet())
		t.total_size = torrent_size
		return t
	CLIENT.on_add = _add_sized
	text = bot.perform_add_torrent({"magnet": "m", "data": None}, "/downloads")
	check(f"a bot add with size {size!r} tells no size", "💾" in text, False)

fresh(auto_rename=True)
CLIENT.on_add = lambda: CLIENT.put(magnet())
text = bot.perform_add_torrent({"magnet": "m", "data": None}, "/downloads", chat_id=55, thread_id=7)
check("a magnet added by the bot waits for its metadata", len(DEFERRED), 1)
check("and answers in the conversation it came from", DEFERRED[0][2:], (55, 7))
check("and says so", bot.get_text("ADD_AUTO_RENAME_PENDING") in text, True)

# ---------------------------------------------------------------------------
# 7. THE RACE: the manager lists a torrent before the bot's add returns
# ---------------------------------------------------------------------------

# qBittorrent does not answer an add with the new torrent: the bot looks for it
# by a temporary tag, so for a moment the torrent is in the list and the bot
# does not know its id yet. A poll right then must not take it for external.

fresh(notify_external=True)
states = poll(None)
listed = threading.Event()
release = threading.Event()
racing = movie(name="Racing.Movie.2020.mkv")


def _slow_add():
	CLIENT.put(racing)
	listed.set()
	release.wait(5)
	return racing


CLIENT.on_add = _slow_add
adder = threading.Thread(target=bot.add_torrent_from_bot, kwargs={"magnet": "m"})
adder.start()
listed.wait(5)
result = {}
poller = threading.Thread(target=lambda: result.setdefault("states", poll(states)))
poller.start()
real_sleep(0.3)
check("a poll that sees a torrent being added waits for the add", poller.is_alive(), True)
release.set()
adder.join(5)
poller.join(5)
check("the poll finishes once the add is done", poller.is_alive(), False)
check("the torrent being added is not taken for external", count_external(), 0)
states = poll(result["states"])
check("nor later", count_external(), 0)

# Download Station answers a duplicate with a task in error that the bot then
# deletes: a poll can list it in between, and it must not be announced
fresh(notify_external=True)
states = poll(None)
listed.clear()
release.clear()
ghost = movie(name="Ghost.Movie.2020.mkv")


def _duplicate_add():
	CLIENT.put(ghost)
	listed.set()
	release.wait(5)
	del CLIENT.torrents[ghost.id]
	raise TorrentClientError("duplicate")


CLIENT.on_add = _duplicate_add


def _add_ignoring_errors():
	try:
		bot.add_torrent_from_bot(magnet="m")
	except TorrentClientError:
		pass


adder = threading.Thread(target=_add_ignoring_errors)
adder.start()
listed.wait(5)
poller = threading.Thread(target=lambda: result.update(states=poll(states)))
poller.start()
real_sleep(0.3)
release.set()
adder.join(5)
poller.join(5)
check("a task the bot deleted after a failed add is not announced", NOTIFIED, [])

# ---------------------------------------------------------------------------
# 8. FAILURES ON THE WAY
# ---------------------------------------------------------------------------

fresh(notify_external=True)
t = CLIENT.put(movie())
states = poll(None)
CLIENT.fail_list = TorrentClientError("connection refused")
before = copy.deepcopy(states)
try:
	poll(states)
	check("a manager that cannot be reached raises", False, True)
except TorrentClientError:
	pass
check("a failed poll keeps what was known", states, before)
CLIENT.fail_list = None
t.progress = 100.0
states = poll(states)
check("after a failed poll the next one carries on, not a new baseline", texts(), [completed_text(t.name)])

fresh(notify_external=True)
states = poll(None)
t = CLIENT.put(movie())
CLIENT.fail_get = TorrentClientError("timeout")
states = poll(states)
check("an external torrent that cannot be read is not told", NOTIFIED, [])
check("and the failure is logged", any("externally added" in w for w in WARNINGS), True)
CLIENT.fail_get = None
states = poll(states)
check("and it is not retried on every poll", NOTIFIED, [])

fresh(notify_external=True)
states = poll(None)
t = CLIENT.put(movie())
CLIENT.hide_from_get.add(t.id)
states = poll(states)
check("an external torrent gone before it is read is not told", NOTIFIED, [])

fresh(notify_external=True, auto_rename=True, rename_files=True, rename_external=True)
states = poll(None)
t = CLIENT.put(series())
CLIENT.fail_rename = TorrentClientError("rename refused")
states = poll(states)
check("an external torrent that cannot be renamed is still told", count_external(), 1)
check("without claiming a rename", "✏️" in texts()[0], False)
check("and the failure is logged", any("Auto-rename failed" in w for w in WARNINGS), True)

fresh(notify_external=False, auto_rename=True, rename_external=True)
states = poll(None)
t = CLIENT.put(movie())
CLIENT.fail_rename = TorrentClientError("rename refused")
states = poll(states)
check("with only the rename on, a failed rename tells nothing", NOTIFIED, [])

# Something nobody expected inside the external handling must not take the
# rest of the poll down with it, nor make it handle that torrent again forever
fresh(notify_external=True, auto_rename=True, rename_external=True)
other = CLIENT.put(movie(name="Other.Movie.2010.mkv"))
states = poll(None)
t = CLIENT.put(movie())
other.progress = 100.0
CLIENT.fail_rename = RuntimeError("unexpected")
states = poll(states)
check("an unexpected failure is logged", any("Cannot handle the externally added torrent" in w for w in WARNINGS), True)
check("the rest of the poll goes on", completed_text(other.name) in texts(), True)
CLIENT.fail_rename = None
NOTIFIED.clear()
states = poll(states)
check("the torrent that failed is not handled again", NOTIFIED, [])

# A name already taken
fresh(notify_external=False, auto_rename=True, rename_external=True)
CLIENT.put(movie(name=MOVIE_RENAMED, progress=100.0))
states = poll(None)
t = CLIENT.put(movie())
states = poll(states)
check("an external torrent whose new name is taken keeps its name", CLIENT.torrents[t.id].name, MOVIE_NAME)
check("and that is told even with the add notification off",
	bot.get_text("ADD_AUTO_RENAME_DUPLICATE", bot.html.escape(MOVIE_RENAMED)) in "".join(texts()), True)

# Names and paths are HTML in the message
fresh(notify_external=True)
states = poll(None)
t = CLIENT.put(movie(name="<b>Evil & Co</b>.2020.mkv"))
t.download_dir = "/data/<i>"
states = poll(states)
check("the name is escaped", "&lt;b&gt;Evil &amp; Co&lt;/b&gt;" in texts()[0], True)
check("the folder is escaped", "/data/&lt;i&gt;" in texts()[0], True)
check("nothing raw slips through", "<i>" in texts()[0] or "Evil & Co" in texts()[0], False)

fresh(notify_external=True)
states = poll(None)
t = CLIENT.put(movie())
t.download_dir = ""
states = poll(states)
check("a torrent with no folder is still told", count_external(), 1)

for size, shown in ((0, False), (-1, False), (None, False), (1, True), (5 * 1024 ** 3, True)):
	fresh(notify_external=True)
	states = poll(None)
	t = CLIENT.put(movie())
	t.total_size = size
	states = poll(states)
	check(f"an external torrent of size {size!r} tells its size: {shown}", "💾" in texts()[0], shown)

# ---------------------------------------------------------------------------
# 9. THE DEFERRED RENAME OF AN EXTERNAL MAGNET
# ---------------------------------------------------------------------------

bot.deferred_auto_rename = _real_deferred


class MetadataLater:
	"""get_torrent answers without files a few times, then with them"""

	def __init__(self, after, full):
		self.after, self.full, self.asked = after, full, 0

	def __call__(self, torrent_id):
		self.asked += 1
		CLIENT.calls.append(("get_torrent", torrent_id))
		if self.asked <= self.after:
			return magnet(torrent_id=torrent_id)
		CLIENT.torrents[torrent_id] = self.full
		return copy.deepcopy(self.full)


_real_get = FakeClient.get_torrent

for rename_files in (False, True):
	fresh(auto_rename=True, rename_files=rename_files, rename_external=True)
	bot.deferred_auto_rename = _real_deferred
	m = magnet()
	full = series(torrent_id=m.id)
	CLIENT.put(m)
	CLIENT.get_torrent = MetadataLater(3, full)
	_real_deferred(m.id, m.name)
	CLIENT.get_torrent = _real_get.__get__(CLIENT)
	names = texts()
	check(f"[files={rename_files}] the magnet is renamed once its metadata arrives", CLIENT.torrents[m.id].name, SERIES_RENAMED)
	check(f"[files={rename_files}] and that is told", any(bot.get_text("NOTIFY_AUTO_RENAMED", MAGNET_NAME, SERIES_RENAMED) == n for n in names), True)
	check(f"[files={rename_files}] the contents follow the setting", any(c[0] == "rename_file" for c in CLIENT.calls), rename_files)
	check(f"[files={rename_files}] it goes to the configured destination", {(c, th) for _, c, th in NOTIFIED}, {(None, None)})

fresh(auto_rename=True, rename_external=True)
m = CLIENT.put(magnet())
CLIENT.get_torrent = lambda torrent_id: None
_real_deferred(m.id, m.name)
CLIENT.get_torrent = _real_get.__get__(CLIENT)
check("a magnet deleted while waiting is left alone", (NOTIFIED, renames()), ([], []))

fresh(auto_rename=True, rename_external=True)
m = CLIENT.put(magnet())
_real_deferred(m.id, m.name)
check("a magnet whose metadata never arrives is not renamed", renames(), [])
check("and giving up is logged", any("gave up" in w for w in WARNINGS), True)
check("it asked as many times as configured",
	len([c for c in CLIENT.calls if c[0] == "get_torrent"]), bot.AUTO_RENAME_WAIT_ATTEMPTS)

fresh(auto_rename=True, rename_external=True)
m = CLIENT.put(magnet())
CLIENT.fail_get = TorrentClientError("gone")
_real_deferred(m.id, m.name)
check("a manager failing while waiting stops the wait", len([c for c in CLIENT.calls if c[0] == "get_torrent"]), 1)
check("and that is logged", any("Auto-rename failed" in w for w in WARNINGS), True)

# The whole chain: the monitor finds a magnet and the real deferred rename finishes it
fresh(notify_external=True, auto_rename=True, rename_external=True)
bot.deferred_auto_rename = _real_deferred
states = poll(None)
m = CLIENT.put(magnet())
full = movie(name=MAGNET_NAME, torrent_id=m.id)
CLIENT.get_torrent = MetadataLater(2, full)
states = poll(states)
check("monitor and deferred rename together: the poll does not wait for the metadata", len(PENDING), 1)
flush()
CLIENT.get_torrent = _real_get.__get__(CLIENT)
check("monitor and deferred rename together: told as added", count_external(), 1)
check("monitor and deferred rename together: renamed in the end", CLIENT.torrents[m.id].name, MAGNET_RENAMED)
check("monitor and deferred rename together: the rename told after the add",
	[external_header(m) in texts()[0], texts()[-1]], [True, bot.get_text("NOTIFY_AUTO_RENAMED", MAGNET_NAME, MAGNET_RENAMED)])

# ---------------------------------------------------------------------------
# 10. SETTINGS
# ---------------------------------------------------------------------------

# A settings file from before 1.6.0 knows nothing of the new keys
with open(bot_settings.SETTINGS_FILE, "w", encoding="utf-8") as file:
	file.write('{"notify_completed": false, "auto_rename": true, "auto_rename_files": true, "gone_key": 1}')
bot_settings._settings = None
check("an old settings file keeps what it had", (bot_settings.get("notify_completed"), bot_settings.get("auto_rename")), (False, True))
check("the external notification starts off", bot_settings.get("notify_external_added"), False)
check("the external rename starts off", bot_settings.get("auto_rename_external"), False)
check("unknown keys are dropped", bot_settings.get("gone_key"), None)

os.remove(bot_settings.SETTINGS_FILE)
bot_settings._settings = None
check("a fresh install has the external notification off", bot_settings.get("notify_external_added"), False)
check("a fresh install has the external rename off", bot_settings.get("auto_rename_external"), False)


def settings_screen():
	EDITS.clear()
	bot.dispatch_callback(1, 100, 1, bot.build_call("settings"))
	text, markup = EDITS[-1]
	return text, [b.callback_data for row in markup.keyboard for b in row]


def toggle(key):
	bot.dispatch_callback(1, 100, 1, bot.build_call("toggleSetting", key))


for auto_rename, rename_files, rename_external, supports_rename in itertools.product((False, True), repeat=4):
	label = f"[rename={auto_rename} files={rename_files} external={rename_external} supported={supports_rename}]"
	configure(auto_rename=auto_rename, rename_files=rename_files and auto_rename,
			rename_external=rename_external and auto_rename, supports_rename=supports_rename)
	text, buttons = settings_screen()
	children = auto_rename and supports_rename
	check(f"{label} the external notification is always offered", "toggleSetting|notify_external_added" in buttons, True)
	check(f"{label} the rename is offered when the manager can rename", "toggleSetting|auto_rename" in buttons, supports_rename)
	check(f"{label} the files scope only under an active rename", "toggleSetting|auto_rename_files" in buttons, children)
	check(f"{label} the external scope only under an active rename", "toggleSetting|auto_rename_external" in buttons, children)
	if children:
		check(f"{label} the files scope goes right under the rename",
			buttons.index("toggleSetting|auto_rename_files"), buttons.index("toggleSetting|auto_rename") + 1)
		check(f"{label} the external scope goes right after it",
			buttons.index("toggleSetting|auto_rename_external"), buttons.index("toggleSetting|auto_rename") + 2)
	summary = bot.get_text("SETTINGS_AUTO_RENAME", "").split(":")[0]
	check(f"{label} the summary is shown when the manager can rename", summary in text, supports_rename)
	if supports_rename:
		if not auto_rename:
			expected = bot.get_text("SETTINGS_AUTO_RENAME", bot.get_text("DISABLED"))
		else:
			expected = bot.get_text("SETTINGS_AUTO_RENAME", bot.get_text(
				"SETTINGS_AUTO_RENAME_ON",
				bot.get_text("AUTO_RENAME_SCOPE_FILES" if rename_files else "AUTO_RENAME_SCOPE_TORRENT"),
				bot.get_text("AUTO_RENAME_SOURCE_ALL" if rename_external else "AUTO_RENAME_SOURCE_BOT")))
		check(f"{label} the summary says exactly what is renamed", expected in text.split("\n"), True)

FakeClient.supports_rename = True
configure(auto_rename=True, rename_files=True, rename_external=True)
toggle("auto_rename")
check("turning the rename off turns the files scope off", bot_settings.get("auto_rename_files"), False)
check("turning the rename off turns the external scope off", bot_settings.get("auto_rename_external"), False)
toggle("auto_rename")
check("turning the rename back on does not bring the scopes back",
	(bot_settings.get("auto_rename_files"), bot_settings.get("auto_rename_external")), (False, False))
toggle("auto_rename_external")
check("the external scope turns on alone", (bot_settings.get("auto_rename_files"), bot_settings.get("auto_rename_external")), (False, True))
toggle("auto_rename_files")
toggle("auto_rename_external")
check("each scope turns off without touching the other",
	(bot_settings.get("auto_rename_files"), bot_settings.get("auto_rename_external")), (True, False))
configure()
toggle("notify_external_added")
check("the external notification turns on", bot_settings.get("notify_external_added"), True)
check("without touching the rename", bot_settings.get("auto_rename"), False)
toggle("notify_external_added")
check("the external notification turns off", bot_settings.get("notify_external_added"), False)

with open(bot_settings.SETTINGS_FILE, encoding="utf-8") as file:
	stored = file.read()
check("the new settings are saved to disk", ('"notify_external_added"' in stored, '"auto_rename_external"' in stored), (True, True))

# Every text the new settings use exists in both languages, with the same placeholders
import json
import re
locales = {lang: json.load(open(os.path.join(REPO, "locale", f"{lang}.json"), encoding="utf-8")) for lang in ("es", "en")}
for key in ("BUTTON_SETTING_NOTIFY_EXTERNAL", "BUTTON_SETTING_AUTO_RENAME_EXTERNAL", "BUTTON_SETTING_AUTO_RENAME_FILES",
			"NOTIFY_EXTERNAL_ADDED", "SETTINGS_AUTO_RENAME", "SETTINGS_AUTO_RENAME_ON", "AUTO_RENAME_SCOPE_TORRENT",
			"AUTO_RENAME_SCOPE_FILES", "AUTO_RENAME_SOURCE_BOT", "AUTO_RENAME_SOURCE_ALL"):
	check(f"{key} exists in both languages", all(key in texts_ for texts_ in locales.values()), True)
	check(f"{key} has the same placeholders in both languages",
		sorted(re.findall(r"\$\d", locales["es"].get(key, ""))), sorted(re.findall(r"\$\d", locales["en"].get(key, ""))))
check("both languages have exactly the same keys", sorted(locales["es"]), sorted(locales["en"]))

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({CHECKS[0]} checks)")
