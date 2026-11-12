"""Test battery for the automatic mover. Run with: python3 test_auto_mover.py

First what auto_mover.py decides on its own (which torrents, in what order,
how many, and where each one goes), then the bot around it against a fake
torrent client and a fake Telegram: the rule typed in the settings, the pass
of the monitor, the one asked by hand, and what is told when the manager
moves, refuses, or says nothing and leaves the torrents where they were.

The fake client behaves the ways the real ones were seen to (Transmission
4.1, qBittorrent 5.3 and Deluge 2.2): one moves at once, the others take the
order and tell the torrent as checking until it is in its new folder, and a
move that fails leaves the torrent in the source, in error or just as it was."""

import importlib.util
import os
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

import auto_mover
import torrent_clients
from torrent_clients import PermanentTorrentError, TorrentStatus
from torrent_clients.base import TorrentInfo

GB = auto_mover.GB
DAY = auto_mover.DAY
NOW = 1_800_000_000
CHAT_ID = 1
MESSAGE_ID = 100

FAILED = []
CHECKS = [0]


def check(description, actual, expected):
	CHECKS[0] += 1
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def torrent(torrent_id, days, size_gb, download_dir="/ssd/PELICULAS", now=NOW, **changes):
	values = dict(id=torrent_id, name=f"Movie.{torrent_id}", status=TorrentStatus.SEEDING, progress=100.0,
				total_size=size_gb * GB, download_dir=download_dir, completed_date=now - days * DAY)
	values.update(changes)
	return TorrentInfo(**values)


def ids(torrents):
	return [t.id for t in torrents]


def unit_ids(units):
	return [ids(unit.torrents) for unit in units]


# ---------------------------------------------------------------------------
# 1. THE FOLDERS
# ---------------------------------------------------------------------------

check("the source itself is under the source", auto_mover.is_under("/ssd", "/ssd"), True)
check("a trailing slash changes nothing", auto_mover.is_under("/ssd/PELICULAS/", "/ssd/"), True)
check("a folder that only starts the same is another one", auto_mover.is_under("/ssd2/PELICULAS", "/ssd"), False)
check("the parent is not under its child", auto_mover.is_under("/ssd", "/ssd/PELICULAS"), False)
# Download Station tells its folders without the leading slash
check("a folder told without its slash is the same folder", auto_mover.is_under("ssd/PELICULAS", "/ssd"), True)
check("whichever of the two lacks it", auto_mover.is_under("/ssd/PELICULAS", "ssd"), True)

check("the subfolder is kept under the target", auto_mover.target_dir("/ssd/PELICULAS", "/ssd", "/media"), "/media/PELICULAS")
check("and so is a deeper one", auto_mover.target_dir("/ssd/SERIES/Lost/S01/", "/ssd/", "/media/"), "/media/SERIES/Lost/S01")
check("a torrent right in the source goes right in the target", auto_mover.target_dir("/ssd", "/ssd", "/media"), "/media")
check("a rule on a subfolder moves only its inside", auto_mover.target_dir("/ssd/PELICULAS/4K", "/ssd/PELICULAS", "/media/PELICULAS"), "/media/PELICULAS/4K")
check("a target without slash stays without it", auto_mover.target_dir("ssd/PELICULAS", "ssd", "media"), "media/PELICULAS")
check("a folder out of the source goes nowhere", auto_mover.target_dir("/other/PELICULAS", "/ssd", "/media"), None)

check("two different folders are a rule", auto_mover.paths_error("/ssd", "/media"), None)
check("a rule still being typed is not an error", auto_mover.paths_error("/ssd", ""), None)
check("the same folder is not", auto_mover.paths_error("/ssd/", "ssd"), "same")
check("nor the target inside the source", auto_mover.paths_error("/ssd", "/ssd/old"), "nested")
check("nor the source inside the target", auto_mover.paths_error("/media/ssd", "/media"), "nested")

# ---------------------------------------------------------------------------
# 2. WHICH TORRENTS
# ---------------------------------------------------------------------------

pool = [
	torrent("new", 2, 10),
	torrent("old", 30, 10),
	torrent("oldest", 90, 10, download_dir="/ssd/SERIES/Lost"),
	torrent("elsewhere", 90, 10, download_dir="/media/PELICULAS"),
	torrent("lookalike", 90, 10, download_dir="/ssd2/PELICULAS"),
	torrent("downloading", 90, 10, progress=40.0, status=TorrentStatus.DOWNLOADING, completed_date=None),
	torrent("checking", 90, 10, status=TorrentStatus.CHECKING),
	torrent("broken", 90, 10, status=TorrentStatus.ERROR),
	torrent("managed", 90, 10, auto_managed=True),
	torrent("empty", 90, 0),
	torrent("paused", 60, 10, status=TorrentStatus.PAUSED),
	torrent("undated", 0, 10, completed_date=None, seeding_time=45 * DAY),
	torrent("unknown", 0, 10, completed_date=None, seeding_time=-1),
]
found, skipped = auto_mover.candidates(pool, "/ssd", 7 * DAY, NOW)
check("the finished ones in the source, oldest first", unit_ids(found), [["oldest"], ["paused"], ["undated"], ["old"]])
check("and why each of the others stays", dict(skipped), {"recent": 1, "unfinished": 2, "busy": 2, "managed": 1, "undated": 1})
check("no minimum age lets the recent one in", unit_ids(auto_mover.candidates(pool, "/ssd", 0, NOW)[0])[-1], ["new"])
check("a rule on a subfolder only sees its torrents", unit_ids(auto_mover.candidates(pool, "/ssd/SERIES", 7 * DAY, NOW)[0]), [["oldest"]])
check("a source without its slash finds them all the same", unit_ids(auto_mover.candidates(pool, "ssd/SERIES", 7 * DAY, NOW)[0]), [["oldest"]])

check("only as many as it takes", unit_ids(auto_mover.pick(found, 40 * GB, 55 * GB)), [["oldest"], ["paused"]])
check("one is enough when it covers it", unit_ids(auto_mover.pick(found, 40 * GB, 50 * GB)), [["oldest"]])
check("all of them when they do not add up", len(auto_mover.pick(found, 0, 500 * GB)), 4)
check("none when the space is already there", auto_mover.pick(found, 60 * GB, 55 * GB), [])

groups = auto_mover.group_by_target(found, "/ssd", "/media")
check("one order per target folder", [(target, ids(group)) for target, group in groups],
	[("/media/SERIES/Lost", ["oldest"]), ("/media/PELICULAS", ["paused", "undated", "old"])])

# Two torrents on the same data (cross seeding): the same name in the same folder
shared = [
	torrent("x1", 50, 20, name="Shared.Movie"), torrent("x2", 40, 20, name="Shared.Movie"),
	torrent("y1", 30, 20, name="Half.Recent"), torrent("y2", 1, 20, name="Half.Recent"),
	torrent("z", 45, 20, name="Shared.Movie", download_dir="/ssd/OTRA"),
	torrent("solo", 20, 20),
]
found, skipped = auto_mover.candidates(shared, "/ssd", 7 * DAY, NOW)
check("torrents sharing their data go as one", unit_ids(found), [["z"], ["x1", "x2"], ["solo"]])
check("aged by the most recent of them", [unit.age // DAY for unit in found], [45, 40, 20])
check("their data counted once", [unit.size // GB for unit in found], [20, 20, 20])
check("and all stay when one of them has to", dict(skipped), {"recent": 2})
check("picked whole", unit_ids(auto_mover.pick(found, 0, 30 * GB)), [["z"], ["x1", "x2"]])
check("and moved in the same order", auto_mover.group_by_target(found[1:2], "/ssd", "/media")[0][0:1] + (ids(auto_mover.group_by_target(found[1:2], "/ssd", "/media")[0][1]),),
	("/media/PELICULAS", ["x1", "x2"]))

# ---------------------------------------------------------------------------
# 3. THE BOT
# ---------------------------------------------------------------------------

CALLS = []  # (ids, target folder) of every move order
SENT = []  # Text of every message sent
EDITS = []  # (text, markup) of every message sent or edited
STATE = {}


def reset_state():
	STATE.update(free={"/ssd": 500 * GB, "/media": 4000 * GB}, torrents=[], move="instant", default_dir="/ssd", frees=True, refill=False)


reset_state()


class FakeClient:
	supports_alt_speed = True
	supports_rename = True
	supports_verify = True
	supports_categories = False
	supports_ratio_limit = True
	ratio_limit_copied_on_add = False
	supports_torrent_speed_limit = True
	supports_reannounce = True
	reports_last_activity = True

	def test_connection(self):
		return "Fake 1.0"

	def get_torrents(self, status=None, query=None):
		return list(STATE["torrents"])

	def get_torrent(self, torrent_id):
		return next((t for t in STATE["torrents"] if t.id == torrent_id), None)

	def get_free_space(self, path):
		for root, free in STATE["free"].items():
			if auto_mover.is_under(path, root):
				return free
		return -1

	def get_default_download_dir(self):
		return STATE["default_dir"]

	def get_download_dirs(self):
		return sorted({t.download_dir for t in STATE["torrents"]})

	def get_settings(self):
		return {"version": "Fake 1.0", "alt_speed_enabled": False, "alt_speed_down": 0, "alt_speed_up": 0,
				"speed_limit_down": 0, "speed_limit_down_enabled": False,
				"speed_limit_up": 0, "speed_limit_up_enabled": False, "download_dir": "/ssd"}

	def get_global_ratio_limit(self):
		return -1

	def move_torrents(self, torrent_ids, new_dir):
		"""STATE["move"] is how the manager takes it, or {text in the target folder: how}"""
		how = STATE["move"]
		if isinstance(how, dict):
			how = next((value for key, value in how.items() if key in new_dir), "instant")
		if how == "refuse":
			raise PermanentTorrentError("no such folder")
		CALLS.append((list(torrent_ids), new_dir))
		for t in STATE["torrents"]:
			if t.id not in torrent_ids:
				continue
			if how == "instant":  # Transmission: moved by the time it answers
				arrive(new_dir, t.id)
			elif how == "later":  # qBittorrent, Deluge: told as checking until it is there
				t.status = TorrentStatus.CHECKING
			elif how == "error":  # Transmission, Deluge: left in the source, in error
				t.status = TorrentStatus.ERROR
				t.error_message = "Couldn't move: Permission denied (13)"
			# "silent": qBittorrent, Deluge: left in the source as it was, nothing said


def fake_resume(self, torrent_ids):
	"""Starting a torrent a failed move left in error clears it, as in Transmission"""
	RESUMED.extend(torrent_ids)
	for t in STATE["torrents"]:
		if t.id in torrent_ids and t.status in (TorrentStatus.ERROR, TorrentStatus.PAUSED):
			t.status, t.error_message = TorrentStatus.SEEDING, ""


def fake_pause(self, torrent_ids):
	for t in STATE["torrents"]:
		if t.id in torrent_ids:
			t.status = TorrentStatus.PAUSED


RESUMED = []  # Ids of the torrents started again
FakeClient.resume_torrents = fake_resume
FakeClient.pause_torrents = fake_pause


def arrive(download_dir, *torrent_ids):
	"""The manager finished moving these torrents"""
	for t in list(STATE["torrents"]):
		if t.id in torrent_ids:
			if STATE["frees"] and auto_mover.is_under(t.download_dir, "/ssd"):
				root = next(key for key in STATE["free"] if key.strip("/") == "ssd")
				STATE["free"][root] += t.total_size
				if STATE["refill"]:  # A download takes the room as soon as it is free
					STATE["free"][root] -= t.total_size
					STATE["torrents"].append(torrent(f"new-{t.id}", 0, t.total_size // GB, status=TorrentStatus.DOWNLOADING))
			t.download_dir = download_dir
			t.status = TorrentStatus.SEEDING


torrent_clients.create_client = lambda config_module: FakeClient()

_spec = importlib.util.spec_from_file_location("tcbot", os.path.join(REPO, "torrent-controller-bot.py"))
bot = importlib.util.module_from_spec(_spec)
sys.modules["tcbot"] = bot
_spec.loader.exec_module(bot)


def fake_send_message(chat_id, text, **kwargs):
	check("a message fits in what Telegram takes", len(text) <= 4096, True)
	SENT.append(text)
	EDITS.append((text, kwargs.get("reply_markup")))
	return type("FakeMessage", (), {"message_id": MESSAGE_ID + len(SENT)})()


def fake_edit_message_text(text, chat_id, message_id, **kwargs):
	check("an edited message fits in what Telegram takes", len(text) <= 4096, True)
	EDITS.append((text, kwargs.get("reply_markup")))
	return True


bot.bot.send_message = fake_send_message
bot.bot.edit_message_text = fake_edit_message_text
DELETED = []  # Ids of the messages the bot deleted
bot.bot.delete_message = lambda chat_id, message_id, *a, **k: DELETED.append(message_id) or True
bot.AUTO_MOVER_WATCH_SECONDS = 0.005
bot.AUTO_MOVER_GRACE_SECONDS = 0.15
bot.AUTO_MOVER_SETTLE_SECONDS = 0
settings = bot.bot_settings
T = bot.get_text


def callbacks_of(markup):
	return [button.callback_data for row in markup.keyboard for button in row] if markup is not None else []


def labels_of(markup):
	return [button.text for row in markup.keyboard for button in row]


def press(call):
	before = len(EDITS)
	bot.dispatch_callback(CHAT_ID, MESSAGE_ID, 1, call)
	return EDITS[-1] if len(EDITS) > before else None


def typed(field, answer):
	"""Presses the button of a field and answers the question it asks"""
	press(bot.build_call("moverType", field))
	pending = bot.pop_pending_input(CHAT_ID, 1, any_thread=True)
	message = type("FakeMessage", (), {
		"chat": type("Chat", (), {"id": CHAT_ID, "type": "private"})(), "message_id": 7, "text": answer,
		"from_user": type("User", (), {"id": 1})(), "message_thread_id": None, "is_topic_message": False})()
	bot.handle_pending_input(message, pending)
	# What it answers, which comes on top of the screen of the rule
	return EDITS[-1][0].split("\n\n")[0]


def job_over(seconds=5):
	"""Waits for the move going on to be over"""
	deadline = time.time() + seconds
	while bot._auto_mover_job[0] and time.time() < deadline:
		time.sleep(0.005)
	return bot._auto_mover_job[0] is None


def tick(wait=True):
	"""A pass of the monitor: the move orders it gave"""
	CALLS.clear()
	bot.auto_mover_tick()
	if wait:
		job_over()
	return list(CALLS)


def fresh(free_gb=35, **state):
	"""Five torrents of 30 GB in the source, a to d from older to newer and one too recent"""
	job_over()
	reset_state()
	STATE.update(state)
	STATE["free"]["/ssd"] = free_gb * GB
	now = time.time()
	STATE["torrents"] = [
		torrent("a", 40, 30, now=now),
		torrent("b", 30, 30, download_dir="/ssd/SERIES/Lost", now=now),
		torrent("c", 20, 30, now=now),
		torrent("d", 10, 30, now=now),
		torrent("recent", 1, 30, now=now),
	]
	bot._auto_mover_told[0] = None
	SENT.clear()
	EDITS.clear()
	CALLS.clear()


def where(*torrent_ids):
	return [next(t.download_dir for t in STATE["torrents"] if t.id == torrent_id) for torrent_id in torrent_ids]


# --- The settings screen -----------------------------------------------------

check("the main screen has a row for it, off", T("SETTINGS_ROW_MOVER", "❌") in labels_of(bot.build_settings("main")[1]), True)

text, markup = bot.build_settings("mover")
calls = callbacks_of(markup)
check("without folders there is no switch", bot.build_call("toggleSetting", "auto_mover") in calls, False)
check("nor a preview", bot.build_call("moverPreview") in calls, False)
check("and it says what is missing", T("MOVER_NEEDS_DIRS") in text, True)
settings.set("auto_mover", True)
check("a switch left on does nothing without folders", bot.auto_mover_enabled(), False)
fresh(1)
check("so the monitor moves nothing", tick(), [])
settings.set("auto_mover", False)

# The folders are picked with a button: the ones above those in use, each
# with what it holds, so that nobody picks a subfolder and leaves the rest out
check("the folders above the ones in use", auto_mover.root_folders(["/ssd/PELICULAS", "/ssd/SERIES/Lost", "/downloads/complete"]),
	["/ssd", "/downloads"])
check("a folder at the top stands for itself", auto_mover.root_folders(["/downloads", "/ssd/PELICULAS", "/ssd"]), ["/downloads", "/ssd"])
check("without the leading slash where the manager has none", auto_mover.root_folders(["ssd/PELICULAS", "video"]), ["ssd", "video"])
check("deep layouts keep what they share", auto_mover.root_folders(["/mnt/user/ssd/A", "/mnt/user/ssd/B/C", "/mnt/user/media/A"]),
	["/mnt/user/ssd", "/mnt/user/media"])
check("and nothing out of nothing", auto_mover.root_folders(["", "/"]), [])
question, markup = press(bot.build_call("moverAsk", "source"))
calls = [button.callback_data for row in markup.keyboard for button in row]
check("the source is picked among the folders above", ("<code>/ssd</code>" in question, "SERIES/Lost" in question, bot.pop_pending_input(CHAT_ID, 1, any_thread=True)),
	(True, True, None))
check("with a button to type another one", bot.build_call("moverType", "source") in calls, True)
press(next(call for call in calls if call.startswith("moverSet") and bot.get_dir_by_id(call.split("|")[2]) == "/ssd"))
check("a pressed folder is the source", settings.get("auto_mover_source"), "/ssd")
check("the target is not offered the source", bot.mover_folder_choices("target"), [])
press(bot.build_call("moverAsk", "target"))
check("so, with nothing to offer, it is typed", (bot.pop_pending_input(CHAT_ID, 1, any_thread=True) or {}).get("field"), "target")
settings.set("auto_mover_source", "")

check("a source is stored without its last slash", (typed("source", "/ssd/"), settings.get("auto_mover_source"))[1], "/ssd")
check("a folder typed without its first slash gets it", (typed("source", "ssd"), settings.get("auto_mover_source"))[1], "/ssd")
check("the target cannot be inside the source", typed("target", "/ssd/old"), T("MOVER_ERROR_NESTED"))
check("nor the source itself", typed("target", "/ssd"), T("MOVER_ERROR_NESTED"))
check("a target apart is stored", (typed("target", "/media"), settings.get("auto_mover_target"))[1], "/media")
check("the source cannot then move inside the target", typed("source", "/media/ssd"), T("MOVER_ERROR_NESTED"))
check("which leaves the source as it was", settings.get("auto_mover_source"), "/ssd")

# Download Station writes its folders without the leading slash
STATE["default_dir"] = "downloads"
check("where the manager writes no slash, neither does the rule", (bot.mover_path("/ssd/"), bot.mover_path("ssd/PELICULAS")), ("ssd", "ssd/PELICULAS"))
STATE["default_dir"] = "/ssd"

check("a margin has to be a number", typed("min", "mucho"), T("MOVER_ERROR_NUMBER"))
check("and not negative", typed("days", "-1"), T("MOVER_ERROR_DAYS"))
check("nor zero, with which nothing would ever be moved", typed("min", "0"), T("MOVER_ERROR_ZERO"))
# The start stays under the stop whatever is typed first: the other follows, and it is told
check("a minimum over the wanted raises the wanted", (typed("min", "150"), settings.get("auto_mover_wanted_free_gb")), (T("MOVER_ADJUSTED_WANTED", bot.sizeof_fmt(300 * GB)), 300))
check("a wanted under the minimum lowers the minimum", (typed("wanted", "50"), settings.get("auto_mover_min_free_gb")), (T("MOVER_ADJUSTED_MIN", bot.sizeof_fmt(25 * GB)), 25))
check("a small disk is set in any order", (typed("wanted", "0,7"), settings.get("auto_mover_min_free_gb"), typed("min", "0.5"), settings.get("auto_mover_wanted_free_gb")),
	(T("MOVER_ADJUSTED_MIN", bot.sizeof_fmt(0.35 * GB)), 0.35, T("SETTINGS_UPDATED"), 0.7))
# The unit that comes along counts, and one that is not known is not guessed
check("terabytes are told from gigabytes", (typed("wanted", "1 TB"), settings.get("auto_mover_wanted_free_gb")), (T("SETTINGS_UPDATED"), 1024))
check("and so are megabytes", (typed("min", "512 mb"), settings.get("auto_mover_min_free_gb")), (T("SETTINGS_UPDATED"), 0.5))
check("a unit that is not a size is refused", (typed("min", "5 semanas"), settings.get("auto_mover_min_free_gb")), (T("MOVER_ERROR_NUMBER"), 0.5))
check("as are weeks for the days", typed("days", "2 semanas"), T("MOVER_ERROR_DAYS"))
check("days may say they are days", (typed("days", "3 días"), settings.get("auto_mover_min_days")), (T("SETTINGS_UPDATED"), 3))
settings.set("auto_mover_min_days", 7)
settings.set("auto_mover_wanted_free_gb", 150)
settings.set("auto_mover_min_free_gb", 50)
check("the unit may come along", (typed("min", "40 GB"), settings.get("auto_mover_min_free_gb"))[1], 40)
check("and so may a comma", (typed("min", "40,5"), settings.get("auto_mover_min_free_gb"))[1], 40.5)
typed("min", "40")
typed("wanted", "100gb")
typed("days", "7")
check("the margins are stored", [settings.get(f"auto_mover_{key}") for key in ("min_free_gb", "wanted_free_gb", "min_days")], [40, 100, 7])
check("a typed field goes back to the screen of the rule", T("SETTINGS_MOVER_TITLE") in EDITS[-1][0], True)

fresh(500)
text, markup = bot.build_settings("mover")
calls = callbacks_of(markup)
check("with both folders the switch shows up", bot.build_call("toggleSetting", "auto_mover") in calls, True)
check("and the preview", bot.build_call("moverPreview") in calls, True)
check("each folder with its free space", (f"<code>/ssd</code> · {T('MOVER_FREE', bot.sizeof_fmt(500 * GB))}" in text,
	f"<code>/media</code> · {T('MOVER_FREE', bot.sizeof_fmt(4000 * GB))}" in text), (True, True))
check("off, it says nothing moves by itself", T("MOVER_STATUS_OFF") in text, True)
check("every button fits in what Telegram takes", [c for c in calls if len(c.encode()) > 64], [])
typed("days", "1")
check("one day is not days", T("MOVER_DAYS", T("MOVER_DAYS_ONE")) in bot.build_settings("mover")[0], True)
typed("days", "7")

# A manager that does not tell the free space of the source (qBittorrent out of its default folder)
STATE["free"] = {"/downloads": 100 * GB}
text, markup = bot.build_settings("mover")
check("without the free space of the source it says why it cannot work", T("MOVER_FREE_UNKNOWN") in text, True)
check("which for most managers is a folder that is not there", T("MOVER_FREE_UNKNOWN_MISSING", "/ssd") in text, True)
bot.TORRENT_CLIENT = "qbittorrent"
check("and for qBittorrent, that it only knows its own", T("MOVER_FREE_UNKNOWN_HINT", "/ssd") in bot.build_settings("mover")[0], True)
bot.TORRENT_CLIENT = "transmission"
check("with no switch to turn on what cannot work", bot.build_call("toggleSetting", "auto_mover") in callbacks_of(markup), False)
settings.set("auto_mover", True)
check("but one to turn it off when it was on", bot.build_call("toggleSetting", "auto_mover") in callbacks_of(bot.build_settings("mover")[1]), True)
check("the monitor moves nothing without knowing the space", tick(), [])
settings.set("auto_mover", False)

fresh(500)
screen = press(bot.build_call("toggleSetting", "auto_mover"))
check("the switch turns it on", bot.auto_mover_enabled(), True)
check("and the screen says there is nothing to do yet", T("MOVER_STATUS_OK") in screen[0], True)
check("the main row tells it is on", T("SETTINGS_ROW_MOVER", "✅") in labels_of(bot.build_settings("main")[1]), True)
check("and so do the statistics, as a yes or no", bot.collect_telemetry_metrics()["auto_mover"], True)
STATE["free"]["/ssd"] = 10 * GB
check("short of space, the screen warns that it is about to move", T("MOVER_STATUS_DUE") in bot.build_settings("mover")[0], True)

# --- The pass of the monitor -------------------------------------------------

fresh(40)
check("at the minimum nothing is moved yet", tick(), [])

fresh(35)
moves = tick()
# 65 GB are missing: a (30), b (30) and c (30), oldest first, each to its folder
check("under the minimum, the oldest ones go, each to its folder", moves, [(["a", "c"], "/media/PELICULAS"), (["b"], "/media/SERIES/Lost")])
check("and are where they were sent", where("a", "b", "c", "d"), ["/media/PELICULAS", "/media/SERIES/Lost", "/media/PELICULAS", "/ssd/PELICULAS"])
check("one notice is sent", len(SENT), 1)
check("which tells what and where", T("MOVER_MOVING", T("MOVER_TORRENTS", 3), bot.sizeof_fmt(90 * GB), "/ssd", "/media", bot.sizeof_fmt(35 * GB), bot.sizeof_fmt(125 * GB)) in SENT[0], True)
check("names the torrents", all(f"Movie.{name}" in SENT[0] for name in "abc"), True)
check("under the folder each one goes to", SENT[0].index("/media/PELICULAS</code>") < SENT[0].index("Movie.a") < SENT[0].index("/media/SERIES/Lost</code>") < SENT[0].index("Movie.b"), True)
check("and is updated once they are moved", T("MOVER_DONE", T("MOVER_TORRENTS", 3), bot.sizeof_fmt(90 * GB), "/ssd", "/media") in EDITS[-1][0], True)
check("the mover stays on", settings.get("auto_mover"), True)
check("with space again, the next pass does nothing", (tick(), len(SENT)), ([], 1))

STATE["free"]["/ssd"] = 10 * GB
check("the next time the one left goes, but never the recent one", tick(), [(["d"], "/media/PELICULAS")])
check("one torrent is not torrents", T("MOVER_DONE", T("MOVER_TORRENTS_ONE"), bot.sizeof_fmt(30 * GB), "/ssd", "/media") in EDITS[-1][0], True)

# With space needed and nothing to move, the monitor says so once
STATE["free"]["/ssd"] = 10 * GB
SENT.clear()
check("with nothing left to move nothing is ordered", tick(), [])
check("and that is told", len(SENT) == 1 and T("MOVER_NONE", bot.sizeof_fmt(10 * GB), "/ssd") in SENT[0], True)
check("with why: the only one left is too recent", T("MOVER_SKIP_RECENT", T("MOVER_TORRENTS_ONE"), T("MOVER_DAYS_MANY", "7")) in SENT[0], True)
check("but only once", (tick(), tick(), len(SENT)), ([], [], 1))
STATE["free"]["/ssd"] = 200 * GB
tick()
STATE["free"]["/ssd"] = 10 * GB
tick()
check("and again if it comes back after being fine", len(SENT), 2)

# A manager that moves later: nothing is told as moved until it is there
fresh(35, move="later")
moves = tick(wait=False)
check("the order is given", moves, [(["a", "c"], "/media/PELICULAS"), (["b"], "/media/SERIES/Lost")])
time.sleep(0.3)  # Well past the grace: a torrent being moved is not taken as failed
check("while the manager moves, the move is not over", bot._auto_mover_job[0], {"count": 3})
check("nor told as done", T("MOVER_DONE", T("MOVER_TORRENTS", 3), bot.sizeof_fmt(90 * GB), "/ssd", "/media") in EDITS[-1][0], False)
check("another pass orders nothing", tick(wait=False), [])
check("a pass asked by hand says how many are on their way", bot.mover_outcome_text(bot.auto_mover_plan(force=True)), T("MOVER_BUSY", T("MOVER_TORRENTS", 3)))
check("and so does the screen of the rule", T("MOVER_BUSY", T("MOVER_TORRENTS", 3)) in bot.build_settings("mover")[0], True)
arrive("/media/PELICULAS", "a", "c")
time.sleep(0.05)
check("not until the last one is there", bot._auto_mover_job[0], {"count": 3})
arrive("/media/SERIES/Lost", "b")
check("then it is over", job_over(), True)
check("and told", T("MOVER_DONE", T("MOVER_TORRENTS", 3), bot.sizeof_fmt(90 * GB), "/ssd", "/media") in EDITS[-1][0], True)

# The manager is moving or checking torrents of the source on its own: a
# move by hand, or the one a restart of the bot lost track of
fresh(35)
STATE["torrents"][0].status = TorrentStatus.CHECKING
check("torrents of the source being moved hold the pass", (tick(), bot.auto_mover_plan().outcome), ([], "wait"))
check("and the pass by hand says so", bot.mover_outcome_text(bot.auto_mover_plan(force=True)), T("MOVER_WAIT", "/ssd"))
STATE["torrents"][0].status = TorrentStatus.SEEDING
STATE["torrents"].append(torrent("far", 5, 30, download_dir="/media/X", status=TorrentStatus.CHECKING))
check("one being checked elsewhere does not", bot.auto_mover_plan().outcome, "move")

# A folder the manager refuses: the rest is moved, the failure is told, the mover turned off
fresh(35, move={"SERIES": "refuse"})
moves = tick()
check("a refused folder does not stop the others", moves, [(["a", "c"], "/media/PELICULAS")])
final = EDITS[-1][0]
check("the notice tells what was moved", T("MOVER_DONE", T("MOVER_TORRENTS", 2), bot.sizeof_fmt(60 * GB), "/ssd", "/media") in final, True)
check("what was not, and why", T("MOVER_FAILED", T("MOVER_TORRENTS_ONE")) in final and "<i>no such folder</i>" in final, True)
check("and that the mover is now off", T("MOVER_TURNED_OFF") in final and settings.get("auto_mover"), False)
STATE["free"]["/ssd"] = 35 * GB
check("so the same failure is not run into every five minutes", tick(), [])
settings.set("auto_mover", True)

# A manager that takes the order and leaves the torrent where it was, in error
fresh(35, move="error")
tick()
final = EDITS[-1][0]
check("a torrent left in error is told as not moved", T("MOVER_FAILED", T("MOVER_TORRENTS", 3)) in final, True)
check("with what the manager says, once for all of them", final.count("Permission denied"), 1)
check("nothing is told as moved", "✅" in final, False)
check("and the mover is turned off", settings.get("auto_mover"), False)
# Bad news is a message of its own (nobody is told of an edit), and what
# the failed move left stopped in error is started again
check("the failure is a new message and the notice goes", (len(SENT), SENT[-1] == final, MESSAGE_ID + 1 in DELETED), (2, True, True))
check("what was left in error seeds again", (sorted(RESUMED), [t.status for t in STATE["torrents"] if t.id in "abc"]), (["a", "b", "c"], [TorrentStatus.SEEDING] * 3))
settings.set("auto_mover", True)

# A manager that takes the order and leaves the torrent where it was, saying nothing
fresh(35, move="silent")
tick()
final = EDITS[-1][0]
check("a torrent left as it was is told as not moved", T("MOVER_FAILED", T("MOVER_TORRENTS", 3)) in final, True)
check("with the likely reasons, since the manager gives none", T("MOVER_REASON_SILENT") in final, True)
check("and the mover is turned off", settings.get("auto_mover"), False)
check("where they stayed", where("a", "b", "c"), ["/ssd/PELICULAS", "/ssd/SERIES/Lost", "/ssd/PELICULAS"])
settings.set("auto_mover", True)

# A manager that never finishes
fresh(35, move="later")
bot.AUTO_MOVER_TIMEOUT = 0.1
tick()
check("a move that never ends is given up and told", T("MOVER_REASON_TIMEOUT") in EDITS[-1][0], True)
bot.AUTO_MOVER_TIMEOUT = config.AUTO_MOVER_TIMEOUT
settings.set("auto_mover", True)

# A torrent removed while it was being moved
fresh(35, move="later")
tick(wait=False)
STATE["torrents"] = [t for t in STATE["torrents"] if t.id not in ("a", "b", "c")]
check("torrents removed meanwhile end the move", job_over(), True)
check("which says so instead of telling them moved or failed", T("MOVER_GONE") in EDITS[-1][0], True)
check("and is no reason to turn the mover off", settings.get("auto_mover"), True)

# A move that frees nothing: the same disk, or files linked from somewhere else
fresh(35, frees=False)
tick()
check("a move that freed nothing is told", T("MOVER_NO_GAIN", bot.sizeof_fmt(35 * GB), bot.sizeof_fmt(35 * GB)) in SENT[-1], True)
check("and turns the mover off, or it would move everything", settings.get("auto_mover"), False)
settings.set("auto_mover", True)

# Downloads that fill the room the move freed: the free space is the same as
# before, but the move did free it
fresh(35, refill=True)
SENT.clear()
tick()
check("room freed and taken again by new downloads is no alarm", (settings.get("auto_mover"), [text for text in SENT if "⚠️" in text]), (True, []))

# Once told as moved, the wait for the free space is not told as a move
fresh(35)
bot.AUTO_MOVER_SETTLE_SECONDS = 0.5
tick(wait=False)
deadline = time.time() + 2
while not (bot._auto_mover_job[0] or {}).get("settling") and time.time() < deadline:
	time.sleep(0.005)
check("after the move, the wait for the free space says what it is", (bot.mover_outcome_text(bot.auto_mover_plan(force=True)),
	T("MOVER_SETTLING") in bot.build_settings("mover")[0]), (T("MOVER_SETTLING"), True))
bot.AUTO_MOVER_SETTLE_SECONDS = 0
job_over()

# A target at the top that is not there (a path typed wrong) is seen too
fresh(35)
settings.set("auto_mover_target", "/nope")
STATE["free"] = {"/ssd": 35 * GB, "/nope": -1, "/": 900 * GB}
check("a missing folder at the top is told on the screen and in the preview",
	(T("MOVER_TARGET_MISSING", "/nope") in bot.build_settings("mover")[0], T("MOVER_TARGET_MISSING", "/nope") in press(bot.build_call("moverPreview"))[0]), (True, True))
settings.set("auto_mover_target", "/media")

# A tracker that does not answer is no reason to leave a torrent behind
fresh(35)
STATE["torrents"][0].status, STATE["torrents"][0].error_is_tracker = TorrentStatus.ERROR, True
check("a torrent whose only error is the tracker's is moved", "a" in [i for moved_ids, _dir in tick() for i in moved_ids], True)
fresh(35)
STATE["torrents"][0].status = TorrentStatus.ERROR
check("one with an error of its data is not", "a" in [i for moved_ids, _dir in tick() for i in moved_ids], False)

# A source the manager stops telling the space of: told, but not at the first pass
fresh(35)
STATE["free"] = {"/media": 4000 * GB}
SENT.clear()
passes = [(tick(), len(SENT)) for _ in range(4)]
check("an unknown free space is told once, after a few passes", passes, [([], 0), ([], 0), ([], 1), ([], 1)])
check("saying the folder is not seen", T("MOVER_FREE_UNKNOWN_MISSING", "/ssd") in SENT[-1], True)

# A stop that cannot be reached is warned of on the screen
fresh(35)
settings.set("auto_mover_wanted_free_gb", 500)
check("a stop over what the source could ever have free is warned of", T("MOVER_WANTED_TOO_HIGH", bot.sizeof_fmt(500 * GB), bot.sizeof_fmt(185 * GB)) in bot.build_settings("mover")[0], True)
settings.set("auto_mover_wanted_free_gb", 100)
check("not one that can", "«Parar al llegar a» pide" in bot.build_settings("mover")[0], False)

# The notice of a move the bot stopped following is not left saying it will change
settings.set("auto_mover_notice", [CHAT_ID, 555, None])
SENT.clear(); DELETED.clear()
bot.mover_close_lost_notice()
check("a notice left behind by a restart is replaced", (555 in DELETED, T("MOVER_LOST") in SENT[-1], settings.get("auto_mover_notice")), (True, True, None))
bot.mover_close_lost_notice()
check("only once", len(SENT), 1)

# The target tells the same free space as the source: the same disk
fresh(35)
STATE["free"]["/media"] = 35 * GB
check("a target on the disk of the source moves nothing", tick(), [])
check("and is told once", (len(SENT), T("MOVER_SAME_DISK") in SENT[0], tick(), len(SENT)), (1, True, [], 1))
check("the screen says it too", T("MOVER_SAME_DISK") in bot.build_settings("mover")[0], True)

# What would be moved does not fit in the target
fresh(35)
STATE["free"]["/media"] = 50 * GB
check("a target without room moves nothing", tick(), [])
check("and is told once", (len(SENT), T("MOVER_TARGET_FULL", "/media", bot.sizeof_fmt(50 * GB)) in SENT[0], tick(), len(SENT)), (1, True, [], 1))

# A target that does not exist yet: its space is the one of the folder it will be made in
fresh(35)
settings.set("auto_mover_target", "/media/NUEVA/SUB")
STATE["free"] = {"/ssd": 35 * GB, "/media/NUEVA": -1, "/media": 50 * GB}
check("the room of a target still to be made is the one of the folder above", (bot.auto_mover_plan().outcome, bot.auto_mover_plan().target_free), ("target_full", 50 * GB))
text = bot.build_settings("mover")[0]
check("and the screen says the folder is not there yet, without a room that is another folder's", (T("MOVER_TARGET_MISSING", "/media/NUEVA/SUB") in text, T("MOVER_FREE", bot.sizeof_fmt(50 * GB)) in text), (True, False))
settings.set("auto_mover_target", "/media")
check("which it does not say of a folder that is", T("MOVER_TARGET_MISSING", "/media") in bot.build_settings("mover")[0], False)

# A manager that does not tell the free space of the target (qBittorrent)
fresh(35)
del STATE["free"]["/media"]
check("an unknown target space does not stop the move", len(tick()), 2)

# Torrents sharing their data go in one order
fresh(35)
now = time.time()
STATE["torrents"] = [torrent("x1", 50, 30, name="Shared", now=now), torrent("x2", 45, 30, name="Shared", now=now), torrent("solo", 40, 80, now=now)]
check("torrents sharing their data go together, counted once", tick(), [(["x1", "x2", "solo"], "/media/PELICULAS")])
check("which the notice counts as what it takes on disk", T("MOVER_DONE", T("MOVER_TORRENTS", 3), bot.sizeof_fmt(110 * GB), "/ssd", "/media") in EDITS[-1][0], True)
check("and names once", (EDITS[-1][0].count("Shared"), f"• Shared ({bot.sizeof_fmt(30 * GB)}) ×2" in EDITS[-1][0]), (1, True))

# Download Station: folders without the leading slash
fresh(35, default_dir="ssd")
for t in STATE["torrents"]:
	t.download_dir = t.download_dir.lstrip("/")
settings.set("auto_mover_source", "ssd")
settings.set("auto_mover_target", "media")
STATE["free"] = {"ssd": 35 * GB, "media": 4000 * GB}
check("folders without slash are moved the same", tick(), [(["a", "c"], "media/PELICULAS"), (["b"], "media/SERIES/Lost")])
check("and told as moved", T("MOVER_DONE", T("MOVER_TORRENTS", 3), bot.sizeof_fmt(90 * GB), "ssd", "media") in EDITS[-1][0], True)
settings.set("auto_mover_source", "/ssd")
settings.set("auto_mover_target", "/media")

# Turned off, the monitor does nothing
fresh(1)
settings.set("auto_mover", False)
check("turned off, nothing is moved", tick(), [])

# --- By hand -------------------------------------------------------------------

fresh(60)
text, markup = press(bot.build_call("moverPreview"))
check("the preview tells what would go and the space it would leave", T("MOVER_WOULD_MOVE", bot.sizeof_fmt(60 * GB), "/ssd", T("MOVER_TORRENTS", 2), bot.sizeof_fmt(60 * GB), bot.sizeof_fmt(120 * GB)) in text, True)
check("which gets to the wanted space, so nothing else is said", "⚠️" in text, False)
# Moving everything there is does not always get there, and it is told
wanted_before = settings.get("auto_mover_wanted_free_gb")
settings.set("auto_mover_wanted_free_gb", 500)
short = press(bot.build_call("moverPreview"))[0]
check("when all that can be moved is not enough, it says so and why", (T("MOVER_SHORT_REST", bot.sizeof_fmt(500 * GB)) in short, T("MOVER_SKIP_RECENT", bot.mover_count(1), bot.mover_days()) in short), (True, True))
STATE["torrents"] = [t for t in STATE["torrents"] if t.id != "recent"]
short = press(bot.build_call("moverPreview"))[0]
check("and that it is all there is when nothing is held back", T("MOVER_SHORT_ALL", bot.sizeof_fmt(500 * GB), "/ssd") in short, True)
settings.set("auto_mover_wanted_free_gb", wanted_before)
fresh(60)
text, markup = press(bot.build_call("moverPreview"))
check("each folder with where it is and where it goes", "📁 <code>/ssd/SERIES/Lost</code> → <code>/media/SERIES/Lost</code>" in text, True)
check("and that by itself it would still wait", T("MOVER_NOT_YET", bot.sizeof_fmt(40 * GB)) in text, True)
check("with the button to move now", bot.build_call("moverRun") in callbacks_of(markup), True)
check("looking moves nothing", CALLS, [])

STATE["free"]["/ssd"] = 30 * GB
text, markup = press(bot.build_call("moverPreview"))
check("under the minimum there is no such note", T("MOVER_NOT_YET", bot.sizeof_fmt(40 * GB)) in text, False)

STATE["free"]["/ssd"] = 100 * GB
text, markup = press(bot.build_call("moverPreview"))
check("with the wanted space there is nothing to move", T("MOVER_ENOUGH", bot.sizeof_fmt(100 * GB), "/ssd", bot.sizeof_fmt(100 * GB)) in text, True)
check("so no button to move", bot.build_call("moverRun") in callbacks_of(markup), False)

STATE["free"]["/media"] = 10 * GB
STATE["free"]["/ssd"] = 60 * GB
text, markup = press(bot.build_call("moverPreview"))
check("a target without room is told in the preview", T("MOVER_TARGET_FULL", "/media", bot.sizeof_fmt(10 * GB)) in text, True)
check("with no button to move", bot.build_call("moverRun") in callbacks_of(markup), False)
STATE["free"]["/media"] = 4000 * GB

STATE["move"] = "later"
DELETED.clear()
before = len(EDITS)
screen = press(bot.build_call("moverRun"))
check("moving now does not wait for the minimum", CALLS, [(["a"], "/media/PELICULAS"), (["b"], "/media/SERIES/Lost")])
check("and works with the switch off", settings.get("auto_mover"), False)
# The notice of the move is all there is to see: the menu the button was on goes
check("the menu goes away, the notice of the move stays", (MESSAGE_ID in DELETED, [T("MOVER_MOVING_WAIT") in text for text, _markup in EDITS[before:]]), (True, [True]))
screen = press(bot.build_call("moverRun"))
check("pressed again while moving, it says so", screen[0], T("MOVER_BUSY", T("MOVER_TORRENTS", 2)))
arrive("/media/PELICULAS", "a")
arrive("/media/SERIES/Lost", "b")
job_over()

fresh(60)
STATE["torrents"] = [t for t in STATE["torrents"] if t.id == "recent"] + [torrent("tmm", 90, 30, auto_managed=True, now=time.time())]
text, markup = press(bot.build_call("moverPreview"))
check("with nothing that can go it tells why", (T("MOVER_NONE", bot.sizeof_fmt(60 * GB), "/ssd") in text,
	T("MOVER_SKIP_RECENT", T("MOVER_TORRENTS_ONE"), T("MOVER_DAYS_MANY", "7")) in text, T("MOVER_SKIP_MANAGED", T("MOVER_TORRENTS_ONE")) in text), (True, True, True))
STATE["torrents"] = []
check("or that the folder is empty", T("MOVER_NONE_EMPTY") in press(bot.build_call("moverPreview"))[0], True)

# A long list is cut, and still fits in a message
fresh(0)
now = time.time()
STATE["torrents"] = [torrent(f"t{n:03d}", 100 + n, 1, now=now, name="Some.Very.Long.Release.Name." * 8 + str(n),
	download_dir=f"/ssd/SERIES/A.Rather.Long.Series.Folder.Name.{n}") for n in range(60)]
text, markup = press(bot.build_call("moverPreview"))
check("a long list is cut", T("MOVER_AND_MORE", 60 - bot.AUTO_MOVER_MAX_LISTED) in text, True)
STATE["move"] = "silent"
press(bot.build_call("moverRun"))
check("and so is the one of a move that failed whole", (job_over(), T("MOVER_FAILED", T("MOVER_TORRENTS", 60)) in EDITS[-1][0]), (True, True))

# --- The settings file ---------------------------------------------------------

settings.set("auto_mover_min_free_gb", "nonsense")
check("a margin that is not a number falls back to the default", bot.auto_mover_rule()["min_free"], settings.DEFAULTS["auto_mover_min_free_gb"] * GB)
settings.set("auto_mover_min_free_gb", 40)

# Torrents come by age, which mixes the folders: each folder is named once
mixed = [torrent("m1", 9, 1), torrent("m2", 8, 1, download_dir="/ssd/SERIES/Lost"), torrent("m3", 7, 1), torrent("m4", 6, 1, download_dir="/ssd/SERIES/Lost")]
listing = bot.mover_listing(mixed, {"source": "/ssd", "target": "/media"}).split("\n")
check("the torrents of a folder go together under it, each folder once",
	[line[2:9] if line.startswith("•") else line.split("</code>")[0][-10:] for line in listing],
	["/PELICULAS"[-10:], "Movie.m", "Movie.m", "ERIES/Lost", "Movie.m", "Movie.m"])
check("in the order of the oldest of each", ["Movie.m1" in listing[1], "Movie.m3" in listing[2], "Movie.m2" in listing[4], "Movie.m4" in listing[5]], [True] * 4)

# --- Told once -------------------------------------------------------------------

# The notice of the mover names what failed and why: the monitor leaves the
# errors of those torrents to it, during the move and a while after
fresh(35)
tick()
check("the errors of what the mover just moved are its to tell", [bot.mover_tells(i) for i in ("a", "b", "c")], [True, True, True])
check("not the ones of a torrent it left alone", bot.mover_tells("recent"), False)
bot._mover_quiet["a"] = time.time() - 1
check("nor any longer once a while has gone by", bot.mover_tells("a"), False)
bot._mover_quiet.clear()

failed = [torrent("x", 9, 1), torrent("y", 9, 1), torrent("z", 9, 1)]
listing = bot.mover_listing(failed, reasons={"x": "no room", "y": "no permission", "z": "no room"}).split("\n")
check("what failed for the same reason goes together, with the reason once",
	[line.strip().replace("<i>", "").replace("</i>", "") for line in listing if "Movie" not in line], ["no room", "no permission"])
check("in that order", ["Movie.x" in listing[0], "Movie.z" in listing[1], "Movie.y" in listing[3]], [True, True, True])

if FAILED:
	print("\n\n".join(FAILED))
	print(f"\n{len(FAILED)} FAILED of {CHECKS[0]} checks")
	sys.exit(1)
print(f"ALL TESTS OK ({CHECKS[0]} checks)")
