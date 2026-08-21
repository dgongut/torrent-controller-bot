"""Deluge implementation of the TorrentClient interface (Web UI JSON-RPC API)"""

import base64
import time
from collections import Counter
from itertools import count
from urllib.parse import urlparse

import requests

from torrent_clients.base import (
	SessionSummary,
	TorrentClient,
	TorrentClientError,
	TorrentInfo,
	TorrentStatus,
	content_root,
	sort_files,
)

REQUEST_TIMEOUT = 15
MOVE_RPC_TIMEOUT = 60

STATUS_MAP = {
	"Downloading": TorrentStatus.DOWNLOADING,
	"Allocating": TorrentStatus.DOWNLOADING,
	"Seeding": TorrentStatus.SEEDING,
	"Paused": TorrentStatus.PAUSED,
	"Checking": TorrentStatus.CHECKING,
	"Moving": TorrentStatus.CHECKING,
	"Queued": TorrentStatus.QUEUED,
	"Error": TorrentStatus.ERROR,
}

LIGHT_KEYS = [
	"name", "state", "progress", "total_wanted", "download_payload_rate",
	"upload_payload_rate", "save_path", "time_added", "tracker_host", "message",
]

FULL_KEYS = LIGHT_KEYS + [
	"total_done", "total_uploaded", "eta", "ratio", "num_peers", "num_seeds",
	"files", "file_progress", "trackers",
]


class DelugeClient(TorrentClient):
	supports_alt_speed = False

	def __init__(self, host, port, password=None, protocol="http"):
		self.base_url = f"{protocol}://{host}:{port}/json"
		self.password = password or ""
		self.session = requests.Session()
		self._id_counter = count(1)
		self._saved_limits = {}
		self._login()

	def _rpc(self, method, *params, timeout=REQUEST_TIMEOUT):
		payload = {"method": method, "params": list(params), "id": next(self._id_counter)}
		try:
			response = self.session.post(self.base_url, json=payload, timeout=timeout)
		except Exception as e:
			raise TorrentClientError(f"Deluge request failed ({method}): {e}")
		if response.status_code >= 400:
			raise TorrentClientError(f"Deluge error {response.status_code} on {method}")
		data = response.json()
		if data.get("error"):
			raise TorrentClientError(f"Deluge error on {method}: {data['error'].get('message', data['error'])}")
		return data.get("result")

	def _login(self):
		try:
			if not self._rpc("auth.login", self.password):
				raise TorrentClientError("Deluge login failed: wrong password")
			if not self._rpc("web.connected"):
				hosts = self._rpc("web.get_hosts") or []
				if not hosts:
					raise TorrentClientError("Deluge web UI has no daemon hosts configured")
				self._rpc("web.connect", hosts[0][0])
		except TorrentClientError:
			raise
		except Exception as e:
			raise TorrentClientError(f"Cannot connect to Deluge at {self.base_url}: {e}")

	def _call(self, method, *params, timeout=REQUEST_TIMEOUT):
		"""RPC with automatic re-login when the web session expires"""
		try:
			return self._rpc(method, *params, timeout=timeout)
		except TorrentClientError as e:
			if "Not authenticated" not in str(e):
				raise
			self._login()
			return self._rpc(method, *params, timeout=timeout)

	def _to_info(self, torrent_hash, t, full=False):
		files = []
		if full:
			progress = t.get("file_progress") or []
			for i, f in enumerate(t.get("files") or []):
				size = f.get("size", 0)
				done = progress[i] if i < len(progress) else 0
				files.append((f.get("path", ""), size, int(size * done)))
			sort_files(files)
		trackers = []
		if full:
			for tracker in t.get("trackers") or []:
				host = urlparse(tracker.get("url", "")).hostname
				if host and host not in trackers:
					trackers.append(host)
		else:
			host = t.get("tracker_host", "")
			if host:
				trackers.append(host)
		eta = t.get("eta", -1)
		state = t.get("state", "")
		return TorrentInfo(
			id=torrent_hash,
			name=t.get("name", ""),
			status=STATUS_MAP.get(state, TorrentStatus.PAUSED),
			progress=round(float(t.get("progress", 0)), 2),
			total_size=t.get("total_wanted", 0),
			downloaded=t.get("total_done", 0),
			uploaded=t.get("total_uploaded", 0),
			download_rate=t.get("download_payload_rate", 0),
			upload_rate=t.get("upload_payload_rate", 0),
			eta=eta if eta and eta > 0 else -1,
			ratio=round(float(t.get("ratio", 0) or 0), 2),
			peers=t.get("num_peers", 0) + t.get("num_seeds", 0),
			seeders=t.get("num_seeds", 0),
			leechers=t.get("num_peers", 0),
			download_dir=t.get("save_path", "") or "",
			error_message=t.get("message", "") if state == "Error" else "",
			added_date=t.get("time_added", None),
			files=files,
			trackers=trackers,
		)

	def _torrents_status(self, keys, filter_dict=None):
		return self._call("core.get_torrents_status", filter_dict or {}, keys) or {}

	def test_connection(self):
		try:
			try:
				version = self._call("daemon.get_version")
			except TorrentClientError:
				version = self._call("daemon.info")  # Deluge 1.3
			return f"Deluge {version}"
		except TorrentClientError as e:
			raise TorrentClientError(f"Cannot connect to Deluge: {e}")

	def get_summary(self):
		try:
			torrents = self._torrents_status(["state", "progress", "upload_payload_rate", "download_payload_rate"])
			session = self._call(
				"core.get_session_status", ["payload_download_rate", "payload_upload_rate"]) or {}
			free_space = self.get_free_space(None)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting summary: {e}")

		counts = Counter()
		completed = 0
		uploading = 0
		downloading = 0
		for t in torrents.values():
			counts[STATUS_MAP.get(t.get("state", ""), TorrentStatus.PAUSED)] += 1
			if float(t.get("progress", 0)) >= 100:
				completed += 1
			if int(t.get("upload_payload_rate", 0) or 0) > 0:
				uploading += 1
			if int(t.get("download_payload_rate", 0) or 0) > 0:
				downloading += 1

		return SessionSummary(
			counts=dict(counts),
			total=len(torrents),
			completed=completed,
			uploading=uploading,
			downloading=downloading,
			download_rate=int(session.get("payload_download_rate", 0)),
			upload_rate=int(session.get("payload_upload_rate", 0)),
			free_space=free_space,
			alt_speed_enabled=False,  # Deluge has no native alternative speed mode
		)

	def get_torrents(self, status=None, query=None):
		try:
			torrents = self._torrents_status(LIGHT_KEYS)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error listing torrents: {e}")

		result = []
		query_lower = query.lower() if query else None
		for torrent_hash, t in torrents.items():
			if query_lower:
				if query_lower not in t.get("name", "").lower() and query_lower not in t.get("save_path", "").lower():
					continue
			info = self._to_info(torrent_hash, t)
			if status and info.status != status:
				continue
			result.append(info)
		result.sort(key=lambda i: i.added_date or 0, reverse=True)
		return result

	def get_torrent(self, torrent_id):
		try:
			t = self._call("core.get_torrent_status", torrent_id, FULL_KEYS)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting torrent {torrent_id}: {e}")
		if not t:
			return None
		return self._to_info(torrent_id, t, full=True)

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None):
		options = {}
		if download_dir:
			options["download_location"] = download_dir
		try:
			if magnet:
				torrent_hash = self._call("core.add_torrent_magnet", magnet, options)
			elif torrent_data:
				filedump = base64.b64encode(torrent_data).decode("ascii")
				torrent_hash = self._call("core.add_torrent_file", "upload.torrent", filedump, options)
			else:
				raise TorrentClientError("No magnet or torrent data provided")
			if not torrent_hash:
				raise TorrentClientError("Torrent added but no hash returned (duplicate?)")
			for _ in range(10):
				info = self.get_torrent(torrent_hash)
				if info and info.name:
					return info
				time.sleep(0.5)
			return self.get_torrent(torrent_hash)
		except TorrentClientError:
			raise
		except Exception as e:
			raise TorrentClientError(f"Error adding torrent: {e}")

	def remove_torrents(self, torrent_ids, delete_data=False):
		try:
			try:
				failures = self._call("core.remove_torrents", list(torrent_ids), bool(delete_data))
				if failures:
					raise TorrentClientError(f"Could not remove: {failures}")
			except TorrentClientError as e:
				if "remove_torrents" not in str(e):  # Method exists: real failure
					raise
				for torrent_id in torrent_ids:  # Deluge 1.3 fallback
					self._call("core.remove_torrent", torrent_id, bool(delete_data))
		except TorrentClientError as e:
			raise TorrentClientError(f"Error removing torrents: {e}")

	def pause_torrents(self, torrent_ids):
		try:
			self._call("core.pause_torrent", list(torrent_ids))
		except TorrentClientError as e:
			raise TorrentClientError(f"Error pausing torrents: {e}")

	def resume_torrents(self, torrent_ids):
		try:
			self._call("core.resume_torrent", list(torrent_ids))
		except TorrentClientError as e:
			raise TorrentClientError(f"Error resuming torrents: {e}")

	def verify_torrent(self, torrent_id):
		try:
			self._call("core.force_recheck", [torrent_id])
		except TorrentClientError as e:
			raise TorrentClientError(f"Error verifying torrent {torrent_id}: {e}")

	def rename_torrent(self, torrent_id, new_name):
		try:
			t = self._call("core.get_torrent_status", torrent_id, ["name", "files"])
			if not t:
				raise TorrentClientError("Torrent not found")
			files = t.get("files") or []
			# The path on disk has to come from the file list: Deluge sanitizes it,
			# so it does not always match the torrent name
			root = content_root(f.get("path", "") for f in files)
			if root:
				# Content inside a folder: rename the folder
				self._call("core.rename_folder", torrent_id, f"{root}/", f"{new_name}/")
			elif len(files) == 1:
				# Single file at top level: rename the file on disk
				self._call("core.rename_files", torrent_id, [[files[0].get("index", 0), new_name]])
			else:
				raise TorrentClientError("Torrent content layout does not support renaming")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error renaming torrent {torrent_id}: {e}")

	def rename_file(self, torrent_id, old_path, new_name):
		folder = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
		new_path = f"{folder}/{new_name}" if folder else new_name
		try:
			t = self._call("core.get_torrent_status", torrent_id, ["files"])
			index = None
			for f in (t or {}).get("files") or []:
				if f.get("path", "") == old_path:
					index = f.get("index")
					break
			if index is None:
				raise TorrentClientError("File not found in torrent")
			self._call("core.rename_files", torrent_id, [[index, new_path]])
		except TorrentClientError as e:
			raise TorrentClientError(f"Error renaming file {old_path}: {e}")

	def move_torrents(self, torrent_ids, new_dir):
		try:
			self._call("core.move_storage", list(torrent_ids), new_dir, timeout=MOVE_RPC_TIMEOUT)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error moving torrents: {e}")

	def get_download_dirs(self):
		try:
			torrents = self._torrents_status(["save_path"])
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting download dirs: {e}")
		counts = Counter()
		for t in torrents.values():
			directory = t.get("save_path", "")
			if directory:
				counts[directory.rstrip("/") or "/"] += 1
		default_dir = self.get_default_download_dir()
		if default_dir:
			counts[default_dir.rstrip("/") or "/"] += 0
		return [d for d, _ in counts.most_common()]

	def get_default_download_dir(self):
		try:
			config = self._call("core.get_config_values", ["download_location"]) or {}
			return config.get("download_location", "")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting default download dir: {e}")

	def get_free_space(self, path):
		try:
			free = self._call("core.get_free_space", path) if path else self._call("core.get_free_space")
			return free if free is not None and free >= 0 else -1
		except TorrentClientError:
			return -1

	def get_settings(self):
		try:
			config = self._call(
				"core.get_config_values",
				["max_download_speed", "max_upload_speed", "download_location"]) or {}
			down = float(config.get("max_download_speed", -1) or -1)  # KiB/s, -1 unlimited
			up = float(config.get("max_upload_speed", -1) or -1)
			return {
				"version": self.test_connection(),
				"alt_speed_enabled": False,
				"alt_speed_down": 0,
				"alt_speed_up": 0,
				"speed_limit_down": int(down) if down > 0 else 0,
				"speed_limit_down_enabled": down > 0,
				"speed_limit_up": int(up) if up > 0 else 0,
				"speed_limit_up_enabled": up > 0,
				"download_dir": config.get("download_location", ""),
			}
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting settings: {e}")

	def set_alt_speed(self, enabled):
		raise TorrentClientError("Deluge does not support alternative speed limits")

	def set_speed_limit(self, direction, kbps, enabled):
		key = "max_download_speed" if direction == "down" else "max_upload_speed"
		try:
			if kbps is None:
				current = float((self._call("core.get_config_values", [key]) or {}).get(key, -1) or -1)
				kbps = int(current) if current > 0 else self._saved_limits.get(key, 0)
			if enabled and int(kbps) > 0:
				self._call("core.set_config", {key: int(kbps)})
			else:
				current = float((self._call("core.get_config_values", [key]) or {}).get(key, -1) or -1)
				if current > 0:
					self._saved_limits[key] = int(current)  # Remember for re-enabling
				self._call("core.set_config", {key: -1})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting speed limit: {e}")
