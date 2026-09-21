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
DONORS_URL = "https://donate.dgongut.com/donors.json"
LOCALE_PATH = os.environ.get("LOCALE_PATH", "/app/locale")
CONFIG_PATH = "/config"  # Persistent bot settings (mapped as a volume)
MAX_DIR_BUTTONS = 10
MAX_NAME_LENGTH_IN_BUTTON = 35
MAX_CALLBACK_DATA_BYTES = 64  # Telegram hard limit for inline button callback_data
MAX_MESSAGE_LENGTH = 4096  # Telegram hard limit for a message body
SEARCH_CONTEXT_TTL = 3600  # Seconds a search/filter context is kept in memory
MOVE_RETRY_DELAY = 60  # Seconds between retries when delivering a move order
MOVE_RETRY_ATTEMPTS = 120  # Retries before giving up (client busy moving data)
MONITOR_INTERVAL_SECONDS = 30  # Seconds between torrent monitor polls (completed/error notifications)
AUTO_RENAME_WAIT_DELAY = 5  # Seconds between checks while a magnet downloads its metadata
AUTO_RENAME_WAIT_ATTEMPTS = 60  # Checks before giving up on the deferred auto-rename
MAX_TRACKER_BUTTONS = 25  # Max trackers listed in the tracker filter menu
CATEGORIES_PER_PAGE = 10  # Categories listed per page in the category screens
CATEGORIES_CACHE_TTL = 10  # Seconds the category list is reused before asking again
FILES_PER_PAGE = 8  # Files listed per page in the torrent files screen
MAX_PLAN_PREVIEW_LINES = 12  # Renames shown in the batch rename preview
URL_DOWNLOAD_TIMEOUT = 10  # Seconds before aborting a .torrent URL download
URL_DOWNLOAD_MAX_BYTES = 10 * 1024 * 1024  # A .torrent bigger than this is rejected

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

