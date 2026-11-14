"""Test battery for the inline keyboards. Run with: python3 test_callbacks.py

Telegram refuses the WHOLE keyboard with 400 BUTTON_DATA_INVALID as soon as one
button carries more than 64 bytes of callback_data. The screen then never
changes and the button looks dead, which is how a too long confirmDelete made
the delete button useless when the list was filtered by tracker.

The bot is loaded here against a fake torrent client and a fake Telegram that
enforces that same limit, so any screen that would be refused fails the test.
The crawler below walks every screen reachable from the dashboard, which is what
keeps this honest: it does not need to know in advance which keyboard is new."""

import html
import importlib.util
import os
import re
import sys
import tempfile
import time
from datetime import datetime

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

# A complete series with a folder per season, the case that needs to browse
# folders: the episodes are two levels deep and the season folders have names
# of their own to fix, next to a folder with nothing to suggest
SERIES_NAME = "Mad.Men.2007.COMPLETE.1080p.NF.WEB-DL"
SERIES_FILES = ([(f"{SERIES_NAME}/Mad.Men.2007.S{s:02d}.NF.WEB-DL.1080P.AV1-Txv2/Mad.Men.S{s:02d}E{e:02d}.1080p.mkv", 1024, True)
				for s in (1, 2) for e in range(1, 13)]
				+ [(f"{SERIES_NAME}/Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2/Mad.Men.S01E01.1080p.es.srt", 16, True),
				(f"{SERIES_NAME}/Extras/Making.of.mkv", 512, True)])

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
					file_priorities=dict(PROFILE.get("priorities") or {}),
					category=category, auto_managed=PROFILE["auto_managed"],
					trackers=["tracker.example.org", "otro.tracker.example.net"])


class FakeClient:
	"""Records what it is told to do and never mutates its torrent, so the
	crawler keeps finding the same screens no matter what order it presses"""

	supports_alt_speed = True
	supports_rename = True
	supports_verify = True
	supports_categories = True
	supports_ratio_limit = True
	ratio_limit_copied_on_add = False
	supports_torrent_speed_limit = True
	supports_reannounce = True
	reports_last_activity = True
	skipped_files_in_size = False
	file_priority_levels = ("skip", "low", "normal", "high")

	def set_file_priority(self, torrent_id, paths, priority):
		CALLS.append(("set_file_priority", torrent_id, sorted(paths), priority))

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

	def rename_folder(self, torrent_id, old_path, new_name):
		CALLS.append(("rename_folder", torrent_id, old_path, new_name))

	def move_torrents(self, torrent_ids, new_dir):
		CALLS.append(("move_torrents", list(torrent_ids), new_dir))

	def set_alt_speed(self, enabled):
		CALLS.append(("set_alt_speed", enabled))

	def set_speed_limit(self, direction, kbps, enabled):
		CALLS.append(("set_speed_limit", direction, kbps, enabled))

	def set_ratio_limit(self, torrent_ids, limit):
		CALLS.append(("set_ratio_limit", list(torrent_ids), limit))

	def set_torrent_speed_limit(self, torrent_ids, direction, kbps):
		CALLS.append(("set_torrent_speed_limit", list(torrent_ids), direction, kbps))

	def reannounce_torrents(self, torrent_ids):
		CALLS.append(("reannounce_torrents", list(torrent_ids)))

	global_ratio = 2.0

	def get_global_ratio_limit(self):
		return self.global_ratio

	def set_global_ratio_limit(self, limit):
		CALLS.append(("set_global_ratio_limit", limit))
		self.global_ratio = limit


torrent_clients.create_client = lambda config_module: FakeClient()

_spec = importlib.util.spec_from_file_location("tcbot", os.path.join(REPO, "torrent-controller-bot.py"))
bot_module = importlib.util.module_from_spec(_spec)
sys.modules["tcbot"] = bot_module
_spec.loader.exec_module(bot_module)

real_live_refresher = bot_module._live_refresher
bot_module._live_refresher = lambda *a, **k: None  # No background repainting during the test

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
MAX_STEPS = 3000


def normalize(call):
	"""Context ids are random and minted on every render, so two callbacks that
	only differ in their context id are the same screen for crawling purposes"""
	parts = []
	for part in call.split("|"):
		if re.fullmatch(r"[0-9a-f]{6}|[0-9a-f]{8}", part):
			parts.append("<ctx>")
		elif re.fullmatch(r"t[0-9a-f]{6}", part):
			parts.append("t<ctx>")
		elif re.fullmatch(r"[mr][0-9a-f]{6}", part):
			parts.append(f"{part[0]}<ctx>")  # What a mass action or a replace counted
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
	"complete series with season folders": {"name": SERIES_NAME, "files": SERIES_FILES, "auto_managed": False},
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
					"categories", "catMenu", "catSet", "folder", "folderManual", "filesAll", "filesAllOk",
					"ratioMenu", "ratioSet", "globalRatioSet", "reannounce", "massSpeedAsk", "tlAdd", "tlRule", "tlRatio", "tlDel", "tlDelOk",
					"sort", "sortSet", "filePrio", "folderPrio"):
		check(f"[{label}] the crawler reached the {command} screen", command in reached, True)
	if profile["files"] is SERIES_FILES:
		check(f"[{label}] the crawler reached the folder suggestion", "folderAuto" in reached, True)

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
# 4b. COMPLETE SERIES: the folders in between are renamed too, in an order
#     where every planned path still exists when its turn comes
# ---------------------------------------------------------------------------

PROFILE.update({"name": SERIES_NAME, "files": SERIES_FILES})
bot_module.nav_contexts.clear()
EDITS.clear()
CALLS.clear()
screen = press(bot_module.build_call("rename", TORRENT_ID, config.FILTER_ALL, 0))
check("the full rename screen lists the season folders", "Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2" in (screen or ("",))[0], True)
press(callbacks_of(screen[1])[0])
renames = [c for c in CALLS if c[0] in ("rename_file", "rename_folder", "rename_torrent")]
kinds = [c[0] for c in renames]
check("every episode and its subtitle are renamed", kinds.count("rename_file"), 25)
check("both season folders are renamed, the extras folder is not",
	sorted(c[3] for c in renames if c[0] == "rename_folder"), ["T1 - Mad Men - 1080p", "T2 - Mad Men - 1080p"])
check("files go before folders and the torrent goes last",
	kinds, sorted(kinds, key=["rename_file", "rename_folder", "rename_torrent"].index))
check("the torrent is renamed as well", kinds[-1], "rename_torrent")
check("an episode takes the season of its folder",
	("rename_file", TORRENT_ID, f"{SERIES_NAME}/Mad.Men.2007.S02.NF.WEB-DL.1080P.AV1-Txv2/Mad.Men.S02E03.1080p.mkv",
		"2x03 - Mad Men - 1080p.mkv") in renames, True)
check("the subtitle follows its episode",
	("rename_file", TORRENT_ID, f"{SERIES_NAME}/Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2/Mad.Men.S01E01.1080p.es.srt",
		"1x01 - Mad Men - 1080p.es.srt") in renames, True)

# Browsing: the torrent folder opens first, a season folder can be entered and left
torrent = fake_torrent()
folders = bot_module.torrent_folders(torrent)
ctx = bot_module.new_nav_context(TORRENT_ID, config.FILTER_ALL, 0)
root_screen = press(bot_module.build_call("files", ctx, folders.index(SERIES_NAME), 0))
root_buttons = callbacks_of(root_screen[1])
check("the torrent folder shows its three subfolders", sum(b.startswith("files|") and b.endswith("|0") and b.count("|") == 3
	for b in root_buttons[:3]), 3)
check("the torrent folder cannot go further up", bot_module.get_text("BUTTON_FOLDER_UP", "/") in
	[b.text for row in root_screen[1].keyboard for b in row], False)
season = f"{SERIES_NAME}/Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2"
season_screen = press(bot_module.build_call("files", ctx, folders.index(season), 0))
season_texts = [b.text for row in season_screen[1].keyboard for b in row]
check("a season folder lists its episodes", sum(t.startswith("🎬") for t in season_texts), bot_module.FILES_PER_PAGE)
up = [b.callback_data for row in season_screen[1].keyboard for b in row if b.text.startswith("⬆️")]
check("a season folder can go up to the torrent folder",
	up, [bot_module.build_call("files", ctx, bot_module.folder_ref(folders, folders.index(SERIES_NAME)), 0)])

# Renaming the season folder being browsed leaves the torrent and its files alone
CALLS.clear()
press(bot_module.build_call("folderAuto", ctx, folders.index(season), 0))
check("renaming a season folder only renames that folder",
	CALLS, [("rename_folder", TORRENT_ID, season, "T1 - Mad Men - 1080p")])
CALLS.clear()
press(bot_module.build_call("folderAuto", ctx, folders.index(f"{SERIES_NAME}/Extras"), 0))
check("a folder with nothing to suggest is not renamed", CALLS, [])

# Everything inside one season, and nothing outside it
CALLS.clear()
press(bot_module.build_call("filesAllOk", ctx, folders.index(season), 0))
check("renaming everything in a season stays inside that season",
	all(season in c[2] for c in CALLS), True)
check("renaming everything in a season renames its episodes", len(CALLS), 13)

# Renaming a folder never moves the index of the folder holding it
renamed = [(p.replace("Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2", "T1 - Mad Men - 1080p"), s, c) for p, s, c in SERIES_FILES]
after = bot_module.torrent_folders(fake_torrent().__class__(**{**fake_torrent().__dict__, "files": renamed}))
check("the torrent folder keeps its index after a season is renamed",
	after.index(SERIES_NAME), folders.index(SERIES_NAME))

# Choosing what to download: leaving out and the priority are two things on
# screen. A whole season left out, then one episode back in
get_text = bot_module.get_text
season_paths = sorted(p for p, _, _ in SERIES_FILES if p.startswith(season + "/"))
all_paths = sorted(p for p, _, _ in SERIES_FILES)
other_paths = sorted(set(all_paths) - set(season_paths))


def texts(screen):
	return [b.text for row in screen[1].keyboard for b in row]


CALLS.clear()
screen = press(bot_module.build_call("folderPrio", ctx, folders.index(season), 0))
check("the folder screen counts every file inside", get_text("FOLDER_PRIORITY_TITLE",
	"Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2", len(season_paths)) in screen[0], True)
check("and how many are downloaded", get_text("FOLDER_WANTED_COUNT", len(season_paths), 0) in screen[0], True)
check("with everything downloaded it only offers to download none",
	(get_text("BUTTON_FOLDER_WANT_ALL") in texts(screen), get_text("BUTTON_FOLDER_SKIP_ALL") in texts(screen)), (False, True))
check("the priority they all share is marked", "✅ " + get_text("PRIORITY_NORMAL") in texts(screen), True)
check("the priorities share one row", [len(row) for row in screen[1].keyboard][:2], [1, 3])
screen = press(bot_module.build_call("folderPrio", ctx, folders.index(season), 0, "s"))
check("downloading none of a folder leaves out every file inside it",
	CALLS, [("set_file_priority", TORRENT_ID, season_paths, "skip")])
check("and the folder screen stays", screen[0].startswith(get_text("FOLDER_PRIORITY_TITLE", "Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2", len(season_paths))), True)

PROFILE["priorities"] = {path: "skip" for path in season_paths}
screen = press(bot_module.build_call("files", ctx, folders.index(season), 0))
check("a file left out shows no share, it is not going anywhere",
	any("%" in b.text for row in screen[1].keyboard for b in row if b.callback_data.startswith("file|")), False)
check("the files left out are marked in the list",
	all(b.text.startswith("⏭️ ") for row in screen[1].keyboard for b in row if b.callback_data.startswith("file|")), True)
check("the folder says how much of it is downloaded", get_text("FILES_WANTED", 0, len(season_paths), "0.0B") in screen[0], True)
screen = press(bot_module.build_call("folderPrio", ctx, folders.index(season), 0))
check("a folder with nothing downloaded only offers to download all",
	[t for t in texts(screen) if t not in (get_text("BUTTON_BACK"), get_text("BUTTON_CLOSE"))], [get_text("BUTTON_FOLDER_WANT_ALL")])
check("and says nothing about priorities", get_text("FOLDER_PRIORITY_HINT") in screen[0], False)
check("a manager that counts the files left out in the size does not say it", get_text("FOLDER_SKIPPED_IN_SIZE") in screen[0], False)
bot_module.client.skipped_files_in_size = True
check("Download Station warns that it still counts them", get_text("FOLDER_SKIPPED_IN_SIZE") in press(bot_module.build_call("folderPrio", ctx, folders.index(season), 0))[0], True)
PROFILE["priorities"] = {}
check("but not when nothing is left out", get_text("FOLDER_SKIPPED_IN_SIZE") in press(bot_module.build_call("folderPrio", ctx, folders.index(season), 0))[0], False)
PROFILE["priorities"] = {path: "skip" for path in season_paths}
bot_module.client.skipped_files_in_size = False
screen = press(bot_module.build_call("folderPrio", ctx, folders.index(SERIES_NAME), 0))
check("a folder half downloaded offers both", (get_text("BUTTON_FOLDER_WANT_ALL") in texts(screen),
	get_text("BUTTON_FOLDER_SKIP_ALL") in texts(screen)), (True, True))
check("and counts both", get_text("FOLDER_WANTED_COUNT", len(other_paths), len(season_paths)) in screen[0], True)
CALLS.clear()
press(bot_module.build_call("folderPrio", ctx, folders.index(SERIES_NAME), 0, "h"))
check("a priority on a folder only goes to the files downloaded",
	CALLS, [("set_file_priority", TORRENT_ID, other_paths, "high")])
CALLS.clear()
press(bot_module.build_call("folderPrio", ctx, folders.index(SERIES_NAME), 0, "d"))
check("downloading all only brings back the files left out, with the normal priority",
	CALLS, [("set_file_priority", TORRENT_ID, season_paths, "normal")])
CALLS.clear()
press(bot_module.build_call("folderPrio", ctx, folders.index(SERIES_NAME), 0, "s"))
check("downloading none only touches the files downloaded",
	CALLS, [("set_file_priority", TORRENT_ID, other_paths, "skip")])

episode = SERIES_FILES.index(next(f for f in SERIES_FILES if f[0] == season_paths[0]))
screen = press(bot_module.build_call("file", ctx, folders.index(season), 0, episode))
check("a file left out says so", get_text("FILE_SKIPPED") in screen[0], True)
_, episode_size, episode_done = SERIES_FILES[episode]
check("a file says how much of it is on disk", get_text("FILE_PROGRESS", f"{episode_done / episode_size * 100:.1f}",
	bot_module.sizeof_fmt(episode_done), bot_module.sizeof_fmt(episode_size)) in screen[0], True)
check("and shows no priority", get_text("FILE_PRIORITY", get_text("PRIORITY_NORMAL")) in screen[0], False)
check("only offering to download it",
	[b.callback_data for row in screen[1].keyboard for b in row if b.callback_data.startswith("filePrio|")],
	[bot_module.file_call("filePrio", ctx, folders.index(season), 0, episode, SERIES_FILES[episode][0], "d")])
CALLS.clear()
screen = press(bot_module.build_call("filePrio", ctx, folders.index(season), 0, episode, "d"))
check("a single file can be brought back", CALLS, [("set_file_priority", TORRENT_ID, [season_paths[0]], "normal")])
check("and its screen stays", screen[0].startswith(f"<b>{bot_module.file_basename(season_paths[0])}</b>"), True)
CALLS.clear()
press(bot_module.build_call("filePrio", ctx, folders.index(season), 0, episode, "zz"))
check("an unknown priority does nothing", CALLS, [])
PROFILE["priorities"] = {}
screen = press(bot_module.build_call("file", ctx, folders.index(season), 0, episode))
check("a downloaded file says so with its priority", (get_text("FILE_DOWNLOADED") in screen[0],
	get_text("FILE_PRIORITY", get_text("PRIORITY_NORMAL")) in screen[0]), (True, True))
rows = [[b.callback_data.split("|")[-1] for b in row] for row in screen[1].keyboard if row[0].callback_data.startswith("filePrio|")]
check("its priorities share a row and leaving it out goes apart", rows, [["l", "n", "h"], ["s"]])
bot_module.client.file_priority_levels = ("skip", "normal")
screen = press(bot_module.build_call("file", ctx, folders.index(season), 0, episode))
check("a manager with a single priority shows none", (get_text("FILE_PRIORITY", get_text("PRIORITY_NORMAL")) in screen[0],
	[b.callback_data.split("|")[-1] for row in screen[1].keyboard for b in row if b.callback_data.startswith("filePrio|")]), (False, ["s"]))
del bot_module.client.file_priority_levels
del PROFILE["priorities"]

# A manager that cannot rename (Download Station) still chooses the files, and
# one that can do neither has no files screen at all
bot_module.client.supports_rename = False
screen = press(bot_module.build_call("files", ctx, folders.index(SERIES_NAME), 0))
texts = [b.text for row in screen[1].keyboard for b in row]
check("without renaming the files screen has no rename buttons",
	(get_text("BUTTON_FOLDER_RENAME") in texts, get_text("BUTTON_FILES_RENAME_ALL") in texts), (False, False))
check("but it still chooses what is downloaded", get_text("BUTTON_FOLDER_PRIORITY") in texts, True)
bot_module.client.supports_rename = True
check("the folder buttons share a row", [[b.text for b in row] for row in press(bot_module.build_call("files", ctx, folders.index(SERIES_NAME), 0))[1].keyboard
	if any(b.callback_data.startswith("folderPrio|") for b in row)], [[get_text("BUTTON_FOLDER_PRIORITY"), get_text("BUTTON_FOLDER_RENAME")]])
bot_module.client.supports_rename = False
screen = press(bot_module.build_call("file", ctx, folders.index(season), 0, episode))
check("nor has a file screen", any(b.callback_data.startswith(("fileManual|", "fileAuto|")) for row in screen[1].keyboard for b in row), False)
check("the torrent screen offers the files", any(c.startswith("files|") for c in callbacks_of(press(bot_module.build_call("info", TORRENT_ID, config.FILTER_ALL, 0))[1])), True)
bot_module.client.file_priority_levels = ()
check("with neither the torrent screen has no files button", any(c.startswith("files|") for c in callbacks_of(press(bot_module.build_call("info", TORRENT_ID, config.FILTER_ALL, 0))[1])), False)
bot_module.client.supports_rename = True
check("a manager that cannot choose files hides it in the files screen",
	get_text("BUTTON_FOLDER_PRIORITY") in [b.text for row in press(bot_module.build_call("files", ctx, folders.index(SERIES_NAME), 0))[1].keyboard for b in row], False)
del bot_module.client.file_priority_levels

PROFILE.update(PROFILES["plain names"])

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
bot_module.live_views.clear()
bot_module.show_dashboard(CHAT_ID, thread_id=THREAD)
bot_module.show_dashboard(CHAT_ID, thread_id=THREAD + 1)
check("each topic keeps its own dashboard", sorted(bot_module.live_views), [(CHAT_ID, THREAD), (CHAT_ID, THREAD + 1)])
bot_module.live_views.clear()

# Live screens: the dashboard, a list and a torrent repaint themselves for a
# while, then stop and offer the update button on the left of the bottom row
def live_generation():
	return bot_module.live_views[(CHAT_ID, None)]["generation"]


def run_live_refresher(message_id, build):
	EDITS.clear()
	real_live_refresher(CHAT_ID, None, message_id, live_generation(), build)
	return list(EDITS)


update_text = bot_module.get_text("BUTTON_UPDATE")
autoupdating = bot_module.get_text("DASHBOARD_AUTOUPDATING")
live_ctx = bot_module.new_nav_context(TORRENT_ID, config.FILTER_ALL, 0)
live_root = bot_module.torrent_folders(fake_torrent()).index(bot_module.torrent_root(fake_torrent()))
for name, call, build in (
		("list", bot_module.build_call("list", config.FILTER_ALL, 0),
			lambda refreshing: bot_module.build_list(config.FILTER_ALL, 0, refreshing)),
		("torrent", bot_module.build_call("info", TORRENT_ID, config.FILTER_ALL, 0),
			lambda refreshing: bot_module.build_detail(TORRENT_ID, config.FILTER_ALL, 0, refreshing)),
		("file list", bot_module.build_call("files", live_ctx, bot_module.folder_ref(bot_module.torrent_folders(fake_torrent()), live_root), 0),
			lambda refreshing: bot_module.build_files(live_ctx, TORRENT_ID, config.FILTER_ALL, 0, live_root, 0, refreshing)),
		("file", bot_module.file_call("file", live_ctx, live_root, 0, 0, FILES[0][0]),
			lambda refreshing: bot_module.build_file_screen(TORRENT_ID, live_ctx, live_root, 0, 0, refreshing))):
	bot_module.live_views.clear()
	screen = press(call)
	check(f"the {name} becomes the live screen", bot_module.live_views.get((CHAT_ID, None), {}).get("message_id"), MESSAGE_ID)
	check(f"the {name} opens auto-updating", autoupdating in screen[0], True)
	check(f"the {name} has no update button while auto-updating",
		any(b.text == update_text for row in screen[1].keyboard for b in row), False)
	edits = run_live_refresher(MESSAGE_ID, build)
	# Nothing changes in the fake client, so the repeated screens are not sent
	check(f"the {name} only sends the screens that changed", len(edits), 2)
	last_text, last_markup = edits[-1]
	check(f"the {name} ends with its last update time", autoupdating in last_text, False)
	check(f"the {name} ends with the update button on the left", last_markup.keyboard[-1][0].text, update_text)
	check(f"the {name} update button repaints the same screen", last_markup.keyboard[-1][0].callback_data, call)

# What a live screen shows can vanish while it repaints: the screen says so and
# stops, instead of being left behind saying it updates itself
press(bot_module.build_call("file", live_ctx, live_root, 0, 0))
# The torrent lost files meanwhile: the index points past the end of its list
edits = run_live_refresher(MESSAGE_ID, lambda refreshing: bot_module.build_file_screen(TORRENT_ID, live_ctx, live_root, 0, len(FILES), refreshing))
check("a file that vanishes is said to be gone", [e[0] for e in edits], [bot_module.get_text("FILE_NOT_FOUND")])
check("and its screen goes back to the files", callbacks_of(edits[-1][1])[0], bot_module.build_call("files", live_ctx, live_root, 0))
press(bot_module.build_call("list", config.FILTER_ALL, 0))
def expired_list(refreshing):
	raise bot_module.ExpiredContext()
edits = run_live_refresher(MESSAGE_ID, expired_list)
check("an expired list says so instead of a stale footer", [e[0] for e in edits], [bot_module.get_text("SEARCH_EXPIRED")])

# A button pressed on the live message stops it without painting over it
bot_module.live_views.clear()
press(bot_module.build_call("list", config.FILTER_ALL, 0))
generation = live_generation()
bot_module.stop_live_view(CHAT_ID, MESSAGE_ID)
EDITS.clear()
real_live_refresher(CHAT_ID, None, MESSAGE_ID, generation, lambda refreshing: bot_module.build_list(config.FILTER_ALL, 0, refreshing))
check("a stopped live screen is never painted again", EDITS, [])

# A newer live screen in the chat leaves the old one with its update button
bot_module.live_views.clear()
press(bot_module.build_call("list", config.FILTER_ALL, 0))
generation = live_generation()
bot_module.start_live_view(CHAT_ID, MESSAGE_ID + 1, None, bot_module.build_dashboard)
EDITS.clear()
real_live_refresher(CHAT_ID, None, MESSAGE_ID, generation, lambda refreshing: bot_module.build_list(config.FILTER_ALL, 0, refreshing))
check("a replaced live screen is painted once more", len(EDITS), 1)
check("a replaced live screen is left with the update button", EDITS and EDITS[-1][1].keyboard[-1][0].text, update_text)
bot_module.live_views.clear()

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

# ---------------------------------------------------------------------------
# 11. SEVERAL BOTS IN A GROUP: /command@OtherBot is not for this bot
# ---------------------------------------------------------------------------
# telebot strips the @mention when matching commands without checking whose it
# is, so real updates go through the real dispatcher here, filters included

import telebot

GROUP_ID = -1001234567890
STRANGER_ID = 999  # Not in TELEGRAM_ADMIN
update_ids = iter(range(1, 10_000))


def group_update(text, user_id=1):
	entity_length = len(text.split(maxsplit=1)[0])
	return telebot.types.Update.de_json({
		"update_id": next(update_ids),
		"message": {
			"message_id": next(update_ids),
			"date": int(time.time()),
			"chat": {"id": GROUP_ID, "type": "supergroup", "title": "Group"},
			"from": {"id": user_id, "is_bot": False, "first_name": "User"},
			"text": text,
			"entities": [{"type": "bot_command", "offset": 0, "length": entity_length}],
		},
	})


listed = []
_real_render_list = bot_module.render_list
bot_module.render_list = lambda chat_id, *a, **k: listed.append(chat_id)
bot_module.bot.get_me = lambda: telebot.types.User(id=42, is_bot=True, first_name="Own", username="OwnBot")
bot_module.bot.threaded = False  # Run the handlers inline so they can be asserted
bot_module._bot_username = None


def send_command(text, user_id=1):
	listed.clear()
	SENDS.clear()
	bot_module.bot.process_new_updates([group_update(text, user_id)])
	return len(listed), len(SENDS)


check("/list is answered", send_command("/list")[0], 1)
check("/list@OwnBot is answered", send_command("/list@OwnBot")[0], 1)
check("/list@ownbot is answered (case-insensitive)", send_command("/list@ownbot")[0], 1)
check("/list@OtherBot is ignored", send_command("/list@OtherBot"), (0, 0))
check("an unauthorized /start@OtherBot gets no reply", send_command("/start@OtherBot", STRANGER_ID), (0, 0))
check("an unauthorized /start@OwnBot is still refused", send_command("/start@OwnBot", STRANGER_ID)[1], 1)

# A command for another bot must not cancel nor answer a pending text input
bot_module.set_pending_input(GROUP_ID, 1, "search")
send_command("/list@OtherBot")
check("/list@OtherBot keeps the pending input", (bot_module.pop_pending_input(GROUP_ID, 1) or {}).get("action"), "search")

# When Telegram cannot say who this bot is, every command is taken as its own
def unreachable():
	raise ConnectionError("Telegram unreachable")
bot_module.bot.get_me = unreachable
bot_module._bot_username = None
check("without a known username /list@OtherBot is answered", send_command("/list@OtherBot")[0], 1)

bot_module.render_list = _real_render_list

# ---------------------------------------------------------------------------
# ANONYMOUS STATISTICS
# ---------------------------------------------------------------------------

import json
import types

TELEMETRY_STATE = os.path.join(config.CONFIG_PATH, "telemetry.json")


def telemetry_screen():
	EDITS.clear()
	press(bot_module.build_call("telemetryMenu"))
	return EDITS[-1]


def pending_usage():
	return bot_module.telemetry_client.preview()["usage"]


def press_button(data):
	"""A real press, through the handler that counts it, not only the dispatcher"""
	message = types.SimpleNamespace(chat=types.SimpleNamespace(id=CHAT_ID), message_id=MESSAGE_ID, message_thread_id=None)
	bot_module.handle_callback(types.SimpleNamespace(id="1", data=data, message=message, from_user=types.SimpleNamespace(id=1)))


bot_module.bot.answer_callback_query = lambda *a, **k: True

# The test config dir is not a volume: off, no switch, and nothing is counted
check("a config that is not a volume turns the statistics off", bot_module.telemetry_on(), False)
text, markup = telemetry_screen()
check("the screen says why they are off", bot_module.get_text("TELEMETRY_FORCED_VOLUME", config.CONFIG_PATH) in text, True)
check("without a volume there is no switch", "toggleTelemetry" in callbacks_of(markup), False)
press_button(bot_module.build_call("list", config.FILTER_ALL, 0))
check("without a volume nothing is counted", pending_usage(), {})

bot_module._config_persistent = True
check("on a volume they are on by default", bot_module.telemetry_on(), True)
text, markup = telemetry_screen()
check("on a volume the switch is there", "toggleTelemetry" in callbacks_of(markup), True)

# The variable wins over the setting, whatever it is spelled
for value in ("false", "0", "no"):
	os.environ["TELEMETRY"] = value
	check(f"TELEMETRY={value} turns them off", bot_module.telemetry_on(), False)
	text, markup = telemetry_screen()
	check(f"TELEMETRY={value} is shown as the reason", bot_module.get_text("TELEMETRY_FORCED_ENV", "TELEMETRY") in text, True)
for value in ("true", ""):
	os.environ["TELEMETRY"] = value
	check(f"TELEMETRY={value!r} leaves them on", bot_module.telemetry_on(), True)
del os.environ["TELEMETRY"]

# Presses and commands are counted by name, page labels are not
press_button(bot_module.build_call("list", config.FILTER_ALL, 0))
press_button(bot_module.build_call("list", config.FILTER_ALL, 0))
press_button(bot_module.build_call("noop"))
check("a button press is counted", pending_usage().get("btn_list"), 2)
check("the page label is not a button", "btn_noop" in pending_usage(), False)
send_command("/version")
check("a command is counted", pending_usage().get("cmd_version"), 1)
send_command("/version", STRANGER_ID)
check("an unauthorized command is not counted", pending_usage().get("cmd_version"), 1)

# Only declared, closed values: no torrent, path or category ever leaves
metrics = bot_module.collect_telemetry_metrics()
check("the metrics are exactly the declared ones", sorted(metrics), sorted([
	"client", "language", "admins", "telegram_group", "torrents_per_page", "notify_completed",
	"notify_errors", "notify_external_added", "notify_ratio_reached", "auto_remove_ratio", "notify_auto_removed", "auto_download", "auto_download_dir", "auto_category",
	"auto_rename", "auto_rename_files", "auto_rename_external", "auto_replace", "auto_move_series", "auto_move_series_external", "new_series_dir",
	"low_space_warning", "auto_mover", "favorite_dirs", "tracker_limits",
	"custom_templates", "torrents", "transmission_major"]))
bot_module.bot_settings.set("tracker_limits", {"tracker.example.org": {"ratio": 0.5, "down": 0, "up": 0}})
check("the trackers with limits go as a count, never which ones", bot_module.collect_telemetry_metrics()["tracker_limits"], 1)
bot_module.client.supports_ratio_limit = bot_module.client.supports_torrent_speed_limit = False
check("and as none for a manager without limits per torrent", bot_module.collect_telemetry_metrics()["tracker_limits"], 0)
bot_module.client.supports_ratio_limit = bot_module.client.supports_torrent_speed_limit = True
bot_module.bot_settings.set("tracker_limits", {})
check("the torrent count goes as a number, so the server can add them up", metrics["torrents"], len(bot_module.client.get_torrents()))
check("the ratio limit notice is reported", metrics["notify_ratio_reached"], True)
bot_module.client.supports_ratio_limit = False
check("but never as on for a manager without ratio limits", bot_module.collect_telemetry_metrics()["notify_ratio_reached"], False)
bot_module.client.supports_ratio_limit = True
check("the automatic removal is reported off by default", (metrics["auto_remove_ratio"], metrics["notify_auto_removed"]), (False, False))
bot_module.bot_settings.set("auto_remove_ratio", True)
check("and on once turned on, with its notice", [bot_module.collect_telemetry_metrics()[k] for k in ("auto_remove_ratio", "notify_auto_removed")], [True, True])
bot_module.client.supports_ratio_limit = False
check("but never as on for a manager without ratio limits",
	[bot_module.collect_telemetry_metrics()[k] for k in ("auto_remove_ratio", "notify_auto_removed")], [False, False])
bot_module.client.supports_ratio_limit = True
bot_module.bot_settings.set("auto_remove_ratio", False)
for name, version, expected in (("qbittorrent", "qBittorrent 5.2.4", 5), ("transmission", "Transmission 4.1.3 (abc)", 4), ("deluge", "Deluge 2.2.0", 2)):
	_real_test_connection, _real_client_name = bot_module.client.test_connection, bot_module.TORRENT_CLIENT
	bot_module.client.test_connection = lambda version=version: version
	bot_module.TORRENT_CLIENT = name
	check(f"{name} reports its major version", bot_module.collect_telemetry_metrics().get(f"{name}_major"), expected)
	bot_module.client.test_connection, bot_module.TORRENT_CLIENT = _real_test_connection, _real_client_name
text, _ = bot_module.build_telemetry_preview()
check("the preview carries no torrent name", any(t.name in text for t in [fake_torrent()]), False)
check("the preview carries no directory", any(d in text for d in DIRS), False)

# The preview fits a message however many buttons were pressed
for n in range(40):
	bot_module.count_usage(f"btn_fake{n}")
text, _ = bot_module.build_telemetry_preview()
check("a long preview is cut and says so", bot_module.get_text("TELEMETRY_PREVIEW_TRUNCATED", 17)[:10] in text, True)
check("a long preview fits a message", len(text) <= TEXT_LIMIT, True)

# Turning them off forgets the installation and stops counting
bot_module.telemetry_client._state["install_id"] = "3f0c8f7e-5b1a-4b8e-9d57-2a9e0c1d4b6f"
press(bot_module.build_call("toggleTelemetry"))
check("the switch turns them off", bot_module.bot_settings.get("telemetry"), False)
with open(TELEMETRY_STATE, encoding="utf-8") as handle:
	stored = json.load(handle)
check("turning them off deletes the install id", stored["install_id"], None)
check("turning them off drops the pending counters", stored["pending"], {})
press_button(bot_module.build_call("list", config.FILTER_ALL, 0))
check("once off nothing is counted", pending_usage(), {})
press(bot_module.build_call("toggleTelemetry"))
check("the switch turns them back on", bot_module.telemetry_on(), True)
bot_module._config_persistent = None

# The torrent screen condenses the seeding and swarm details into the lines
# they belong to, and leaves out whatever the client does not know
def detail_text(**changes):
	torrent = fake_torrent()
	for key, value in changes.items():
		setattr(torrent, key, value)
	real_get_torrent = bot_module.client.get_torrent
	bot_module.client.get_torrent = lambda torrent_id: torrent
	try:
		return bot_module.build_detail(TORRENT_ID, config.FILTER_ALL, 0)[0]
	finally:
		bot_module.client.get_torrent = real_get_torrent


get_text = bot_module.get_text
now = int(time.time())
seeding = detail_text(ratio_limit=2.0, seeding_time=3 * 3600, seeding_time_limit=7 * 86400, eta=2 * 3600,
	swarm_seeders=42, swarm_leechers=7, seeders=1, leechers=2, peers=3, private=True,
	tracker_error="Unregistered <torrent>", added_date=now - 86400, completed_date=now - 3600,
	last_activity=now - 5400, upload_rate=0)
check("the ratio carries its limit", f"{get_text('INFO_RATIO')}: 0.5 {get_text('INFO_RATIO_LIMIT', '2')}" in seeding, True)
check("the seeding line has time, limit and time left",
	f"{get_text('INFO_SEEDING_TIME')}: 3h 0m {get_text('INFO_SEEDING_TIME_LIMIT', '7d 0h')} {get_text('INFO_SEEDING_LEFT', '2h 0m')}" in seeding, True)
check("the peers line has the whole swarm", get_text("INFO_PEERS_DETAIL_SWARM", 1, 42, 2, 7) in seeding, True)
check("the tracker line says it is private", get_text("INFO_PRIVATE") in seeding, True)
check("the tracker error is escaped", "Unregistered &lt;torrent&gt;" in seeding, True)
check("the added line has the completion date",
	get_text("INFO_COMPLETED", datetime.fromtimestamp(now - 3600).strftime("%Y-%m-%d %H:%M")) in seeding, True)
check("the status says how long it has been idle", get_text("INFO_IDLE", "1h 30m") in seeding, True)
check("no download eta while seeding", get_text("INFO_ETA") in seeding, False)

plain = detail_text()
for key in ("INFO_RATIO_LIMIT", "INFO_SEEDING_TIME", "INFO_PRIVATE", "INFO_IDLE", "INFO_AVAILABILITY"):
	check(f"nothing unknown is shown ({key})", get_text(key).split("$")[0].strip() in plain, False)
check("the peers line falls back to the connected ones", get_text("INFO_PEERS_DETAIL", 0, 0) in plain, True)

downloading = detail_text(status=TorrentStatus.DOWNLOADING, progress=40.0, availability=0.857, eta=600,
	seeding_time=0, download_rate=100, last_activity=now - 5400, swarm_seeders=0, swarm_leechers=-1, seeders=1)
check("the progress carries the availability", get_text("INFO_AVAILABILITY", "0.86") in downloading, True)
check("a transferring torrent is not idle", get_text("INFO_IDLE", "1h 30m") in downloading, False)
check("no seeding line before it seeds", get_text("INFO_SEEDING_TIME") in downloading, False)
check("the download eta stays", f"{get_text('INFO_ETA')}: 10m" in downloading, True)
check("a half known swarm is left out", get_text("INFO_PEERS_DETAIL", 1, 0) in downloading, True)

# The ratio limit of a single torrent: one tap between the usual values, and
# the screen marks the one in force
def ratio_screen(**changes):
	torrent = fake_torrent()
	for key, value in changes.items():
		setattr(torrent, key, value)
	return bot_module.build_ratio_screen(torrent, config.FILTER_ALL, 0)


def labelled(markup):
	return {button.text: button.callback_data for row in markup.keyboard for button in row}


ratio_button = next((c for c in detail_buttons() if c.startswith("ratioMenu|")), None)
check("the torrent screen offers the ratio limit", ratio_button is not None, True)
text, markup = ratio_screen(ratio_limit=2.0, ratio_limit_mode="global")
buttons = labelled(markup)
check("the ratio screen says the limit comes from the manager",
	get_text("RATIO_LIMIT_NOW", get_text("RATIO_GLOBAL_VALUE", "2")) in text, True)
check("the manager's option is the one marked", "✅ " + get_text("BUTTON_RATIO_GLOBAL") in buttons, True)
check("only one option is marked", sum(label.startswith("✅") for label in buttons), 1)
text, markup = ratio_screen(ratio_limit=-1, ratio_limit_mode="none")
check("no limit reads as such", get_text("RATIO_LIMIT_NOW", get_text("NO_LIMIT")) in text, True)
check("and its option is marked", "✅ " + get_text("BUTTON_RATIO_NONE") in labelled(markup), True)
check("a preset is marked", "✅ 0.5" in labelled(ratio_screen(ratio_limit=0.5, ratio_limit_mode="torrent")[1]), True)
check("any other value is marked on the typed option",
	"✅ " + get_text("BUTTON_RATIO_CUSTOM") + " (1.5)" in labelled(ratio_screen(ratio_limit=1.5, ratio_limit_mode="torrent")[1]), True)
check("an unknown mode marks nothing", sum(label.startswith("✅") for label in labelled(ratio_screen(ratio_limit_mode="")[1])), 0)

screen = press(ratio_button)
for label, expected in ((get_text("BUTTON_RATIO_NONE"), -1), ("0.5", 0.5), ("2", 2.0), (get_text("BUTTON_RATIO_GLOBAL"), None)):
	CALLS.clear()
	press(labelled(screen[1])[label])
	check(f"the {label} option sets the limit", CALLS, [("set_ratio_limit", [TORRENT_ID], expected)])
	check(f"the {label} option stays on the ratio screen", EDITS[-1][0].startswith(get_text("LIMITS_TITLE", html.escape(fake_torrent().name))), True)
# The mark follows the choice, read back from the manager
chosen = {}
real_get, real_set = bot_module.client.get_torrent, bot_module.client.set_ratio_limit
def get_with_limit(torrent_id):
	torrent = real_get(torrent_id)
	limit = chosen.get("limit", "unset")
	if limit != "unset":
		torrent.ratio_limit_mode = "global" if limit is None else ("none" if limit < 0 else "torrent")
		torrent.ratio_limit = 2.0 if limit is None else limit
	return torrent
bot_module.client.get_torrent = get_with_limit
bot_module.client.set_ratio_limit = lambda ids, limit: chosen.update(limit=limit)
for label, marked in (("0.5", "✅ 0.5"), (get_text("BUTTON_RATIO_NONE"), "✅ " + get_text("BUTTON_RATIO_NONE")),
					(get_text("BUTTON_RATIO_GLOBAL"), "✅ " + get_text("BUTTON_RATIO_GLOBAL"))):
	screen = press(labelled(screen[1]).get(label) or labelled(screen[1])["✅ " + label])
	check(f"pressing {label} moves the mark to it", marked in labelled(screen[1]), True)
	check(f"pressing {label} leaves a single mark", sum(text.startswith("✅") for text in labelled(screen[1])), 1)
bot_module.client.get_torrent, bot_module.client.set_ratio_limit = real_get, real_set
screen = press(ratio_button)

CALLS.clear()
press(bot_module.build_call("ratioSet", ratio_button.split("|")[1], "x"))
check("a forged value sets nothing", CALLS, [])

for typed, expected in (("0,5", 0.5), ("1.25", 1.25), ("0", 0.0), ("-1", None), ("abc", None), ("nan", None), ("99999", None)):
	check(f"typing {typed!r}", bot_module.parse_ratio(typed), expected)
bot_module.pending_inputs.clear()
press(labelled(screen[1])[get_text("BUTTON_RATIO_CUSTOM")])
pending = bot_module.pop_pending_input(CHAT_ID, 1)
check("cancelling the typed value goes back to the ratio screen", pending["back_call"], ratio_button)
CALLS.clear()
message = type("M", (), {"chat": type("C", (), {"id": CHAT_ID})(), "message_thread_id": None, "text": "0,75", "message_id": 9,
						"from_user": type("U", (), {"id": 1})()})()
bot_module.handle_pending_input(message, pending)
check("a typed ratio is set", CALLS, [("set_ratio_limit", [TORRENT_ID], 0.75)])
check("and confirmed on top of the ratio screen", EDITS[-1][0].startswith(
	get_text("RATIO_SET_OK", html.escape(fake_torrent().name), "0.75") + "\n\n" + get_text("LIMITS_TITLE", html.escape(fake_torrent().name))), True)
check("which keeps its buttons", bot_module.build_call("info", TORRENT_ID, config.FILTER_ALL, 0) in labelled(EDITS[-1][1]).values(), True)

# The speed of a single torrent, typed, back on the limits screen
screen = press(ratio_button)
check("the limits screen tells both speeds", all(get_text(key, get_text("NO_LIMIT")) in screen[0] for key in ("LIMITS_DOWN", "LIMITS_UP")), True)
for label, direction in ((get_text("BUTTON_TORRENT_DOWN_LIMIT"), "down"), (get_text("BUTTON_TORRENT_UP_LIMIT"), "up")):
	bot_module.pending_inputs.clear()
	press(labelled(screen[1])[label])
	pending = bot_module.pop_pending_input(CHAT_ID, 1)
	check(f"cancelling the {direction} limit goes back to the limits screen", pending["back_call"], ratio_button)
	for typed, expected in (("500", 500), ("0", 0), ("abc", None), ("-3", None)):
		CALLS.clear()
		message.text = typed
		bot_module.handle_pending_input(message, dict(pending))
		calls = [c for c in CALLS if c[0] == "set_torrent_speed_limit"]
		check(f"typing {typed!r} as the {direction} limit", calls, [] if expected is None else [("set_torrent_speed_limit", [TORRENT_ID], direction, expected)])
		if expected is not None:
			check(f"typing {typed!r} as the {direction} limit comes back to the limits screen",
				EDITS[-1][0].startswith(get_text("LIMIT_SET_OK") + "\n\n" + get_text("LIMITS_TITLE", html.escape(fake_torrent().name))), True)
limited = fake_torrent()
limited.download_limit, limited.upload_limit = 800, -1
real_get = bot_module.client.get_torrent
bot_module.client.get_torrent = lambda torrent_id: limited
detail = bot_module.build_detail(TORRENT_ID, config.FILTER_ALL, 0)[0]
check("the torrent screen tells its own download limit", get_text("INFO_SPEED_LIMIT", "800 KB/s") in detail, True)
check("and nothing for the upload one it does not have", detail.count(get_text("INFO_SPEED_LIMIT", "")[:4]), 1)
check("the limits screen shows it", get_text("LIMITS_DOWN", "800 KB/s") in bot_module.build_ratio_screen(limited, config.FILTER_ALL, 0)[0], True)
bot_module.client.get_torrent = real_get

# A manager with speed but no ratio limits per torrent, and the other way round
bot_module.client.supports_ratio_limit = False
text, markup = ratio_screen()
check("without ratio limits the screen only has the speeds",
	(get_text("RATIO_HINT") in text, get_text("BUTTON_RATIO_NONE") in labelled(markup), get_text("BUTTON_TORRENT_DOWN_LIMIT") in labelled(markup)),
	(False, False, True))
check("and the limits button stays", any(c.startswith("ratioMenu|") for c in detail_buttons()), True)
bot_module.client.supports_ratio_limit = True
bot_module.client.supports_torrent_speed_limit = False
text, markup = ratio_screen()
check("without speed limits per torrent the screen only has the ratio",
	(get_text("LIMITS_DOWN", "")[:6] in text, get_text("BUTTON_TORRENT_DOWN_LIMIT") in labelled(markup)), (False, False))
bot_module.client.supports_torrent_speed_limit = True

# Reannounce: only an active torrent with a tracker
check("a seeding torrent with trackers can reannounce", any(c.startswith("reannounce|") for c in detail_buttons()), True)
CALLS.clear()
press(next(c for c in detail_buttons() if c.startswith("reannounce|")))
check("reannouncing asks the manager", CALLS, [("reannounce_torrents", [TORRENT_ID])])
for changes, label in (({"status": TorrentStatus.PAUSED}, "a paused torrent"), ({"trackers": []}, "a torrent without trackers")):
	quiet = fake_torrent()
	for key, value in changes.items():
		setattr(quiet, key, value)
	bot_module.client.get_torrent = lambda torrent_id: quiet
	check(f"{label} has nothing to reannounce", bot_module.build_call("reannounce", TORRENT_ID, config.FILTER_ALL, 0)
		in callbacks_of(bot_module.build_detail(TORRENT_ID, config.FILTER_ALL, 0)[1]), False)
	bot_module.client.get_torrent = real_get
bot_module.client.supports_reannounce = False
check("a manager that cannot reannounce has no button", any(c.startswith("reannounce|") for c in detail_buttons()), False)
bot_module.client.supports_reannounce = True

# Limits of every torrent of a filter: ratio, download and upload
def mass_menu(filter_key=config.FILTER_ALL):
	return press(bot_module.build_call("massMenu", filter_key, 0))


list_calls = callbacks_of(mass_menu()[1])
mass_ratio = bot_module.build_call("mass", "ratio", config.FILTER_ALL)
check("the actions on all offer the limits of all", mass_ratio in list_calls, True)
check("named limits", get_text("BUTTON_MASS_LIMITS") in labelled(mass_menu()[1]), True)
screen = press(mass_ratio)
check("it shows the limits of all of them", screen[0].startswith(get_text("MASS_LIMITS_TITLE", len(TORRENT_IDS))), True)
check("with nothing marked", sum(label.startswith("✅") for label in labelled(screen[1])), 0)
speed_asks = [labelled(screen[1]).get(get_text(k)) for k in ("BUTTON_TORRENT_DOWN_LIMIT", "BUTTON_TORRENT_UP_LIMIT")]
check("and the speed of each one, for the torrents counted on the screen",
	[(c or "").split("|")[0] + "|" + (c or "").split("|")[-1] for c in speed_asks], ["massSpeedAsk|down", "massSpeedAsk|up"])
check("the limits of all carry what they counted, not the filter", all(c.split("|")[1].startswith("m") for c in speed_asks), True)
check("back goes to the list", labelled(screen[1]).get(get_text("BUTTON_BACK")), bot_module.build_call("list", config.FILTER_ALL, 0))
for label, expected, said in ((get_text("BUTTON_RATIO_NONE"), -1, get_text("NO_LIMIT")), ("0.5", 0.5, "0.5"),
							(get_text("BUTTON_RATIO_GLOBAL"), None, get_text("BUTTON_RATIO_GLOBAL"))):
	CALLS.clear()
	done = press(labelled(screen[1])[label])
	check(f"{label} for all is set on all of them", CALLS, [("set_ratio_limit", TORRENT_IDS, expected)])
	check(f"{label} for all is confirmed", done[0].split("\n")[0], get_text("MASS_DONE_RATIO", len(TORRENT_IDS), said))
	check(f"and {label} stays on the limits", get_text("MASS_LIMITS_TITLE", len(TORRENT_IDS)) in done[0], True)
bot_module.pending_inputs.clear()
press(labelled(screen[1])[get_text("BUTTON_RATIO_CUSTOM")])
pending = bot_module.pop_pending_input(CHAT_ID, 1)
check("cancelling the typed ratio of all goes back to its screen", pending["back_call"], mass_ratio)
CALLS.clear()
message.text = "1,5"
bot_module.handle_pending_input(message, pending)
check("a typed ratio is set on all of them", CALLS, [("set_ratio_limit", TORRENT_IDS, 1.5)])
check("and confirmed", EDITS[-1][0].split("\n")[0], get_text("MASS_DONE_RATIO", len(TORRENT_IDS), "1.5"))
for direction, done_key, typed, said in (("down", "MASS_DONE_DOWN", "300", "300 KB/s"), ("up", "MASS_DONE_UP", "0", get_text("NO_LIMIT"))):
	bot_module.pending_inputs.clear()
	press(speed_asks[0 if direction == "down" else 1])
	pending = bot_module.pop_pending_input(CHAT_ID, 1)
	check(f"cancelling the typed {direction} limit of all goes back to the limits", pending["back_call"], mass_ratio)
	CALLS.clear()
	message.text = typed
	bot_module.handle_pending_input(message, pending)
	check(f"a typed {direction} limit is set on all of them", CALLS, [("set_torrent_speed_limit", TORRENT_IDS, direction, int(typed))])
	check(f"and the {direction} limit is confirmed on the limits", (EDITS[-1][0].split("\n")[0],
		get_text("MASS_LIMITS_TITLE", len(TORRENT_IDS)) in EDITS[-1][0]), (get_text(done_key, len(TORRENT_IDS), said), True))
bot_module.pending_inputs.clear()
old_button = press(bot_module.build_call("massSpeedAsk", config.FILTER_ALL, "down"))
check("a limits button from before the confirmations kept their torrents shows the limits again, counted now",
	(bot_module.pop_pending_input(CHAT_ID, 1), old_button[0].startswith(get_text("MASS_LIMITS_TITLE", len(TORRENT_IDS)))), (None, True))
bot_module.pending_inputs.clear()
press(speed_asks[0])
pending = bot_module.pop_pending_input(CHAT_ID, 1)
CALLS.clear()
message.text = "mucho"
bot_module.handle_pending_input(message, pending)
check("a speed that is not a number sets nothing", CALLS, [])

bot_module.client.supports_ratio_limit = False
check("without ratio limits the limits of all are only the speed", (mass_ratio in callbacks_of(mass_menu()[1]),
	get_text("BUTTON_RATIO_NONE") in labelled(press(mass_ratio)[1])), (True, False))
bot_module.client.supports_torrent_speed_limit = False
check("and with no limits at all there are no limits of all", mass_ratio in callbacks_of(mass_menu()[1]), False)
check("a manager without per torrent limits has no limits button",
	any(c.startswith("ratioMenu|") for c in detail_buttons()), False)
bot_module.client.supports_torrent_speed_limit = True
check("nor a ratio row in the settings",
	bot_module.settings_call("ratio") in callbacks_of(bot_module.build_settings("limits")[1]), False)
check("nor a ratio settings screen", bot_module.build_settings("ratio")[0].startswith(get_text("SETTINGS_RATIO_TITLE")), False)
bot_module.client.supports_ratio_limit = True

# The manager's own ratio, from the settings: same choices but the manager's one
bot_module.client.global_ratio = 2.0
main_text, main_markup = bot_module.build_settings("limits")
check("the settings show the manager's ratio",
	labelled(main_markup).get(get_text("SETTINGS_ROW_RATIO", "2")), bot_module.settings_call("ratio"))
screen = press(bot_module.settings_call("ratio"))
check("the ratio settings screen opens", screen[0].startswith(get_text("SETTINGS_RATIO_TITLE")), True)
check("it says the limit in force", get_text("RATIO_LIMIT_NOW", "2") in screen[0], True)
check("its preset is marked", "✅ 2" in labelled(screen[1]), True)
check("there is no manager's option inside the manager", any(get_text("BUTTON_RATIO_GLOBAL") in label for label in labelled(screen[1])), False)
check("only Deluge warns that it only applies to new torrents", get_text("SETTINGS_RATIO_ONLY_NEW") in screen[0], False)
for label, expected, marked in ((get_text("BUTTON_RATIO_NONE"), -1, "✅ " + get_text("BUTTON_RATIO_NONE")), ("0.5", 0.5, "✅ 0.5")):
	CALLS.clear()
	screen = press(labelled(screen[1])[label])
	check(f"the manager's {label} is set", CALLS, [("set_global_ratio_limit", expected)])
	check(f"and the mark moves to {label}", marked in labelled(screen[1]), True)
	check(f"and stays on the ratio settings", screen[0].startswith(get_text("SETTINGS_RATIO_TITLE")), True)
bot_module.client.global_ratio = -1
check("no limit reads as such in the settings",
	get_text("SETTINGS_ROW_RATIO", get_text("NO_LIMIT")) in labelled(bot_module.build_settings("limits")[1]), True)
bot_module.client.global_ratio = 0.5
bot_module.pending_inputs.clear()
press(labelled(screen[1])[get_text("BUTTON_RATIO_CUSTOM")])
pending = bot_module.pop_pending_input(CHAT_ID, 1)
check("cancelling the typed global ratio goes back to its screen", pending["back_call"], bot_module.settings_call("ratio"))
CALLS.clear()
message.text = "3,5"
bot_module.handle_pending_input(message, pending)
check("a typed global ratio is set", CALLS, [("set_global_ratio_limit", 3.5)])
check("and its screen comes back confirmed",
	EDITS[-1][0].startswith(f"{get_text('SETTINGS_UPDATED')}\n\n{get_text('SETTINGS_RATIO_TITLE')}"), True)
check("with the typed value marked", "✅ " + get_text("BUTTON_RATIO_CUSTOM") + " (3.5)" in labelled(EDITS[-1][1]), True)
bot_module.client.ratio_limit_copied_on_add = True
check("Deluge warns that it only applies to new torrents", get_text("SETTINGS_RATIO_ONLY_NEW") in bot_module.build_settings("ratio")[0], True)
del bot_module.client.ratio_limit_copied_on_add
bot_module.client.global_ratio = 2.0

# Order of the lists: every criterion, turned around, with the unknown values last
def sort_sample():
	return [TorrentInfo(id="a", name="beta", status=TorrentStatus.SEEDING, total_size=10, progress=100.0, ratio=2.0,
					added_date=300, last_activity=None, download_rate=0, upload_rate=5),
			TorrentInfo(id="b", name="Alfa", status=TorrentStatus.DOWNLOADING, total_size=30, progress=40.0, ratio=0.1,
					added_date=200, last_activity=50, download_rate=100, upload_rate=0),
			TorrentInfo(id="c", name="gamma", status=TorrentStatus.PAUSED, total_size=20, progress=70.0, ratio=1.0,
					added_date=100, last_activity=90, download_rate=0, upload_rate=0)]


def sorted_ids(key, reverse=False):
	bot_module.bot_settings.set("list_sort", key)
	bot_module.bot_settings.set("list_sort_reverse", reverse)
	return [t.id for t in bot_module.sort_torrents(sort_sample())]


check("the lists open newest first", sorted_ids("added"), ["a", "b", "c"])
check("oldest first", sorted_ids("added", True), ["c", "b", "a"])
check("by name, whatever the case", sorted_ids("name"), ["b", "a", "c"])
check("by name backwards", sorted_ids("name", True), ["c", "a", "b"])
check("largest first", sorted_ids("size"), ["b", "c", "a"])
check("smallest first", sorted_ids("size", True), ["a", "c", "b"])
check("most advanced first", sorted_ids("progress"), ["a", "c", "b"])
check("highest ratio first", sorted_ids("ratio"), ["a", "c", "b"])
check("fastest first, down and up together", sorted_ids("speed"), ["b", "a", "c"])
check("ties keep the newest first", sorted_ids("speed", True), ["c", "a", "b"])
check("recent activity first, the never active last", sorted_ids("activity"), ["c", "b", "a"])
check("old activity first, the never active still last", sorted_ids("activity", True), ["b", "c", "a"])
bot_module.bot_settings.set("list_sort", "nonsense")
check("an unknown order falls back to the newest first", bot_module.current_sort()[0], "added")

bot_module.bot_settings.set("list_sort", "added")
bot_module.bot_settings.set("list_sort_reverse", False)
screen = press(bot_module.build_call("list", config.FILTER_ALL, 0))
sort_button = labelled(screen[1])[get_text("BUTTON_SORT", get_text("SORT_ADDED_SHORT"))]
screen = press(sort_button)
check("the sort menu marks the current order", "✅ " + get_text("SORT_ADDED_SHORT") in labelled(screen[1]), True)
screen = press(labelled(screen[1])[get_text("SORT_SIZE_SHORT")])
check("choosing an order keeps the menu open", screen[0], get_text("SORT_TITLE", get_text("SORT_SIZE")))
check("with the mark moved to it", "✅ " + get_text("SORT_SIZE_SHORT") in labelled(screen[1]), True)
check("and keeps it", bot_module.current_sort(), ("size", False))
screen = press(labelled(screen[1])[get_text("BUTTON_SORT_REVERSE")])
check("reversing right away turns the chosen order around", bot_module.current_sort(), ("size", True))
check("and the mark shows the reversed order", "✅ " + get_text("SORT_SIZE_REVERSE_SHORT") in labelled(screen[1]), True)
screen = press(labelled(screen[1])[get_text("BUTTON_SORT_REVERSE")])
check("reversing twice goes back to the natural order", bot_module.current_sort(), ("size", False))
screen = press(labelled(screen[1])[get_text("BUTTON_SORT_REVERSE")])
screen = press(labelled(screen[1])["✅ " + get_text("SORT_SIZE_REVERSE_SHORT")])
check("pressing the marked order leaves it as it is", bot_module.current_sort(), ("size", True))
screen = press(labelled(screen[1])[get_text("BUTTON_BACK")])
check("back opens the list on its first page", screen[0].startswith(bot_module.get_text("LIST_TITLE", bot_module.get_filter_label(config.FILTER_ALL), len(TORRENT_IDS), 1, 2)), True)
check("the list shows the order in its button", get_text("BUTTON_SORT", get_text("SORT_SIZE_REVERSE_SHORT")) in labelled(screen[1]), True)
screen = press(labelled(press(labelled(screen[1])[get_text("BUTTON_SORT", get_text("SORT_SIZE_REVERSE_SHORT"))])[1])[get_text("SORT_NAME_SHORT")])
check("another order starts in its natural direction", bot_module.current_sort(), ("name", False))
# The list keeps a single row for what is not a torrent nor a page: the order
# and the menu of the actions on all, which has every one of them
screen = press(bot_module.build_call("list", config.FILTER_ALL, 1))
rows = [[b.callback_data.split("|")[0] for b in row] for row in screen[1].keyboard]
check("the list has the order and the actions on all in one row", ["sort", "massMenu"] in rows, True)
check("and no action on all of its own", any(c in ("mass", "tlRule") for row in rows for c in row), False)
check("after it only the bottom row", rows[-1], ["list", "dashboard", "cerrar"] if rows[-1][0] == "list" else ["dashboard", "cerrar"])
screen = press(labelled(screen[1])[get_text("BUTTON_MASS_MENU")])
calls = callbacks_of(screen[1])
check("the actions on all say how many torrents they cover",
	screen[0].startswith(get_text("MASS_MENU_TITLE", bot_module.get_filter_label(config.FILTER_ALL), len(TORRENT_IDS))), True)
check("the actions on all are all there", [c for c in calls if c.startswith("mass|")],
	[bot_module.build_call("mass", a, config.FILTER_ALL) for a in ("resume", "pause", "move", "cat", "ratio", "delete")])
check("their back goes to the page they were opened from", calls[-2], bot_module.build_call("list", config.FILTER_ALL, 1))
tf = tracker_filter()
calls = callbacks_of(press(bot_module.build_call("massMenu", tf, 0))[1])
check("on a tracker the limits of the tracker replace the limits of all",
	(any(c.startswith("tlRule|") for c in calls), bot_module.build_call("mass", "ratio", tf) in calls), (True, False))
bot_module.bot_settings.set("list_sort", "added")
bot_module.bot_settings.set("list_sort_reverse", False)
menu = bot_module.build_sort_menu(config.FILTER_ALL)[1]
check("the sort menu shows the orders in two columns", [len(row) for row in menu.keyboard], [2, 2, 2, 1, 1, 2])
CALLS.clear()

bot_module.client.reports_last_activity = False
check("the activity order is not offered when the manager does not report it",
	get_text("SORT_ACTIVITY_SHORT") in labelled(bot_module.build_sort_menu(config.FILTER_ALL)[1]), False)
bot_module.bot_settings.set("list_sort", "activity")
check("and a stored activity order falls back to the newest first", bot_module.current_sort()[0], "added")
bot_module.client.reports_last_activity = True
bot_module.bot_settings.set("list_sort", "added")
bot_module.bot_settings.set("list_sort_reverse", False)

# ---------------------------------------------------------------------------
# REVIEW FIXES: each block is a bug that was reproduced before it was fixed
# ---------------------------------------------------------------------------

import subprocess
import threading

PROFILE.update({"name": None, "files": None, "auto_managed": False, "priorities": None})
_real_start_add_flow = bot_module.start_add_flow
_real_get_torrents = bot_module.client.get_torrents


def finishes(function, seconds=3):
	"""Whether function returns within seconds (a hang must fail, not block the tests)"""
	done = threading.Event()
	threading.Thread(target=lambda: (function(), done.set()), daemon=True).start()
	return done.wait(seconds)


def tg_message(text="", thread=None, topic=False, chat_type="supergroup", user_id=1, chat_id=CHAT_ID):
	"""A message as telebot builds it: outside a forum a reply still carries
	message_thread_id (the id of the message it answers), with is_topic_message false"""
	return types.SimpleNamespace(text=text, chat=types.SimpleNamespace(id=chat_id, type=chat_type),
								from_user=types.SimpleNamespace(id=user_id), message_thread_id=thread, is_topic_message=topic,
								message_id=900, entities=None, caption_entities=None, caption=None, document=None)


# get_text fills every placeholder in one pass: a value holding "$2" keeps it
check("a value holding $2 is not filled in again", get_text("MOVE_OK", "Price$2Off", "/dest").count("Price$2Off"), 1)

# Topics: only a topic message is in a topic
check("a reply outside a forum is not in a topic", bot_module.message_thread(tg_message(thread=501)), None)
check("a topic message is", bot_module.message_thread(tg_message(thread=7, topic=True)), 7)
check("and General is no topic", bot_module.message_thread(tg_message(thread=1, topic=True)), None)
bot_module.pending_inputs.clear()
press(bot_module.build_call("search"))
SENDS.clear()
bot_module.handle_text(tg_message("Some.Movie", thread=MESSAGE_ID))
check("the answer to a prompt in a group without topics is answered without a thread", SENDS[-1:], [(CHAT_ID, None)])
bot_module.pending_inputs.clear()
bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("search"), thread_id=7)
SENDS.clear()
bot_module.handle_text(tg_message("Some.Movie", thread=7, topic=True))
check("and in a forum topic it stays in that topic", SENDS[-1:], [(CHAT_ID, 7)])
bot_module.live_views.clear()
bot_module.live_messages.clear()
bot_module.pending_inputs.clear()
bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("list", config.FILTER_ALL, 0), thread_id=7)
goto = next(c for c in callbacks_of(EDITS[-1][1]) if c.startswith("goto|"))
bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, goto, thread_id=7)
bot_module.handle_text(tg_message("2", thread=7, topic=True))
check("a page jump in a topic keeps the list live in that topic", bot_module.live_views.get((CHAT_ID, 7), {}).get("message_id"), MESSAGE_ID)
check("and registers nothing outside it", (CHAT_ID, None) in bot_module.live_views, False)

# A question waits a while, in its own topic, and gives way to anything else
bot_module.pending_inputs.clear()
bot_module.set_pending_input(CHAT_ID, 1, "search", thread_id=7)
check("a question is not answered from another topic", bot_module.pop_pending_input(CHAT_ID, 1, thread_id=9), None)
check("and still waits for its own", (bot_module.pop_pending_input(CHAT_ID, 1, thread_id=7) or {}).get("action"), "search")
bot_module.set_pending_input(CHAT_ID, 1, "search")
bot_module.pending_inputs[(CHAT_ID, 1)]["ts"] -= config.INPUT_TTL + 1
check("an old question is not waited for", bot_module.pop_pending_input(CHAT_ID, 1), None)
bot_module.set_pending_input(CHAT_ID, 1, "massMove", mass_ctx="m000000")
press_button(bot_module.build_call("dashboard"))
check("a button pressed meanwhile drops the question", bot_module.pending_inputs.get((CHAT_ID, 1)), None)
bot_module.set_pending_input(GROUP_ID, 1, "rename", torrent_id=TORRENT_ID)
send_command("/list")
check("a command drops it too", bot_module.pending_inputs.get((GROUP_ID, 1)), None)
added = []
bot_module.start_add_flow = lambda chat_id, name, magnet=None, data=None, thread_id=None: added.append(magnet)
pasted = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=New.Movie.2024.1080p"
bot_module.set_pending_input(CHAT_ID, 1, "massMove", mass_ctx="m000000")
CALLS.clear()
bot_module.handle_text(tg_message(pasted))
check("a magnet pasted while a question waits is added", added, [pasted])
check("and not taken as the answer", ([c for c in CALLS if c[0] == "move_torrents"], bot_module.pending_inputs.get((CHAT_ID, 1))), ([], None))
bot_module.start_add_flow = _real_start_add_flow

# Mass actions act on what their confirmation counted
counted = press(bot_module.build_call("mass", "delete", config.FILTER_ALL))
delete_with_data = labelled(counted[1])[get_text("BUTTON_DELETE_WITH_DATA")]
check("the delete confirmation carries what it counted, not the filter", delete_with_data.split("|")[2][0], "m")
newcomer = fake_torrent(TORRENT_IDS[1])
newcomer.id = "f" * 40
bot_module.client.get_torrents = lambda status=None, query=None: [fake_torrent(t) for t in TORRENT_IDS[2:]] + [newcomer]
CALLS.clear()
press(delete_with_data)
check("confirming deletes only the torrents counted that are still there", CALLS, [("remove_torrents", TORRENT_IDS[2:], True)])
bot_module.client.get_torrents = lambda status=None, query=None: [newcomer]
CALLS.clear()
done = press(delete_with_data)
check("with none of them left nothing is deleted", (CALLS, done[0]), ([], get_text("MASS_GONE")))
bot_module.client.get_torrents = _real_get_torrents
move_screen = press(bot_module.build_call("mass", "move", config.FILTER_ALL))
bot_module.client.get_torrents = lambda status=None, query=None: [fake_torrent(t) for t in TORRENT_IDS[:2]] + [newcomer]
CALLS.clear()
press(next(c for c in callbacks_of(move_screen[1]) if c.startswith("massMoveDir|")))
real_sleep(0.3)  # The move goes out on a worker thread
check("moving all moves only the torrents counted", [c[1] for c in CALLS if c[0] == "move_torrents"], [TORRENT_IDS[:2]])
bot_module.client.get_torrents = _real_get_torrents

# The category of all: where the folders go by category, that is what is
# changed. The manager moves the torrents it manages and only labels the rest
cat_screen = press(bot_module.build_call("mass", "cat", config.FILTER_ALL))
check("the category of all asks which one, for how many", cat_screen[0].startswith(get_text("MASS_CATEGORY_ASK", len(TORRENT_IDS))), True)
check("and says that torrents in manual mode stay where they are", get_text("MASS_CATEGORY_STAYS") in cat_screen[0], True)
cat_calls = [c for c in callbacks_of(cat_screen[1]) if c.startswith("massCat|")]
check("with every category and none", [bot_module.get_cat_by_id(c.split("|")[2]) for c in cat_calls], [name for name, _path in CATEGORIES] + [""])
bot_module.client.get_torrents = lambda status=None, query=None: [fake_torrent(t) for t in TORRENT_IDS[:2]] + [newcomer]
CALLS.clear()
changed = press(cat_calls[1])
real_sleep(0.3)  # It may move data: delivered on a worker thread, like a move
check("a category is given only to the torrents counted", [c for c in CALLS if c[0] == "set_category"], [("set_category", TORRENT_IDS[:2], CATEGORIES[1][0])])
check("and it is told", EDITS[-1][0], get_text("MASS_CATEGORY_DONE", 2, CATEGORIES[1][0]))
CALLS.clear()
press(cat_calls[-1])
real_sleep(0.3)
check("none takes the category away, and says so in its own words", ([c for c in CALLS if c[0] == "set_category"], EDITS[-1][0]),
	([("set_category", TORRENT_IDS[:2], "")], get_text("MASS_CATEGORY_CLEARED", 2)))
bot_module.client.get_torrents = lambda status=None, query=None: [newcomer]
CALLS.clear()
check("with none of them left nothing is changed", (press(cat_calls[0])[0], CALLS), (get_text("MASS_GONE"), []))
_managed = fake_torrent(TORRENT_IDS[0])
_managed.auto_managed = True
bot_module.client.get_torrents = lambda status=None, query=None: [_managed, fake_torrent(TORRENT_IDS[1])]
check("torrents under automatic management are warned to be moved, telling how many",
	get_text("MASS_CATEGORY_MOVES_SOME", 1) in press(bot_module.build_call("mass", "cat", config.FILTER_ALL))[0], True)
bot_module.client.get_torrents = lambda status=None, query=None: [_managed]
check("or all of them", get_text("MASS_CATEGORY_MOVES_ALL") in press(bot_module.build_call("mass", "cat", config.FILTER_ALL))[0], True)
bot_module.client.get_torrents = _real_get_torrents
check("an expired category of all changes nothing", press(bot_module.build_call("massCat", "m000000", bot_module.get_cat_id("Series")))[0], get_text("MASS_EXPIRED"))
FakeClient.supports_categories = False
check("a manager without categories has no category of all",
	bot_module.build_call("mass", "cat", config.FILTER_ALL) in callbacks_of(bot_module.build_mass_menu(config.FILTER_ALL, 0)[1]), False)
FakeClient.supports_categories = True
CALLS.clear()
old_button = press(bot_module.build_call("confirmMass", "delete", config.FILTER_ALL, "1"))
check("a confirm button from before deletes nothing and asks again", (CALLS, old_button[0]),
	([], get_text("MASS_CONFIRM_DELETE", len(TORRENT_IDS))))
expired = press(bot_module.build_call("confirmMass", "delete", "m000000", "1"))
check("an expired confirmation deletes nothing", (CALLS, expired[0]), ([], get_text("MASS_EXPIRED")))

# Folder and category ids are the same in every run
dir_id = bot_module.get_dir_id(DIRS[3])
check("a folder id does not depend on the order folders were seen", dir_id, bot_module._stable_id(DIRS[3]))
bot_module._dir_by_id.clear()  # What a restart leaves
check("after a restart an old folder button still finds its folder", bot_module.get_dir_by_id(dir_id), DIRS[3])
check("and a button from before the stable ids finds none", bot_module.get_dir_by_id("2"), None)
cat_id = bot_module.get_cat_id(CATEGORIES[1][0])
bot_module._cat_by_id.clear()
check("a category button also survives a restart", bot_module.get_cat_by_id(cat_id), CATEGORIES[1][0])

# The buttons of a file follow it when a rename reorders the list
moved = fake_torrent()
paths = [p for p, _, _ in moved.files]
check("a file is found by its position and hash", bot_module.resolve_file(moved, 1, bot_module.file_key(paths[1]))[1], paths[1])
moved.files = [moved.files[1], moved.files[0]] + moved.files[2:]
check("after a reorder it is found where it went", bot_module.resolve_file(moved, 1, bot_module.file_key(paths[1]))[:2], (0, paths[1]))
moved.files = [(p + ".renamed", s, c) if p == paths[1] else (p, s, c) for p, s, c in moved.files]
check("and a renamed one is gone, never another file", bot_module.resolve_file(moved, 0, bot_module.file_key(paths[1])), None)
file_ctx = bot_module.new_nav_context(TORRENT_ID, config.FILTER_ALL, 0)
file_root = bot_module.torrent_folders(fake_torrent()).index(bot_module.torrent_root(fake_torrent()))
CALLS.clear()
press(bot_module.build_call("filePrio", file_ctx, file_root, 0, 0, bot_module.file_key(FILES[0][0] + ".gone"), "s"))
check("a skip button whose file is gone leaves every file alone", [c for c in CALLS if c[0] == "set_file_priority"], [])
press(bot_module.file_call("filePrio", file_ctx, file_root, 0, 0, FILES[0][0], "s"))
check("and with its file there it acts on that one", [c for c in CALLS if c[0] == "set_file_priority"],
	[("set_file_priority", TORRENT_ID, [FILES[0][0]], "skip")])

# Live screens: a press on a message stops its own refresher, wherever it was registered
bot_module.live_views.clear()
bot_module.live_messages.clear()
press(bot_module.build_call("list", config.FILTER_ALL, 0))
generation = bot_module.live_messages[(CHAT_ID, MESSAGE_ID)]
bot_module.start_live_view(CHAT_ID, MESSAGE_ID + 1, None, bot_module.build_dashboard)
bot_module.stop_live_view(CHAT_ID, MESSAGE_ID)
EDITS.clear()
real_live_refresher(CHAT_ID, None, MESSAGE_ID, generation, lambda refreshing: bot_module.build_list(config.FILTER_ALL, 0, refreshing))
check("a replaced screen whose button was pressed is not painted over", EDITS, [])
bot_module.live_messages.clear()
bot_module.start_live_view(CHAT_ID, MESSAGE_ID, None, bot_module.build_dashboard)
generation = bot_module.live_messages[(CHAT_ID, MESSAGE_ID)]
bot_module.stop_live_view(CHAT_ID, MESSAGE_ID, 7)
EDITS.clear()
real_live_refresher(CHAT_ID, None, MESSAGE_ID, generation, bot_module.build_dashboard)
check("a press stops the refresher of its message whatever topic it was registered under", EDITS, [])
bot_module.live_messages.clear()
press(bot_module.build_call("list", config.FILTER_ALL, 0))
generation = bot_module.live_messages[(CHAT_ID, MESSAGE_ID)]
press_button(bot_module.build_call("noop"))
check("a dead arrow does not stop the screen repainting itself", bot_module.live_messages.get((CHAT_ID, MESSAGE_ID)), generation)
press_button(bot_module.build_call("dashboard"))
check("a real button does", bot_module.live_messages.get((CHAT_ID, MESSAGE_ID)) != generation, True)
bot_module.live_messages.clear()
bot_module.start_live_view(CHAT_ID, MESSAGE_ID, None, bot_module.build_dashboard, "refreshDashboard")
generation = bot_module.live_messages[(CHAT_ID, MESSAGE_ID)]


def failing_last(refreshing):
	if not refreshing:
		raise torrent_clients.TorrentClientError("manager restarting")
	return bot_module.build_dashboard(refreshing)


EDITS.clear()
real_live_refresher(CHAT_ID, None, MESSAGE_ID, generation, failing_last, "refreshDashboard")
check("a last repaint that fails says so", EDITS[-1][0], get_text("CONNECTION_ERROR", "manager restarting"))
check("with the button to try again", callbacks_of(EDITS[-1][1])[0], "refreshDashboard")
bot_module.live_messages.clear()
bot_module.bot_settings.set("list_sort", "speed")
speeds = {torrent_id: position for position, torrent_id in enumerate(TORRENT_IDS)}


def with_speeds(status=None, query=None):
	torrents = [fake_torrent(t) for t in TORRENT_IDS]
	for torrent in torrents:
		torrent.download_rate = speeds[torrent.id]
	return torrents


bot_module.client.get_torrents = with_speeds
order = {}
first = [c for c in callbacks_of(bot_module.build_list(config.FILTER_ALL, 0, True, order)[1]) if c.startswith("info|")]
for torrent_id in speeds:
	speeds[torrent_id] = -speeds[torrent_id]
again = [c for c in callbacks_of(bot_module.build_list(config.FILTER_ALL, 0, True, order)[1]) if c.startswith("info|")]
check("a live list keeps its order while it repaints", again, first)
anew = [c for c in callbacks_of(bot_module.build_list(config.FILTER_ALL, 0, False)[1]) if c.startswith("info|")]
check("and a new one sorts again", anew != first, True)
bot_module.client.get_torrents = _real_get_torrents
bot_module.bot_settings.set("list_sort", "added")

# Telegram calls that cannot do what was asked are handled, not painted as errors
removed_markups = []


def refuse_delete(*a, **k):
	raise FakeTelegramError("Bad Request: message can't be deleted for everyone")


bot_module.bot.delete_message = refuse_delete
bot_module.bot.edit_message_reply_markup = lambda chat_id, message_id, reply_markup=None: removed_markups.append(message_id) or True
press(bot_module.build_call("cerrar"))
check("closing a message too old to delete leaves it without buttons", removed_markups, [MESSAGE_ID])


def not_modified(*a, **k):
	raise FakeTelegramError("Bad Request: message is not modified")


bot_module.bot.edit_message_reply_markup = not_modified
EDITS.clear()
press(bot_module.build_call("serKeep"))
check("a second Keep paints no error over the notice", EDITS, [])
bot_module.bot.delete_message = lambda *a, **k: True
bot_module.bot.edit_message_reply_markup = lambda *a, **k: True

# Adding
taken = bot_module.new_pending_torrent("Some.Movie.2024.1080p", magnet="magnet:?xt=urn:btih:" + "b" * 40)
bot_module.pop_pending_torrent(taken)
bot_module._adding.add(taken)  # The folder was chosen: the add is under way
bot_module.live_messages.clear()
EDITS.clear()
press(bot_module.build_call("cancelAdd", taken))
check("cancelling an add already under way does not put a dashboard over its result", (EDITS, bot_module.live_messages), ([], {}))
bot_module._adding.discard(taken)
expired_add = bot_module.new_pending_torrent("Some.Movie.2024.1080p", magnet="magnet:?xt=urn:btih:" + "e" * 40)
bot_module.pending_torrents.clear()  # What a restart leaves
EDITS.clear()
press(bot_module.build_call("cancelAdd", expired_add))
check("cancelling an add the bot forgot still clears the prompt", bool(EDITS) and EDITS[-1][0].startswith(get_text("DASHBOARD_TITLE", 12)[:10]), True)
_real_add = bot_module.client.add_torrent


def duplicate(**kwargs):
	raise torrent_clients.DuplicateTorrentError("already there", torrent_id=TORRENT_ID)


bot_module.client.add_torrent = duplicate
pending_id = bot_module.new_pending_torrent("Some.Movie.2024.1080p", magnet="magnet:?xt=urn:btih:" + "c" * 40)
done = press(bot_module.build_call("addTo", pending_id, bot_module.get_dir_id(DIRS[0])))
check("adding a torrent already there names the one there", done[0], get_text("ADD_DUPLICATE", bot_module.html.escape(fake_torrent().name)))
check("and leaves no late add waiting for it", "c" * 40 in bot_module._late_adds, False)


def refuse_add(**kwargs):
	raise torrent_clients.TorrentClientError("invalid torrent")


bot_module.client.add_torrent = refuse_add
check("a negative length in a .torrent is not looped on", finishes(lambda: bot_module.torrent_data_hash(b"d-3:abce")), True)
check("nor one inside the info dict", bot_module.torrent_data_hash(b"d4:infod-3:abceee") if finishes(
	lambda: bot_module.torrent_data_hash(b"d4:infod-3:abceee")) else "hangs", None)


def corrupt_add():
	try:
		bot_module.add_torrent_from_bot(late_add={"chat_id": CHAT_ID}, torrent_data=b"d4:infod-3:abceee")
	except torrent_clients.TorrentClientError:
		pass


check("a corrupt .torrent the manager refuses does not hang the add", finishes(corrupt_add), True)
check("and the add lock is free afterwards", bot_module._bot_adds_lock.acquire(timeout=1), True)
bot_module._bot_adds_lock.release()
bot_module.client.add_torrent = _real_add


def document(name, chat_type, user_id=1):
	sent = tg_message(chat_type=chat_type, user_id=user_id)
	sent.document = types.SimpleNamespace(file_name=name, file_size=10, file_id="file")
	return sent


SENDS.clear()
bot_module.handle_document(document("photo.jpg", "supergroup"))
bot_module.handle_document(document("Movie.torrent", "supergroup", user_id=STRANGER_ID))
check("in a group a file that is not for the bot gets no answer", SENDS, [])
bot_module.handle_document(document("photo.jpg", "private"))
check("in private it is told it is not a .torrent", len(SENDS), 1)
_real_test_connection = bot_module.client.test_connection


def proxy_page():
	raise torrent_clients.TorrentClientError("qBittorrent error 502 on app/version: <html><title>Bad Gateway</title></html>")


bot_module.client.test_connection = proxy_page
EDITS.clear()
bot_module.command_version(tg_message("/version"))
check("/version escapes the error of a proxy", "&lt;html&gt;" in EDITS[-1][0], True)
bot_module.client.test_connection = _real_test_connection


class FakeResponse:
	def __init__(self, status=200, location=None, chunks=(), delay=0):
		self.status_code = status
		self.headers = {"Location": location} if location else {}
		self.is_redirect = location is not None
		self.chunks = list(chunks)
		self.delay = delay

	def raise_for_status(self):
		pass

	def iter_content(self, chunk_size=1):
		for chunk in self.chunks:
			real_sleep(self.delay)
			yield chunk

	def close(self):
		pass


_real_requests_get = bot_module.requests.get
redirected = "magnet:?xt=urn:btih:" + "d" * 40 + "&dn=Redirected.Movie"
bot_module.requests.get = lambda url, **kw: FakeResponse(302, location=redirected)
try:
	bot_module.download_torrent_from_url("http://indexer.local/download/1")
	fail("a redirect to a magnet is taken as a download")
except bot_module.MagnetRedirect as redirect:
	check("a link that redirects to a magnet gives the magnet", redirect.magnet, redirected)
added.clear()
bot_module.start_add_flow = lambda chat_id, name, magnet=None, data=None, thread_id=None: added.append(magnet)
bot_module.add_url_from_message(tg_message("http://indexer.local/download/1", chat_type="private"), "http://indexer.local/download/1")
check("and that magnet is added", added, [redirected])
bot_module.start_add_flow = _real_start_add_flow
bot_module.URL_DOWNLOAD_DEADLINE = 0.3
bot_module.requests.get = lambda url, **kw: FakeResponse(200, chunks=[b"d"] * 50, delay=0.1)
started = time.monotonic()
try:
	bot_module.download_torrent_from_url("http://slow.local/file.torrent")
	fail("a download past its deadline is not given up")
except ValueError:
	pass
check("a server that trickles is given up at the deadline", time.monotonic() - started < 2, True)
bot_module.URL_DOWNLOAD_DEADLINE = config.URL_DOWNLOAD_DEADLINE
bot_module.requests.get = _real_requests_get

# Templates with path characters or absurd padding are refused with their reason
for template, key, detail in (("{title}/{year}", "TPL_ERROR_INVALID_CHAR", "/"),
							("{episode.100000000}", "TPL_ERROR_PADDING_TOO_WIDE", "episode.100000000")):
	bot_module.pending_inputs.clear()
	press(bot_module.build_call("tplEdit", "movie"))
	pending = bot_module.pop_pending_input(CHAT_ID, 1)
	EDITS.clear()
	message.text = template
	bot_module.handle_pending_input(message, pending)
	check(f"the template {template!r} is refused with its reason", EDITS[-1][0].startswith(get_text(key, bot_module.html.escape(detail))), True)
	check(f"and the template {template!r} is not saved", bot_module.bot_settings.get("template_movie"), "")
	bot_module.bot_settings.set("template_movie", "")

# Limits per tracker
tracker_limits = bot_module.tracker_limits
_real_rules = tracker_limits.rules
tracker_limits.rules = lambda: {f"tracker{n:03d}.example.org": dict(tracker_limits.EMPTY_RULE, ratio=1.0) for n in range(150)}
text, markup = bot_module.build_settings_tracker_limits()
check("150 tracker rules fit in a keyboard Telegram accepts", len(callbacks_of(markup)) <= BUTTONS_LIMIT, True)
check("with pages", any(c.startswith("tlPage|") for c in callbacks_of(markup)), True)
second = press(bot_module.build_call("tlPage", 1))
check("the next page has the next rules", "tracker020.example.org" in " ".join(b.text for row in second[1].keyboard for b in row), True)
tracker_limits.rules = _real_rules
tracker_limits.reset_for_tests()
ruled = "tracker.example.org"
tracker_limits.set_field(ruled, "ratio", 1.0)
tracker_limits.mark_applied(TORRENT_IDS, ruled)
_real_set_ratio = bot_module.client.set_ratio_limit


def busy_ratio(torrent_ids, limit):
	raise torrent_clients.TorrentClientError("busy")


tracker_limits.set_field(ruled, "up", 50)
CALLS.clear()
bot_module.client.set_ratio_limit = busy_ratio
try:
	bot_module.set_tracker_rule(ruled, "ratio", 2.0)
	fail("a failed rule change is not told")
except torrent_clients.TorrentClientError:
	pass
bot_module.client.set_ratio_limit = _real_set_ratio
CALLS.clear()
bot_module.apply_pending_tracker_limits([fake_torrent(t) for t in TORRENT_IDS])
check("a rule change the manager failed to apply is applied by the monitor, that field only",
	CALLS, [("set_ratio_limit", TORRENT_IDS, 2.0)])
CALLS.clear()
bot_module.apply_pending_tracker_limits([fake_torrent(t) for t in TORRENT_IDS])
check("and only once", CALLS, [])
# A torrent the rule has not reached yet gets the rest of it with a hand-set limit
tracker_limits.forget(ruled)
ratio_ctx = bot_module.new_nav_context(TORRENT_ID, config.FILTER_ALL, 0)
CALLS.clear()
press(bot_module.build_call("ratioSet", ratio_ctx, "0.5"))
check("a ratio set by hand before the rule reached the torrent: the rest of the rule is given now",
	CALLS, [("set_ratio_limit", [TORRENT_ID], 0.5), ("set_torrent_speed_limit", [TORRENT_ID], "up", 50)])
check("and the monitor leaves its ratio alone",
	TORRENT_ID in [i for tracker, ids in tracker_limits.pending([fake_torrent(t) for t in TORRENT_IDS]) for i in ids], False)
tracker_limits.delete_rule(ruled)
tracker_limits.set_field("persist.example.org", "ratio", 3.0)
bot_module.bot_settings._settings = None  # What a restart does
check("tracker rules survive a restart", "persist.example.org" in tracker_limits.rules(), True)
bot_module.bot_settings.set("notify_errors", True)
with open(bot_module.bot_settings.SETTINGS_FILE, encoding="utf-8") as stored:
	check("and saving another setting keeps them on disk", "persist.example.org" in json.load(stored).get("tracker_limits", {}), True)
tracker_limits.delete_rule("persist.example.org")
check("settings hand out copies", bot_module.bot_settings.get("favorite_dirs") is bot_module.bot_settings.get("favorite_dirs"), False)

# A move retried after a newer order for the same torrents gives up
attempts = []


def busy_then_newer(torrent_ids, new_dir):
	attempts.append(new_dir)
	if len(attempts) == 1:
		bot_module.register_move_order(torrent_ids)  # Meanwhile the user moved them elsewhere
	raise torrent_clients.TorrentClientError("busy")


_real_move = bot_module.client.move_torrents
bot_module.client.move_torrents = busy_then_newer
EDITS.clear()
bot_module.deliver_move_order(CHAT_ID, [TORRENT_ID], "/old/target", "moving", "moved", message_id=MESSAGE_ID)
real_sleep(0.5)
check("a move superseded by a newer order is not retried", attempts, ["/old/target"])
check("and says so", EDITS[-1][0], get_text("MOVE_SUPERSEDED"))
bot_module.client.move_torrents = _real_move

# The check that a name is free and the rename happen together
held = []
_real_rename = bot_module.client.rename_torrent
bot_module.client.rename_torrent = lambda torrent_id, new_name: held.append(bot_module._rename_lock.locked())
bot_module.auto_rename_torrent(TorrentInfo(id="9" * 40, name="Some.Movie.2030.1080p.BluRay.x264.mkv", status=TorrentStatus.SEEDING))
check("an automatic rename checks the name and renames in one go", held, [True])
bot_module.client.rename_torrent = _real_rename


# Second review round
# A folder button follows its folder, and acts on nothing once it is gone
folders = bot_module.torrent_folders(PROFILE_SERIES := fake_torrent())  # plain profile: one folder
plain_root = bot_module.folder_ref(folders, folders.index(bot_module.torrent_root(PROFILE_SERIES)))
check("a folder button carries its key", plain_root.split(".")[1], bot_module.folder_key(bot_module.torrent_root(PROFILE_SERIES)))
moved_series = TorrentInfo(id="8" * 40, name="Pack", status=TorrentStatus.SEEDING,
							files=[("Pack/A/1.mkv", 1, 0), ("Pack/B/2.mkv", 1, 0)])
pack_folders = bot_module.torrent_folders(moved_series)
b_ref = bot_module.folder_ref(pack_folders, pack_folders.index("Pack/B"))
moved_series.files = [("Pack/0/1.mkv", 1, 0), ("Pack/B/2.mkv", 1, 0)]  # A renamed to 0 sorts first now
check("a folder renamed elsewhere does not change where a button points", bot_module.get_torrent_folder(moved_series, b_ref), "Pack/B")
moved_series.files = [("Pack/0/1.mkv", 1, 0), ("Pack/C/2.mkv", 1, 0)]
check("a folder that is gone acts on nothing", bot_module.get_torrent_folder(moved_series, b_ref, strict=True), None)
check("and browsing it shows the torrent folder", bot_module.get_torrent_folder(moved_series, b_ref), "Pack")
# A refusal is not a connection error
check("a refusal is told as such", bot_module.client_error_text(torrent_clients.PermanentTorrentError("cannot verify")),
	get_text("ACTION_REFUSED", "cannot verify"))
check("a manager to restart is told so", bot_module.add_error_text(torrent_clients.RestartRequiredError("restart Deluge")),
	get_text("MANAGER_RESTART_REQUIRED", "restart Deluge"))


def refused(**kwargs):
	raise torrent_clients.PermanentTorrentError("qBittorrent did not add the torrent")


bot_module.client.add_torrent = refused
bot_module._late_adds.clear()
try:
	bot_module.add_torrent_from_bot(late_add={"chat_id": CHAT_ID}, magnet="magnet:?xt=urn:btih:" + "7" * 40)
except torrent_clients.TorrentClientError:
	pass
check("an add refused for good is not waited for", bot_module._late_adds, {})
bot_module.client.add_torrent = _real_add
# Folders that had a button survive a restart, even ones nothing else knows
new_season = "/media/series/Show - Temporada 14"
season_id = bot_module.get_dir_id(new_season)
bot_module._dir_by_id.clear()
bot_module._dir_ids_loaded[0] = False  # What a restart leaves
check("a folder button from before a restart finds a folder no torrent is in yet", bot_module.get_dir_by_id(season_id), new_season)
# A download past its deadline has its connection cut, not left reading
closed = []


class StuckResponse(FakeResponse):
	def iter_content(self, chunk_size=1):
		while not closed:
			real_sleep(0.05)
		raise ValueError("connection closed")
		yield b""

	def close(self):
		closed.append(True)


bot_module.URL_DOWNLOAD_DEADLINE = 0.3
bot_module.requests.get = lambda url, **kw: StuckResponse(200)
try:
	bot_module.download_torrent_from_url("http://stuck.local/file.torrent")
except ValueError:
	pass
check("a download past its deadline has its connection closed", closed, [True])
bot_module.URL_DOWNLOAD_DEADLINE = config.URL_DOWNLOAD_DEADLINE
bot_module.requests.get = _real_requests_get

# A typed folder for a torrent waiting to be added that never comes (cancel
# typed, the cancel button, another button) brings the folder choice back:
# the question replaced it, and nothing else leads to that torrent
def add_folder_screen_after(leave):
	bot_module.pending_inputs.clear()
	pending_id = bot_module.new_pending_torrent("Some.Movie.2024", magnet="magnet:?xt=urn:btih:abc")
	bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("addNewDir", pending_id))
	EDITS.clear()
	leave()
	calls = [data for text, markup in EDITS for data in buttons_of((text, markup))]
	return bot_module.build_call("addNewDir", pending_id) in calls


check("typing cancel on the folder of a new torrent shows the folders again",
	add_folder_screen_after(lambda: bot_module.handle_text(tg_message("cancelar"))), True)
check("the cancel button on the folder of a new torrent shows the folders again",
	add_folder_screen_after(lambda: bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("cancelInput"))), True)
check("another button while the folder is asked shows the folders again",
	add_folder_screen_after(lambda: bot_module.drop_pending_input(CHAT_ID, 1)), True)
bot_module.pending_inputs.clear()

# A typed torrent name with a slash is refused, as for a file or a folder,
# and '/cancel@Bot' (the command as a group sends it) cancels
def typed_torrent_rename(answer):
	bot_module.pending_inputs.clear()
	CALLS.clear()
	bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("rename", TORRENT_ID, "a", 0))
	manual = [c for c in callbacks_of(EDITS[-1][1]) if c.startswith("renameManual")][0]
	bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, manual)
	before = len(EDITS)
	bot_module.handle_text(tg_message(answer, chat_type="private"))
	TYPED_AFTER[:] = EDITS[before:]
	return [c for c in CALLS if "rename" in str(c)], EDITS[-1][0]


TYPED_AFTER = []  # What the last typed answer sent
TIMERS = []  # (seconds, function) of what the bot left for later


class FakeTimer:
	def __init__(self, seconds, function, args=()):
		TIMERS.append((seconds, function))

	def start(self):
		pass


bot_module.threading.Timer = FakeTimer


renamed, text = typed_torrent_rename("/cancel@OwnBot")
# Cancelled is told in a message with no buttons, and the screen the question
# took the place of comes back by itself
def cancelled(result):
	renamed, last = result
	notice = [markup for text, markup in TYPED_AFTER if text == bot_module.get_text("INPUT_CANCELLED")]
	return renamed, notice, "Some.Movie.2024.1080p" in last


check("'/cancel@Bot' cancels a typed rename", cancelled((renamed, text)), ([], [None], True))
# The prompt offers /cancel to tap: it cancels like the words always did
for answer in ("/cancel", "/CANCEL", "/cancel@OwnBot", "cancelar", "cancel"):
	check(f"{answer!r} cancels a question", cancelled(typed_torrent_rename(answer)), ([], [None], True))
TIMERS.clear()
typed_torrent_rename("cancelar")
check("the cancelled notice is deleted by itself after a while", [(seconds, function.__name__) for seconds, function in TIMERS],
	[(bot_module.NOTICE_SECONDS, "delete_message")])
check("the question offers /cancel to tap", "/cancel" in bot_module.get_text("INPUT_CANCEL_HINT"), True)
renamed, text = typed_torrent_rename("Some/Name")
check("a typed torrent name with a slash is refused", (renamed, "/" in text and "<code>" in text), ([], True))
bot_module.pending_inputs.clear()

# A global speed limit turned off and on again comes back, even when the
# manager stores no limit as 0 (qBittorrent) and the bot restarted meanwhile
class ForgetfulLimits:
	def __init__(self):
		self.limits = {"down": 512, "up": 0}

	def get_settings(self):
		settings = dict(_real_get_settings())
		for direction, kbps in self.limits.items():
			settings[f"speed_limit_{direction}"] = kbps
			settings[f"speed_limit_{direction}_enabled"] = kbps > 0
		return settings

	def set_speed_limit(self, direction, kbps, enabled):
		self.limits[direction] = int(kbps) if enabled and kbps else 0


forgetful = ForgetfulLimits()
_real_get_settings, _real_set_speed_limit = bot_module.client.get_settings, bot_module.client.set_speed_limit
bot_module.client.get_settings, bot_module.client.set_speed_limit = forgetful.get_settings, forgetful.set_speed_limit
bot_module.bot_settings.set("saved_speed_limits", {})
bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("toggleDownLimit"))
check("a global limit turned off is off", forgetful.limits["down"], 0)
bot_module.bot_settings._settings = None  # A restart: the settings are read back from the file
bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("toggleDownLimit"))
check("turned on again, after a restart, it is the limit it had", forgetful.limits["down"], 512)
bot_module.pending_inputs.clear()
bot_module.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, bot_module.build_call("toggleUpLimit"))
check("with no limit to bring back, one is asked", (forgetful.limits["up"], bot_module.pending_inputs.get((CHAT_ID, 1), {}).get("action")),
	(0, "upLimit"))
bot_module.pending_inputs.clear()
bot_module.client.get_settings, bot_module.client.set_speed_limit = _real_get_settings, _real_set_speed_limit
bot_module.bot_settings.set("saved_speed_limits", {})

# /turtle and /tortuga toggle turtle mode, whatever the language, and say what is on now
turtle = {"on": False}
_real_get_settings, _real_set_alt_speed = bot_module.client.get_settings, bot_module.client.set_alt_speed
bot_module.client.get_settings = lambda: dict(_real_get_settings(), alt_speed_enabled=turtle["on"])
bot_module.client.set_alt_speed = lambda enabled: turtle.update(on=enabled)
for command, expected in (("/tortuga", True), ("/turtle", False), ("/turtle@OwnBot", True)):
	EDITS.clear()
	send_command(command)
	text, markup = EDITS[-1]
	check(f"{command} turns turtle mode {'on' if expected else 'off'}", turtle["on"], expected)
	check(f"{command} says it is {'on' if expected else 'off'}",
		bot_module.get_text("TURTLE_ON" if expected else "TURTLE_OFF").split("<b>")[1].split("</b>")[0] in text, True)
	check(f"{command} offers the speed screen", bot_module.settings_call("speed") in callbacks_of(markup), True)
check("on, it shows the turtle speeds", "100 KB/s" in EDITS[-1][0] and "50 KB/s" in EDITS[-1][0], True)
turtle["on"] = False
send_command("/tortuga", STRANGER_ID)
check("an unauthorized /tortuga changes nothing", turtle["on"], False)
bot_module.client.supports_alt_speed = False
EDITS.clear()
send_command("/turtle")
check("without turtle mode in the manager, it says so", EDITS[-1][0], bot_module.get_text("TURTLE_NOT_SUPPORTED"))
check("without turtle mode in the manager, nothing changes", turtle["on"], False)
EDITS.clear()
send_command("/help")
check("/help leaves the turtle out when the manager has none", "HELP_TURTLE" not in EDITS[-1][0] and "/turtle" not in EDITS[-1][0], True)
bot_module.client.supports_alt_speed = True
EDITS.clear()
send_command("/help")
check("/help lists the turtle command", bot_module.get_text("HELP_TURTLE", bot_module.turtle_command()) in EDITS[-1][0], True)
bot_module.client.get_settings, bot_module.client.set_alt_speed = _real_get_settings, _real_set_alt_speed

# Environment values that used to crash the start, or break the lists
def config_with(**env):
	run = subprocess.run([sys.executable, "-c", "import config; print(config.TORRENTS_PER_PAGE, config.TORRENT_CLIENT_HOST, "
						"config.TORRENT_CLIENT_PROTOCOL, len(config.CONFIG_WARNINGS))"],
						cwd=REPO, env=dict(os.environ, **env), capture_output=True, text=True)
	return run.stdout.split()


check("an empty TORRENTS_PER_PAGE falls back to 10", config_with(TORRENTS_PER_PAGE="")[0], "10")
check("0 is raised to 1, with a warning", config_with(TORRENTS_PER_PAGE="0")[::3], ["1", "1"])
check("more than a keyboard holds is lowered to 90", config_with(TORRENTS_PER_PAGE="500")[0], "90")
check("not a number falls back to 10", config_with(TORRENTS_PER_PAGE="diez")[0], "10")
check("an empty host and protocol take their defaults", config_with(TORRENT_CLIENT_HOST="", TORRENT_CLIENT_PROTOCOL=" ")[1:3], ["localhost", "http"])
check("HTTPS in capitals is understood", config_with(TORRENT_CLIENT_PROTOCOL="HTTPS")[2], "https")

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({crawled} screens crawled)")
