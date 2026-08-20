"""
Abstraction layer between the bot and the torrent managers.
The bot only talks to this interface, so any torrent client
(Transmission, qBittorrent, Deluge...) can be plugged in by
implementing TorrentClient.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class TorrentStatus:
	DOWNLOADING = "downloading"
	SEEDING = "seeding"
	PAUSED = "paused"
	CHECKING = "checking"
	QUEUED = "queued"
	ERROR = "error"

	ALL = (DOWNLOADING, SEEDING, PAUSED, CHECKING, QUEUED, ERROR)


class TorrentClientError(Exception):
	"""Normalized error raised by any torrent client implementation"""
	pass


@dataclass
class TorrentInfo:
	id: str
	name: str
	status: str
	progress: float = 0.0  # 0-100
	total_size: int = 0  # bytes (size when done)
	downloaded: int = 0  # bytes
	uploaded: int = 0  # bytes
	download_rate: int = 0  # bytes/s
	upload_rate: int = 0  # bytes/s
	eta: int = -1  # seconds, -1 unknown
	ratio: float = 0.0
	peers: int = 0
	seeders: int = 0  # connected peers that have the whole torrent
	leechers: int = 0  # connected peers still downloading
	download_dir: str = ""
	error_message: str = ""
	added_date: object = None  # datetime
	files: list = field(default_factory=list)  # list of (path, size, completed)
	trackers: list = field(default_factory=list)  # list of tracker hostnames

	@property
	def is_finished(self):
		return self.progress >= 100.0


@dataclass
class SessionSummary:
	counts: dict = field(default_factory=dict)  # {TorrentStatus: int}
	total: int = 0
	completed: int = 0
	download_rate: int = 0  # bytes/s
	upload_rate: int = 0  # bytes/s
	free_space: int = -1  # bytes, -1 unknown
	alt_speed_enabled: bool = False


class TorrentClient(ABC):
	"""Interface that every torrent manager implementation must fulfill"""

	supports_alt_speed = True  # Clients without a turtle mode set this to False

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
		by normalized status (TorrentStatus) and/or by substring matching
		the name or the download directory"""
		pass

	@abstractmethod
	def get_torrent(self, torrent_id):
		"""Returns a full TorrentInfo (including files) or None if not found"""
		pass

	@abstractmethod
	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None):
		"""Adds a torrent from a magnet link or the binary content of a
		.torrent file. Returns the TorrentInfo of the added torrent"""
		pass

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
