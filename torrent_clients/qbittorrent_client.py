"""qBittorrent implementation of the TorrentClient interface (Web API v2)"""

import json
import re
import time
import uuid
from collections import Counter
from urllib.parse import urlparse

import requests

from torrent_clients.base import (
	DuplicateTorrentError,
	FILE_HIGH,
	FILE_NORMAL,
	FILE_SKIP,
	PermanentTorrentError,
	RATIO_MODE_GLOBAL,
	RATIO_MODE_NONE,
	RATIO_MODE_TORRENT,
	SessionSummary,
	TorrentClient,
	TorrentClientError,
	TorrentInfo,
	TorrentStatus,
	capped_progress,
	clean_ratio,
	content_root,
	expected_torrent_ids,
	query_matcher,
	sort_files,
)

REQUEST_TIMEOUT = 15
MOVE_RPC_TIMEOUT = 60
ETA_INFINITE = 8640000
TRACKER_WORKING = 2
TRACKER_FAILED = (4, 5, 6)  # Not working, tracker error (5.0+), unreachable (5.0+)
TEMP_TAG_PREFIX = "tcb-"
TEMP_TAG_RE = re.compile(rf"^{TEMP_TAG_PREFIX}[0-9a-f]{{8}}$")
# What qBittorrent 5.x answers when it does not add a torrent: it already has
# it, or the magnet link cannot be read. Older versions answer "Fails."
HTTP_CONFLICT = 409
ADD_FAILED = "Fails."
NOT_THE_API = "is a proxy or a login page in between?"


class QBittorrentHTTPError(TorrentClientError):
	"""qBittorrent answered with an HTTP error status"""

	def __init__(self, message, status_code):
		super().__init__(message)
		self.status_code = status_code


def _num(value, default):
	return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _limit(value, scale=1):
	"""A limit as qBittorrent reports it, where any negative value means none"""
	value = _num(value, -1)
	return value * scale if value >= 0 else -1


def _ratio_mode(value):
	"""ratio_limit of a torrent as qBittorrent reports it: -2 follows the
	global setting, -1 none, anything else a limit of its own"""
	value = _num(value, None)
	if value is None:
		return ""
	if value == -2:
		return RATIO_MODE_GLOBAL
	return RATIO_MODE_NONE if value < 0 else RATIO_MODE_TORRENT


def _speed_limit(value):
	"""A speed limit as qBittorrent reports it (bytes/s, 0 or less none) in KB/s"""
	value = _num(value, -1)
	return value // 1024 if value > 0 else -1


def _timestamp(value):
	value = _num(value, 0)
	return value if value > 0 else None


def _progress(t):
	"""progress is a share of the wanted bytes: only the bytes left tell a big
	torrent that lacks a few MB (99.999 %) from a finished one"""
	return capped_progress(_num(t.get("progress"), 0) * 100, _num(t.get("amount_left"), None))


def _duplicate(existing):
	"""DuplicateTorrentError for the torrent qBittorrent already had, as torrents/info lists it"""
	return DuplicateTorrentError(f"Torrent already in qBittorrent: {existing.get('name', '')}", torrent_id=existing.get("hash"))


STATUS_MAP = {
	"downloading": TorrentStatus.DOWNLOADING,
	"metaDL": TorrentStatus.DOWNLOADING,
	"forcedMetaDL": TorrentStatus.DOWNLOADING,  # Force started while it fetches the metadata
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
	"queuedForChecking": TorrentStatus.CHECKING,  # Older versions, still known to the 5.x web UI
	"moving": TorrentStatus.CHECKING,
	"queuedDL": TorrentStatus.QUEUED,
	"queuedUP": TorrentStatus.QUEUED,
	"error": TorrentStatus.ERROR,
	"missingFiles": TorrentStatus.ERROR,
}


def _file_priority(value):
	"""qBittorrent file priority -> normalized one: 0 is not downloaded, 1 is
	normal, 6 high and 7 maximal. It has nothing below normal"""
	value = _num(value, 1)
	if value <= 0:
		return FILE_SKIP
	return FILE_HIGH if value >= 6 else FILE_NORMAL


FILE_PRIORITY_VALUES = {FILE_SKIP: 0, FILE_NORMAL: 1, FILE_HIGH: 6}


class QBittorrentClient(TorrentClient):
	file_priority_levels = (FILE_SKIP, FILE_NORMAL, FILE_HIGH)
	supports_categories = True

	def __init__(self, host, port, username=None, password=None, protocol="http"):
		self.base_url = f"{protocol}://{host}:{port}/api/v2"
		self.username = username or ""
		self.password = password or ""
		self.session = requests.Session()
		self._login()
		self._purge_temp_tags()

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
			raise QBittorrentHTTPError(
				f"qBittorrent error {response.status_code} on {endpoint}: {response.text[:200]}", response.status_code)
		return response

	def _get(self, endpoint, params=None):
		return self._request("GET", endpoint, params=params)

	def _get_json(self, endpoint, params=None, kind=None):
		"""GET whose answer is JSON, of the given type when there is one. A
		reverse proxy or a login page in front of qBittorrent answers 200 with
		a web page instead, which must fail like any other unusable answer"""
		response = self._get(endpoint, params=params)
		try:
			data = response.json()
		except ValueError:
			raise TorrentClientError(f"qBittorrent answered {endpoint} with something that is not JSON: {NOT_THE_API}")
		if kind is not None and not isinstance(data, kind):
			raise TorrentClientError(f"qBittorrent answered {endpoint} with something unexpected: {NOT_THE_API}")
		return data

	def _version(self):
		version = self._get("app/version").text.strip()
		# Anything else answering there with 200 (a proxy, a login page...) is not qBittorrent
		if not re.match(r"v?\d", version) or "<" in version or len(version) > 64:
			raise TorrentClientError(f"qBittorrent answered app/version with something that is not a version: {NOT_THE_API}")
		return f"qBittorrent {version.lstrip('v')}"

	def _post(self, endpoint, data=None, timeout=REQUEST_TIMEOUT, **kwargs):
		return self._request("POST", endpoint, data=data, timeout=timeout, **kwargs)

	def _pause_resume(self, action, hashes):
		# qBittorrent 5.x renamed pause/resume to stop/start
		primary, fallback = ("stop", "pause") if action == "stop" else ("start", "resume")
		try:
			self._post(f"torrents/{primary}", data={"hashes": hashes})
		except TorrentClientError:
			self._post(f"torrents/{fallback}", data={"hashes": hashes})

	def _to_info(self, t, files=None, trackers=None, tracker_error="", swarm=(-1, -1)):
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
			progress=_progress(t),
			total_size=t.get("size", 0) or t.get("total_size", 0),
			downloaded=t.get("downloaded", 0),
			uploaded=t.get("uploaded", 0),
			download_rate=t.get("dlspeed", 0),
			upload_rate=t.get("upspeed", 0),
			eta=eta,
			ratio=clean_ratio(t.get("ratio", 0)),
			# max_* are the limits that apply, whether set on the torrent or global
			ratio_limit=_limit(t.get("max_ratio")),
			ratio_limit_mode=_ratio_mode(t.get("ratio_limit")),
			download_limit=_speed_limit(t.get("dl_limit")),
			upload_limit=_speed_limit(t.get("up_limit")),
			seeding_time=_num(t.get("seeding_time"), -1),
			seeding_time_limit=_limit(t.get("max_seeding_time"), 60),  # minutes
			peers=t.get("num_leechs", 0) + t.get("num_seeds", 0),
			seeders=t.get("num_seeds", 0),
			leechers=t.get("num_leechs", 0),
			swarm_seeders=swarm[0],
			swarm_leechers=swarm[1],
			availability=_num(t.get("availability"), -1),  # -1 once finished
			download_dir=t.get("save_path", "") or "",
			category=t.get("category", "") or "",
			auto_managed=bool(t.get("auto_tmm", False)),
			error_message=error_message,
			tracker_error=tracker_error,
			private=bool(t.get("private", False)),  # qBittorrent 5.0+
			added_date=t.get("added_on", None),
			completed_date=_timestamp(t.get("completion_on")),
			last_activity=_timestamp(t.get("last_activity")),
			files=files or [],
			trackers=trackers or [],
		)

	def _alt_speed_enabled(self):
		try:
			return self._get("transfer/speedLimitsMode").text.strip() == "1"
		except TorrentClientError:
			return False

	def _trackers(self, torrent_hash):
		"""Hostnames of the trackers, the swarm size they report (-1 until one
		answers: num_complete in the torrent info says 0 instead, and counts
		peers found through LSD) and the message of the first one that fails
		when none of them works (empty while any of them answers)"""
		trackers, errors, working = [], [], False
		seeders, leechers = -1, -1
		try:
			for tracker in self._get_json("torrents/trackers", params={"hash": torrent_hash}, kind=list):
				url = tracker.get("url", "")
				if url.startswith("**"):  # Pseudo-entries: DHT, PeX, LSD
					continue
				host = urlparse(url).hostname
				if host and host not in trackers:
					trackers.append(host)
				seeders = max(seeders, _num(tracker.get("num_seeds"), -1))
				leechers = max(leechers, _num(tracker.get("num_leeches"), -1))
				status = tracker.get("status")
				if status == TRACKER_WORKING:
					working = True
				elif status in TRACKER_FAILED and tracker.get("msg"):
					errors.append(tracker["msg"])
		except TorrentClientError:
			pass
		return trackers, (seeders, leechers), "" if working or not errors else errors[0]

	def test_connection(self):
		try:
			return self._version()
		except TorrentClientError as e:
			raise TorrentClientError(f"Cannot connect to qBittorrent: {e}")

	def get_summary(self):
		try:
			torrents = self._get_json("torrents/info", kind=list)
			transfer = self._get_json("transfer/info", kind=dict)
			maindata = self._get_json("sync/maindata", kind=dict)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting summary: {e}")

		counts = Counter()
		completed = 0
		uploading = 0
		downloading = 0
		for t in torrents:
			counts[STATUS_MAP.get(t.get("state", ""), TorrentStatus.PAUSED)] += 1
			if _progress(t) >= 100:
				completed += 1
			if int(t.get("upspeed", 0) or 0) > 0:
				uploading += 1
			if int(t.get("dlspeed", 0) or 0) > 0:
				downloading += 1

		server_state = maindata.get("server_state", {})
		return SessionSummary(
			counts=dict(counts),
			total=len(torrents),
			completed=completed,
			uploading=uploading,
			downloading=downloading,
			download_rate=transfer.get("dl_info_speed", 0),
			upload_rate=transfer.get("up_info_speed", 0),
			free_space=server_state.get("free_space_on_disk", -1),
			alt_speed_enabled=self._alt_speed_enabled(),
			session_downloaded=transfer.get("dl_info_data", -1),
			session_uploaded=transfer.get("up_info_data", -1),
			total_downloaded=server_state.get("alltime_dl", -1),
			total_uploaded=server_state.get("alltime_ul", -1),
		)

	def get_torrents(self, status=None, query=None):
		try:
			torrents = self._get_json("torrents/info", kind=list)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error listing torrents: {e}")

		result = []
		matches = query_matcher(query)
		for t in torrents:
			if matches and not matches(t.get("name", ""), t.get("save_path", "")):
				continue
			info = self._to_info(t)
			if status and info.status != status:
				continue
			result.append(info)
		result.sort(key=lambda i: i.added_date or 0, reverse=True)
		return result

	def get_torrent(self, torrent_id):
		try:
			torrents = self._get_json("torrents/info", params={"hashes": torrent_id}, kind=list)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting torrent {torrent_id}: {e}")
		if not torrents:
			return None
		t = torrents[0]
		files = []
		priorities = {}
		try:
			for f in self._get_json("torrents/files", params={"hash": torrent_id}, kind=list):
				size = f.get("size", 0)
				files.append((f.get("name", ""), size, int(size * float(f.get("progress", 0)))))
				priorities[f.get("name", "")] = _file_priority(f.get("priority"))
			sort_files(files)
		except TorrentClientError:
			files, priorities = [], {}
		trackers, swarm, tracker_error = self._trackers(torrent_id)
		info = self._to_info(t, files=files, trackers=trackers, tracker_error=tracker_error, swarm=swarm)
		info.file_priorities = priorities
		return info

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None, category=None):
		tag = f"{TEMP_TAG_PREFIX}{uuid.uuid4().hex[:8]}"  # Unique tag to find the new hash
		data = {"tags": tag}
		if category:
			# The category is what decides the location, and only a torrent under
			# automatic management follows it, so a save path here would fight it
			data["category"] = category
			data["autoTMM"] = "true"
		elif download_dir:
			data["savepath"] = download_dir
		try:
			files = None
			if magnet:
				data["urls"] = magnet
			elif torrent_data:
				files = {"torrents": ("upload.torrent", torrent_data)}
			else:
				raise TorrentClientError("No magnet or torrent data provided")
			try:
				response = self._post("torrents/add", data=data, files=files)
			except QBittorrentHTTPError as e:
				if e.status_code != HTTP_CONFLICT:
					raise
				raise self._refusal(magnet, torrent_data, e)
			try:
				answer = response.json()
			except ValueError:
				answer = response.text.strip()
			torrent_hash = None
			if isinstance(answer, dict):
				# qBittorrent 5.x tells what it added
				added = answer.get("added_torrent_ids") or []
				if added:
					torrent_hash = added[0]
				elif answer.get("failure_count") and not answer.get("success_count") and not answer.get("pending_count"):
					raise self._refusal(magnet, torrent_data, answer)
			elif answer == ADD_FAILED:
				raise self._refusal(magnet, torrent_data, answer)
			if not torrent_hash:
				torrent_hash = self._find_added(tag, magnet, torrent_data)
			for _ in range(10):
				info = self.get_torrent(torrent_hash)
				if info:
					return info
				time.sleep(0.5)
			raise TorrentClientError(f"Torrent added but not found: {torrent_hash}")
		except TorrentClientError:
			raise
		except Exception as e:
			raise TorrentClientError(f"Error adding torrent: {e}")
		finally:
			# deleteTags (not removeTags) so the tag also disappears from
			# qBittorrent's global tag list instead of piling up there
			self._delete_tags([tag])

	def _existing_torrent(self, magnet, torrent_data):
		"""The torrent (as torrents/info lists it) qBittorrent already has under
		an id the magnet or .torrent would get, or None"""
		ids = expected_torrent_ids(magnet, torrent_data)
		if not ids:
			return None
		try:
			torrents = self._get_json("torrents/info", params={"hashes": "|".join(ids)}, kind=list)
		except TorrentClientError:
			return None
		return torrents[0] if torrents else None

	def _refusal(self, magnet, torrent_data, answer):
		"""Error of an add qBittorrent did not do: DuplicateTorrentError when it
		already has the torrent, which is what it refuses most"""
		existing = self._existing_torrent(magnet, torrent_data)
		if existing:
			return _duplicate(existing)
		return PermanentTorrentError(f"qBittorrent did not add the torrent ({answer})")

	def _find_added(self, tag, magnet, torrent_data):
		"""Hash of the torrent just added under the temporary tag, for versions
		whose answer does not tell it. Some of them do not tell a torrent they
		already had either, so not finding it can be a duplicate"""
		for _ in range(10):
			torrents = self._get_json("torrents/info", params={"tag": tag}, kind=list)
			if torrents:
				return torrents[0].get("hash")
			time.sleep(0.5)
		existing = self._existing_torrent(magnet, torrent_data)
		if existing is None:
			raise TorrentClientError("Torrent added but not found")
		if tag not in [t.strip() for t in (existing.get("tags") or "").split(",")]:
			raise _duplicate(existing)
		return existing.get("hash")  # It is the new one, it just took longer to show up

	def _delete_tags(self, tags):
		if not tags:
			return
		try:
			self._post("torrents/deleteTags", data={"tags": ",".join(tags)})
		except Exception:
			pass  # Cleaning up temporary tags must never break the operation

	def _purge_temp_tags(self):
		"""Delete leftover temporary tags from previous runs (older versions
		only removed them from the torrent, so they stayed in the tag list)"""
		try:
			tags = self._get_json("torrents/tags", kind=list)
		except Exception:
			return
		self._delete_tags([t for t in tags if TEMP_TAG_RE.match(t)])

	def remove_torrents(self, torrent_ids, delete_data=False):
		try:
			self._post("torrents/delete", data={
				"hashes": "|".join(torrent_ids),
				"deleteFiles": "true" if delete_data else "false",
			})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error removing torrents: {e}")

	def set_ratio_limit(self, torrent_ids, limit):
		try:
			# The same call sets the seeding time limits and, since qBittorrent
			# 5.x, what to do when one is reached (it refuses the call without
			# them): each torrent is sent back its own so only the ratio changes
			current = {t.get("hash"): t for t in self._get_json("torrents/info", params={"hashes": "|".join(torrent_ids)}, kind=list)}
			ratio = -2 if limit is None else (-1 if limit < 0 else round(float(limit), 2))
			for torrent_id in torrent_ids:
				t = current.get(torrent_id, {})
				data = {
					"hashes": torrent_id,
					"ratioLimit": ratio,
					"seedingTimeLimit": _num(t.get("seeding_time_limit"), -2),
					"inactiveSeedingTimeLimit": _num(t.get("inactive_seeding_time_limit"), -2),
				}
				for field, param in (("share_limit_action", "shareLimitAction"), ("share_limits_mode", "shareLimitsMode")):
					if t.get(field):
						data[param] = t[field]
				self._post("torrents/setShareLimits", data=data)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting ratio limit: {e}")

	def set_torrent_speed_limit(self, torrent_ids, direction, kbps):
		endpoint = "torrents/setDownloadLimit" if direction == "down" else "torrents/setUploadLimit"
		try:
			self._post(endpoint, data={"hashes": "|".join(torrent_ids), "limit": int(kbps) * 1024 if kbps > 0 else 0})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting speed limit: {e}")

	def reannounce_torrents(self, torrent_ids):
		try:
			self._post("torrents/reannounce", data={"hashes": "|".join(torrent_ids)})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error reannouncing: {e}")

	def get_global_ratio_limit(self):
		try:
			prefs = self._get_json("app/preferences", kind=dict)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting ratio limit: {e}")
		return _limit(prefs.get("max_ratio")) if prefs.get("max_ratio_enabled") else -1

	def set_global_ratio_limit(self, limit):
		try:
			prefs = {"max_ratio_enabled": False} if limit < 0 else {"max_ratio_enabled": True, "max_ratio": round(float(limit), 2)}
			self._post("app/setPreferences", data={"json": json.dumps(prefs)})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting ratio limit: {e}")

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

	def set_file_priority(self, torrent_id, paths, priority):
		try:
			files = self._get_json("torrents/files", params={"hash": torrent_id}, kind=list)
			wanted = set(paths)
			# The index comes with each file since qBittorrent 4.2, before that it is its position
			indexes = [str(f.get("index", i)) for i, f in enumerate(files) if f.get("name") in wanted]
			if not indexes:
				raise PermanentTorrentError(f"No such files in torrent {torrent_id}")
			self._post("torrents/filePrio", data={
				"hash": torrent_id, "id": "|".join(indexes), "priority": FILE_PRIORITY_VALUES[priority]})
		except TorrentClientError as e:
			if isinstance(e, PermanentTorrentError):
				raise
			raise TorrentClientError(f"Error setting the priority of the files of {torrent_id}: {e}")

	def rename_torrent(self, torrent_id, new_name):
		try:
			torrents = self._get_json("torrents/info", params={"hashes": torrent_id}, kind=list)
			if not torrents:
				raise TorrentClientError("Torrent not found")
			files = self._get_json("torrents/files", params={"hash": torrent_id}, kind=list)
			paths = [f.get("name", "") for f in files]
			self._post("torrents/rename", data={"hash": torrent_id, "name": new_name})
			# The path on disk has to come from the file list: qBittorrent sanitizes
			# it, so it does not always match the torrent name
			root = content_root(paths)
			if root:
				# Content inside a folder: rename the folder, unless it has that
				# name already (qBittorrent answers 409 to a rename onto itself)
				if root != new_name:
					self._post("torrents/renameFolder", data={
						"hash": torrent_id, "oldPath": root, "newPath": new_name})
			elif len(paths) == 1 and paths[0] != new_name:
				# Single file at top level: rename the file on disk too
				self._post("torrents/renameFile", data={
					"hash": torrent_id, "oldPath": paths[0], "newPath": new_name})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error renaming torrent {torrent_id}: {e}")

	def rename_file(self, torrent_id, old_path, new_name):
		folder = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
		new_path = f"{folder}/{new_name}" if folder else new_name
		try:
			self._post("torrents/renameFile", data={
				"hash": torrent_id, "oldPath": old_path, "newPath": new_path})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error renaming file {old_path}: {e}")

	def rename_folder(self, torrent_id, old_path, new_name):
		parent = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
		new_path = f"{parent}/{new_name}" if parent else new_name
		try:
			self._post("torrents/renameFolder", data={
				"hash": torrent_id, "oldPath": old_path, "newPath": new_path})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error renaming folder {old_path}: {e}")

	def move_torrents(self, torrent_ids, new_dir):
		try:
			self._post("torrents/setLocation", data={
				"hashes": "|".join(torrent_ids), "location": new_dir}, timeout=MOVE_RPC_TIMEOUT)
		except TorrentClientError as e:
			# 400, 403 and 409 are qBittorrent refusing the folder (empty, not
			# writable, cannot be created): asking again gets the same answer
			kind = PermanentTorrentError if getattr(e, "status_code", None) in (400, 403, 409) else TorrentClientError
			raise kind(f"Error moving torrents: {e}")

	def get_categories(self):
		try:
			categories = self._get_json("torrents/categories")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting categories: {e}")
		if not isinstance(categories, dict):
			return []
		result = [(name, (data or {}).get("savePath", "") or "") for name, data in categories.items()]
		result.sort(key=lambda c: c[0].lower())
		return result

	def set_category(self, torrent_ids, category):
		# An auto managed torrent is relocated by this call, so it blocks for as
		# long as moving the data takes, exactly like setLocation does
		try:
			self._post("torrents/setCategory", data={
				"hashes": "|".join(torrent_ids), "category": category or ""}, timeout=MOVE_RPC_TIMEOUT)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting category: {e}")

	def set_auto_managed(self, torrent_ids, enabled):
		# Relocates the data to the category folder, so it blocks like a move
		try:
			self._post("torrents/setAutoManagement", data={
				"hashes": "|".join(torrent_ids),
				"enable": "true" if enabled else "false"}, timeout=MOVE_RPC_TIMEOUT)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting automatic management: {e}")

	def get_download_dirs(self):
		try:
			torrents = self._get_json("torrents/info", kind=list)
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
			return self._get_json("app/preferences", kind=dict).get("save_path", "")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting default download dir: {e}")

	def get_free_space(self, path):
		"""qBittorrent only tells the free space of its default save path: any
		other folder may be on another disk, so its space is unknown (-1)"""
		try:
			if path:
				save_path = (self._get_json("app/preferences", kind=dict).get("save_path") or "").rstrip("/")
				folder = path.rstrip("/")
				if save_path and folder != save_path and not folder.startswith(save_path + "/"):
					return -1
			server_state = self._get_json("sync/maindata", kind=dict).get("server_state", {})
			return server_state.get("free_space_on_disk", -1)
		except TorrentClientError:
			return -1

	def get_settings(self):
		try:
			prefs = self._get_json("app/preferences", kind=dict)
			return {
				"version": self._version(),
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
				prefs = self._get_json("app/preferences", kind=dict)
				current = prefs.get("dl_limit" if direction == "down" else "up_limit", 0) or 0
				kbps = current // 1024
			limit = int(kbps) * 1024 if enabled and int(kbps) > 0 else 0
			key = "dl_limit" if direction == "down" else "up_limit"
			self._post("app/setPreferences", data={"json": json.dumps({key: limit})})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting speed limit: {e}")
