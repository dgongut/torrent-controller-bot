"""qBittorrent implementation of the TorrentClient interface (Web API v2)"""

import json
import time
import uuid
from collections import Counter
from urllib.parse import urlparse

import requests

from torrent_clients.base import (
	SessionSummary,
	TorrentClient,
	TorrentClientError,
	TorrentInfo,
	TorrentStatus,
)

REQUEST_TIMEOUT = 15
MOVE_RPC_TIMEOUT = 60
ETA_INFINITE = 8640000

STATUS_MAP = {
	"downloading": TorrentStatus.DOWNLOADING,
	"metaDL": TorrentStatus.DOWNLOADING,
	"stalledDL": TorrentStatus.DOWNLOADING,
	"forcedDL": TorrentStatus.DOWNLOADING,
	"allocating": TorrentStatus.DOWNLOADING,
	"uploading": TorrentStatus.SEEDING,
	"stalledUP": TorrentStatus.SEEDING,
	"forcedUP": TorrentStatus.SEEDING,
	"pausedDL": TorrentStatus.PAUSED,
	"pausedUP": TorrentStatus.PAUSED,
	"stoppedDL": TorrentStatus.PAUSED,
	"stoppedUP": TorrentStatus.PAUSED,
	"checkingDL": TorrentStatus.CHECKING,
	"checkingUP": TorrentStatus.CHECKING,
	"checkingResumeData": TorrentStatus.CHECKING,
	"moving": TorrentStatus.CHECKING,
	"queuedDL": TorrentStatus.QUEUED,
	"queuedUP": TorrentStatus.QUEUED,
	"error": TorrentStatus.ERROR,
	"missingFiles": TorrentStatus.ERROR,
}


class QBittorrentClient(TorrentClient):
	def __init__(self, host, port, username=None, password=None, protocol="http"):
		self.base_url = f"{protocol}://{host}:{port}/api/v2"
		self.username = username or ""
		self.password = password or ""
		self.session = requests.Session()
		self._login()

	def _login(self):
		try:
			response = self.session.post(
				f"{self.base_url}/auth/login",
				data={"username": self.username, "password": self.password},
				timeout=REQUEST_TIMEOUT,
			)
		except Exception as e:
			raise TorrentClientError(f"Cannot connect to qBittorrent at {self.base_url}: {e}")
		if response.status_code >= 300 or response.text.strip() == "Fails.":
			raise TorrentClientError(f"qBittorrent login failed (HTTP {response.status_code})")

	def _request(self, method, endpoint, timeout=REQUEST_TIMEOUT, **kwargs):
		url = f"{self.base_url}/{endpoint}"
		try:
			response = self.session.request(method, url, timeout=timeout, **kwargs)
			if response.status_code == 403:  # Expired session: re-login and retry
				self._login()
				response = self.session.request(method, url, timeout=timeout, **kwargs)
		except TorrentClientError:
			raise
		except Exception as e:
			raise TorrentClientError(f"qBittorrent request failed ({endpoint}): {e}")
		if response.status_code >= 400:
			raise TorrentClientError(f"qBittorrent error {response.status_code} on {endpoint}: {response.text[:200]}")
		return response

	def _get(self, endpoint, params=None):
		return self._request("GET", endpoint, params=params)

	def _post(self, endpoint, data=None, timeout=REQUEST_TIMEOUT, **kwargs):
		return self._request("POST", endpoint, data=data, timeout=timeout, **kwargs)

	def _pause_resume(self, action, hashes):
		# qBittorrent 5.x renamed pause/resume to stop/start
		primary, fallback = ("stop", "pause") if action == "stop" else ("start", "resume")
		try:
			self._post(f"torrents/{primary}", data={"hashes": hashes})
		except TorrentClientError:
			self._post(f"torrents/{fallback}", data={"hashes": hashes})

	def _to_info(self, t, files=None, trackers=None):
		if trackers is None:
			# Light listing: the info payload carries the active tracker URL
			host = urlparse(t.get("tracker", "") or "").hostname
			trackers = [host] if host else []
		eta = t.get("eta", -1)
		if eta is None or eta < 0 or eta >= ETA_INFINITE:
			eta = -1
		state = t.get("state", "")
		error_message = ""
		if STATUS_MAP.get(state) == TorrentStatus.ERROR:
			error_message = "Files missing" if state == "missingFiles" else "Error"
		return TorrentInfo(
			id=t.get("hash", ""),
			name=t.get("name", ""),
			status=STATUS_MAP.get(state, TorrentStatus.PAUSED),
			progress=round(float(t.get("progress", 0)) * 100, 2),
			total_size=t.get("size", 0) or t.get("total_size", 0),
			downloaded=t.get("downloaded", 0),
			uploaded=t.get("uploaded", 0),
			download_rate=t.get("dlspeed", 0),
			upload_rate=t.get("upspeed", 0),
			eta=eta,
			ratio=round(float(t.get("ratio", 0) or 0), 2),
			peers=t.get("num_leechs", 0) + t.get("num_seeds", 0),
			download_dir=t.get("save_path", "") or "",
			error_message=error_message,
			added_date=t.get("added_on", None),
			files=files or [],
			trackers=trackers or [],
		)

	def _alt_speed_enabled(self):
		try:
			return self._get("transfer/speedLimitsMode").text.strip() == "1"
		except TorrentClientError:
			return False

	def _tracker_hosts(self, torrent_hash):
		trackers = []
		try:
			for tracker in self._get("torrents/trackers", params={"hash": torrent_hash}).json():
				url = tracker.get("url", "")
				if url.startswith("**"):  # Pseudo-entries: DHT, PeX, LSD
					continue
				host = urlparse(url).hostname
				if host and host not in trackers:
					trackers.append(host)
		except TorrentClientError:
			pass
		return trackers

	def test_connection(self):
		try:
			version = self._get("app/version").text.strip()
			return f"qBittorrent {version.lstrip('v')}"
		except TorrentClientError as e:
			raise TorrentClientError(f"Cannot connect to qBittorrent: {e}")

	def get_summary(self):
		try:
			torrents = self._get("torrents/info").json()
			transfer = self._get("transfer/info").json()
			maindata = self._get("sync/maindata").json()
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting summary: {e}")

		counts = Counter()
		completed = 0
		for t in torrents:
			counts[STATUS_MAP.get(t.get("state", ""), TorrentStatus.PAUSED)] += 1
			if float(t.get("progress", 0)) >= 1:
				completed += 1

		server_state = maindata.get("server_state", {})
		return SessionSummary(
			counts=dict(counts),
			total=len(torrents),
			completed=completed,
			download_rate=transfer.get("dl_info_speed", 0),
			upload_rate=transfer.get("up_info_speed", 0),
			free_space=server_state.get("free_space_on_disk", -1),
			alt_speed_enabled=self._alt_speed_enabled(),
		)

	def get_torrents(self, status=None, query=None):
		try:
			torrents = self._get("torrents/info").json()
		except TorrentClientError as e:
			raise TorrentClientError(f"Error listing torrents: {e}")

		result = []
		query_lower = query.lower() if query else None
		for t in torrents:
			if query_lower:
				if query_lower not in t.get("name", "").lower() and query_lower not in t.get("save_path", "").lower():
					continue
			info = self._to_info(t)
			if status and info.status != status:
				continue
			result.append(info)
		result.sort(key=lambda i: i.added_date or 0, reverse=True)
		return result

	def get_torrent(self, torrent_id):
		try:
			torrents = self._get("torrents/info", params={"hashes": torrent_id}).json()
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting torrent {torrent_id}: {e}")
		if not torrents:
			return None
		t = torrents[0]
		files = []
		try:
			for f in self._get("torrents/files", params={"hash": torrent_id}).json():
				size = f.get("size", 0)
				files.append((f.get("name", ""), size, int(size * float(f.get("progress", 0)))))
		except TorrentClientError:
			files = []
		return self._to_info(t, files=files, trackers=self._tracker_hosts(torrent_id))

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None):
		tag = f"tcb-{uuid.uuid4().hex[:8]}"  # Unique tag to find the new hash
		data = {"tags": tag}
		if download_dir:
			data["savepath"] = download_dir
		try:
			if magnet:
				data["urls"] = magnet
				self._post("torrents/add", data=data)
			elif torrent_data:
				self._post("torrents/add", data=data, files={"torrents": ("upload.torrent", torrent_data)})
			else:
				raise TorrentClientError("No magnet or torrent data provided")

			torrent_hash = None
			for _ in range(10):
				torrents = self._get("torrents/info", params={"tag": tag}).json()
				if torrents:
					torrent_hash = torrents[0].get("hash")
					break
				time.sleep(0.5)
			if not torrent_hash:
				raise TorrentClientError("Torrent added but not found (duplicate?)")
			self._post("torrents/removeTags", data={"hashes": torrent_hash, "tags": tag})
			return self.get_torrent(torrent_hash)
		except TorrentClientError:
			raise
		except Exception as e:
			raise TorrentClientError(f"Error adding torrent: {e}")

	def remove_torrents(self, torrent_ids, delete_data=False):
		try:
			self._post("torrents/delete", data={
				"hashes": "|".join(torrent_ids),
				"deleteFiles": "true" if delete_data else "false",
			})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error removing torrents: {e}")

	def pause_torrents(self, torrent_ids):
		try:
			self._pause_resume("stop", "|".join(torrent_ids))
		except TorrentClientError as e:
			raise TorrentClientError(f"Error pausing torrents: {e}")

	def resume_torrents(self, torrent_ids):
		try:
			self._pause_resume("start", "|".join(torrent_ids))
		except TorrentClientError as e:
			raise TorrentClientError(f"Error resuming torrents: {e}")

	def verify_torrent(self, torrent_id):
		try:
			self._post("torrents/recheck", data={"hashes": torrent_id})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error verifying torrent {torrent_id}: {e}")

	def rename_torrent(self, torrent_id, new_name):
		try:
			torrents = self._get("torrents/info", params={"hashes": torrent_id}).json()
			if not torrents:
				raise TorrentClientError("Torrent not found")
			old_name = torrents[0].get("name", "")
			files = self._get("torrents/files", params={"hash": torrent_id}).json()
			self._post("torrents/rename", data={"hash": torrent_id, "name": new_name})
			if len(files) == 1 and "/" not in files[0].get("name", ""):
				# Single file at top level: rename the file on disk too
				self._post("torrents/renameFile", data={
					"hash": torrent_id, "oldPath": files[0]["name"], "newPath": new_name})
			elif files and files[0].get("name", "").startswith(f"{old_name}/"):
				# Content inside a folder named like the torrent: rename the folder
				self._post("torrents/renameFolder", data={
					"hash": torrent_id, "oldPath": old_name, "newPath": new_name})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error renaming torrent {torrent_id}: {e}")

	def move_torrents(self, torrent_ids, new_dir):
		try:
			self._post("torrents/setLocation", data={
				"hashes": "|".join(torrent_ids), "location": new_dir}, timeout=MOVE_RPC_TIMEOUT)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error moving torrents: {e}")

	def get_download_dirs(self):
		try:
			torrents = self._get("torrents/info").json()
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting download dirs: {e}")
		counts = Counter()
		for t in torrents:
			directory = t.get("save_path", "")
			if directory:
				counts[directory.rstrip("/") or "/"] += 1
		default_dir = self.get_default_download_dir()
		if default_dir:
			counts[default_dir.rstrip("/") or "/"] += 0
		return [d for d, _ in counts.most_common()]

	def get_default_download_dir(self):
		try:
			return self._get("app/preferences").json().get("save_path", "")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting default download dir: {e}")

	def get_free_space(self, path):
		try:
			server_state = self._get("sync/maindata").json().get("server_state", {})
			return server_state.get("free_space_on_disk", -1)
		except TorrentClientError:
			return -1

	def get_settings(self):
		try:
			prefs = self._get("app/preferences").json()
			version = self._get("app/version").text.strip()
			return {
				"version": f"qBittorrent {version.lstrip('v')}",
				"alt_speed_enabled": self._alt_speed_enabled(),
				"alt_speed_down": (prefs.get("alt_dl_limit", 0) or 0) // 1024,
				"alt_speed_up": (prefs.get("alt_up_limit", 0) or 0) // 1024,
				"speed_limit_down": (prefs.get("dl_limit", 0) or 0) // 1024,
				"speed_limit_down_enabled": (prefs.get("dl_limit", 0) or 0) > 0,
				"speed_limit_up": (prefs.get("up_limit", 0) or 0) // 1024,
				"speed_limit_up_enabled": (prefs.get("up_limit", 0) or 0) > 0,
				"download_dir": prefs.get("save_path", ""),
			}
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting settings: {e}")

	def set_alt_speed(self, enabled):
		try:
			if self._alt_speed_enabled() != bool(enabled):
				self._post("transfer/toggleSpeedLimitsMode")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting alternative speed: {e}")

	def set_speed_limit(self, direction, kbps, enabled):
		try:
			if kbps is None:
				prefs = self._get("app/preferences").json()
				current = prefs.get("dl_limit" if direction == "down" else "up_limit", 0) or 0
				kbps = current // 1024
			limit = int(kbps) * 1024 if enabled and int(kbps) > 0 else 0
			key = "dl_limit" if direction == "down" else "up_limit"
			self._post("app/setPreferences", data={"json": json.dumps({key: limit})})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting speed limit: {e}")
