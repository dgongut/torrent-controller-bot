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

PROFILE = {"name": None, "files": None}  # None = the plain values above


def fake_torrent(torrent_id=TORRENT_ID):
	index = TORRENT_IDS.index(torrent_id)
	name = PROFILE["name"] or f"Some.Movie.{2024 - index}.1080p"
	files = PROFILE["files"] or FILES
	return TorrentInfo(id=torrent_id, name=name, status=TorrentStatus.SEEDING,
					progress=100.0, total_size=1024, downloaded=1024, uploaded=512, ratio=0.5,
					download_dir=DIRS[0], files=list(files),
					trackers=["tracker.example.org", "otro.tracker.example.net"])


class FakeClient:
	"""Records what it is told to do and never mutates its torrent, so the
	crawler keeps finding the same screens no matter what order it presses"""

	supports_alt_speed = True
	supports_rename = True
	supports_verify = True

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

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None):
		CALLS.append(("add_torrent", download_dir))
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
	return type("FakeMessage", (), {"message_id": MESSAGE_ID})()


bot_module.bot.edit_message_text = fake_edit_message_text
bot_module.bot.send_message = fake_send_message
bot_module.bot.delete_message = lambda *a, **k: True


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
					"settings", "toggleSetting", "favDirsMenu", "tplMenu", "trackers", "goto"):
		check(f"[{label}] the crawler reached the {command} screen", command in reached, True)

PROFILE.update(PROFILES["plain names"])

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

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({crawled} screens crawled)")
