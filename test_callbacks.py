"""Test battery for the inline keyboards. Run with: python3 test_callbacks.py

Telegram refuses the WHOLE keyboard with 400 BUTTON_DATA_INVALID as soon as one
button carries more than 64 bytes of callback_data. The screen then never
changes and the button looks dead, which is how a too long confirmDelete made
the delete button useless when the list was filtered by tracker.

The bot is loaded here against a fake torrent client and a fake Telegram that
enforces that same limit, so any screen that would be refused fails the test.
The crawler below walks every screen reachable from the dashboard, which is what
keeps this honest: it does not need to know in advance which keyboard is new."""

import importlib.util
import os
import re
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

os.environ.setdefault("TELEGRAM_TOKEN", "0:fake")
os.environ.setdefault("TELEGRAM_ADMIN", "1")
os.environ.setdefault("LOCALE_PATH", os.path.join(REPO, "locale"))

import config
config.CONFIG_PATH = tempfile.mkdtemp()  # Never touch the real /config

import torrent_clients
from torrent_clients import TorrentStatus
from torrent_clients.base import SessionSummary, TorrentInfo

CALLBACK_LIMIT = 64  # Telegram hard limit, see MAX_CALLBACK_DATA_BYTES
TEXT_LIMIT = 4096  # Telegram hard limit for a message body
BUTTONS_LIMIT = 100  # Telegram hard limit for buttons in one inline keyboard
TORRENT_ID = "0123456789abcdef0123456789abcdef01234567"  # A qBittorrent hash is 40 hex chars
CHAT_ID = 1
MESSAGE_ID = 100

FAILED = []
CALLS = []  # Everything the fake client was asked to do
EDITS = []  # (text, markup) of every screen Telegram accepted
REJECTIONS = []  # Keyboards Telegram refused, even if the bot swallowed the error
SENDS = []  # (chat_id, message_thread_id) of every message sent as a new one


def check(description, actual, expected):
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def fail(description):
	FAILED.append(description)


# ---------------------------------------------------------------------------
# FAKE TORRENT CLIENT
# ---------------------------------------------------------------------------

# Deliberately awkward values: long directories, files to rename and several
# trackers, and enough of everything to paginate (which is where the goto button
# and the dir pages appear), so the id and context generators are exercised
DIRS = ["/mnt/user/media/peliculas/completadas", "/mnt/user/media/series/en-curso",
		"/downloads"] + [f"/mnt/disk{n}/descargas/completadas" for n in range(9)]
FILES = ([("Some.Movie.2024.1080p/Some.Movie.2024.1080p.mkv", 1024, True),
		("Some.Movie.2024.1080p/Some.Movie.2024.1080p.es.srt", 16, True)]
		+ [(f"Some.Movie.2024.1080p/extra{n}.mkv", 8, True) for n in range(8)])

# Enough torrents for several list pages; the first one is the one under test
TORRENT_IDS = [TORRENT_ID] + [f"{n:040x}" for n in range(1, 12)]


# Long names are not a corner case: release names and season packs really look
# like this, and they end up inside the message body, not only in the buttons
LONG_NAME = ("Some.Long.Series.Name.S01.COMPLETE.2160p.UHD.BluRay.REMUX.DV.HDR10Plus."
			"HEVC.TrueHD.7.1.Atmos-SOMEVERYLONGRELEASEGROUPNAME")
LONG_FILES = [(f"{LONG_NAME}/{LONG_NAME}.S01E{n:02d}.mkv", 1024, True) for n in range(1, 21)]

# 255 characters is what a single path component can hold on ext4 or btrfs, and
# a screen listing ten of those files goes well past the Telegram message limit
EXTREME_NAME = ("Some.Absurdly.Long.Series.Name.With.Everything.S01.COMPLETE.2160p.UHD.BluRay."
				"REMUX.DoVi.HDR10Plus.HEVC.TrueHD.7.1.Atmos.MULTi.SUBS.PROPER.REPACK.iNTERNAL-"
				"AVERYVERYLONGRELEASEGROUPNAMEHERE").ljust(255, "X")[:255]
EXTREME_FILES = [(f"{EXTREME_NAME}/{EXTREME_NAME[:200]}.S01E{n:02d}.mkv", 1024, True)
				for n in range(1, 21)]

CATEGORIES = [("Peliculas", "/mnt/user/media/peliculas"), ("Series", "/mnt/user/media/series"),
			("Documentales", "/mnt/user/media/documentales")]

PROFILE = {"name": None, "files": None, "auto_managed": False}  # None = the plain values above


def fake_torrent(torrent_id=TORRENT_ID):
	index = TORRENT_IDS.index(torrent_id)
	name = PROFILE["name"] or f"Some.Movie.{2024 - index}.1080p"
	files = PROFILE["files"] or FILES
	# Half the torrents carry a category so the category filter has both kinds
	category = CATEGORIES[index % len(CATEGORIES)][0] if index % 2 == 0 else ""
	return TorrentInfo(id=torrent_id, name=name, status=TorrentStatus.SEEDING,
					progress=100.0, total_size=1024, downloaded=1024, uploaded=512, ratio=0.5,
					download_dir=DIRS[0], files=list(files),
					category=category, auto_managed=PROFILE["auto_managed"],
					trackers=["tracker.example.org", "otro.tracker.example.net"])


class FakeClient:
	"""Records what it is told to do and never mutates its torrent, so the
	crawler keeps finding the same screens no matter what order it presses"""

	supports_alt_speed = True
	supports_rename = True
	supports_verify = True
	supports_categories = True

	def test_connection(self):
		return "qBittorrent 5.1.0 (fake)"

	def get_summary(self):
		return SessionSummary(counts={TorrentStatus.SEEDING: len(TORRENT_IDS)}, total=len(TORRENT_IDS),
							completed=len(TORRENT_IDS), uploading=1, downloading=0,
							download_rate=0, upload_rate=10, free_space=1024 ** 4)

	def get_torrents(self, status=None, query=None):
		return [fake_torrent(tid) for tid in TORRENT_IDS]

	def get_torrent(self, torrent_id):
		return fake_torrent(torrent_id) if torrent_id in TORRENT_IDS else None

	def get_download_dirs(self):
		return list(DIRS)

	def get_default_download_dir(self):
		return DIRS[0]

	def get_free_space(self, path):
		return 1024 ** 4

	def get_settings(self):
		return {"version": "qBittorrent 5.1.0", "alt_speed_enabled": False,
				"alt_speed_down": 100, "alt_speed_up": 50,
				"speed_limit_down": 0, "speed_limit_down_enabled": False,
				"speed_limit_up": 0, "speed_limit_up_enabled": False, "download_dir": DIRS[0]}

	def get_categories(self):
		return list(CATEGORIES)

	def set_category(self, torrent_ids, category):
		CALLS.append(("set_category", list(torrent_ids), category))

	def set_auto_managed(self, torrent_ids, enabled):
		CALLS.append(("set_auto_managed", list(torrent_ids), enabled))

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None, category=None):
		CALLS.append(("add_torrent", download_dir, category))
		return fake_torrent()

	def remove_torrents(self, torrent_ids, delete_data=False):
		CALLS.append(("remove_torrents", list(torrent_ids), delete_data))

	def pause_torrents(self, torrent_ids):
		CALLS.append(("pause_torrents", list(torrent_ids)))

	def resume_torrents(self, torrent_ids):
		CALLS.append(("resume_torrents", list(torrent_ids)))

	def verify_torrent(self, torrent_id):
		CALLS.append(("verify_torrent", torrent_id))

	def rename_torrent(self, torrent_id, new_name):
		CALLS.append(("rename_torrent", torrent_id, new_name))

	def rename_file(self, torrent_id, old_path, new_name):
		CALLS.append(("rename_file", torrent_id, old_path, new_name))

	def move_torrents(self, torrent_ids, new_dir):
		CALLS.append(("move_torrents", list(torrent_ids), new_dir))

	def set_alt_speed(self, enabled):
		CALLS.append(("set_alt_speed", enabled))

	def set_speed_limit(self, direction, kbps, enabled):
		CALLS.append(("set_speed_limit", direction, kbps, enabled))


torrent_clients.create_client = lambda config_module: FakeClient()

_spec = importlib.util.spec_from_file_location("tcbot", os.path.join(REPO, "torrent-controller-bot.py"))
bot_module = importlib.util.module_from_spec(_spec)
sys.modules["tcbot"] = bot_module
_spec.loader.exec_module(bot_module)

bot_module._dashboard_refresher = lambda *a, **k: None  # No background repainting during the test

real_sleep = time.sleep
time.sleep = lambda seconds: None  # The bot waits on the client; the fake one is instant


# ---------------------------------------------------------------------------
# FAKE TELEGRAM (enforces the callback_data limit exactly like the real API)
# ---------------------------------------------------------------------------

class FakeTelegramError(Exception):
	pass


def callbacks_of(markup):
	if markup is None or not hasattr(markup, "keyboard"):
		return []
	return [button.callback_data or "" for row in markup.keyboard for button in row]


def unbalanced_tags(text):
	stack = []
	for closing, tag in re.findall(r"<(/?)([a-z]+)[^>]*>", text):
		if closing:
			if not stack or stack.pop() != tag:
				return True
		else:
			stack.append(tag)
	return bool(stack)


def reject_oversized(markup, text, where):
	"""edit_message() swallows every exception and only logs a warning, exactly
	how this bug stayed invisible, so rejections are recorded here as well"""
	def refuse(message):
		REJECTIONS.append(f"{where}: {message}")
		raise FakeTelegramError(f"{where}: {message}")

	if text is not None and len(text) > TEXT_LIMIT:
		refuse(f"Bad Request: message is too long ({len(text)} chars)")
	if text is not None and unbalanced_tags(text):
		refuse(f"Bad Request: can't parse entities in: {text[-80:]!r}")
	buttons = callbacks_of(markup)
	if len(buttons) > BUTTONS_LIMIT:
		refuse(f"Bad Request: too many inline keyboard buttons ({len(buttons)})")
	for data in buttons:
		size = len(data.encode("utf-8"))
		if size > CALLBACK_LIMIT:
			refuse(f"Bad Request: BUTTON_DATA_INVALID ({size} bytes: {data})")


def fake_edit_message_text(text, chat_id, message_id, **kwargs):
	reject_oversized(kwargs.get("reply_markup"), text, "edit_message_text")
	EDITS.append((text, kwargs.get("reply_markup")))
	return True


def fake_send_message(chat_id, text, **kwargs):
	reject_oversized(kwargs.get("reply_markup"), text, "send_message")
	EDITS.append((text, kwargs.get("reply_markup")))
	SENDS.append((chat_id, kwargs.get("message_thread_id")))
	return type("FakeMessage", (), {"message_id": MESSAGE_ID})()


bot_module.bot.edit_message_text = fake_edit_message_text
bot_module.bot.send_message = fake_send_message
bot_module.bot.delete_message = lambda *a, **k: True


def _expired_category():
	"""The error raised when a category filter points at a context that is gone"""
	try:
		bot_module.get_filtered_torrents("cffffff")
	except Exception as e:
		return e
	return None


def press(call):
	"""Runs a callback and returns the screen it produced, or None if the
	keyboard was refused (which is exactly what a dead button looks like)"""
	before = len(EDITS)
	bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, call)
	return EDITS[-1] if len(EDITS) > before else None


def tracker_filter():
	return "t" + bot_module.new_tracker_context("tracker.example.org")


# ---------------------------------------------------------------------------
# 1. CRAWLER: every screen reachable from the dashboard must be accepted
# ---------------------------------------------------------------------------

# "cerrar" just deletes the message and "noop" does nothing, so they lead nowhere
SKIPPED = {"cerrar", "noop"}
MAX_STEPS = 2000


def normalize(call):
	"""Context ids are random and minted on every render, so two callbacks that
	only differ in their context id are the same screen for crawling purposes"""
	parts = []
	for part in call.split("|"):
		if re.fullmatch(r"[0-9a-f]{6}|[0-9a-f]{8}", part):
			parts.append("<ctx>")
		elif re.fullmatch(r"t[0-9a-f]{6}", part):
			parts.append("t<ctx>")
		else:
			parts.append(part)
	return "|".join(parts)


def crawl():
	"""Presses every button of every screen reachable from the dashboard and
	reports the ones Telegram would refuse"""
	pending = [bot_module.build_call("dashboard"), bot_module.build_call("trackers")]
	seen = {normalize(call) for call in pending}
	refused = []
	visited = 0

	while pending and visited < MAX_STEPS:
		call = pending.pop(0)
		visited += 1
		EDITS.clear()
		REJECTIONS.clear()
		try:
			bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, call)
		except Exception as e:  # A crash is also a dead button for the user
			refused.append(f"pressing {call}\n  -> {type(e).__name__}: {e}")
			continue
		for rejection in REJECTIONS:
			refused.append(f"pressing {call}\n  -> {rejection}")
		for _text, markup in EDITS:
			for found in callbacks_of(markup):
				if not found or found.split("|")[0] in SKIPPED:
					continue
				key = normalize(found)
				if key not in seen:
					seen.add(key)
					pending.append(found)

	real_sleep(0.5)  # Let the background move workers finish before asserting
	return visited, refused, {call.split("|")[0] for call in seen}


# Plain data first, then release names long enough to stress the message body
PROFILES = {
	"plain names": {"name": None, "files": None},
	"long release names": {"name": LONG_NAME, "files": LONG_FILES},
	"names at the filesystem limit": {"name": EXTREME_NAME, "files": EXTREME_FILES},
	# qBittorrent relocating by itself: the move button is gone and the category
	# screen is the one that decides where the data lives
	"auto managed torrents": {"name": None, "files": None, "auto_managed": True},
}

crawled = 0
for label, profile in PROFILES.items():
	PROFILE.update(profile)
	bot_module.nav_contexts.clear()
	visited, refused, reached = crawl()
	crawled += visited
	check(f"[{label}] the crawler reached a meaningful number of screens", visited > 40, True)
	check(f"[{label}] the crawler did not hit the step cap", visited < MAX_STEPS, True)
	if refused:
		fail(f"[{label}] {len(refused)} screens were refused by Telegram, first ones:\n"
			+ "\n".join(refused[:5]))

	# Sanity: the crawl really went through the screens this bug lived in
	for command in ("list", "info", "delete", "confirmDelete", "rename", "renameAuto", "renameManual",
					"move", "moveToDir", "files", "file", "fileAuto", "mass", "confirmMass",
					"settings", "toggleSetting", "favDirsMenu", "tplMenu", "trackers", "goto",
					"categories", "catMenu", "catSet"):
		check(f"[{label}] the crawler reached the {command} screen", command in reached, True)

PROFILE.update(PROFILES["plain names"])
PROFILE["auto_managed"] = False

# ---------------------------------------------------------------------------
# 2. DELETE: the reported bug, under every filter
# ---------------------------------------------------------------------------

def delete_flow(filter_key, with_data):
	EDITS.clear()
	CALLS.clear()
	screen = press(bot_module.build_call("delete", TORRENT_ID, filter_key, 0))
	if screen is None:
		return None
	press(callbacks_of(screen[1])[1 if with_data else 0])
	return [c for c in CALLS if c[0] == "remove_torrents"]


check("delete with data, status filter",
	delete_flow(config.FILTER_ALL, True), [("remove_torrents", [TORRENT_ID], True)])
check("delete keeping data, status filter",
	delete_flow(config.FILTER_ALL, False), [("remove_torrents", [TORRENT_ID], False)])
check("delete with data, tracker filter",
	delete_flow(tracker_filter(), True), [("remove_torrents", [TORRENT_ID], True)])
check("delete keeping data, tracker filter",
	delete_flow(tracker_filter(), False), [("remove_torrents", [TORRENT_ID], False)])

# ---------------------------------------------------------------------------
# 3. A confirmation that is no longer valid must never delete anything
# ---------------------------------------------------------------------------

EDITS.clear()
CALLS.clear()
press(f"confirmDelete|{TORRENT_ID}|1|al|0")  # Pre-1.3.1 message still sitting in a chat
check("a stale delete confirmation deletes nothing", CALLS, [])
check("a stale delete confirmation explains why", EDITS[-1][0], bot_module.get_text("DELETE_EXPIRED"))

EDITS.clear()
CALLS.clear()
expired = bot_module.new_nav_context(TORRENT_ID, config.FILTER_ALL, 0)
bot_module.nav_contexts.clear()  # As if the context TTL had run out
press(bot_module.build_call("confirmDelete", expired, "1"))
check("an expired delete context deletes nothing", CALLS, [])
check("an expired delete context explains why", EDITS[-1][0], bot_module.get_text("DELETE_EXPIRED"))

EDITS.clear()
CALLS.clear()
expired = bot_module.new_nav_context(TORRENT_ID, config.FILTER_ALL, 0)
bot_module.nav_contexts.clear()
press(bot_module.build_call("renameAuto", expired))
check("an expired rename context renames nothing", CALLS, [])
check("an expired rename context explains why", EDITS[-1][0], bot_module.get_text("RENAME_EXPIRED"))

# ---------------------------------------------------------------------------
# 4. RENAME still works end to end after moving to a nav context
# ---------------------------------------------------------------------------

EDITS.clear()
CALLS.clear()
screen = press(bot_module.build_call("rename", TORRENT_ID, tracker_filter(), 999))
check("the rename screen is accepted", screen is not None, True)
if screen is not None:
	press(callbacks_of(screen[1])[0])  # Automatic rename
	check("automatic rename reaches the client",
		[c[:2] for c in CALLS if c[0] == "rename_torrent"], [("rename_torrent", TORRENT_ID)])

# ---------------------------------------------------------------------------
# 5. WORST CASE: longest filter key and a three digit page
# ---------------------------------------------------------------------------

# A tracker filter ("t" + 6 hex) is the longest filter key there is, and the
# page number grows with the torrent count and TORRENTS_PER_PAGE
LONG_FILTER = tracker_filter()
check("a tracker filter key is 7 bytes", len(LONG_FILTER), 7)

COMMANDS_WITH_TORRENT_ID = ("info", "pause", "resume", "verify", "rename", "move", "delete")
for command in COMMANDS_WITH_TORRENT_ID:
	call = bot_module.build_call(command, TORRENT_ID, LONG_FILTER, 999)
	check(f"{command} fits in callback_data ({len(call.encode())} bytes)",
		len(call.encode("utf-8")) <= CALLBACK_LIMIT, True)

# The screens reached from the detail view build their own keyboards, and that
# is where the long command names (confirmDelete, renameManual) used to overflow
for entry in ("info", "delete", "rename", "move", "files"):
	EDITS.clear()
	if entry == "files":
		ctx = bot_module.new_nav_context(TORRENT_ID, LONG_FILTER, 999)
		screen = press(bot_module.build_call("files", ctx, 0))
	else:
		screen = press(bot_module.build_call(entry, TORRENT_ID, LONG_FILTER, 999))
	check(f"the {entry} screen is accepted by Telegram", screen is not None, True)
	if screen is None:
		continue
	for call in callbacks_of(screen[1]):
		check(f"{entry} screen button fits ({len(call.encode())} bytes): {call}",
			len(call.encode("utf-8")) <= CALLBACK_LIMIT, True)

# ---------------------------------------------------------------------------
# 6. Long messages are cut instead of being silently rejected
# ---------------------------------------------------------------------------

short = "<b>Short</b>\nnothing to cut here"
check("a message that fits is left alone", bot_module.clamp_text(short), short)

long_lines = "\n".join(f"• <code>file number {n}</code>" for n in range(400))
clamped = bot_module.clamp_text(long_lines)
check("a long message is brought under the limit", len(clamped) <= TEXT_LIMIT, True)
check("a cut message says so", clamped.endswith(bot_module.get_text("MESSAGE_TRUNCATED")), True)
check("a cut message keeps its HTML balanced", unbalanced_tags(clamped), False)
check("a cut message keeps as much as it can", len(clamped) > TEXT_LIMIT - 200, True)

# Worst case for the cut: a single line longer than the whole limit, with the
# tags left open right where the text has to be chopped
one_line = "<b>" + "x" * (TEXT_LIMIT * 2) + "</b>"
clamped = bot_module.clamp_text(one_line)
check("a single oversized line is cut too", len(clamped) <= TEXT_LIMIT, True)
check("a single oversized line keeps its HTML balanced", unbalanced_tags(clamped), False)
check("the cut never leaves half a tag", "<" in clamped.split(">")[-1], False)

# ---------------------------------------------------------------------------
# 7. build_call complains instead of failing silently
# ---------------------------------------------------------------------------

warnings = []
_original_warning = bot_module.warning
bot_module.warning = warnings.append
bot_module.build_call("oversized", "x" * 70)
bot_module.build_call("fine", "x" * 10)
bot_module.warning = _original_warning
check("an oversized callback_data is logged", len(warnings), 1)
check("a callback_data that fits is not logged", "fine" in "".join(warnings), False)
check("the limit matches Telegram's", getattr(config, "MAX_CALLBACK_DATA_BYTES", None), CALLBACK_LIMIT)

# ---------------------------------------------------------------------------
# 8. TOPICS: an answer never leaves the topic the button was pressed in
# ---------------------------------------------------------------------------

# A screen that edits its own message stays in the right topic by itself, but
# the ones that answer with a NEW message (every ForceReply prompt, the move
# progress, the dashboard) have to be told the thread or Telegram drops them
# into General, which is what "the bot always writes in the same topic" was
THREAD = 7


def press_in_thread(call, wait=False):
	SENDS.clear()
	EDITS.clear()
	bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, call, thread_id=THREAD)
	if wait:
		real_sleep(0.3)  # The move order answers from a background worker
	return list(SENDS)


# Every screen reachable from the dashboard, pressed inside a topic
pending = [bot_module.build_call("dashboard"), bot_module.build_call("trackers")]
seen = {normalize(call) for call in pending}
strays = []
sent_anything = 0
real_sleep(0.5)  # No stray background worker from the crawl above may count here
while pending and len(seen) < MAX_STEPS:
	call = pending.pop(0)
	try:
		sends = press_in_thread(call)
	except Exception:
		continue
	sent_anything += len(sends)
	for chat_id, thread in sends:
		if thread != THREAD:
			strays.append(f"pressing {call} sent a message to thread {thread!r}")
	for _text, markup in EDITS:
		for found in callbacks_of(markup):
			if not found or found.split("|")[0] in SKIPPED:
				continue
			key = normalize(found)
			if key not in seen:
				seen.add(key)
				pending.append(found)

check("some screens really answer with a new message", sent_anything > 0, True)
if strays:
	fail(f"{len(strays)} answers left the topic, first ones:\n" + "\n".join(strays[:5]))

# The prompts are the ones the user notices: they are a new message every time
for call, command in ((bot_module.build_call("search"), "search"),
					(bot_module.build_call("setDownLimit"), "setDownLimit"),
					(bot_module.build_call("favDirAdd"), "favDirAdd"),
					(bot_module.build_call("tplEdit", "movie"), "tplEdit")):
	sends = press_in_thread(call, wait=True)
	check(f"the {command} prompt is asked in the same topic", sends, [(CHAT_ID, THREAD)])

# Outside a topic nothing must be tagged, and General (thread 1) is addressed
# by omitting the thread, not by sending message_thread_id=1
SENDS.clear()
bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("search"))
check("no topic means no message_thread_id", SENDS, [(CHAT_ID, None)])
SENDS.clear()
bot_module.send_message(CHAT_ID, "x", thread_id=1)
check("the General topic is addressed by omitting the thread", SENDS, [(CHAT_ID, None)])

check("a missing thread normalizes to none", bot_module.normalize_thread(None), None)
check("an empty thread normalizes to none", bot_module.normalize_thread(""), None)
check("the General topic normalizes to none", bot_module.normalize_thread(1), None)
check("a real topic is kept", bot_module.normalize_thread("7"), 7)

# Two dashboards in two topics of the same group are two live dashboards
bot_module.dashboards.clear()
bot_module.show_dashboard(CHAT_ID, thread_id=THREAD)
bot_module.show_dashboard(CHAT_ID, thread_id=THREAD + 1)
check("each topic keeps its own dashboard", sorted(bot_module.dashboards), [(CHAT_ID, THREAD), (CHAT_ID, THREAD + 1)])
bot_module.dashboards.clear()

# ---------------------------------------------------------------------------
# 9. CATEGORIES: the category decides the folder, so the two never collide
# ---------------------------------------------------------------------------

# In qBittorrent a category carries a save path, and a torrent under automatic
# management (auto_tmm) takes its folder from it: changing the category moves
# the data, and moving by hand is what drops it out of automatic management.
# The bot must therefore never offer both controls for the same torrent.

bot_settings = bot_module.bot_settings
bot_settings.set("auto_download", False)
bot_settings.set("auto_category", "")
bot_settings.set("auto_download_dir", "")
bot_settings.set("auto_rename", False)


def buttons_of(screen):
	return callbacks_of(screen[1]) if screen else []


def detail_buttons(filter_key=bot_module.FILTER_ALL):
	return buttons_of(press(bot_module.build_call("info", TORRENT_ID, filter_key, 0)))


PROFILE["auto_managed"] = True
auto_buttons = detail_buttons()
check("an auto managed torrent is not offered the move button",
	any(c.startswith("move|") for c in auto_buttons), False)
check("an auto managed torrent is offered the category button",
	any(c.startswith("catMenu|") for c in auto_buttons), True)

# The way out has to exist somewhere, and the category screen is where it can
# be spelled out that it gives up the automatic management
cat_screen = press(next(c for c in auto_buttons if c.startswith("catMenu|")))
check("the category screen offers the manual move as a way out",
	any(c.startswith("move|") for c in buttons_of(cat_screen)), True)

# Assigning a category to an auto managed torrent relocates the data, so it has
# to go through the same background worker as a move instead of blocking
CALLS.clear()
press(next(c for c in buttons_of(cat_screen) if c.startswith("catSet|")))
real_sleep(0.4)
check("an auto managed category change is delivered like a move",
	[c for c in CALLS if c[0] == "set_category"], [("set_category", [TORRENT_ID], CATEGORIES[0][0])])

PROFILE["auto_managed"] = False
manual_buttons = detail_buttons()
check("a manual torrent keeps the move button",
	any(c.startswith("move|") for c in manual_buttons), True)
check("a manual torrent is also offered the category button",
	any(c.startswith("catMenu|") for c in manual_buttons), True)

cat_screen = press(next(c for c in manual_buttons if c.startswith("catMenu|")))
CALLS.clear()
press(next(c for c in buttons_of(cat_screen) if c.startswith("catSet|")))
real_sleep(0.4)
check("a manual category change is applied straight away",
	[c for c in CALLS if c[0] == "set_category"], [("set_category", [TORRENT_ID], CATEGORIES[0][0])])
check("a manual category change never moves anything",
	[c for c in CALLS if c[0] == "move_torrents"], [])

# Filtering by category, including the torrents that have none
series = bot_module.get_filtered_torrents("c" + bot_module.new_category_context("Series"))
check("the category filter finds torrents", len(series) > 0, True)
check("the category filter only returns that category",
	sorted({t.category for t in series}), ["Series"])
uncategorized = bot_module.get_filtered_torrents("c" + bot_module.new_category_context(""))
check("the no-category filter finds torrents", len(uncategorized) > 0, True)
check("the no-category filter only returns torrents without one",
	sorted({t.category for t in uncategorized}), [""])

# "co" and "ck" are real filter keys that start with the category prefix, and
# reading them as an expired category context would break the status filters
check("the completed filter is not read as a category",
	bot_module.get_filter_label(bot_module.FILTER_COMPLETED), bot_module.get_text("STATUS_COMPLETED"))
check("the checking filter is not read as a category",
	bot_module.get_filter_label(bot_module.FILTER_CHECKING),
	f"{bot_module.STATUS_EMOJI[TorrentStatus.CHECKING]} {bot_module.get_text('STATUS_CHECKING')}")
check("an expired category context is reported as expired",
	isinstance(_expired_category(), bot_module.ExpiredContext), True)

# Adding a torrent: the category is asked first, and it replaces the directory
EDITS.clear()
CALLS.clear()
bot_module.start_add_flow(CHAT_ID, "Some.Movie.2024.1080p", magnet="magnet:?xt=urn:btih:deadbeef")
add_buttons = buttons_of(EDITS[-1] if EDITS else None)
check("adding asks for the category first",
	any(c.startswith("addCat|") for c in add_buttons), True)
check("adding still lets you pick a folder instead",
	any(c.startswith("addDirPage|") for c in add_buttons), True)
CALLS.clear()
press(next(c for c in add_buttons if c.startswith("addCat|")))
check("a torrent added by category carries no directory",
	[c for c in CALLS if c[0] == "add_torrent"], [("add_torrent", None, CATEGORIES[0][0])])

# The automatic category takes over the automatic directory
bot_settings.set("auto_download", True)
bot_settings.set("auto_category", "Series")
CALLS.clear()
bot_module.start_add_flow(CHAT_ID, "Auto.Movie.2024", magnet="magnet:?xt=urn:btih:deadbee2")
check("the automatic category is used instead of the directory",
	[c for c in CALLS if c[0] == "add_torrent"], [("add_torrent", "", "Series")])

# A category deleted in the manager must not silently swallow the download
bot_settings.set("auto_category", "GoneForever")
CALLS.clear()
bot_module.start_add_flow(CHAT_ID, "Auto.Movie.2025", magnet="magnet:?xt=urn:btih:deadbee3")
check("an automatic category that no longer exists falls back to the directory",
	[c for c in CALLS if c[0] == "add_torrent"], [("add_torrent", DIRS[0], None)])

bot_settings.set("auto_category", "")
bot_settings.set("auto_download", False)

# A manual torrent whose category points elsewhere: the screen used to show a
# category and a directory that contradict each other with nothing explaining it
PROFILE["auto_managed"] = False
detail = press(bot_module.build_call("info", TORRENT_ID, bot_module.FILTER_ALL, 0))
check("a manual torrent says why its category is not deciding the folder",
	bot_module.get_text("INFO_CATEGORY_IGNORED") in detail[0], True)
PROFILE["auto_managed"] = True
detail = press(bot_module.build_call("info", TORRENT_ID, bot_module.FILTER_ALL, 0))
check("an auto managed torrent says the category decides instead",
	bot_module.get_text("INFO_AUTO_MANAGED") in detail[0], True)
check("an auto managed torrent never gets the manual explanation",
	bot_module.get_text("INFO_CATEGORY_IGNORED") in detail[0], False)
check("an auto managed torrent is not offered automatic management again",
	any(c.startswith("autoManage|") for c in buttons_of(press(
		next(c for c in buttons_of(detail) if c.startswith("catMenu|"))))), False)

# Handing the location over to the manager relocates the data, so it goes
# through the same worker as a move
PROFILE["auto_managed"] = False
cat_screen = press(next(c for c in detail_buttons() if c.startswith("catMenu|")))
auto_button = [c for c in buttons_of(cat_screen) if c.startswith("autoManage|")]
check("a manual torrent with a category can be handed over to the manager",
	len(auto_button), 1)
CALLS.clear()
press(auto_button[0])
real_sleep(0.4)
check("handing it over is delivered like a move",
	[c for c in CALLS if c[0] == "set_auto_managed"], [("set_auto_managed", [TORRENT_ID], True)])

# Without a category it would drag the torrent to the default download folder
_original_fake_torrent = bot_module.client.get_torrent
def _uncategorized(torrent_id):
	torrent = _original_fake_torrent(torrent_id)
	if torrent:
		torrent.category = ""
	return torrent
bot_module.client.get_torrent = _uncategorized
cat_screen = press(next(c for c in detail_buttons() if c.startswith("catMenu|")))
check("a torrent with no category is not offered automatic management",
	any(c.startswith("autoManage|") for c in buttons_of(cat_screen)), False)
bot_module.client.get_torrent = _original_fake_torrent

# Transmission, Deluge and Synology have no categories at all: not one button,
# not one line, and the directory flow exactly as it was
FakeClient.supports_categories = False
plain_buttons = detail_buttons()
check("without categories the move button is always offered",
	any(c.startswith("move|") for c in plain_buttons), True)
check("without categories nothing offers one",
	any(c.startswith("catMenu|") for c in plain_buttons), False)
check("without categories the dashboard has no category filter",
	"categories" in buttons_of(press(bot_module.build_call("dashboard"))), False)
EDITS.clear()
bot_module.start_add_flow(CHAT_ID, "No.Cat.2024", magnet="magnet:?xt=urn:btih:deadbee4")
check("without categories adding asks for the directory",
	any(c.startswith("addTo|") for c in buttons_of(EDITS[-1] if EDITS else None)), True)
FakeClient.supports_categories = True

# ---------------------------------------------------------------------------
# 10. MANY CATEGORIES: a big library must not lose any of them
# ---------------------------------------------------------------------------

# The first version of this screen copied the tracker menu and cut the list at
# 25, which does not fail anywhere Telegram can see: the keyboard is valid and
# the bot answers, the categories past the cut simply stop existing for the
# user. Someone organizing a library by category can easily have hundreds.

MANY = [(f"Categoria{n:03d}", f"/downloads/c{n:03d}") for n in range(200)]
_few_categories = FakeClient.get_categories
FakeClient.get_categories = lambda self: list(MANY)
bot_module._categories_cache["ts"] = 0  # The list is held for a few seconds


def walk_pages(render):
	"""Walks the pages the way someone pressing the next arrow would, and
	reports what was reachable and the worst keyboard on the way"""
	seen, worst_buttons, worst_text, oversized, visited = set(), 0, 0, 0, 0
	for page in range(len(MANY) + 1):  # Cannot loop longer than one page each
		visited = page + 1
		text, markup = render(page)
		buttons = [b for row in markup.keyboard for b in row]
		worst_buttons = max(worst_buttons, len(buttons))
		worst_text = max(worst_text, len(text))
		oversized += sum(1 for b in buttons if len(str(b.callback_data).encode()) > CALLBACK_LIMIT)
		for b in buttons:
			if b.text.startswith("🏷️"):
				seen.add(b.text.split(" ", 1)[1].split(" (")[0])
		if not any(b.text == "➡️" and "noop" not in str(b.callback_data) for b in buttons):
			break
	return seen, worst_buttons, worst_text, oversized, visited


names = {n for n, _ in MANY}
torrent = fake_torrent()
for label, render in (
		("the category filter menu", lambda p: bot_module.build_categories_menu(p)),
		("the category screen of a torrent",
			lambda p: bot_module.build_torrent_category_screen(torrent, bot_module.FILTER_ALL, 0, p))):
	seen, worst_buttons, worst_text, oversized, visited = walk_pages(render)
	check(f"{label} reaches every category", sorted(names - seen), [])
	check(f"{label} paginates instead of cutting", visited > 1, True)
	check(f"{label} never exceeds the button limit", worst_buttons <= BUTTONS_LIMIT, True)
	check(f"{label} never exceeds the message limit", worst_text <= TEXT_LIMIT, True)
	check(f"{label} keeps every callback_data in range", oversized, 0)

# The automatic category menu is built straight in the dispatcher, and its
# first row is the pinned "none" button, so the categories are what to compare
auto_pages = []
for page in (0, 1):
	EDITS.clear()
	press(bot_module.build_call("autoCatMenu", page))
	buttons = [b.text for row in (EDITS[-1][1].keyboard if EDITS else []) for b in row]
	auto_pages.append([b for b in buttons if b.startswith("🏷️")])
check("the automatic category menu lists categories", all(auto_pages), True)
check("the automatic category menu paginates too", auto_pages[0] != auto_pages[1], True)

# And with only a handful of categories there must be no pagination at all
FakeClient.get_categories = _few_categories
bot_module._categories_cache["ts"] = 0
_text, markup = bot_module.build_categories_menu(0)
check("a handful of categories shows no pagination",
	any(b.text in ("⬅️", "➡️") for row in markup.keyboard for b in row), False)

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({crawled} screens crawled)")
