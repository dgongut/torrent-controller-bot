import os

# What was wrong in the environment and got a default instead: the bot logs
# it on start (this module has no logger, it is imported by everything)
CONFIG_WARNINGS = []


def _env(name, default=None):
	"""The variable without surrounding spaces, default when unset or empty: a
	compose file often has 'NAME=' for the optional ones"""
	value = (os.environ.get(name) or "").strip()
	return value if value else default


def _env_int(name, default, minimum, maximum):
	value = _env(name)
	if value is None:
		return default
	try:
		number = int(value)
	except ValueError:
		CONFIG_WARNINGS.append(f"{name}={value!r} is not a number, using {default}")
		return default
	if not minimum <= number <= maximum:
		clamped = min(max(number, minimum), maximum)
		CONFIG_WARNINGS.append(f"{name}={number} is out of range ({minimum}-{maximum}), using {clamped}")
		return clamped
	return number


# DOCKER ENVIRONMENT VARIABLES
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN")
TELEGRAM_GROUP = os.environ.get("TELEGRAM_GROUP")
TELEGRAM_THREAD = os.environ.get("TELEGRAM_THREAD", "1")
LANGUAGE = os.environ.get("LANGUAGE", "ES")
# A list keyboard holds the torrents plus up to 8 more buttons (pages, sort,
# actions, update, back, close), and Telegram refuses more than 100 buttons
TORRENTS_PER_PAGE = _env_int("TORRENTS_PER_PAGE", 10, 1, 90)

# Screens that repaint themselves (dashboard, lists, torrent and file screens):
# every few seconds for a minute, then the update button takes over. Internal,
# no longer read from the environment (the bot warns when they are still set)
DASHBOARD_REFRESH_SECONDS = 2
DASHBOARD_REFRESH_DURATION = 60
REMOVED_VARIABLES = ("DASHBOARD_REFRESH_SECONDS", "DASHBOARD_REFRESH_DURATION")

# TORRENT CLIENT (generic, valid for any supported manager)
TORRENT_CLIENT = _env("TORRENT_CLIENT", "transmission")
TORRENT_CLIENT_HOST = _env("TORRENT_CLIENT_HOST", "localhost")
TORRENT_CLIENT_PORT = _env("TORRENT_CLIENT_PORT")  # Default depends on the client
TORRENT_CLIENT_USER = os.environ.get("TORRENT_CLIENT_USER")
TORRENT_CLIENT_PASSWORD = os.environ.get("TORRENT_CLIENT_PASSWORD")
TORRENT_CLIENT_PROTOCOL = _env("TORRENT_CLIENT_PROTOCOL", "http").lower()
if TORRENT_CLIENT_PROTOCOL not in ("http", "https"):
	CONFIG_WARNINGS.append(f"TORRENT_CLIENT_PROTOCOL={TORRENT_CLIENT_PROTOCOL!r} is not http or https, using http")
	TORRENT_CLIENT_PROTOCOL = "http"
if TORRENT_CLIENT_PORT is not None and not TORRENT_CLIENT_PORT.isdigit():
	CONFIG_WARNINGS.append(f"TORRENT_CLIENT_PORT={TORRENT_CLIENT_PORT!r} is not a number, using the default port of the client")
	TORRENT_CLIENT_PORT = None
TORRENT_CLIENT_RPC_PATH = _env("TORRENT_CLIENT_RPC_PATH")  # Default depends on the client

# Anonymous usage statistics. On by default, turned off from /settings or with
# TELEMETRY=false, which telemetry.py reads itself: it is the same variable in
# every project that shares that client.
# Development only, not documented: sends a minute after every start instead
# of once a day. Debug pings go to a server on the same machine unless told
# otherwise, never to the real one, where they would count as installations.
TELEMETRY_DEBUG = os.environ.get("TELEMETRY_DEBUG", "0").strip().lower() in ("1", "true", "yes")
TELEMETRY_ENDPOINT = os.environ.get(
	"TELEMETRY_ENDPOINT",
	"http://host.docker.internal:8000/v1/ping" if TELEMETRY_DEBUG else "https://telemetry.dgongut.com/v1/ping")

# CONSTANTS
ANONYMOUS_USER_ID = "1087968824"
DONORS_URL = "https://donate.dgongut.com/donors.json"
LOCALE_PATH = os.environ.get("LOCALE_PATH", "/app/locale")
CONFIG_PATH = "/config"  # Persistent bot settings (mapped as a volume)
MAX_DIR_BUTTONS = 10
MAX_NAME_LENGTH_IN_BUTTON = 35
MAX_CALLBACK_DATA_BYTES = 64  # Telegram hard limit for inline button callback_data
MAX_MESSAGE_LENGTH = 4096  # Telegram hard limit for a message body
SEARCH_CONTEXT_TTL = 3600  # Seconds a search/filter context is kept in memory
INPUT_TTL = 600  # Seconds the bot waits for the answer to a question it asked (a path, a name...)
MOVE_RETRY_DELAY = 60  # Seconds between retries when delivering a move order
MOVE_RETRY_ATTEMPTS = 120  # Retries before giving up (client busy moving data)
MONITOR_INTERVAL_SECONDS = 30  # Seconds between torrent monitor polls (completed/error notifications)
AUTO_MOVER_INTERVAL_SECONDS = 300  # Seconds between checks of the free space of the automatic mover
AUTO_MOVER_WATCH_SECONDS = 10  # Seconds between looks at the torrents the mover ordered to move
AUTO_MOVER_GRACE_SECONDS = 90  # Seconds a torrent may stay in the source without being moved before its move is taken as failed
AUTO_MOVER_SETTLE_SECONDS = 120  # Seconds after a move before the free space is trusted again: qBittorrent takes about half a minute to tell what it freed
AUTO_MOVER_TIMEOUT = 24 * 3600  # Seconds the manager may take to move what the mover ordered: a terabyte to a slow disk is many hours
NOTICE_SECONDS = 10  # How long a passing notice (a cancelled question) stays before the bot deletes it
AUTO_MOVER_MAX_LISTED = 15  # Torrents named in a message of the automatic mover, the rest are counted
AUTO_RENAME_WAIT_DELAY = 5  # Seconds between checks while a magnet downloads its metadata
AUTO_RENAME_WAIT_ATTEMPTS = 60  # Checks before giving up on the deferred auto-rename
REPLACE_WAIT_DELAY = 0.5  # Seconds between checks while the replaced torrent is being removed
REPLACE_WAIT_ATTEMPTS = 40  # Checks before renaming anyway (the rename itself is retried as long)
MAX_TRACKER_BUTTONS = 25  # Max trackers listed in the tracker filter menu
TRACKER_RULES_PER_PAGE = 20  # Tracker limit rules listed per page in the settings
MAX_REMEMBERED_DIRS = 500  # Folders whose buttons keep working after a restart (CONFIG_PATH/dir_ids.json)
CATEGORIES_PER_PAGE = 10  # Categories listed per page in the category screens
CATEGORIES_CACHE_TTL = 10  # Seconds the category list is reused before asking again
FILES_PER_PAGE = 8  # Files listed per page in the torrent files screen
MAX_PLAN_PREVIEW_LINES = 12  # Renames shown in the batch rename preview
URL_DOWNLOAD_TIMEOUT = 10  # Seconds the server of a .torrent URL may stay silent
URL_DOWNLOAD_DEADLINE = 60  # Seconds a whole .torrent URL download may take
URL_MAX_REDIRECTS = 5  # Redirects followed when downloading a .torrent URL
URL_DOWNLOAD_MAX_BYTES = 10 * 1024 * 1024  # A .torrent bigger than this is rejected
TELEGRAM_DOWNLOAD_ATTEMPTS = 3  # Tries to fetch a .torrent sent to the bot before giving up
TELEGRAM_DOWNLOAD_RETRY_DELAY = 3  # Seconds between those tries
HEARTBEAT_PATH = "/tmp/torrent-controller-bot.heartbeat"  # Touched on every successful poll, read by the Docker HEALTHCHECK (keep both in sync)

# TORRENT STATUS FILTER CODES (short to fit in callback_data)
FILTER_ALL = "al"
FILTER_DOWNLOADING = "dl"
FILTER_SEEDING = "sd"
FILTER_UPLOADING = "up"  # Seeding torrents with actual upload traffic right now
FILTER_DOWNLOADING_NOW = "dn"  # Torrents with actual download traffic right now
FILTER_PAUSED = "ps"
FILTER_COMPLETED = "co"
FILTER_QUEUED = "qu"
FILTER_CHECKING = "ck"
FILTER_ERROR = "er"

