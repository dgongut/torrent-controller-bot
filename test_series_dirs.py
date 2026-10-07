"""Test battery for the series folders. Run with: python3 test_series_dirs.py

A new episode is offered (or sent straight to) the folder where the rest of its
series already is. Getting it wrong is worse than not offering anything: an
episode moved to the wrong show is data in the wrong place, and the user only
notices when the media server shows it under another title. So most of this
file is about what must NOT match: shows that share words, a movie named after
a series, the same title from another year, the default download folder where
anything lands before being sorted.

The first half tests the detection in name_parser on its own. The second half
loads the bot against a mutable fake torrent client and drives the screens,
the automatic add, the buttons under the add message and the monitor."""

import copy
import importlib.util
import itertools
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from name_parser import series_auto_dir, series_dir_candidates, series_identity

FAILED = []
CHECKS = [0]


def check(description, actual, expected):
	CHECKS[0] += 1
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def key(name):
	identity = series_identity(name)
	return identity and (identity["key"], identity["year"], identity["season"])


def dirs(name, torrents, **kwargs):
	return [d for d, _named in series_dir_candidates(name, torrents, **kwargs)]


# ---------------------------------------------------------------------------
# 1. WHAT A SERIES IS
# ---------------------------------------------------------------------------

check("a release name", key("Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL"), ("chicago fire", "", 14))
check("a name the bot renamed with the default template", key("14x04 - Chicago Fire.mkv"), ("chicago fire", "", 14))
check("renamed, with the resolution", key("14x04 - Chicago Fire - 1080p HDR.mkv"), ("chicago fire", "", 14))
check("renamed season pack", key("T14 - Chicago Fire - 1080p"), ("chicago fire", "", 14))
check("renamed season pack in English", key("S14 - Chicago Fire"), ("chicago fire", "", 14))
check("a season pack in Spanish", key("Chicago Fire - Temporada 14 [HDTV 720p]"), ("chicago fire", "", 14))
check("a name that starts with 'Temporada'", key("Temporada 14 - Chicago Fire"), ("chicago fire", "", 14))
check("a name that starts with S01E01", key("S14E04 - Chicago Fire - 1080p.mkv"), ("chicago fire", "", 14))
check("a pack of several seasons has no single season", key("Chicago.Fire.S01-S03.1080p"), ("chicago fire", "", None))
check("an episode range", key("Chicago.Fire.S14E01-E08.1080p"), ("chicago fire", "", 14))
check("the year is kept apart from the title", key("Doctor.Who.2005.S01E01.720p"), ("doctor who", "2005", 1))
check("a year in parentheses", key("Chicago Fire (2012) S14E05"), ("chicago fire", "2012", 14))
check("lowercase and dots", key("chicago.fire.s14e04"), ("chicago fire", "", 14))
check("accents do not count", key("Pokémon S01E01"), ("pokemon", "", 1))
check("underscores", key("chicago_fire_S14E04"), ("chicago fire", "", 14))
check("a .torrent file name", key("Chicago.Fire.S14E04.1080p.torrent"), ("chicago fire", "", 14))
check("the 'Capitulo' convention", key("La.Que.Se.Avecina.Capitulo.1301.1080p.mkv"), ("la que se avecina", "", 13))
check("a movie is not a series", series_identity("El Camino Una pelicula de Breaking Bad (2019) 1080p"), None)
check("a movie with a series name in it", series_identity("El.Camino.A.Breaking.Bad.Movie.2019.1080p.NF.WEB-DL"), None)
check("a plain movie", series_identity("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv"), None)
check("an episode with no title at all", series_identity("1x01.mkv"), None)
check("an episode marker and nothing else", series_identity("S01E01"), None)
check("nothing", series_identity(""), None)
check("None", series_identity(None), None)
check("a magnet with no name", series_identity("magnet"), None)
check("a number as .torrent name", series_identity("123456"), None)
check("the title keeps how it was written, for the messages",
	series_identity("Chicago.Fire.S14E04.1080p")["title"], "Chicago Fire")

# ---------------------------------------------------------------------------
# 2. WHERE THE SERIES IS
# ---------------------------------------------------------------------------

CHICAGO = [
	("Chicago.Fire.S14E01.1080p.WEB.h264-ETHEL", "/media/SERIES/Chicago Fire", 100),
	("14x02 - Chicago Fire.mkv", "/media/SERIES/Chicago Fire/", 200),
	("chicago fire s14e03 720p", "/media/SERIES/Chicago Fire", 300),
	("Chicago.Med.S10E01.1080p", "/media/SERIES/Chicago Med", 400),
	("Chicago.PD.S12E01.1080p", "/media/SERIES/Chicago PD", 500),
]
NEW = "Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL"

check("the episodes of the series point at their folder",
	series_dir_candidates(NEW, CHICAGO, current_dir="/downloads", generic_dirs=["/downloads"]),
	[("/media/SERIES/Chicago Fire", True)])
check("a trailing slash is the same folder", dirs(NEW, CHICAGO), ["/media/SERIES/Chicago Fire"])
check("a show that shares a word is another show", dirs("Chicago.Med.S10E02", CHICAGO), ["/media/SERIES/Chicago Med"])
check("the user's example, renamed", dirs("14x04 - Chicago Fire.mkv", CHICAGO), ["/media/SERIES/Chicago Fire"])
check("a word of the title is not the title", dirs("Fire.S01E01", CHICAGO), [])
check("a longer title is not the title", dirs("Chicago.Fire.Rescue.S01E01", CHICAGO), [])
check("a movie never gets a series folder", dirs("Chicago.Fire.2019.1080p", CHICAGO), [])
check("nothing of the series", dirs("Severance.S02E01", CHICAGO), [])
check("no torrents at all", dirs(NEW, []), [])

BREAKING = [("Breaking.Bad.S05E16.1080p", "/tv/Breaking Bad", 1)]
check("El Camino is a movie: no Breaking Bad folder for it",
	dirs("El.Camino.A.Breaking.Bad.Movie.2019.1080p", BREAKING), [])
check("and a Breaking Bad episode never goes where El Camino is",
	dirs("Breaking.Bad.S01E01", [("El.Camino.A.Breaking.Bad.Movie.2019.1080p", "/movies", 1)]), [])

# Years
WHO = [("Doctor.Who.1963.S01E01", "/tv/Doctor Who (1963)", 1), ("Doctor.Who.2005.S01E01", "/tv/Doctor Who (2005)", 2)]
check("a year picks its own series", dirs("Doctor.Who.2005.S02E01", WHO), ["/tv/Doctor Who (2005)"])
check("the other year too", dirs("Doctor Who (1963) S02E01", WHO), ["/tv/Doctor Who (1963)"])
check("without a year both are possible, and both are offered",
	sorted(dirs("Doctor.Who.S02E01", WHO)), ["/tv/Doctor Who (1963)", "/tv/Doctor Who (2005)"])
check("so nothing is moved without asking", series_auto_dir(series_dir_candidates("Doctor.Who.S02E01", WHO)), None)
check("a reference without a year matches a name with one",
	dirs("Chicago Fire (2012) S14E05", CHICAGO), ["/media/SERIES/Chicago Fire"])
check("a folder named with another year is not the series",
	dirs("Chicago Fire (2012) S14E05", [], known_dirs=["/tv/Chicago Fire (1999)"]), [])

# The default download folder
STRAY = [("Chicago.Fire.S14E01", "/media/SERIES/Chicago Fire", 1),
		("Chicago.Fire.S14E02", "/downloads", 2), ("Chicago.Fire.S14E03", "/downloads", 3)]
check("the folder where everything lands is never the series folder",
	dirs(NEW, STRAY, generic_dirs=["/downloads"]), ["/media/SERIES/Chicago Fire"])
check("even when more episodes are still there",
	series_auto_dir(series_dir_candidates(NEW, STRAY, generic_dirs=["/downloads/"])), "/media/SERIES/Chicago Fire")
check("a generic folder named after the series does count",
	dirs(NEW, [("Chicago.Fire.S14E01", "/media/Chicago Fire", 1)], generic_dirs=["/media/Chicago Fire"]),
	["/media/Chicago Fire"])
check("an empty generic dir changes nothing", dirs(NEW, STRAY, generic_dirs=["", None]),
	["/media/SERIES/Chicago Fire", "/downloads"])

# Where the torrent already is
check("already in the series folder: nothing to suggest",
	dirs(NEW, CHICAGO, current_dir="/media/SERIES/Chicago Fire"), [])
check("already there, written with a trailing slash",
	dirs(NEW, CHICAGO, current_dir="/media/SERIES/Chicago Fire/"), [])
check("a stray episode elsewhere is no reason to move it",
	dirs(NEW, CHICAGO + [("Chicago.Fire.S14E09", "/old/stuff", 1)], current_dir="/media/SERIES/Chicago Fire"), [])
check("but from somewhere else the series folder is offered",
	dirs(NEW, CHICAGO, current_dir="/media/peliculas"), ["/media/SERIES/Chicago Fire"])

# Ranking
FLAT = [("Chicago.Fire.S13E01", "/data/series", 50), ("Chicago.Fire.S13E02", "/data/series", 60),
		("Chicago.Fire.S14E01", "/media/SERIES/Chicago Fire", 10)]
check("a folder named after the series goes before a shared one",
	dirs(NEW, FLAT), ["/media/SERIES/Chicago Fire", "/data/series"])
check("a shared folder alone is still offered",
	series_dir_candidates(NEW, FLAT[:2]), [("/data/series", False)])
check("but never moved to without asking", series_auto_dir(series_dir_candidates(NEW, FLAT[:2])), None)
TWO = [("Chicago.Fire.S14E01", "/a/Chicago Fire", 10), ("Chicago.Fire.S14E02", "/b/Chicago Fire", 20),
		("Chicago.Fire.S14E03", "/b/Chicago Fire", 30)]
check("more episodes first", dirs(NEW, TWO), ["/b/Chicago Fire", "/a/Chicago Fire"])
check("two named folders are a doubt", series_auto_dir(series_dir_candidates(NEW, TWO)), None)
TIE = [("Chicago.Fire.S14E01", "/a/Chicago Fire", 10), ("Chicago.Fire.S14E02", "/b/Chicago Fire", 20)]
check("a tie goes to the most recent", dirs(NEW, TIE), ["/b/Chicago Fire", "/a/Chicago Fire"])
check("a missing date is the oldest", dirs(NEW, [TIE[0], ("Chicago.Fire.S14E02", "/b/Chicago Fire", None)]),
	["/a/Chicago Fire", "/b/Chicago Fire"])
check("a torrent without folder is ignored", dirs(NEW, [("Chicago.Fire.S14E01", "", 1)]), [])
check("a torrent without name is ignored", dirs(NEW, [("", "/x/Chicago Fire", 1)]), [])
check("no candidates, nothing automatic", series_auto_dir([]), None)

# Favorites
check("a favorite named after the series counts with no torrent at all",
	series_dir_candidates(NEW, [], known_dirs=["/media/SERIES/Chicago Fire", "/media/SERIES"]),
	[("/media/SERIES/Chicago Fire", True)])
check("which is enough to move it", series_auto_dir(series_dir_candidates(NEW, [], known_dirs=["/tv/Chicago Fire/"])),
	"/tv/Chicago Fire")
check("a favorite that is not named after it does not", dirs(NEW, [], known_dirs=["/media/SERIES", "/downloads"]), [])
check("a favorite for another show does not", dirs(NEW, [], known_dirs=["/media/SERIES/Chicago Med"]), [])
check("a favorite with the series deeper in the path does not",
	dirs(NEW, [], known_dirs=["/media/Chicago Fire/extras"]), [])
check("a favorite with the year in parentheses", dirs(NEW, [], known_dirs=["/tv/Chicago Fire (2012)"]),
	["/tv/Chicago Fire (2012)"])
check("a favorite with dots", dirs(NEW, [], known_dirs=["/tv/chicago.fire"]), ["/tv/chicago.fire"])
check("the root is nobody's folder", dirs(NEW, [], known_dirs=["/", ""]), [])
check("a favorite and the torrents agree on one folder",
	series_dir_candidates(NEW, CHICAGO, known_dirs=["/media/SERIES/Chicago Fire"]), [("/media/SERIES/Chicago Fire", True)])

# Season folders
MAD = [("Mad.Men.S01E01", "/tv/Mad Men/Season 01", 1), ("Mad.Men.S01E02", "/tv/Mad Men/Season 01", 2)]
check("the same season, the same folder", dirs("Mad.Men.S01E03", MAD), ["/tv/Mad Men/Season 01"])
check("the next season, the next folder, padding kept", dirs("Mad.Men.S02E01", MAD), ["/tv/Mad Men/Season 02"])
check("a season folder inside the series folder counts as named",
	series_auto_dir(series_dir_candidates("Mad.Men.S02E01", MAD)), "/tv/Mad Men/Season 02")
check("a season over 9 with padding", dirs("Mad.Men.S10E01", MAD), ["/tv/Mad Men/Season 10"])
check("a pack of several seasons goes to the series folder", dirs("Mad.Men.S01-S03.1080p", MAD), ["/tv/Mad Men"])
check("Temporada without padding", dirs("Mad.Men.S12E01", [("Mad.Men.S11E01", "/tv/Mad Men/Temporada 11", 1)]),
	["/tv/Mad Men/Temporada 12"])
check("Temporada from 9 to 10", dirs("Mad.Men.S10E01", [("Mad.Men.S09E01", "/tv/Mad Men/Temporada 9", 1)]),
	["/tv/Mad Men/Temporada 10"])
check("a bare S01 folder", dirs("Mad.Men.S02E01", [("Mad.Men.S01E01", "/tv/Mad Men/S01", 1)]), ["/tv/Mad Men/S02"])
check("a season pack follows the season folders", dirs("Mad.Men.S03.1080p", MAD), ["/tv/Mad Men/Season 03"])
check("two seasons of folders become one for the next season",
	series_dir_candidates("Mad.Men.S03E01", MAD + [("Mad.Men.S02E01", "/tv/Mad Men/Season 02", 9)]),
	[("/tv/Mad Men/Season 03", True)])
check("a favorite season folder is followed too", dirs("Mad.Men.S02E01", [], known_dirs=["/tv/Mad Men/Season 01"]),
	["/tv/Mad Men/Season 02"])
check("a season folder of another show is not followed",
	dirs("Mad.Men.S02E01", [], known_dirs=["/tv/Lost/Season 01"]), [])
check("already in the next season folder", dirs("Mad.Men.S02E01", MAD, current_dir="/tv/Mad Men/Season 02"), [])
check("in last season's folder, the next one is offered",
	dirs("Mad.Men.S02E05", MAD, current_dir="/tv/Mad Men/Season 01"), ["/tv/Mad Men/Season 02"])
check("a season folder at the root is left as it is", dirs("Mad.Men.S02E01", [("Mad.Men.S01E01", "Season 01", 1)]),
	["Season 01"])
check("a folder with a number that is not a season is left as it is",
	dirs("Mad.Men.S02E01", [("Mad.Men.S01E01", "/tv/Mad Men 2007", 1)]), ["/tv/Mad Men 2007"])

# Windows paths (Deluge on Windows)
check("backslashes are kept", dirs("Mad.Men.S02E01", [("Mad.Men.S01E01", "D:\\TV\\Mad Men\\Temporada 1", 1)]),
	["D:\\TV\\Mad Men\\Temporada 2"])
check("a Windows series folder is named", series_auto_dir(series_dir_candidates("Mad.Men.S02E01",
	[("Mad.Men.S01E01", "D:\\TV\\Mad Men", 1)])), "D:\\TV\\Mad Men")

# The same series written differently
VARIANTS = ["Chicago.Fire.S14E01", "chicago fire 14x02", "CHICAGO_FIRE_S14E03", "Chicago Fire - 14x05 - Título",
			"[Grupo] Chicago Fire S14E06 [1080p]", "14x07 - Chicago Fire - 1080p.mkv"]
for variant in VARIANTS:
	check(f"'{variant}' is Chicago Fire", key(variant)[0], "chicago fire")

# ---------------------------------------------------------------------------
# 3. THE BOT
# ---------------------------------------------------------------------------

os.environ.setdefault("TELEGRAM_TOKEN", "0:fake")
os.environ.setdefault("TELEGRAM_ADMIN", "1")
os.environ.setdefault("LOCALE_PATH", os.path.join(REPO, "locale"))

import config
config.CONFIG_PATH = tempfile.mkdtemp()  # Never touch the real /config

import torrent_clients
from torrent_clients import PermanentTorrentError, TorrentClientError, TorrentStatus
from torrent_clients.base import TorrentInfo

_ids = itertools.count(1)


def new_id():
	return f"{next(_ids):040x}"


class FakeClient:
	supports_alt_speed = True
	supports_rename = True
	supports_verify = True
	supports_categories = False

	def __init__(self):
		self.reset()

	def reset(self):
		self.torrents = {}
		self.calls = []
		self.added = None  # Name the next added torrent takes
		self.fail_move = None
		self.fail_list = None
		self.categories = []
		self.session = {"version": "Fake 1.0", "alt_speed_enabled": False, "alt_speed_down": 50, "alt_speed_up": 10,
						"speed_limit_down": 0, "speed_limit_down_enabled": False,
						"speed_limit_up": 0, "speed_limit_up_enabled": False, "download_dir": "/downloads"}

	def put(self, name, download_dir, added=None, auto_managed=False, category=""):
		torrent = TorrentInfo(id=new_id(), name=name, status=TorrentStatus.SEEDING, progress=100.0,
							total_size=1000, download_dir=download_dir, auto_managed=auto_managed, category=category,
							added_date=added or datetime(2026, 1, 1), files=[(name, 1000, True)])
		self.torrents[torrent.id] = torrent
		return torrent

	def test_connection(self):
		return "Fake 1.0"

	def get_torrents(self, status=None, query=None):
		self.calls.append(("get_torrents",))
		if self.fail_list:
			raise self.fail_list
		return [copy.copy(t) for t in self.torrents.values()]

	def get_torrent(self, torrent_id):
		torrent = self.torrents.get(torrent_id)
		return copy.deepcopy(torrent) if torrent else None

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None, category=None):
		self.calls.append(("add_torrent", download_dir, category))
		directory = download_dir
		auto_managed = False
		if category:
			directory = dict(self.categories).get(category, "/downloads")
			auto_managed = True
		return self.put(self.added, directory, added=datetime(2026, 6, 1), auto_managed=auto_managed, category=category or "")

	def move_torrents(self, torrent_ids, new_dir):
		self.calls.append(("move", list(torrent_ids), new_dir))
		if self.fail_move:
			raise self.fail_move
		for torrent_id in torrent_ids:
			self.torrents[torrent_id].download_dir = new_dir

	def get_free_space(self, path):
		return 1024 ** 4

	def get_default_download_dir(self):
		return "/downloads"

	def get_download_dirs(self):
		return sorted({t.download_dir for t in self.torrents.values()} | {"/downloads"})

	def get_categories(self):
		return list(self.categories)

	def get_settings(self):
		return dict(self.session)

	def set_alt_speed(self, enabled):
		self.session["alt_speed_enabled"] = enabled

	def set_speed_limit(self, direction, kbps, enabled=True):
		if kbps is not None:
			self.session[f"speed_limit_{direction}"] = kbps
		self.session[f"speed_limit_{direction}_enabled"] = enabled


CLIENT = FakeClient()
torrent_clients.create_client = lambda config_module: CLIENT

_spec = importlib.util.spec_from_file_location("tcbot", os.path.join(REPO, "torrent-controller-bot.py"))
bot = importlib.util.module_from_spec(_spec)
sys.modules["tcbot"] = bot
_spec.loader.exec_module(bot)
bot_settings = bot.bot_settings

time.sleep = lambda seconds: None


class _InlineThread(threading.Thread):
	"""The move order runs in a thread: inline here, so its outcome is known
	when the button returns"""
	def start(self):
		self.run()


bot.threading = type("ThreadingProxy", (), {"Thread": _InlineThread, "Lock": threading.Lock})

SCREENS = []  # (kind, text, markup) of every message sent or edited


class _Sent:
	message_id = 500


def _send(chat_id, text, **kw):
	SCREENS.append(("send", text, kw.get("reply_markup")))
	return _Sent()


bot.bot.send_message = _send
bot.bot.edit_message_text = lambda text, chat_id, message_id, **kw: SCREENS.append(("edit", text, kw.get("reply_markup"))) or True
bot.bot.edit_message_reply_markup = lambda chat_id, message_id, reply_markup=None: SCREENS.append(("markup", None, reply_markup)) or True
NOTIFIED = []
bot.notify = lambda text, chat_id=None, thread_id=None, reply_markup=None: NOTIFIED.append((text, reply_markup))


def fresh(**settings):
	CLIENT.reset()
	SCREENS.clear()
	NOTIFIED.clear()
	defaults = {"auto_download": False, "auto_download_dir": "", "auto_category": "", "auto_rename": False,
				"auto_move_series": False, "auto_move_series_external": False, "notify_external_added": False,
				"favorite_dirs": [], "low_space_warning": True, "notify_completed": False}
	defaults.update(settings)
	for name, value in defaults.items():
		bot_settings.set(name, value)


def buttons(markup):
	if markup is None:
		return []
	return [(b.text, b.callback_data) for row in markup.keyboard for b in row]


def button_texts(markup):
	return [text for text, _ in buttons(markup)]


def last():
	return SCREENS[-1]


def roundtrip(text):
	"""What Telegram gives back as html_text: the entities come back as tags,
	but the escaping is its own (it only escapes &, < and >), so an apostrophe
	the bot sent as &#x27; comes back as a plain one"""
	return text.replace("&#x27;", "'").replace("&quot;", '"')


def press(data, message_html=None):
	bot.dispatch_callback(1, 100, 1, data, message_html=message_html)


def chicago():
	CLIENT.put("Chicago.Fire.S14E01.1080p.WEB.h264-ETHEL", "/media/SERIES/Chicago Fire", added=datetime(2026, 9, 1))
	CLIENT.put("14x02 - Chicago Fire.mkv", "/media/SERIES/Chicago Fire", added=datetime(2026, 9, 8))
	CLIENT.put("14x03 - Chicago Fire.mkv", "/media/SERIES/Chicago Fire", added=datetime(2026, 9, 15))
	CLIENT.put("Chicago.Med.S10E01.1080p", "/media/SERIES/Chicago Med", added=datetime(2026, 9, 16))
	CLIENT.put("Some.Movie.2024.1080p", "/media/PELIS", added=datetime(2026, 9, 17))


def series_buttons(markup):
	return [text for text in button_texts(markup) if text.startswith("✨")]


# --- The folder picker when adding -----------------------------------------
fresh(favorite_dirs=[f"/fav/{n}" for n in range(20)])
chicago()
pending_id = bot.new_pending_torrent("Chicago.Fire.S14E04.1080p", magnet="magnet:?xt=urn:btih:abc")
bot.ask_download_dir(1, pending_id, "Chicago.Fire.S14E04.1080p")
kind, text, markup = last()
check("the series folder is the first button", button_texts(markup)[0], "✨ /media/SERIES/Chicago Fire")
check("only once", len(series_buttons(markup)), 1)
check("on the first page even with 20 favorites", any("Chicago Fire" in t for t in button_texts(markup)), True)
check("the message tells why", bot.get_text("SERIES_DIR_HINT", "Chicago Fire") in text, True)
dir_id = bot.get_dir_id("/media/SERIES/Chicago Fire")
check("the button adds it there", buttons(markup)[0][1], bot.build_call("addTo", pending_id, dir_id))
check("the favorites come right after", button_texts(markup)[1], "📂 /fav/0")
check("the folder is not repeated further on",
	sum(1 for t in button_texts(markup) if t.endswith("Chicago Fire")), 1)
bot.ask_download_dir(1, pending_id, "Chicago.Fire.S14E04.1080p", message_id=100, dir_page=1)
kind, text, markup = last()
check("page two has no ✨", series_buttons(markup), [])
check("and does not repeat the folder", any(t.endswith("SERIES/Chicago Fire") for t in button_texts(markup)), False)

fresh(favorite_dirs=["/media/SERIES/Chicago Fire", "/media/PELIS"])
chicago()
pending_id = bot.new_pending_torrent("Chicago.Fire.S14E04.1080p", magnet="m")
bot.ask_download_dir(1, pending_id, "Chicago.Fire.S14E04.1080p")
kind, text, markup = last()
check("a favorite that is the series folder gets the ✨ instead of 📂",
	[t for t in button_texts(markup) if "Chicago Fire" in t], ["✨ /media/SERIES/Chicago Fire"])
check("and goes first", button_texts(markup)[0], "✨ /media/SERIES/Chicago Fire")

fresh()
chicago()
pending_id = bot.new_pending_torrent("Some.Other.Movie.2025.1080p", magnet="m")
before = len(CLIENT.calls)
bot.ask_download_dir(1, pending_id, "Some.Other.Movie.2025.1080p")
kind, text, markup = last()
check("a movie gets no ✨", series_buttons(markup), [])
check("nor the hint", "✨" in text, False)
check("and the manager is not even asked for the torrents of a movie",
	[c for c in CLIENT.calls[before:] if c == ("get_torrents",)], [])

fresh()
chicago()
pending_id = bot.new_pending_torrent("Severance.S02E01.1080p", magnet="m")
bot.ask_download_dir(1, pending_id, "Severance.S02E01.1080p")
check("a series with nothing in the manager gets no ✨", series_buttons(last()[2]), [])

fresh()
chicago()
CLIENT.fail_list = TorrentClientError("down")
pending_id = bot.new_pending_torrent("Chicago.Fire.S14E04.1080p", magnet="m")
bot.ask_download_dir(1, pending_id, "Chicago.Fire.S14E04.1080p")
check("a manager that does not answer still shows the picker", "📂" in " ".join(button_texts(last()[2])) or
	bot.get_text("BUTTON_WRITE_DIR") in button_texts(last()[2]), True)
check("without ✨", series_buttons(last()[2]), [])

# The name inside a .torrent file, not the file name
fresh()
chicago()
data = b"d8:announce3:abc4:infod6:lengthi10e4:name25:Chicago.Fire.S14E04.1080p12:piece lengthi1ee"
pending_id = bot.new_pending_torrent("123456", data=data)
bot.ask_download_dir(1, pending_id, "123456")
check("a .torrent named with a number is recognised by the name inside it",
	series_buttons(last()[2]), ["✨ /media/SERIES/Chicago Fire"])
check("torrent_data_name reads it", bot.torrent_data_name(data), "Chicago.Fire.S14E04.1080p")
check("torrent_data_name without data", bot.torrent_data_name(None), None)
check("torrent_data_name with garbage", bot.torrent_data_name(b"garbage"), None)

# Two folders
fresh()
CLIENT.put("Doctor.Who.1963.S01E01", "/tv/Doctor Who (1963)")
CLIENT.put("Doctor.Who.2005.S01E01", "/tv/Doctor Who (2005)")
CLIENT.put("Doctor.Who.2005.S01E02", "/tv/Doctor Who (2005)")
pending_id = bot.new_pending_torrent("Doctor.Who.S02E01", magnet="m")
bot.ask_download_dir(1, pending_id, "Doctor.Who.S02E01")
check("two possible folders, two ✨, most episodes first", series_buttons(last()[2]),
	["✨ /tv/Doctor Who (2005)", "✨ /tv/Doctor Who (1963)"])
CLIENT.put("Doctor.Who.S01E01", "/tv/Doctor Who")
bot.ask_download_dir(1, pending_id, "Doctor.Who.S02E01")
check("never more than two", len(series_buttons(last()[2])), bot.SERIES_SUGGESTIONS_MAX)

# --- Managers with categories: the category screen ---------------------------
fresh()
chicago()
CLIENT.categories = [("Series", "/media/SERIES"), ("Pelis", "/media/PELIS")]
bot.client.supports_categories = True
pending_id = bot.new_pending_torrent("Chicago.Fire.S14E04.1080p", magnet="m")
bot.ask_add_category(1, pending_id, "Chicago.Fire.S14E04.1080p")
kind, text, markup = last()
check("the series folder goes above the categories", button_texts(markup)[0], "✨ /media/SERIES/Chicago Fire")
check("and adds it to that folder", buttons(markup)[0][1],
	bot.build_call("addTo", pending_id, bot.get_dir_id("/media/SERIES/Chicago Fire")))
check("with the hint", bot.get_text("SERIES_DIR_HINT", "Chicago Fire") in text, True)
check("the categories are still there", any(t.startswith("🏷️") for t in button_texts(markup)), True)
pending_id = bot.new_pending_torrent("Some.Movie.2025", magnet="m")
bot.ask_add_category(1, pending_id, "Some.Movie.2025")
check("a movie gets the category screen as always", button_texts(last()[2])[0].startswith("🏷️"), True)
bot.client.supports_categories = False

# --- The move screen ---------------------------------------------------------
fresh()
chicago()
stray = CLIENT.put("Chicago.Fire.S14E04.1080p", "/downloads")
press(bot.build_call("move", stray.id, config.FILTER_ALL, 0))
kind, text, markup = last()
check("moving an episode offers its series folder first", button_texts(markup)[0], "✨ /media/SERIES/Chicago Fire")
check("with the hint", bot.get_text("SERIES_DIR_HINT", "Chicago Fire") in text, True)
placed = CLIENT.put("Chicago.Fire.S14E05.1080p", "/media/SERIES/Chicago Fire")
press(bot.build_call("move", placed.id, config.FILTER_ALL, 0))
check("one already in its folder gets no ✨", series_buttons(last()[2]), [])
check("nor the hint", bot.get_text("SERIES_DIR_HINT", "Chicago Fire") in last()[1], False)
alone = CLIENT.put("Lonely.Show.S01E01", "/downloads")
press(bot.build_call("move", alone.id, config.FILTER_ALL, 0))
check("an episode does not suggest the folder it is in to itself", series_buttons(last()[2]), [])

# --- The automatic add, without the automatic move ---------------------------
fresh(auto_download=True)
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL"
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL", magnet="m")
kind, text, markup = last()
check("it is added where it was going to", CLIENT.calls[-1] if CLIENT.calls[-1][0] == "add_torrent" else
	[c for c in CLIENT.calls if c[0] == "add_torrent"][-1], ("add_torrent", "/downloads", None))
check("the add message says where it is", "📂 <code>/downloads</code>" in text, True)
check("and suggests the series folder at the end",
	text.endswith(bot.get_text("SERIES_SUGGEST_ONE", "/media/SERIES/Chicago Fire", "Chicago Fire")), True)
check("separated from the rest", f"\n\n{bot.get_text('SERIES_SUGGEST_ONE', '/media/SERIES/Chicago Fire', 'Chicago Fire')}" in text, True)
added_id = [t for t in CLIENT.torrents.values() if t.name == CLIENT.added][0].id
check("with Move and Cancel", button_texts(markup),
	[bot.get_text("BUTTON_SERIES_MOVE", "/media/SERIES/Chicago Fire"), bot.get_text("BUTTON_CANCEL")])
check("Move carries the torrent and the folder", buttons(markup)[0][1],
	bot.build_call("serMove", added_id, bot.get_dir_id("/media/SERIES/Chicago Fire")))
check("the callback fits in Telegram", all(len(c.encode()) <= 64 for _, c in buttons(markup)), True)
check("nothing was moved yet", [c for c in CLIENT.calls if c[0] == "move"], [])
add_message = text

# Move
SCREENS.clear()
press(buttons(markup)[0][1], message_html=roundtrip(add_message))
check("Move moves it", [c for c in CLIENT.calls if c[0] == "move"], [("move", [added_id], "/media/SERIES/Chicago Fire")])
check("first saying it is moving", bot.get_text("SERIES_MOVING", "/media/SERIES/Chicago Fire") in SCREENS[0][1], True)
kind, text, markup = last()
check("then the same add message is edited, not a new one", kind, "edit")
check("its 📂 line now tells the new folder", "📂 <code>/media/SERIES/Chicago Fire</code>" in text, True)
check("the old folder is gone", "/downloads" in text, False)
check("the suggestion is gone", "💡" in text, False)
check("no 'moved to' line", bot.get_text("SERIES_MOVED", "/media/SERIES/Chicago Fire") in text, False)
check("no buttons left", markup, None)
check("the rest of the message is untouched", text.split("\n")[0], add_message.split("\n")[0])
check("no new message was sent", [s for s in SCREENS if s[0] == "send"], [])

# Cancel
fresh(auto_download=True)
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
add_message = last()[1]
press(bot.build_call("serDrop"), message_html=roundtrip(add_message))
kind, text, markup = last()
check("Cancel edits the message", kind, "edit")
check("without the suggestion", "💡" in text, False)
check("keeping where it is", "📂 <code>/downloads</code>" in text, True)
check("and without buttons", markup, None)
check("exactly the message before the suggestion", text, add_message.split("\n\n💡")[0])
check("nothing is moved", [c for c in CLIENT.calls if c[0] == "move"], [])
press(bot.build_call("serDrop"))
check("a Cancel with no message to read only drops the buttons", last()[0], "markup")

# Move with an apostrophe and an ampersand in the folder
fresh(auto_download=True)
CLIENT.put("Grey's.Anatomy.S21E01", "/tv/Grey's Anatomy & Co")
CLIENT.added = "Grey's.Anatomy.S21E02"
bot.start_add_flow(1, "Grey's.Anatomy.S21E02", magnet="m")
kind, add_message, markup = last()
check("a folder with HTML characters is suggested",
	button_texts(markup)[0], bot.get_text("BUTTON_SERIES_MOVE", bot.truncate_dir("/tv/Grey's Anatomy & Co")))
press(buttons(markup)[0][1], message_html=roundtrip(add_message).replace("&#x27;", "'"))
kind, text, markup = last()
check("the 📂 line is rewritten with it escaped", "📂 <code>/tv/Grey's Anatomy &amp; Co</code>" in text, True)
check("and the suggestion removed", "💡" in text, False)

# Move fails
fresh(auto_download=True)
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
kind, add_message, markup = last()
CLIENT.fail_move = PermanentTorrentError("disk full")
press(buttons(markup)[0][1], message_html=roundtrip(add_message))
kind, text, markup = last()
check("a refused move keeps the add message", text.startswith(add_message.split("\n\n💡")[0]), True)
check("telling why", bot.get_text("MOVE_REFUSED", "disk full") in text, True)
check("the 📂 line still tells the truth", "📂 <code>/downloads</code>" in text, True)
check("no suggestion left behind", "💡" in text, False)

# Buttons that outlived a restart, a torrent that is gone
fresh(auto_download=True)
press(bot.build_call("serMove", "f" * 40, "99999"), message_html="✅ Añadido <b>x</b>\n📂 <code>/downloads</code>\n\n💡 Esto quizá...")
check("a folder id the bot forgot says so", bot.get_text("SERIES_EXPIRED") in last()[1], True)
check("keeping the message", last()[1].startswith("✅ Añadido <b>x</b>\n📂 <code>/downloads</code>"), True)
check("and nothing moves", [c for c in CLIENT.calls if c[0] == "move"], [])
press(bot.build_call("serMove", "f" * 40, bot.get_dir_id("/tv/x")), message_html="✅ Añadido <b>x</b>\n📂 <code>/downloads</code>\n\n💡 Esto...")
check("a torrent that was deleted says so", bot.get_text("TORRENT_NOT_FOUND") in last()[1], True)
check("and nothing moves either", [c for c in CLIENT.calls if c[0] == "move"], [])
press(bot.build_call("serMove", "f" * 40, "99999"))
check("without the message to read, the expiry alone", last()[1], bot.get_text("SERIES_EXPIRED"))

# What gets no suggestion
fresh(auto_download=True)
chicago()
CLIENT.added = "Some.New.Movie.2026.1080p"
bot.start_add_flow(1, "Some.New.Movie.2026.1080p", magnet="m")
check("a movie added automatically gets no suggestion", ("💡" in last()[1], last()[2]), (False, None))
CLIENT.added = "Severance.S02E01"
bot.start_add_flow(1, "Severance.S02E01", magnet="m")
check("a series with nothing in the manager neither", ("💡" in last()[1], last()[2]), (False, None))

fresh(auto_download=True, auto_download_dir="/media/SERIES/Chicago Fire")
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
check("added straight into the series folder: nothing to suggest", ("💡" in last()[1], last()[2]), (False, None))

fresh(auto_download=True)
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
pending_id = bot.new_pending_torrent("Chicago.Fire.S14E04.1080p", magnet="m")
bot.do_add_torrent(1, 100, pending_id, "/media/PELIS")
check("a folder the user picked is never second-guessed", "💡" in last()[1], False)

# qBittorrent with an automatic category
fresh(auto_download=True, auto_category="Series")
bot.client.supports_categories = True
CLIENT.categories = [("Series", "/media/SERIES")]
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
check("an auto managed torrent follows its category: no suggestion", ("💡" in last()[1], last()[2]), (False, None))
bot_settings.set("auto_move_series", True)
CLIENT.calls.clear()
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
check("nor an automatic move: the category decides",
	[c for c in CLIENT.calls if c[0] in ("add_torrent", "move")], [("add_torrent", "", "Series")])
bot.client.supports_categories = False

# A list that fails while looking for the series
fresh(auto_download=True)
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
_real_get_torrents = CLIENT.get_torrents
CLIENT.get_torrents = lambda status=None, query=None: (_ for _ in ()).throw(TorrentClientError("busy"))
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
check("a manager that cannot list still gets the torrent added", bot.get_text("ADD_OK", "Chicago.Fire.S14E04.1080p", "/downloads") in last()[1], True)
check("just without suggestion", last()[2], None)
CLIENT.get_torrents = _real_get_torrents

# --- The automatic add, with the automatic move --------------------------------
fresh(auto_download=True, auto_move_series=True)
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL"
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL", magnet="m")
kind, text, markup = last()
check("it is added straight into the series folder, not moved",
	[c for c in CLIENT.calls if c[0] in ("add_torrent", "move")], [("add_torrent", "/media/SERIES/Chicago Fire", None)])
check("the add message tells that folder", "📂 <code>/media/SERIES/Chicago Fire</code>" in text, True)
check("and why", text.endswith(bot.get_text("SERIES_PLACED", "Chicago Fire")), True)
check("no 'moved' line", bot.get_text("SERIES_MOVED", "/media/SERIES/Chicago Fire") in text, False)
added_id = [t for t in CLIENT.torrents.values() if t.name == CLIENT.added][0].id
check("with an undo button", buttons(markup),
	[(bot.get_text("BUTTON_SERIES_UNDO"), bot.build_call("serUndo", added_id, bot.get_dir_id("/downloads")))])
check("a single message", [s[0] for s in SCREENS], ["send"])
add_message = text

press(buttons(markup)[0][1], message_html=roundtrip(add_message))
kind, text, markup = last()
check("Undo moves it where it would have gone", [c for c in CLIENT.calls if c[0] == "move"],
	[("move", [added_id], "/downloads")])
check("editing the same message", kind, "edit")
check("whose 📂 line tells it", "📂 <code>/downloads</code>" in text, True)
check("without the series line", bot.get_text("SERIES_PLACED", "Chicago Fire") in text, False)
check("nor buttons", markup, None)

fresh(auto_download=True, auto_move_series=True, auto_download_dir="/media/incoming")
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
check("the undo goes back to the automatic folder",
	buttons(last()[2])[0][1], bot.build_call("serUndo", [t for t in CLIENT.torrents.values() if t.name == CLIENT.added][0].id,
											bot.get_dir_id("/media/incoming")))

fresh(auto_download=True, auto_move_series=True)
CLIENT.put("Mad.Men.S01E01", "/tv/Mad Men/Season 01")
CLIENT.added = "Mad.Men.S02E01.1080p"
bot.start_add_flow(1, "Mad.Men.S02E01.1080p", magnet="m")
check("a new season goes straight to its new season folder",
	[c for c in CLIENT.calls if c[0] == "add_torrent"], [("add_torrent", "/tv/Mad Men/Season 02", None)])

fresh(auto_download=True, auto_move_series=True)
CLIENT.put("Doctor.Who.1963.S01E01", "/tv/Doctor Who (1963)")
CLIENT.put("Doctor.Who.2005.S01E01", "/tv/Doctor Who (2005)")
CLIENT.added = "Doctor.Who.S02E01"
bot.start_add_flow(1, "Doctor.Who.S02E01", magnet="m")
check("in doubt it is not moved", [c for c in CLIENT.calls if c[0] == "add_torrent"], [("add_torrent", "/downloads", None)])
check("but both folders are offered", len([b for b in button_texts(last()[2]) if b.startswith("📦")]), 2)
check("with the text for several folders", bot.get_text("SERIES_SUGGEST_MANY", "Doctor Who") in last()[1], True)

fresh(auto_download=True, auto_move_series=True)
CLIENT.put("Chicago.Fire.S13E01", "/data/series")
CLIENT.added = "Chicago.Fire.S14E04"
bot.start_add_flow(1, "Chicago.Fire.S14E04", magnet="m")
check("a folder not named after the series is never chosen without asking",
	[c for c in CLIENT.calls if c[0] == "add_torrent"], [("add_torrent", "/downloads", None)])
check("only offered", bot.get_text("SERIES_SUGGEST_ONE", "/data/series", "Chicago Fire") in last()[1], True)

fresh(auto_download=True, auto_move_series=True)
CLIENT.put("Breaking.Bad.S05E16", "/tv/Breaking Bad")
CLIENT.added = "El.Camino.A.Breaking.Bad.Movie.2019.1080p"
bot.start_add_flow(1, "El.Camino.A.Breaking.Bad.Movie.2019.1080p", magnet="m")
check("El Camino goes where movies go", [c for c in CLIENT.calls if c[0] == "add_torrent"], [("add_torrent", "/downloads", None)])
check("with nothing about Breaking Bad", (last()[2], "Breaking" in last()[1].split("\n", 1)[1]), (None, False))

fresh(auto_download=True, auto_move_series=True)
chicago()
data = b"d4:infod6:lengthi10e4:name25:Chicago.Fire.S14E04.1080p12:piece lengthi1ee"
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.start_add_flow(1, "777", data=data)
check("a .torrent named with a number goes by the name inside it",
	[c for c in CLIENT.calls if c[0] == "add_torrent"], [("add_torrent", "/media/SERIES/Chicago Fire", None)])

fresh(auto_download=True, auto_move_series=True)
chicago()
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.start_add_flow(1, "magnet", magnet="magnet:?xt=urn:btih:abc")
check("a magnet without a name is added as always",
	[c for c in CLIENT.calls if c[0] == "add_torrent"], [("add_torrent", "/downloads", None)])
check("and once the manager knows its name, the folder is offered",
	bot.get_text("SERIES_SUGGEST_ONE", "/media/SERIES/Chicago Fire", "Chicago Fire") in last()[1], True)

fresh(auto_download=False, auto_move_series=True)
chicago()
pending_id = bot.new_pending_torrent("Chicago.Fire.S14E04.1080p", magnet="m")
CLIENT.added = "Chicago.Fire.S14E04.1080p"
bot.do_add_torrent(1, 100, pending_id, "/media/PELIS")
check("asking for the folder, the answer is the folder: no automatic move",
	[c for c in CLIENT.calls if c[0] in ("add_torrent", "move")], [("add_torrent", "/media/PELIS", None)])

# --- The automatic move needs the automatic download ----------------------------
# When the bot asks for the folder, the series folder is already the first
# button: the setting only changes what the bot does when it does not ask
fresh(auto_download=False, auto_move_series=True)
chicago()
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
check("without the automatic download it still asks", [c for c in CLIENT.calls if c[0] == "add_torrent"], [])
check("offering the series folder first", series_buttons(last()[2]), ["✨ /media/SERIES/Chicago Fire"])
check("so it is not active", bot.auto_move_series_enabled(), False)

fresh(auto_download=False, auto_move_series=True)
bot.client.supports_categories = True
CLIENT.categories = [("Series", "/media/SERIES")]
chicago()
bot.start_add_flow(1, "Chicago.Fire.S14E04.1080p", magnet="m")
check("with categories it asks the category, the folder first", (
	[c for c in CLIENT.calls if c[0] == "add_torrent"], button_texts(last()[2])[0]), ([], "✨ /media/SERIES/Chicago Fire"))
bot.client.supports_categories = False

# --- Torrents added outside the bot ------------------------------------------
def external(name, directory="/downloads", **kwargs):
	states = bot.poll_torrents(None)
	torrent = CLIENT.put(name, directory, **kwargs)
	bot.poll_torrents(states)
	return torrent


fresh(notify_external_added=True)
chicago()
torrent = external("Chicago.Fire.S14E04.1080p")
check("the notice of an external torrent suggests the folder", len(NOTIFIED), 1)
text, markup = NOTIFIED[0]
check("at its end", text.endswith(bot.get_text("SERIES_SUGGEST_ONE", "/media/SERIES/Chicago Fire", "Chicago Fire")), True)
check("with the buttons", buttons(markup)[0][1], bot.build_call("serMove", torrent.id, bot.get_dir_id("/media/SERIES/Chicago Fire")))
check("nothing moved", [c for c in CLIENT.calls if c[0] == "move"], [])
press(buttons(markup)[0][1], message_html=roundtrip(text))
check("Move edits the notice with the new folder", "📂 <code>/media/SERIES/Chicago Fire</code>" in last()[1], True)
check("dropping the suggestion", "💡" in last()[1], False)
check("keeping the header", last()[1].startswith(text.split("\n")[0]), True)

fresh(notify_external_added=False)
chicago()
external("Chicago.Fire.S14E04.1080p")
check("with the notice off nothing is told", NOTIFIED, [])
check("nor looked for", [c for c in CLIENT.calls if c[0] == "move"], [])

fresh(notify_external_added=True, auto_download=True, auto_move_series=True)
chicago()
torrent = external("Chicago.Fire.S14E04.1080p")
check("the automatic move alone does not touch external torrents", [c for c in CLIENT.calls if c[0] == "move"], [])
check("they are only suggested", "💡" in NOTIFIED[0][0], True)

fresh(notify_external_added=True, auto_download=True, auto_move_series=True, auto_move_series_external=True)
chicago()
torrent = external("Chicago.Fire.S14E04.1080p")
check("with external on, an external torrent is moved (it is already in the manager)",
	[c for c in CLIENT.calls if c[0] == "move"], [("move", [torrent.id], "/media/SERIES/Chicago Fire")])
text, markup = NOTIFIED[0]
check("the notice tells the final folder", "📂 <code>/media/SERIES/Chicago Fire</code>" in text, True)
check("not the one it arrived in", "/downloads" in text, False)
check("and why", text.endswith(bot.get_text("SERIES_PLACED", "Chicago Fire")), True)
check("with undo back to where it arrived", buttons(markup)[0][1], bot.build_call("serUndo", torrent.id, bot.get_dir_id("/downloads")))

fresh(notify_external_added=True, auto_download=False, auto_move_series=True, auto_move_series_external=True)
chicago()
external("Chicago.Fire.S14E04.1080p")
check("without the automatic download no external torrent is moved", [c for c in CLIENT.calls if c[0] == "move"], [])
check("only suggested", "💡" in NOTIFIED[0][0], True)

fresh(notify_external_added=False, auto_download=True, auto_move_series=True, auto_move_series_external=True)
chicago()
torrent = external("Chicago.Fire.S14E04.1080p")
check("a move is told even with the external notice off", len(NOTIFIED), 1)
check("since the torrent changed without anyone asking", "📂 <code>/media/SERIES/Chicago Fire</code>" in NOTIFIED[0][0], True)
external("Some.Movie.2025.1080p")
check("but a movie still says nothing", len(NOTIFIED), 1)
external("Doctor.Who.S01E01")
check("nor a series with nothing to go by", len(NOTIFIED), 1)

fresh(notify_external_added=True, auto_download=True, auto_move_series=True, auto_move_series_external=True)
chicago()
CLIENT.fail_move = TorrentClientError("busy")
torrent = external("Chicago.Fire.S14E04.1080p")
check("a failed external move falls back to suggesting", "💡" in NOTIFIED[0][0], True)
check("telling the folder it is really in", "📂 <code>/downloads</code>" in NOTIFIED[0][0], True)

fresh(notify_external_added=False, auto_download=True, auto_move_series=True, auto_move_series_external=True)
chicago()
CLIENT.fail_move = TorrentClientError("busy")
external("Chicago.Fire.S14E04.1080p")
check("and with the notice off, a failed move is not told", NOTIFIED, [])

fresh(notify_external_added=True, auto_download=True, auto_move_series=True, auto_move_series_external=True)
chicago()
torrent = external("Chicago.Fire.S14E04.1080p", auto_managed=True, category="Series")
check("an external auto managed torrent is never moved", [c for c in CLIENT.calls if c[0] == "move"], [])
check("nor suggested", "💡" in NOTIFIED[0][0], False)

fresh(notify_external_added=True, auto_download=True, auto_move_series=True, auto_move_series_external=True)
chicago()
torrent = external("Chicago.Fire.S14E04.1080p", directory="/media/SERIES/Chicago Fire")
check("an external torrent already in place is left alone", [c for c in CLIENT.calls if c[0] == "move"], [])
check("and told as always", (len(NOTIFIED), "💡" in NOTIFIED[0][0], NOTIFIED[0][1]), (1, False, None))

# --- Settings ----------------------------------------------------------------
fresh()
text, markup = bot.build_settings("add")
check("off by default", bot.auto_move_series_enabled(), False)
check("the add screen tells the bot asks", bot.get_text("SETTINGS_ADD_ASKING") in text, True)
toggles = [c for _, c in buttons(markup)]
check("no toggle while the automatic download is off", bot.build_call("toggleSetting", "auto_move_series") in toggles, False)
press(bot.build_call("toggleSetting", "auto_download"))
toggles = [c for _, c in buttons(last()[2])]
check("the toggle shows up under the automatic download", bot.build_call("toggleSetting", "auto_move_series") in toggles, True)
check("right after its other options",
	toggles.index(bot.build_call("toggleSetting", "auto_move_series")), toggles.index(bot.build_call("autoDirMenu", 0)) + 1)
check("as one of them", [t for t, c in buttons(last()[2]) if c == bot.build_call("toggleSetting", "auto_move_series")][0].startswith("↳ "), True)
check("the external one is hidden while it is off", bot.build_call("toggleSetting", "auto_move_series_external") in toggles, False)
press(bot.build_call("toggleSetting", "auto_move_series"))
check("turning it on", bot.auto_move_series_enabled(), True)
check("the screen tells it is on", bot.get_text("SETTINGS_AUTO_MOVE_SERIES", bot.get_text("SETTINGS_AUTO_MOVE_SERIES_ON",
	bot.get_text("AUTO_RENAME_SOURCE_BOT"))) in last()[1], True)
check("and the toggles stay on the add screen", last()[1].startswith(bot.get_text("SETTINGS_ADD_TITLE")), True)
check("only for the bot's own adds", bot.auto_move_series_enabled(external=True), False)
text, markup = last()[1], last()[2]
check("the settings tell it is on, for the bot's adds",
	bot.get_text("SETTINGS_AUTO_MOVE_SERIES", bot.get_text("SETTINGS_AUTO_MOVE_SERIES_ON", bot.get_text("AUTO_RENAME_SOURCE_BOT"))) in text, True)
check("the external toggle shows up", bot.build_call("toggleSetting", "auto_move_series_external") in [c for _, c in buttons(markup)], True)
press(bot.build_call("toggleSetting", "auto_move_series_external"))
check("external on", bot.auto_move_series_enabled(external=True), True)
check("the settings tell both",
	bot.get_text("SETTINGS_AUTO_MOVE_SERIES_ON", bot.get_text("AUTO_RENAME_SOURCE_ALL")) in last()[1], True)
press(bot.build_call("toggleSetting", "auto_download"))
check("turning the automatic download off turns it off", (bot.auto_move_series_enabled(), bot.auto_move_series_enabled(external=True)), (False, False))
check("and the add screen goes back to asking", bot.get_text("SETTINGS_ADD_ASKING") in last()[1], True)
check("the telemetry agrees", (bot.collect_telemetry_metrics()["auto_move_series"], bot.collect_telemetry_metrics()["auto_move_series_external"]), (False, False))
press(bot.build_call("toggleSetting", "auto_download"))
check("turning it back on brings it back as it was", (bot.auto_move_series_enabled(), bot.auto_move_series_enabled(external=True)), (True, True))
press(bot.build_call("toggleSetting", "auto_move_series"))
check("turning it off turns external off too", bot_settings.get("auto_move_series_external"), False)
check("so it never stays on while hidden", bot.auto_move_series_enabled(external=True), False)

# --- Settings screens ----------------------------------------------------------
# One screen per section, painted on the same message, each one with a way
# back to where it came from and a way out
def screen(call):
	press(call)
	return last()[1], [c for _, c in buttons(last()[2])]


def title_of(text):
	return text.split("\n", 1)[0]


fresh()
text, calls = screen(bot.build_call("settings"))
check("the bare settings call is the main screen", title_of(text), bot.get_text("SETTINGS_TITLE", "Fake 1.0"))
check("the main screen has one row per section", calls[:-2], [
	bot.build_call("settings", "speed"), bot.build_call("settings", "notify"), bot.build_call("settings", "add"),
	bot.build_call("settings", "rename"), bot.build_call("favDirsMenu"), bot.build_call("telemetryMenu")])
check("and goes back to the dashboard", calls[-2:], [bot.build_call("dashboard"), bot.build_call("cerrar")])
check("no toggle on the main screen", [c for c in calls if c.startswith("toggleSetting")], [])
labels = button_texts(last()[2])
check("the speed row tells the speed", labels[0], bot.get_text("SETTINGS_ROW_SPEED", bot.get_text("NO_LIMIT")))
check("the notices row counts them", labels[1], bot.get_text("SETTINGS_ROW_NOTIFY", 2, 4))
check("the add row tells the mode", labels[2], bot.get_text("SETTINGS_ROW_ADD", bot.get_text("ADD_MODE_ASK")))
check("the rename row tells its state", labels[3], bot.get_text("SETTINGS_ROW_RENAME", "❌"))
check("the favorites row counts them", labels[4], bot.get_text("SETTINGS_ROW_FAV_DIRS", 0))
check("an unknown screen is the main one", title_of(screen(bot.build_call("settings", "nope"))[0]), bot.get_text("SETTINGS_TITLE", "Fake 1.0"))

titles = {"speed": "SETTINGS_SPEED_TITLE", "notify": "SETTINGS_NOTIFY_TITLE", "add": "SETTINGS_ADD_TITLE", "rename": "SETTINGS_RENAME_TITLE"}
for name, key in titles.items():
	text, calls = screen(bot.build_call("settings", name))
	check(f"the {name} screen has its title", title_of(text), bot.get_text(key))
	check(f"the {name} screen goes back to the main one", calls[-2:], [bot.build_call("settings", "main"), bot.build_call("cerrar")])
	for call in calls:
		if call.startswith("toggleSetting"):
			text, _ = screen(call)
			check(f"{call} repaints the {name} screen", title_of(text), bot.get_text(key))
			screen(call)  # Back as it was

for call, key in ((bot.build_call("toggleAltSpeed"), "SETTINGS_SPEED_TITLE"), (bot.build_call("toggleDownLimit"), "SETTINGS_SPEED_TITLE"),
				(bot.build_call("toggleUpLimit"), "SETTINGS_SPEED_TITLE")):
	check(f"{call} repaints the speed screen", title_of(screen(call)[0]), bot.get_text(key))
fresh()
press(bot.build_call("toggleAltSpeed"))
check("turtle mode shows on the main row", button_texts(bot.build_settings()[1])[0],
	bot.get_text("SETTINGS_ROW_SPEED", bot.get_text("SPEED_VALUE_ALT")))
press(bot.build_call("toggleAltSpeed"))
CLIENT.set_speed_limit("down", 5000)
check("a limit shows on the main row", button_texts(bot.build_settings()[1])[0],
	bot.get_text("SETTINGS_ROW_SPEED", bot.get_text("SPEED_VALUE_LIMITS", "5000 KB/s", bot.get_text("NO_LIMIT"))))

fresh(auto_download=True)
text, calls = screen(bot.build_call("settings", "add"))
check("the automatic folder lives in the add screen", bot.build_call("autoDirMenu", 0) in calls, True)
text, calls = screen(bot.build_call("autoDirMenu", 0))
check("the automatic folder picker goes back to the add screen", bot.build_call("settings", "add") in calls, True)
press(bot.build_call("autoDirDefault"))
check("picking the default goes back to the add screen", title_of(last()[1]), bot.get_text("SETTINGS_ADD_TITLE"))
press(bot.build_call("autoDirSet", bot.get_dir_id("/media/incoming")))
check("picking a folder goes back to the add screen", title_of(last()[1]), bot.get_text("SETTINGS_ADD_TITLE"))
check("telling it", bot.get_text("SETTINGS_AUTO_DIR", "<code>/media/incoming</code>") in last()[1], True)
check("the add row tells it adds without asking", button_texts(bot.build_settings()[1])[2],
	bot.get_text("SETTINGS_ROW_ADD", bot.get_text("ADD_MODE_AUTO")))
bot.client.supports_categories = True
CLIENT.categories = [("Series", "/media/SERIES")]
text, calls = screen(bot.build_call("settings", "add"))
check("the automatic category lives in the add screen too", bot.build_call("autoCatMenu") in calls, True)
text, calls = screen(bot.build_call("autoCatMenu"))
check("its picker goes back to the add screen", bot.build_call("settings", "add") in calls, True)
press(bot.build_call("autoCatSet", bot.get_cat_id("Series")))
check("picking a category goes back to the add screen", title_of(last()[1]), bot.get_text("SETTINGS_ADD_TITLE"))
bot.client.supports_categories = False
fresh(auto_download=False)
text, calls = screen(bot.build_call("settings", "add"))
check("without the automatic download its options are hidden", [c for c in calls if c != bot.build_call("toggleSetting", "auto_download")][:-2], [])

text, calls = screen(bot.build_call("settings", "rename"))
check("the templates live in the rename screen", bot.build_call("tplMenu") in calls, True)
text, calls = screen(bot.build_call("tplMenu"))
check("and go back to it", bot.build_call("settings", "rename") in calls, True)
text, calls = screen(bot.build_call("favDirsMenu"))
check("the favorites go back to the main screen", bot.build_call("settings") in calls, True)
text, calls = screen(bot.build_call("telemetryMenu"))
check("the statistics go back to the main screen", bot.build_call("settings") in calls, True)

bot.client.supports_rename = False
text, calls = screen(bot.build_call("settings"))
check("a manager that cannot rename has no rename row", bot.build_call("settings", "rename") in calls, False)
check("nor rename screen", title_of(screen(bot.build_call("settings", "rename"))[0]), bot.get_text("SETTINGS_TITLE", "Fake 1.0"))
bot.client.supports_rename = True
bot.client.supports_alt_speed = False
text, calls = screen(bot.build_call("settings", "speed"))
check("a manager without turtle mode has no turtle button", bot.build_call("toggleAltSpeed") in calls, False)
check("nor turtle line", bot.get_text("SETTINGS_ALT_SPEED", "", "", "").split(":")[0] in text, False)
bot.client.supports_alt_speed = True

# The limits are typed: the answer comes back to the speed screen
SCREENS.clear()
bot.pending_inputs.clear()
press(bot.build_call("setDownLimit"))
pending = bot.pop_pending_input(1, 1)
check("typing a limit can be cancelled back to the speed screen", pending["back_call"], bot.build_call("settings", "speed"))
SENT_BEFORE = len(SCREENS)
message = type("M", (), {"chat": type("C", (), {"id": 1})(), "message_thread_id": None, "text": "800", "message_id": 9,
						"from_user": type("U", (), {"id": 1})()})()
bot.handle_pending_input(message, pending)
check("the limit is set", (CLIENT.session["speed_limit_down"], CLIENT.session["speed_limit_down_enabled"]), (800, True))
check("and the speed screen comes back", SCREENS[-1][1].startswith(f"{bot.get_text('SETTINGS_UPDATED')}\n\n{bot.get_text('SETTINGS_SPEED_TITLE')}"), True)

# --- Locales -----------------------------------------------------------------
import json
locales = {lang: json.load(open(os.path.join(REPO, "locale", f"{lang}.json"), encoding="utf-8")) for lang in ("es", "en")}
for name in ("SERIES_DIR_HINT", "SERIES_SUGGEST_ONE", "SERIES_SUGGEST_MANY", "SERIES_PLACED", "SERIES_MOVING", "SERIES_MOVED",
			"SERIES_EXPIRED", "BUTTON_SERIES_MOVE", "BUTTON_SERIES_UNDO", "SETTINGS_AUTO_MOVE_SERIES",
			"SETTINGS_AUTO_MOVE_SERIES_ON", "BUTTON_SETTING_AUTO_MOVE_SERIES", "BUTTON_SETTING_AUTO_MOVE_SERIES_EXTERNAL"):
	for lang, texts in locales.items():
		check(f"{name} is in {lang}", name in texts, True)
for lang, texts in locales.items():
	# The block at the end of an add message is found by how it starts: two
	# blocks starting the same way, or one that starts with a placeholder,
	# would make the buttons cut the wrong part
	prefixes = [texts[name].split("$", 1)[0] for name in bot.SERIES_BLOCK_KEYS]
	check(f"the series blocks start with text of their own in {lang}", all(prefixes), True)
	check(f"and differently in {lang}", len(set(prefixes)), len(prefixes))
	check(f"the add message keeps its 📂 line in {lang}", "\n📂 <code>$2</code>" in texts["ADD_OK"], True)
	check(f"and the external notice in {lang}", "\n📂 <code>$2</code>" in texts["NOTIFY_EXTERNAL_ADDED"], True)

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({CHECKS[0]} checks)")
