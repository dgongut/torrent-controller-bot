import os

# DOCKER ENVIRONMENT VARIABLES
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN")
TELEGRAM_GROUP = os.environ.get("TELEGRAM_GROUP")
TELEGRAM_THREAD = os.environ.get("TELEGRAM_THREAD", "1")
LANGUAGE = os.environ.get("LANGUAGE", "ES")
TORRENTS_PER_PAGE = int(os.environ.get("TORRENTS_PER_PAGE", "10"))
DASHBOARD_REFRESH_SECONDS = int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "2"))
DASHBOARD_REFRESH_DURATION = int(os.environ.get("DASHBOARD_REFRESH_DURATION", "60"))
DOWNLOAD_DIRS = os.environ.get("DOWNLOAD_DIRS", "")  # Extra download dirs, comma separated

# TORRENT CLIENT (generic, valid for any supported manager)
TORRENT_CLIENT = os.environ.get("TORRENT_CLIENT", "transmission")
TORRENT_CLIENT_HOST = os.environ.get("TORRENT_CLIENT_HOST", "localhost")
TORRENT_CLIENT_PORT = os.environ.get("TORRENT_CLIENT_PORT")  # Default depends on the client
TORRENT_CLIENT_USER = os.environ.get("TORRENT_CLIENT_USER")
TORRENT_CLIENT_PASSWORD = os.environ.get("TORRENT_CLIENT_PASSWORD")
TORRENT_CLIENT_PROTOCOL = os.environ.get("TORRENT_CLIENT_PROTOCOL", "http")
TORRENT_CLIENT_RPC_PATH = os.environ.get("TORRENT_CLIENT_RPC_PATH")  # Default depends on the client

# CONSTANTS
ANONYMOUS_USER_ID = "1087968824"
LOCALE_PATH = os.environ.get("LOCALE_PATH", "/app/locale")
MAX_DIR_BUTTONS = 10
MAX_NAME_LENGTH_IN_BUTTON = 35
SEARCH_CONTEXT_TTL = 3600  # Seconds a search/filter context is kept in memory
MOVE_RETRY_DELAY = 60  # Seconds between retries when delivering a move order
MOVE_RETRY_ATTEMPTS = 120  # Retries before giving up (client busy moving data)

# TORRENT STATUS FILTER CODES (short to fit in callback_data)
FILTER_ALL = "al"
FILTER_DOWNLOADING = "dl"
FILTER_SEEDING = "sd"
FILTER_PAUSED = "ps"
FILTER_COMPLETED = "co"
FILTER_QUEUED = "qu"
FILTER_CHECKING = "ck"
FILTER_ERROR = "er"

CALL_PATTERNS = {
	"dashboard": [],
	"refreshDashboard": [],
	"list": ["filterKey", "page"],
	"noop": [],
	"info": ["torrentId", "filterKey", "page"],
	"pause": ["torrentId", "filterKey", "page"],
	"resume": ["torrentId", "filterKey", "page"],
	"verify": ["torrentId", "filterKey", "page"],
	"delete": ["torrentId", "filterKey", "page"],
	"confirmDelete": ["torrentId", "withData", "filterKey", "page"],
	"rename": ["torrentId", "filterKey", "page"],
	"renameAuto": ["torrentId", "filterKey", "page"],
	"renameManual": ["torrentId", "filterKey", "page"],
	"move": ["torrentId", "filterKey", "page"],
	"moveToDir": ["torrentId", "dirId"],
	"moveNewDir": ["torrentId"],
	"search": [],
	"addTo": ["pendingId", "dirId"],
	"addNewDir": ["pendingId"],
	"cancelAdd": ["pendingId"],
	"mass": ["action", "filterKey"],
	"confirmMass": ["action", "filterKey", "extra"],
	"massMoveDir": ["filterKey", "dirId"],
	"massMoveNew": ["filterKey"],
	"settings": [],
	"toggleAltSpeed": [],
	"toggleDownLimit": [],
	"toggleUpLimit": [],
	"setDownLimit": [],
	"setUpLimit": [],
	"cancelInput": [],
	"cerrar": [],
}
