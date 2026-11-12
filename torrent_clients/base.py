"""
Abstraction layer between the bot and the torrent managers.
The bot only talks to this interface, so any torrent client
(Transmission, qBittorrent, Deluge...) can be plugged in by
implementing TorrentClient.
"""

import base64
import binascii
import hashlib
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import parse_qsl


class TorrentStatus:
	DOWNLOADING = "downloading"
	SEEDING = "seeding"
	PAUSED = "paused"
	CHECKING = "checking"
	QUEUED = "queued"
	ERROR = "error"

	ALL = (DOWNLOADING, SEEDING, PAUSED, CHECKING, QUEUED, ERROR)


# How the ratio limit of a torrent is decided (TorrentInfo.ratio_limit_mode)
RATIO_MODE_GLOBAL = "global"  # follows the setting of the manager
RATIO_MODE_TORRENT = "torrent"  # a limit of its own
RATIO_MODE_NONE = "none"  # seeds forever


# Whether and how eagerly a file of a torrent is downloaded (TorrentInfo.file_priorities)
FILE_SKIP = "skip"  # Not downloaded
FILE_LOW = "low"
FILE_NORMAL = "normal"
FILE_HIGH = "high"


class TorrentClientError(Exception):
	"""Normalized error raised by any torrent client implementation"""
	pass


class PermanentTorrentError(TorrentClientError):
	"""Failure that retrying cannot fix, because the torrent manager will not
	do what was asked no matter how many times it is told. Callers that retry
	on TorrentClientError have to give up on this one right away"""
	pass


class DuplicateTorrentError(PermanentTorrentError):
	"""The torrent being added is already in the manager. torrent_id is the id
	of the torrent that was already there, None when the manager does not say"""

	def __init__(self, message, torrent_id=None):
		super().__init__(message)
		self.torrent_id = torrent_id


class RestartRequiredError(PermanentTorrentError):
	"""The manager was left broken in a way only restarting it fixes, and until
	then it may neither list its torrents nor keep the ones added meanwhile
	(Deluge 2.2 removing a torrent only halfway). Retrying is no use: the
	manager has to be restarted"""
	pass


def _fold(text):
	"""Lowercase text without accents, so 'Películas' and 'PELICULAS' compare equal"""
	text = unicodedata.normalize("NFKD", text or "")
	return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def _search_words(text):
	"""Lowercase words of a text without accents or punctuation, so
	'La.Casa.de.Papel' and 'casa papel' share the same words"""
	return re.findall(r'[^\W_]+', _fold(text))


# A query term: a phrase between straight, curly or angle quotes (Telegram
# apps turn typed quotes into curly ones), or anything up to the next space
_QUERY_TERM = re.compile(r'"([^"]*)"|“([^”]*)”|«([^»]*)»|(\S+)')
_QUOTES = '"“”«»'


def query_matcher(query):
	"""Predicate telling whether the given texts (name, download directory...)
	match the query, or None when there is no query. Case and accents never
	matter, and every term of the query has to match, in any order:
	- a word matches any part of a word, ignoring separators, so
	  'casa papel' finds 'La.Casa.de.Papel'
	- a phrase between quotes has to appear as is, word after word, so
	  '"la casa"' finds 'La.Casa.de.Papel' but not 'Casa.La'
	- a term with a '/' is a path and has to appear as is, slashes
	  included, so '/ssd/peliculas' finds '/ssd/Películas' but not
	  '/media/PELICULAS'"""
	if not query:
		return None
	words, phrases, raws = [], [], []
	for match in _QUERY_TERM.finditer(query):
		phrase = next((group for group in match.groups()[:3] if group is not None), None)
		if phrase is not None:
			if _search_words(phrase):
				phrases.append(" ".join(_search_words(phrase)))
			continue
		term = match.group(4).strip(_QUOTES)
		if "/" in term or not _search_words(term):
			# Paths keep their separators; so does a term made only of
			# punctuation, which otherwise would have nothing to look for
			if term:
				raws.append(_fold(term))
		else:
			words.extend(_search_words(term))
	if not (words or phrases or raws):
		return None

	def matches(*texts):
		folded = [_fold(text) for text in texts]
		# A trailing slash in a path term still finds a directory written without it
		folded += [text + "/" for text in folded if text and not text.endswith("/")]
		haystack = " " + " | ".join(" ".join(_search_words(text)) for text in texts) + " "
		return (all(word in haystack for word in words)
			and all(f" {phrase} " in haystack for phrase in phrases)
			and all(any(raw in text for text in folded) for raw in raws))
	return matches


def sort_files(files):
	"""Sorts a list of (path, size, completed) alphabetically by path,
	so files inside a folder keep a predictable order"""
	files.sort(key=lambda f: f[0].lower())
	return files


def content_root(paths, fallback=""):
	"""Top level folder shared by every file path, or an empty string when the
	content is not inside a single folder. Torrent managers sanitize the paths
	they write on disk (trailing dots and spaces, reserved characters...), so
	the torrent name is not always a usable path: it has to be read back from
	the file list. Returns fallback when there are no paths to look at"""
	roots = set()
	for path in paths:
		if path:
			roots.add(path.split("/", 1)[0] if "/" in path else "")
	if not roots:
		return fallback
	if len(roots) == 1:
		return roots.pop()
	return ""


def clean_ratio(value):
	"""Share ratio as the bot shows and sorts it, rounded. Managers answer
	'no ratio yet' with negative sentinels (Transmission's -1 and -2, Deluge's
	-1 while nothing is downloaded): those are 0 here, so a torrent without
	data never sorts below one that really has a ratio"""
	try:
		value = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(value, 2) if value > 0 else 0.0


def capped_progress(percent, left):
	"""Progress (0-100) as the bot shows it, rounded. Rounding must never make
	it 100 while the manager says some wanted bytes are still missing (left
	above 0): 100 is finished, and the bot acts on that. Managers only report
	a share (a big torrent lacking a few MB is at 99.999 %), so the bytes left
	are what tells. left is None when the manager does not say"""
	try:
		percent = min(max(round(float(percent or 0), 2), 0.0), 100.0)
	except (TypeError, ValueError):
		return 0.0
	if isinstance(left, (int, float)) and not isinstance(left, bool) and left > 0:
		return min(percent, 99.99)
	return percent


def _bencode_end(data, start):
	"""Where the bencoded value that starts at start ends. Broken data raises
	ValueError: a negative or oversized length must never step backwards (an
	endless loop) nor read past the end"""
	kind = data[start:start + 1]
	if kind == b"i":
		return data.index(b"e", start) + 1
	if kind in (b"l", b"d"):
		position = start + 1
		while data[position:position + 1] != b"e":
			if position >= len(data):
				raise ValueError("Unterminated bencoded list")
			position = _bencode_end(data, position)
		return position + 1
	colon = data.index(b":", start)
	length = data[start:colon]
	if not length.isdigit():
		raise ValueError("Bad bencoded string length")
	end = colon + 1 + int(length)
	if end > len(data):
		raise ValueError("Truncated bencoded string")
	return end


def _bencode_entries(data):
	"""(key, value start, value end) of each entry of a bencoded dictionary"""
	if data[:1] != b"d":
		raise ValueError("Not a bencoded dictionary")
	position = 1
	while data[position:position + 1] != b"e":
		if position >= len(data):
			raise ValueError("Unterminated bencoded dictionary")
		key_end = _bencode_end(data, position)
		value_end = _bencode_end(data, key_end)
		yield data[data.index(b":", position) + 1:key_end], key_end, value_end
		position = value_end


# Exact topic of a magnet link (xt, xt.1...) and the info hash it gives: v1
# (btih, in hex or base32) or v2 (btmh, a SHA-256 multihash)
_MAGNET_TOPIC = re.compile(r"xt(?:\.\d+)?", re.IGNORECASE)
_MAGNET_V1 = re.compile(r"urn:btih:([0-9a-z]{32}|[0-9a-z]{40})", re.IGNORECASE)
_MAGNET_V2 = re.compile(r"urn:btmh:1220([0-9a-f]{64})", re.IGNORECASE)


def expected_torrent_ids(magnet=None, torrent_data=None):
	"""Ids (lowercase hex info hashes) the torrent of a magnet link or of the
	content of a .torrent file can have once added, an empty list when they
	cannot be known. A torrent of BitTorrent v2 can have two: managers built
	on libtorrent 2 name hybrid and v2 torrents after the SHA-256 info hash
	cut to 40 digits, and the rest after the SHA-1 one"""
	ids = []
	if isinstance(magnet, str) and magnet:
		v2_ids = []
		# Parameter by parameter and decoded, since managers built on libtorrent
		# also read a percent-encoded topic (xt=urn%3Abtih%3A...)
		for key, value in parse_qsl(magnet.split("?", 1)[-1]):
			if not _MAGNET_TOPIC.fullmatch(key.strip()):
				continue
			v1, v2 = _MAGNET_V1.fullmatch(value.strip()), _MAGNET_V2.fullmatch(value.strip())
			if v1 and len(v1.group(1)) == 40 and re.fullmatch(r"[0-9a-fA-F]+", v1.group(1)):
				ids.append(v1.group(1).lower())
			elif v1 and len(v1.group(1)) == 32:
				try:
					ids.append(base64.b32decode(v1.group(1).upper()).hex())
				except (binascii.Error, ValueError):
					pass
			elif v2:
				v2_ids.append(v2.group(1)[:40].lower())
		ids.extend(v2_ids)  # After the v1 one, which is the id Transmission gives
	elif isinstance(torrent_data, (bytes, bytearray)):
		try:
			for key, start, end in _bencode_entries(torrent_data):
				if key == b"info":
					info = bytes(torrent_data[start:end])
					ids.append(hashlib.sha1(info).hexdigest())
					if any(k == b"meta version" and info[s:e] == b"i2e" for k, s, e in _bencode_entries(info)):
						ids.append(hashlib.sha256(info).hexdigest()[:40])
					break
		except (ValueError, IndexError, RecursionError):
			return []
	return list(dict.fromkeys(ids))


@dataclass
class TorrentInfo:
	id: str  # Must survive client restarts: it travels in buttons and the monitor tracks torrents by it
	name: str
	status: str
	progress: float = 0.0  # 0-100
	total_size: int = 0  # bytes (size when done)
	downloaded: int = 0  # bytes
	uploaded: int = 0  # bytes
	download_rate: int = 0  # bytes/s
	upload_rate: int = 0  # bytes/s
	eta: int = -1  # seconds, -1 unknown. While seeding: until the seeding limit is reached
	ratio: float = 0.0
	ratio_limit: float = -1.0  # ratio at which the client stops seeding, -1 none or unknown
	ratio_limit_mode: str = ""  # RATIO_MODE_*, empty when unknown
	download_limit: int = -1  # KB/s this torrent may download at, -1 none or unknown
	upload_limit: int = -1  # KB/s this torrent may upload at, -1 none or unknown
	seeding_time: int = -1  # seconds spent seeding, -1 unknown
	seeding_time_limit: int = -1  # seconds of seeding before the client stops it, -1 none or unknown
	peers: int = 0
	seeders: int = 0  # connected peers that have the whole torrent
	leechers: int = 0  # connected peers still downloading
	swarm_seeders: int = -1  # seeders in the whole swarm as the tracker reports them, -1 unknown
	swarm_leechers: int = -1  # leechers in the whole swarm as the tracker reports them, -1 unknown
	availability: float = -1.0  # distributed copies among connected peers, -1 unknown
	download_dir: str = ""
	category: str = ""  # Managers without categories leave it empty
	auto_managed: bool = False  # The manager decides the location (qBittorrent auto_tmm)
	error_message: str = ""
	error_is_tracker: bool = False  # The error is the tracker's (Transmission tells it like one of the data): the torrent itself is fine
	tracker_error: str = ""  # Message of the tracker when none of them answers right (e.g. Unregistered torrent)
	private: bool = False
	added_date: object = None  # unix timestamp
	completed_date: object = None  # unix timestamp, None while not finished or unknown
	last_activity: object = None  # unix timestamp of the last transfer, None never or unknown
	files: list = field(default_factory=list)  # list of (path, size, completed)
	file_priorities: dict = field(default_factory=dict)  # {path: FILE_*} of the files, empty when unknown
	trackers: list = field(default_factory=list)  # list of tracker hostnames

	@property
	def is_finished(self):
		return self.progress >= 100.0


@dataclass
class SessionSummary:
	counts: dict = field(default_factory=dict)  # {TorrentStatus: int}
	total: int = 0
	completed: int = 0
	uploading: int = 0  # torrents actually uploading right now (upload rate > 0)
	downloading: int = 0  # torrents actually downloading right now (download rate > 0)
	download_rate: int = 0  # bytes/s
	upload_rate: int = 0  # bytes/s
	free_space: int = -1  # bytes, -1 unknown
	alt_speed_enabled: bool = False
	# Bytes transferred, the time running and the number of times started,
	# since the manager was last started (session_*) and in all its history
	# (total_*). -1 when the manager does not report it
	session_downloaded: int = -1
	session_uploaded: int = -1
	session_seconds: int = -1
	total_downloaded: int = -1
	total_uploaded: int = -1
	total_seconds: int = -1
	session_count: int = -1


class TorrentClient(ABC):
	"""Interface that every torrent manager implementation must fulfill"""

	# Capabilities: implementations that lack a feature switch it off and the bot
	# hides the buttons that would use it
	supports_alt_speed = True  # Clients without a turtle mode set this to False
	supports_rename = True  # Clients that cannot rename the content on disk
	supports_verify = True  # Clients that cannot recheck the downloaded data
	supports_categories = False  # Clients with categories/labels bound to a save path
	supports_ratio_limit = True  # Clients that cannot set the ratio limit of a single torrent, nor the global one
	ratio_limit_copied_on_add = False  # The global ratio limit is copied into each torrent when added (Deluge)
	supports_torrent_speed_limit = True  # Clients that cannot limit the speed of a single torrent
	supports_reannounce = True  # Clients that cannot ask the trackers for peers on demand
	reports_last_activity = True  # Clients whose listing has no time of the last transfer
	# Priorities a file can be given, FILE_SKIP included. Empty when the client
	# cannot choose which files are downloaded
	file_priority_levels = (FILE_SKIP, FILE_LOW, FILE_NORMAL, FILE_HIGH)
	skipped_files_in_size = False  # The size of a torrent still counts the files left out

	@abstractmethod
	def test_connection(self):
		"""Returns the client version string. Raises TorrentClientError if unreachable"""
		pass

	@abstractmethod
	def get_summary(self):
		"""Returns a SessionSummary with counters per status and global rates"""
		pass

	@abstractmethod
	def get_torrents(self, status=None, query=None):
		"""Returns a list of TorrentInfo (light fields). Optionally filtered
		by normalized status (TorrentStatus) and/or by a query matched
		against the name and the download directory (see query_matcher)"""
		pass

	@abstractmethod
	def get_torrent(self, torrent_id):
		"""Returns a full TorrentInfo (including files) or None if not found"""
		pass

	@abstractmethod
	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None, category=None):
		"""Adds a torrent from a magnet link or the binary content of a
		.torrent file. Returns the TorrentInfo of the added torrent.
		category is only ever passed to clients with supports_categories, and
		it replaces download_dir: the category is what decides the location"""
		pass

	def get_categories(self):
		"""Returns [(name, save_path)] of the categories defined in the manager,
		sorted by name. save_path may be empty when the category does not force
		one. Only meaningful when supports_categories is True"""
		return []

	def set_auto_managed(self, torrent_ids, enabled):
		"""Hands the location of the torrents over to the manager (or takes it
		back). Enabling it relocates the data to the folder of the torrent's
		category, or to the default one when it has no category, so it blocks
		for as long as a move does"""
		raise TorrentClientError("This torrent manager does not support automatic management")

	def set_category(self, torrent_ids, category):
		"""Assigns a category to the torrents, an empty string clears it.
		On an auto managed torrent this physically moves its data, so it can
		block for as long as a move does"""
		raise TorrentClientError("This torrent manager does not support categories")

	def set_ratio_limit(self, torrent_ids, limit):
		"""Sets the ratio at which the torrents stop seeding: None hands them
		back to the setting of the manager, -1 lets them seed forever and any
		other value is a limit of their own. Leaves the seeding time limits
		as they are"""
		raise TorrentClientError("This torrent manager does not support ratio limits")

	def set_torrent_speed_limit(self, torrent_ids, direction, kbps):
		"""Limits the speed of the torrents alone. direction: 'down'|'up';
		kbps: KB/s, 0 or less removes the limit"""
		raise TorrentClientError("This torrent manager does not support speed limits per torrent")

	def set_file_priority(self, torrent_id, paths, priority):
		"""Sets whether and how eagerly the files of a torrent are downloaded.
		paths are paths as reported in TorrentInfo.files and priority one of
		file_priority_levels, FILE_SKIP to leave them out of the download"""
		raise TorrentClientError("This torrent manager cannot choose the files to download")

	def reannounce_torrents(self, torrent_ids):
		"""Asks the trackers for peers right now instead of waiting for the next announce"""
		raise TorrentClientError("This torrent manager does not support reannouncing")

	def get_global_ratio_limit(self):
		"""Ratio at which the torrents that follow the setting of the manager
		stop seeding, -1 when they seed forever"""
		raise TorrentClientError("This torrent manager does not support ratio limits")

	def set_global_ratio_limit(self, limit):
		"""Sets the ratio of get_global_ratio_limit, -1 to seed forever"""
		raise TorrentClientError("This torrent manager does not support ratio limits")

	@abstractmethod
	def remove_torrents(self, torrent_ids, delete_data=False):
		"""Removes torrents, optionally deleting the downloaded data"""
		pass

	@abstractmethod
	def pause_torrents(self, torrent_ids):
		pass

	@abstractmethod
	def resume_torrents(self, torrent_ids):
		pass

	@abstractmethod
	def verify_torrent(self, torrent_id):
		pass

	@abstractmethod
	def rename_torrent(self, torrent_id, new_name):
		"""Renames the torrent (top level file/folder) while keeping it shared"""
		pass

	@abstractmethod
	def rename_file(self, torrent_id, old_path, new_name):
		"""Renames a single file inside the torrent. old_path is the path as
		reported in TorrentInfo.files and new_name is only the new file name,
		the file stays in the same folder"""
		pass

	@abstractmethod
	def rename_folder(self, torrent_id, old_path, new_name):
		"""Renames a folder inside the torrent, never the top level one (that is
		rename_torrent). old_path is the folder path as it shows in the paths of
		TorrentInfo.files and new_name is only the new folder name"""
		pass

	@abstractmethod
	def move_torrents(self, torrent_ids, new_dir):
		"""Moves the downloaded data of the torrents to another directory"""
		pass

	@abstractmethod
	def get_download_dirs(self):
		"""Returns the list of download directories in use, most used first"""
		pass

	@abstractmethod
	def get_default_download_dir(self):
		pass

	@abstractmethod
	def get_free_space(self, path):
		"""Returns free bytes at path or -1 if unknown"""
		pass

	@abstractmethod
	def get_settings(self):
		"""Returns a normalized settings dict:
		{version, alt_speed_enabled, speed_limit_down, speed_limit_down_enabled,
		 speed_limit_up, speed_limit_up_enabled, download_dir}"""
		pass

	@abstractmethod
	def set_alt_speed(self, enabled):
		pass

	@abstractmethod
	def set_speed_limit(self, direction, kbps, enabled):
		"""direction: 'down'|'up'; kbps: int or None to keep current value"""
		pass
