"""Transmission implementation of the TorrentClient interface"""

from collections import Counter
from urllib.parse import urlparse

import transmission_rpc

from torrent_clients.base import (
	SessionSummary,
	TorrentClient,
	TorrentClientError,
	TorrentInfo,
	TorrentStatus,
)

# Light fields requested when listing (keeps RPC payload small with thousands of torrents)
LIGHT_FIELDS = [
	"id", "name", "status", "error", "errorString", "percentDone",
	"sizeWhenDone", "rateDownload", "rateUpload", "downloadDir", "addedDate",
	"trackers",
]

FULL_FIELDS = LIGHT_FIELDS + [
	"totalSize", "downloadedEver", "uploadedEver", "uploadRatio", "eta",
	"peersConnected", "files", "hashString", "isFinished",
]

SUMMARY_FIELDS = ["id", "status", "error", "percentDone", "rateDownload", "rateUpload"]

# Generous timeout for move orders: the daemon can be slow to answer the RPC
# while it is relocating large amounts of data
MOVE_RPC_TIMEOUT = 300

# Transmission status (string) -> normalized status
STATUS_MAP = {
	"stopped": TorrentStatus.PAUSED,
	"check pending": TorrentStatus.CHECKING,
	"checking": TorrentStatus.CHECKING,
	"download pending": TorrentStatus.QUEUED,
	"downloading": TorrentStatus.DOWNLOADING,
	"seed pending": TorrentStatus.QUEUED,
	"seeding": TorrentStatus.SEEDING,
}


class TransmissionClient(TorrentClient):
	def __init__(self, host, port, username=None, password=None, protocol="http", rpc_path="/transmission/rpc"):
		try:
			self.client = transmission_rpc.Client(
				host=host,
				port=port,
				username=username if username else None,
				password=password if password else None,
				protocol=protocol,
				path=rpc_path,
			)
		except Exception as e:
			raise TorrentClientError(f"Cannot connect to Transmission at {host}:{port}: {e}")

	def _raw(self, torrent, field, default=None):
		try:
			return torrent.fields.get(field, default)
		except Exception:
			return default

	def _normalize_status(self, torrent):
		if self._raw(torrent, "error", 0):
			return TorrentStatus.ERROR
		status = str(torrent.status)
		return STATUS_MAP.get(status, TorrentStatus.PAUSED)

	def _to_info(self, torrent, full=False):
		files = []
		if full:
			try:
				for f in torrent.get_files():
					files.append((f.name, f.size, f.completed))
			except Exception:
				files = []
		trackers = []
		for tracker in self._raw(torrent, "trackers", []) or []:
			try:
				host = urlparse(tracker.get("announce", "")).hostname
			except Exception:
				host = None
			if host and host not in trackers:
				trackers.append(host)
		eta = self._raw(torrent, "eta", -1)
		info = TorrentInfo(
			id=str(torrent.id),
			name=torrent.name,
			status=self._normalize_status(torrent),
			progress=round(self._raw(torrent, "percentDone", 0) * 100, 2),
			total_size=self._raw(torrent, "sizeWhenDone", 0) or self._raw(torrent, "totalSize", 0),
			downloaded=self._raw(torrent, "downloadedEver", 0),
			uploaded=self._raw(torrent, "uploadedEver", 0),
			download_rate=self._raw(torrent, "rateDownload", 0),
			upload_rate=self._raw(torrent, "rateUpload", 0),
			eta=eta if eta is not None and eta >= 0 else -1,
			ratio=round(self._raw(torrent, "uploadRatio", 0) or 0, 2),
			peers=self._raw(torrent, "peersConnected", 0),
			download_dir=self._raw(torrent, "downloadDir", "") or "",
			error_message=self._raw(torrent, "errorString", "") or "",
			added_date=self._raw(torrent, "addedDate", None),
			files=files,
			trackers=trackers,
		)
		return info

	def _version(self, session):
		# Transmission reports "4.1.3 (838877323f)": drop the commit hash
		return f"Transmission {session.version.split(' (')[0]}"

	def test_connection(self):
		try:
			session = self.client.get_session()
			return self._version(session)
		except Exception as e:
			raise TorrentClientError(f"Cannot connect to Transmission: {e}")

	def get_summary(self):
		try:
			torrents = self.client.get_torrents(arguments=SUMMARY_FIELDS)
			session = self.client.get_session()
		except Exception as e:
			raise TorrentClientError(f"Error getting summary: {e}")

		counts = Counter()
		completed = 0
		download_rate = 0
		upload_rate = 0
		for t in torrents:
			counts[self._normalize_status(t)] += 1
			if self._raw(t, "percentDone", 0) >= 1:
				completed += 1
			download_rate += self._raw(t, "rateDownload", 0)
			upload_rate += self._raw(t, "rateUpload", 0)

		free_space = self.get_free_space(session.download_dir)
		return SessionSummary(
			counts=dict(counts),
			total=len(torrents),
			completed=completed,
			download_rate=download_rate,
			upload_rate=upload_rate,
			free_space=free_space,
			alt_speed_enabled=bool(session.alt_speed_enabled),
		)

	def get_torrents(self, status=None, query=None):
		try:
			torrents = self.client.get_torrents(arguments=LIGHT_FIELDS)
		except Exception as e:
			raise TorrentClientError(f"Error listing torrents: {e}")

		result = []
		query_lower = query.lower() if query else None
		for t in torrents:
			if query_lower:
				download_dir = self._raw(t, "downloadDir", "") or ""
				if query_lower not in t.name.lower() and query_lower not in download_dir.lower():
					continue
			info = self._to_info(t)
			if status and info.status != status:
				continue
			result.append(info)
		result.sort(key=lambda i: i.added_date or 0, reverse=True)
		return result

	def get_torrent(self, torrent_id):
		try:
			torrent = self.client.get_torrent(int(torrent_id), arguments=FULL_FIELDS)
		except KeyError:
			return None
		except Exception as e:
			raise TorrentClientError(f"Error getting torrent {torrent_id}: {e}")
		return self._to_info(torrent, full=True)

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None):
		try:
			kwargs = {}
			if download_dir:
				kwargs["download_dir"] = download_dir
			if magnet:
				torrent = self.client.add_torrent(magnet, **kwargs)
			elif torrent_data:
				torrent = self.client.add_torrent(torrent_data, **kwargs)
			else:
				raise TorrentClientError("No magnet or torrent data provided")
			return self.get_torrent(torrent.id) or self._to_info(torrent)
		except TorrentClientError:
			raise
		except Exception as e:
			raise TorrentClientError(f"Error adding torrent: {e}")

	def remove_torrents(self, torrent_ids, delete_data=False):
		try:
			self.client.remove_torrent([int(i) for i in torrent_ids], delete_data=delete_data)
		except Exception as e:
			raise TorrentClientError(f"Error removing torrents: {e}")

	def pause_torrents(self, torrent_ids):
		try:
			self.client.stop_torrent([int(i) for i in torrent_ids])
		except Exception as e:
			raise TorrentClientError(f"Error pausing torrents: {e}")

	def resume_torrents(self, torrent_ids):
		try:
			self.client.start_torrent([int(i) for i in torrent_ids])
		except Exception as e:
			raise TorrentClientError(f"Error resuming torrents: {e}")

	def verify_torrent(self, torrent_id):
		try:
			self.client.verify_torrent(int(torrent_id))
		except Exception as e:
			raise TorrentClientError(f"Error verifying torrent {torrent_id}: {e}")

	def rename_torrent(self, torrent_id, new_name):
		try:
			torrent = self.client.get_torrent(int(torrent_id), arguments=["id", "name"])
			self.client.rename_torrent_path(int(torrent_id), location=torrent.name, name=new_name)
		except Exception as e:
			raise TorrentClientError(f"Error renaming torrent {torrent_id}: {e}")

	def move_torrents(self, torrent_ids, new_dir):
		try:
			self.client.move_torrent_data([int(i) for i in torrent_ids], location=new_dir, timeout=MOVE_RPC_TIMEOUT)
		except Exception as e:
			raise TorrentClientError(f"Error moving torrents: {e}")

	def get_download_dirs(self):
		try:
			torrents = self.client.get_torrents(arguments=["id", "downloadDir"])
		except Exception as e:
			raise TorrentClientError(f"Error getting download dirs: {e}")
		counts = Counter()
		for t in torrents:
			directory = self._raw(t, "downloadDir", "")
			if directory:
				counts[directory.rstrip("/") or "/"] += 1
		default_dir = self.get_default_download_dir()
		if default_dir:
			counts[default_dir.rstrip("/") or "/"] += 0
		return [d for d, _ in counts.most_common()]

	def get_default_download_dir(self):
		try:
			return self.client.get_session().download_dir
		except Exception as e:
			raise TorrentClientError(f"Error getting default download dir: {e}")

	def get_free_space(self, path):
		try:
			free = self.client.free_space(path)
			return free if free is not None else -1
		except Exception:
			return -1

	def get_settings(self):
		try:
			session = self.client.get_session()
			return {
				"version": self._version(session),
				"alt_speed_enabled": bool(session.alt_speed_enabled),
				"alt_speed_down": session.alt_speed_down,
				"alt_speed_up": session.alt_speed_up,
				"speed_limit_down": session.speed_limit_down,
				"speed_limit_down_enabled": bool(session.speed_limit_down_enabled),
				"speed_limit_up": session.speed_limit_up,
				"speed_limit_up_enabled": bool(session.speed_limit_up_enabled),
				"download_dir": session.download_dir,
			}
		except Exception as e:
			raise TorrentClientError(f"Error getting settings: {e}")

	def set_alt_speed(self, enabled):
		try:
			self.client.set_session(alt_speed_enabled=bool(enabled))
		except Exception as e:
			raise TorrentClientError(f"Error setting alternative speed: {e}")

	def set_speed_limit(self, direction, kbps, enabled):
		try:
			kwargs = {}
			if direction == "down":
				kwargs["speed_limit_down_enabled"] = bool(enabled)
				if kbps is not None:
					kwargs["speed_limit_down"] = int(kbps)
			else:
				kwargs["speed_limit_up_enabled"] = bool(enabled)
				if kbps is not None:
					kwargs["speed_limit_up"] = int(kbps)
			self.client.set_session(**kwargs)
		except Exception as e:
			raise TorrentClientError(f"Error setting speed limit: {e}")
