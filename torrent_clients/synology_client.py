"""Synology Download Station implementation of the TorrentClient interface (DSM Web API)

Download Station has no rename and no recheck API, so those capabilities are
switched off and the bot hides the corresponding buttons. It has no turtle mode
either. Deleting the downloaded data is done through File Station, because the
Download Station API only removes the task.
"""

import json
import time
from collections import Counter
from urllib.parse import urlparse

import requests
import urllib3

from torrent_clients.base import (
	PermanentTorrentError,
	SessionSummary,
	TorrentClient,
	TorrentClientError,
	TorrentInfo,
	TorrentStatus,
	sort_files,
)

REQUEST_TIMEOUT = 20
MOVE_RPC_TIMEOUT = 60
UPLOAD_TIMEOUT = 120
MOVE_POLL_DELAY = 2  # Seconds between polls of a File Station move
MOVE_POLL_ATTEMPTS = 1800  # Enough for a big torrent moved across volumes
PAUSE_SETTLE_DELAY = 1  # Seconds given to Download Station to let go of the files
DESTINATION_ATTEMPTS = 8  # Polls waiting for a destination change to be applied
DESTINATION_DELAY = 1
RESTART_ATTEMPTS = 10  # Polls waiting for a task to come out of its finished state
NEW_TASK_ATTEMPTS = 20  # Polls waiting for the created task to show up in the list
METADATA_ATTEMPTS = 6  # Polls waiting for the real name and size of the new task
NEW_TASK_DELAY = 0.5

AUTH_API = "SYNO.API.Auth"
INFO_API = "SYNO.DownloadStation.Info"
TASK_API = "SYNO.DownloadStation.Task"
TASK2_API = "SYNO.DownloadStation2.Task"  # DSM 7: needed to upload .torrent files
STAT_API = "SYNO.DownloadStation.Statistic"
SHARE_API = "SYNO.FileStation.List"
DELETE_API = "SYNO.FileStation.Delete"
FOLDER_API = "SYNO.FileStation.CreateFolder"
COPYMOVE_API = "SYNO.FileStation.CopyMove"

DISCOVER_APIS = (
	AUTH_API, INFO_API, TASK_API, TASK2_API, STAT_API,
	SHARE_API, DELETE_API, FOLDER_API, COPYMOVE_API,
)

# Codes the Task API answers when the destination folder does not exist:
# create reports it properly, edit only says "unknown error"
DESTINATION_MISSING = (403, 100)

UNKNOWN_TASK = 404  # Task API code for an id that is not in Download Station

# Failures Download Station reports for a single task inside a successful
# answer. Both mean there is nothing left to do, so they are only worth an
# error when every task of the batch failed
ITEM_ERRORS = {
	405: "the task cannot do that in its current state",
	544: "the task no longer exists",
}

SESSION_EXPIRED_CODES = {105, 106, 107, 119}

COMMON_ERRORS = {
	100: "Unknown error",
	101: "Invalid parameter",
	102: "The requested API does not exist",
	103: "The requested method does not exist",
	104: "The requested version does not support this functionality",
	105: "The logged in user has no permission for this API",
	106: "Session timeout",
	107: "Session interrupted by a duplicate login",
	119: "Invalid session (SID not found)",
}

AUTH_ERRORS = {
	400: "Wrong user or password",
	401: "Account disabled",
	402: "Permission denied",
	403: "The account has two step verification enabled: use a DSM user without 2FA for the bot",
	404: "Failed to authenticate the two step verification code",
	406: "DSM enforces two step verification for this account: use a DSM user without 2FA for the bot",
}

TASK_ERRORS = {
	400: "File upload failed",
	401: "Maximum number of tasks reached",
	402: "Destination denied",
	403: "Destination does not exist",
	404: "Invalid task id",
	405: "Invalid task action",
	406: "No default destination configured in Download Station",
	407: "Setting the destination failed",
	408: "File does not exist",
}

# File Station reuses the same numbers for other meanings
FILE_ERRORS = {
	400: "Invalid parameter of the file operation",
	401: "Unknown error of the file operation",
	402: "The system is too busy",
	403: "The user is not allowed to do this file operation",
	407: "Operation not permitted",
	408: "No such file or folder",
	409: "Non supported file system",
	414: "No such file or folder",
	900: "Failed to delete the files",
	1100: "Failed to create the folder",
	1101: "The number of folders to create has reached the system limit",
}

# Download Station reports the reason of a failed task as a machine code
DUPLICATE_ERROR = "torrent_duplicate"

ERROR_DETAILS = {
	DUPLICATE_ERROR: "The torrent is already in Download Station",
	"broken_link": "Broken link",
	"destination_not_exist": "The destination folder does not exist",
	"destination_denied": "Access to the destination folder was denied",
	"disk_full": "The disk is full",
	"quota_reached": "The user quota has been reached",
	"timeout": "Timeout",
	"exceed_max_fs_size": "The file exceeds the maximum size of the file system",
	"exceed_max_temp_file_size": "The file exceeds the maximum temporary file size",
	"exceed_max_dest_fs_size": "The file does not fit in the destination file system",
	"name_too_long": "The name of the file is too long",
	"encrypted_name_too_long": "The name of the file is too long",
	"not_supported_type": "Unsupported download type",
	"filesystem_abort": "The file system aborted the download",
	"required_premium_account": "A premium account is required",
	"private_video": "The video is private",
	"try_it_later": "Download Station asks to try again later",
	"torrent_invalid": "The torrent file is not valid",
	"missing_python": "Python is not installed on the NAS",
}

# Download Station statuses. "finished" means the download is complete and the
# task is no longer active, so it behaves like a paused torrent: it is mapped to
# PAUSED and the bot offers to resume (seed) it
STATUS_MAP = {
	"waiting": TorrentStatus.QUEUED,
	"filehosting_waiting": TorrentStatus.QUEUED,
	"downloading": TorrentStatus.DOWNLOADING,
	"finishing": TorrentStatus.DOWNLOADING,
	"seeding": TorrentStatus.SEEDING,
	"paused": TorrentStatus.PAUSED,
	"finished": TorrentStatus.PAUSED,
	"hash_checking": TorrentStatus.CHECKING,
	"extracting": TorrentStatus.CHECKING,
	"error": TorrentStatus.ERROR,
}

# Some DSM versions report the status as a number instead of a string
STATUS_CODES = {
	1: "waiting", 2: "downloading", 3: "paused", 4: "finishing", 5: "finished",
	6: "hash_checking", 7: "seeding", 8: "filehosting_waiting", 9: "extracting",
	101: "error",
}

# A finished task is done for Download Station: it cannot be resumed, and
# pausing what is already stopped is rejected for the whole batch
PAUSABLE_STATUSES = (
	TorrentStatus.DOWNLOADING, TorrentStatus.SEEDING, TorrentStatus.QUEUED, TorrentStatus.CHECKING)
RESUMABLE_STATUSES = (TorrentStatus.PAUSED, TorrentStatus.ERROR)

LIGHT_ADDITIONAL = "detail,transfer"
FULL_ADDITIONAL = "detail,transfer,file,tracker"


def _mapping(value):
	"""Download Station answers an empty string instead of an object for the
	parts of a task it has not computed yet"""
	return value if isinstance(value, dict) else {}


def _sequence(value):
	return value if isinstance(value, list) else []


def _status(t):
	"""Normalized status of a raw task. Some DSM versions report it as a number"""
	status = t.get("status")
	if isinstance(status, int):
		status = STATUS_CODES.get(status, "")
	return STATUS_MAP.get(str(status or "").lower(), TorrentStatus.PAUSED)


def _num(value, default=0):
	"""Download Station returns some numeric fields as strings"""
	try:
		return int(value)
	except (TypeError, ValueError):
		try:
			return int(float(value))
		except (TypeError, ValueError):
			return default


class SynologyApiError(TorrentClientError):
	"""Error reported by DSM, keeping the code so the caller can react to it"""

	def __init__(self, message, code):
		super().__init__(message)
		self.code = code


class SessionExpired(Exception):
	"""Internal marker: the DSM session id is no longer valid"""

	def __init__(self, code):
		super().__init__(f"Synology session expired (code {code})")
		self.code = code


class SynologyClient(TorrentClient):
	supports_alt_speed = False  # Download Station has no alternative speed mode
	supports_rename = False  # No rename API for tasks or files
	supports_verify = False  # No recheck API

	def __init__(self, host, port, username=None, password=None, protocol="http", api_path=None):
		self.base_url = f"{protocol}://{host}:{port}/{(api_path or 'webapi').strip('/')}"
		self.username = username or ""
		self.password = password or ""
		self.session = requests.Session()
		if protocol == "https":
			# DSM ships a self signed certificate on its https port
			self.session.verify = False
			urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
		self.sid = None
		self._api_info = {}
		self._saved_limits = {}
		self._discover()
		self._login()

	# -- Transport ---------------------------------------------------------

	def _url(self, api):
		entry = self._api_info.get(api) or {}
		return f"{self.base_url}/{entry.get('path', 'entry.cgi')}"

	def _version(self, api, preferred):
		"""Highest version we know how to talk that the NAS also supports"""
		entry = self._api_info.get(api) or {}
		low = _num(entry.get("minVersion"), 1)
		high = _num(entry.get("maxVersion"), preferred)
		return max(low, min(preferred, high)) if high else preferred

	def _describe_error(self, api, code):
		if api == AUTH_API:
			table = AUTH_ERRORS
		elif api.startswith("SYNO.FileStation"):
			table = FILE_ERRORS
		else:
			table = TASK_ERRORS
		return COMMON_ERRORS.get(code) or table.get(code) or f"error code {code}"

	def _discover(self):
		url = f"{self.base_url}/query.cgi"
		try:
			response = self.session.get(
				url,
				params={
					"api": "SYNO.API.Info", "version": 1, "method": "query",
					"query": ",".join(DISCOVER_APIS),
				},
				timeout=REQUEST_TIMEOUT,
			)
		except Exception as e:
			raise TorrentClientError(f"Cannot connect to Synology DSM at {self.base_url}: {e}")
		try:
			data = response.json()
		except ValueError:
			# Typically the path of the API left over from another torrent manager
			raise TorrentClientError(
				f"{url} does not answer the DSM web API (HTTP {response.status_code}). "
				f"The API path is wrong: for Download Station it has to be /webapi")
		if not data.get("success"):
			raise TorrentClientError(f"Synology DSM did not answer the API discovery at {self.base_url}")
		self._api_info = data.get("data") or {}
		if TASK_API not in self._api_info:
			raise TorrentClientError("Download Station is not installed or not running on this NAS")

	def _login(self):
		params = {
			"api": AUTH_API,
			"version": self._version(AUTH_API, 3),
			"method": "login",
			"account": self.username,
			"passwd": self.password,
			"session": "DownloadStation",
			"format": "sid",
		}
		try:
			response = self.session.post(self._url(AUTH_API), data=params, timeout=REQUEST_TIMEOUT)
			data = response.json()
		except Exception as e:
			raise TorrentClientError(f"Cannot log in to Synology DSM at {self.base_url}: {e}")
		if not data.get("success"):
			code = _num((data.get("error") or {}).get("code"), 0)
			raise TorrentClientError(f"Synology login failed: {self._describe_error(AUTH_API, code)}")
		self.sid = (data.get("data") or {}).get("sid")
		if not self.sid:
			raise TorrentClientError("Synology login failed: no session id returned")

	def _request(self, api, method, version=1, params=None, files=None, timeout=REQUEST_TIMEOUT):
		payload = {"api": api, "version": self._version(api, version), "method": method}
		payload.update({k: v for k, v in (params or {}).items() if v is not None})
		try:
			if files:
				# DSM does not read _sid out of a multipart body: those calls
				# are authenticated with the session cookie set at login
				response = self.session.post(self._url(api), data=payload, files=files, timeout=timeout)
			else:
				payload["_sid"] = self.sid
				response = self.session.post(self._url(api), data=payload, timeout=timeout)
		except Exception as e:
			raise TorrentClientError(f"Synology request failed ({api}.{method}): {e}")
		if response.status_code >= 400:
			raise TorrentClientError(f"Synology error {response.status_code} on {api}.{method}")
		try:
			data = response.json()
		except ValueError:
			raise TorrentClientError(f"Synology returned a non JSON answer on {api}.{method}")
		if not data.get("success"):
			code = _num((data.get("error") or {}).get("code"), 0)
			if code in SESSION_EXPIRED_CODES:
				raise SessionExpired(code)
			raise SynologyApiError(
				f"Synology error on {api}.{method}: {self._describe_error(api, code)}", code)
		return data.get("data")

	def _call(self, api, method, version=1, params=None, files=None, timeout=REQUEST_TIMEOUT):
		"""Request with automatic re-login when the DSM session expires"""
		if api not in self._api_info:
			raise TorrentClientError(f"{api} is not available on this NAS")
		try:
			return self._request(api, method, version, params, files, timeout)
		except SessionExpired:
			self._login()
			try:
				return self._request(api, method, version, params, files, timeout)
			except SessionExpired as e:
				raise TorrentClientError(f"Synology session error on {api}.{method}: {self._describe_error(api, e.code)}")

	def _check_items(self, data, action, nothing_to_do_is_fine=False):
		"""Download Station answers success and reports the failures of each
		task inside the payload, so they have to be looked at one by one.
		nothing_to_do_is_fine is for actions whose goal is already met when the
		task refuses it or is not there any more, like removing it"""
		items = [_mapping(i) for i in _sequence(data)]
		failed = [i for i in items if _num(i.get("error"))]
		if not failed:
			return
		unexpected = [i for i in failed if _num(i.get("error")) not in ITEM_ERRORS]
		if not unexpected and (nothing_to_do_is_fine or len(failed) < len(items)):
			return  # Part of the batch had nothing to do, the rest went through
		reasons = sorted({
			ITEM_ERRORS.get(_num(i.get("error")), f"error code {_num(i.get('error'))}")
			for i in (unexpected or failed)})
		raise TorrentClientError(
			f"Download Station could not {action} {len(unexpected or failed)} task(s): {', '.join(reasons)}")

	def _filter_by_status(self, torrent_ids, statuses):
		"""Keeps the ids whose task is in one of the given normalized statuses.
		Download Station rejects the whole batch when one task cannot do the
		action, so the ones that have nothing to do are left out"""
		wanted = {str(i) for i in torrent_ids}
		try:
			current = {str(t.get("id")): _status(t) for t in self._list_tasks(additional=None)}
		except TorrentClientError:
			return [str(i) for i in torrent_ids]
		return [i for i in wanted if current.get(i, "") in statuses]

	# -- Mapping -----------------------------------------------------------

	def _to_info(self, t, full=False):
		additional = _mapping(t.get("additional"))
		detail = _mapping(additional.get("detail"))
		transfer = _mapping(additional.get("transfer"))

		status = _status(t)

		total_size = _num(t.get("size"))
		downloaded = _num(transfer.get("size_downloaded"))
		uploaded = _num(transfer.get("size_uploaded"))
		download_rate = _num(transfer.get("speed_download"))
		# The size of a task counts every file of the torrent, including the ones
		# deselected in Download Station, so the byte count of such a task never
		# adds up to its size. Its completion stamp is what settles it
		if _num(detail.get("completed_time")) > 0:
			progress = 100.0
		elif total_size > 0:
			progress = round(min(downloaded / total_size * 100, 100), 2)
		else:
			progress = 0.0
		eta = -1
		if download_rate > 0 and total_size > downloaded:
			eta = int((total_size - downloaded) / download_rate)

		files = []
		if full:
			for f in _sequence(additional.get("file")):
				f = _mapping(f)
				files.append((f.get("filename", ""), _num(f.get("size")), _num(f.get("size_downloaded"))))
			sort_files(files)

		trackers = []
		for tracker in _sequence(additional.get("tracker")):
			host = urlparse(_mapping(tracker).get("url", "") or "").hostname
			if host and host not in trackers:
				trackers.append(host)

		error_message = ""
		if status == TorrentStatus.ERROR:
			reason = _mapping(t.get("status_extra")).get("error_detail", "")
			error_message = ERROR_DETAILS.get(reason, reason) or "Error"

		seeders = _num(detail.get("connected_seeders"))
		leechers = _num(detail.get("connected_leechers"))
		return TorrentInfo(
			id=str(t.get("id", "")),
			name=t.get("title", ""),
			status=status,
			progress=progress,
			total_size=total_size,
			downloaded=downloaded,
			uploaded=uploaded,
			download_rate=download_rate,
			upload_rate=_num(transfer.get("speed_upload")),
			eta=eta,
			ratio=round(uploaded / downloaded, 2) if downloaded > 0 else 0.0,
			peers=seeders + leechers,
			seeders=seeders,
			leechers=leechers,
			download_dir=detail.get("destination", "") or "",
			error_message=error_message,
			added_date=_num(detail.get("create_time"), None) or None,
			files=files,
			trackers=trackers,
		)

	def _list_tasks(self, additional=LIGHT_ADDITIONAL):
		data = _mapping(self._call(TASK_API, "list", params={
			"additional": additional or None, "offset": 0, "limit": -1}))
		return [_mapping(t) for t in _sequence(data.get("tasks"))]

	# -- TorrentClient interface -------------------------------------------

	def test_connection(self):
		try:
			info = self._call(INFO_API, "getinfo") or {}
		except TorrentClientError as e:
			raise TorrentClientError(f"Cannot connect to Download Station: {e}")
		version = info.get("version_string") or info.get("version") or ""
		return f"Download Station {version}".strip()

	def get_summary(self):
		try:
			# The dashboard refreshes every few seconds, so the file list and the
			# trackers are left out: only the counters and the completion stamp
			# are needed here
			tasks = self._list_tasks(additional=LIGHT_ADDITIONAL)
			stats = self._call(STAT_API, "getinfo") or {}
			free_space = self.get_free_space(None)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting summary: {e}")

		counts = Counter()
		completed = 0
		uploading = 0
		downloading = 0
		for t in tasks:
			info = self._to_info(t)
			counts[info.status] += 1
			if info.is_finished:
				completed += 1
			if info.upload_rate > 0:
				uploading += 1
			if info.download_rate > 0:
				downloading += 1

		return SessionSummary(
			counts=dict(counts),
			total=len(tasks),
			completed=completed,
			uploading=uploading,
			downloading=downloading,
			download_rate=_num(stats.get("speed_download")),
			upload_rate=_num(stats.get("speed_upload")),
			free_space=free_space,
			alt_speed_enabled=False,
		)

	def get_torrents(self, status=None, query=None):
		try:
			# Trackers only come with the full detail, and the bot needs them to
			# offer the tracker filter, so they are asked for in the listing too
			tasks = self._list_tasks(additional=f"{LIGHT_ADDITIONAL},tracker")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error listing torrents: {e}")

		result = []
		query_lower = query.lower() if query else None
		for t in tasks:
			info = self._to_info(t)
			if query_lower and query_lower not in info.name.lower() and query_lower not in info.download_dir.lower():
				continue
			if status and info.status != status:
				continue
			result.append(info)
		result.sort(key=lambda i: i.added_date or 0, reverse=True)
		return result

	def get_torrent(self, torrent_id):
		try:
			data = self._call(TASK_API, "getinfo", params={
				"id": torrent_id, "additional": FULL_ADDITIONAL})
		except SynologyApiError as e:
			if e.code == UNKNOWN_TASK:
				return None  # Already removed
			raise TorrentClientError(f"Error getting torrent {torrent_id}: {e}")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting torrent {torrent_id}: {e}")
		tasks = [_mapping(t) for t in _sequence(_mapping(data).get("tasks"))]
		if not tasks or (tasks[0].get("error") and not tasks[0].get("title")):
			return None
		return self._to_info(tasks[0], full=True)

	def _task_ids(self):
		try:
			return {str(t.get("id")) for t in self._list_tasks(additional=None)}
		except TorrentClientError:
			return set()

	def _ensure_destination(self, destination):
		"""Download Station refuses a destination that does not exist yet, so it
		is created through File Station. Returns False when it cannot be done
		(no File Station permission, or the missing folder is a shared folder,
		which only DSM itself can create)"""
		if FOLDER_API not in self._api_info:
			return False
		parent, _, name = destination.strip("/").rpartition("/")
		if not parent or not name:
			return False
		try:
			self._call(FOLDER_API, "create", version=2, params={
				"folder_path": json.dumps([f"/{parent}"]),
				"name": json.dumps([name]),
				"force_parent": "true",
			})
		except TorrentClientError:
			return False
		return True

	def _create_task(self, magnet, torrent_data, download_dir):
		"""Creates the task and returns its id when the API reports it"""
		destination = (download_dir or "").strip("/") or self.get_default_download_dir()
		if not destination:
			raise TorrentClientError("No default destination configured in Download Station")
		try:
			return self._create_task_once(magnet, torrent_data, destination)
		except SynologyApiError as e:
			if e.code not in DESTINATION_MISSING or not self._ensure_destination(destination):
				raise
			return self._create_task_once(magnet, torrent_data, destination)

	def _create_task_once(self, magnet, torrent_data, destination):
		if magnet:
			# task.cgi accepts links on every DSM version
			try:
				self._call(TASK_API, "create", params={
					"uri": magnet, "destination": destination}, timeout=UPLOAD_TIMEOUT)
				return None
			except SynologyApiError:
				if TASK2_API not in self._api_info:
					raise
				data = self._call(TASK2_API, "create", version=2, params={
					"type": json.dumps("url"),
					"url": json.dumps([magnet]),
					"destination": json.dumps(destination),
					"create_list": "false",
				}, timeout=UPLOAD_TIMEOUT) or {}
				return (data.get("task_id") or [None])[0]

		if TASK2_API in self._api_info:
			# DSM 7 only accepts uploads through DownloadStation2, whose values
			# are JSON encoded. The name inside "file" is the multipart part that
			# carries the content, and it cannot be called "file"
			data = self._call(TASK2_API, "create", version=2, params={
				"type": json.dumps("file"),
				"file": json.dumps(["torrent"]),
				"destination": json.dumps(destination),
				"create_list": "false",
			}, files={"torrent": ("upload.torrent", torrent_data, "application/x-bittorrent")},
				timeout=UPLOAD_TIMEOUT) or {}
			return (data.get("task_id") or [None])[0]

		self._call(TASK_API, "create", params={"destination": destination}, files={
			"file": ("upload.torrent", torrent_data, "application/x-bittorrent")}, timeout=UPLOAD_TIMEOUT)
		return None

	def _new_task(self, known_ids):
		"""Id of the first task that was not in the list before creating this
		one, asked with the cheapest listing there is: a library with thousands
		of torrents is polled several times while the task shows up"""
		for t in self._list_tasks(additional=None):
			task_id = str(t.get("id"))
			if task_id and task_id not in known_ids:
				return task_id
		return None

	def _wait_for_task(self, task_id, known_ids):
		"""A new task is titled after the uploaded file or the link until
		Download Station parses the metadata, so it is polled until the real
		name and size arrive. A magnet has no metadata to parse yet, so the
		wait gives up quickly and the placeholder is returned, like the other
		torrent managers do"""
		info = None
		for _ in range(NEW_TASK_ATTEMPTS):
			if not task_id:
				task_id = self._new_task(known_ids)
			if task_id:
				info = self.get_torrent(task_id)
				if info:
					break
			time.sleep(NEW_TASK_DELAY)
		if not info:
			raise TorrentClientError("Torrent added but not found in Download Station")
		for _ in range(METADATA_ATTEMPTS):
			if info.total_size > 0 or info.files or info.status == TorrentStatus.ERROR:
				break
			time.sleep(NEW_TASK_DELAY)
			info = self.get_torrent(info.id) or info
		return info

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None, category=None):
		if not magnet and not torrent_data:
			raise TorrentClientError("No magnet or torrent data provided")
		known_ids = self._task_ids()
		try:
			task_id = self._create_task(magnet, torrent_data, download_dir)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error adding torrent: {e}")
		info = self._wait_for_task(task_id, known_ids)
		# A torrent that is already there does not fail: Download Station
		# creates a task in error state that has to be cleaned up
		if info.status == TorrentStatus.ERROR and info.error_message == ERROR_DETAILS[DUPLICATE_ERROR]:
			try:
				self._call(TASK_API, "delete", params={"id": info.id, "force_complete": "false"})
			except TorrentClientError:
				pass
			raise TorrentClientError(f"Error adding torrent: {ERROR_DETAILS[DUPLICATE_ERROR]}")
		return info

	def _content_paths(self, info):
		"""Candidate paths of the content of a task on disk, best guess first.
		Download Station lists the files of a torrent relative to its root
		folder without ever naming that folder, so the name of the folder can
		only come from the title of the task"""
		directory = (info.download_dir or "").strip("/")
		if not directory:
			return []
		names = []
		if len(info.files) == 1 and "/" not in info.files[0][0]:
			names.append(info.files[0][0])  # Single file torrent: the file itself
		names.append(info.name)  # Folder torrent, and tasks that list no files
		return [f"/{directory}/{name.strip('/')}" for name in dict.fromkeys(names) if name.strip("/")]

	def _existing_paths(self, paths):
		"""The paths that really are on disk, asked to File Station. A missing
		path comes back as an entry with an error code and no name"""
		if not paths:
			return set()
		try:
			data = self._call(SHARE_API, "getinfo", version=2, params={"path": json.dumps(paths)})
		except TorrentClientError:
			return set()
		return {_mapping(f).get("path") for f in _sequence(_mapping(data).get("files")) if _mapping(f).get("name")}

	def _content_on_disk(self, tasks):
		"""Paths of the content of the given tasks that really is on disk. A
		task that has not written anything yet has nothing to move"""
		candidates = [self._content_paths(info) for info in tasks]
		existing = self._existing_paths([path for paths in candidates for path in paths])
		found = []
		for paths in candidates:
			for path in paths:
				if path in existing:
					found.append(path)
					break
		return found

	def _delete_data(self, torrents):
		"""Removes the content of the given torrents through File Station:
		Download Station only ever deletes the task. Nothing is deleted unless
		File Station confirms the path is there, so a title that does not match
		what is on disk cannot take another folder down with it"""
		targets = self._content_on_disk(torrents)
		if not targets:
			return
		self._call(DELETE_API, "delete", version=2, params={
			"path": json.dumps(targets), "recursive": "true"}, timeout=MOVE_RPC_TIMEOUT)

	def remove_torrents(self, torrent_ids, delete_data=False):
		torrent_ids = [str(i) for i in torrent_ids]
		targets = []
		if delete_data:
			if DELETE_API not in self._api_info:
				raise TorrentClientError(
					"Deleting the downloaded data needs File Station, which is not available for this user")
			# The paths have to be resolved before the tasks disappear
			targets = self._tasks_by_id(torrent_ids)
		try:
			self._check_items(self._call(TASK_API, "delete", params={
				"id": ",".join(torrent_ids), "force_complete": "false"}),
				"remove", nothing_to_do_is_fine=True)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error removing torrents: {e}")
		if targets:
			try:
				self._delete_data(targets)
			except TorrentClientError as e:
				raise TorrentClientError(f"Torrents removed but their data could not be deleted: {e}")

	def pause_torrents(self, torrent_ids):
		targets = self._filter_by_status(torrent_ids, PAUSABLE_STATUSES)
		if not targets:
			return  # Everything is already stopped
		try:
			self._check_items(self._call(TASK_API, "pause", params={"id": ",".join(targets)}), "pause")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error pausing torrents: {e}")

	def resume_torrents(self, torrent_ids):
		targets = self._filter_by_status(torrent_ids, RESUMABLE_STATUSES)
		if not targets:
			return
		try:
			self._check_items(self._call(TASK_API, "resume", params={"id": ",".join(targets)}), "resume")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error resuming torrents: {e}")

	def verify_torrent(self, torrent_id):
		raise PermanentTorrentError("Download Station cannot verify the data of a task")

	def rename_torrent(self, torrent_id, new_name):
		raise PermanentTorrentError("Download Station cannot rename tasks")

	def rename_file(self, torrent_id, old_path, new_name):
		raise PermanentTorrentError("Download Station cannot rename files inside a task")

	def move_torrents(self, torrent_ids, new_dir):
		"""Changing the destination of a task only tells Download Station where
		the data still to come has to go: whatever is already on disk stays
		where it is (the API answers success all the same). So the content is
		moved with File Station and the task is pointed at the new folder"""
		destination = new_dir.strip("/")
		try:
			self._move(torrent_ids, destination)
		except PermanentTorrentError as e:
			raise PermanentTorrentError(f"Error moving torrents: {e}")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error moving torrents: {e}")

	def _move(self, torrent_ids, destination):
		ids = [str(i) for i in torrent_ids]
		if not destination:
			raise TorrentClientError("No destination given")
		self._require_folder(destination)
		tasks = [t for t in self._tasks_by_id(ids) if (t.download_dir or "").strip("/") != destination]
		if not tasks:
			return  # Everything is already there
		moving = [t.id for t in tasks]
		sources = self._content_on_disk(tasks)
		self._refuse_collisions(destination, sources)

		# A task that is running holds its files: it is stopped for the move and
		# started again afterwards, whatever the outcome
		active = [t.id for t in tasks if t.status in PAUSABLE_STATUSES]
		if active:
			self._check_items(self._call(TASK_API, "pause", params={"id": ",".join(active)}), "pause")
			time.sleep(PAUSE_SETTLE_DELAY)
		try:
			# Download Station is asked first: it records the new destination
			# and, depending on the kind of task, moves the data by itself.
			# Nothing is touched on disk until it confirms the change, so a
			# refusal leaves everything exactly as it was
			self._edit_destination(moving, destination)
			fresh, refused = self._destination_state(moving, destination)
			if refused:
				# Download Station ignores the change for a task it considers
				# over. Starting it and stopping it again takes it out of that
				# state, at the price of a full data check, so it is only done
				# for the tasks that need it
				self._restart(refused)
				self._edit_destination(refused, destination)
				fresh, refused = self._destination_state(moving, destination)
			if refused:
				name = next((t.name for t in fresh if t.id in refused), refused[0])
				raise PermanentTorrentError(
					f"Download Station refuses to change the destination of {name!r}: it considers "
					f"that download over. The data was left where it was")
			# Whatever it left behind is moved with File Station
			arrived = set(self._content_on_disk(fresh))
			pending = sorted(
				path for path in self._existing_paths(sources)
				if f"/{destination}/{path.rsplit('/', 1)[-1]}" not in arrived)
			if pending:
				self._copy_move(pending, destination)
		finally:
			if active:
				try:
					self._call(TASK_API, "resume", params={"id": ",".join(active)})
				except TorrentClientError:
					pass  # Reporting the move matters more than the restart

	def _destination_state(self, ids, destination):
		"""Download Station applies a destination change in the background, and
		answers success even for the tasks it ends up ignoring, so the change
		has to be waited for and checked. Returns the tasks with their new
		state and the ids that did not take the change"""
		fresh = []
		for _ in range(DESTINATION_ATTEMPTS):
			fresh = self._tasks_by_id(ids)
			refused = [t.id for t in fresh if (t.download_dir or "").strip("/") != destination]
			if not refused:
				return fresh, []
			time.sleep(DESTINATION_DELAY)
		return fresh, refused

	def _restart(self, ids):
		"""Takes tasks out of the state where Download Station considers them
		over, which is the state in which it refuses to move them. Not every
		task can leave it (a finished download that is not a torrent has
		nothing to restart), so failures are left for the caller to report"""
		joined = ",".join(ids)
		try:
			self._call(TASK_API, "resume", params={"id": joined})
			for _ in range(RESTART_ATTEMPTS):
				time.sleep(DESTINATION_DELAY)
				statuses = {str(t.get("id")): _status(t) for t in self._list_tasks(additional=None)}
				if all(statuses.get(i) != TorrentStatus.PAUSED for i in ids):
					break
			self._call(TASK_API, "pause", params={"id": joined})
			time.sleep(PAUSE_SETTLE_DELAY)
		except TorrentClientError:
			pass

	def _tasks_by_id(self, ids):
		"""Full info of the given tasks, in a single call when it can be done.
		Ids that are not there any more are left out of the result"""
		try:
			data = self._call(TASK_API, "getinfo", params={
				"id": ",".join(ids), "additional": "detail,file"})
			tasks = [_mapping(t) for t in _sequence(_mapping(data).get("tasks"))]
		except SynologyApiError as e:
			if e.code != UNKNOWN_TASK:
				raise
			# One stale id makes Download Station refuse the whole batch, so the
			# tasks are picked out of the listing instead
			wanted = {str(i) for i in ids}
			tasks = [
				t for t in self._list_tasks(additional="detail,file")
				if str(t.get("id")) in wanted
			]
		return [self._to_info(t, full=True) for t in tasks if t.get("title")]

	def _require_folder(self, destination):
		"""Makes sure the destination folder is there before moving into it"""
		if SHARE_API not in self._api_info or self._existing_paths([f"/{destination}"]):
			return
		if not self._ensure_destination(destination):
			raise TorrentClientError(
				f"The destination folder '{destination}' does not exist and cannot be created")

	def _refuse_collisions(self, destination, sources):
		"""Moving on top of something already there would either overwrite the
		content or be silently skipped, so it is refused instead"""
		targets = {f"/{destination}/{path.rsplit('/', 1)[-1]}": path for path in sources}
		clashing = sorted(self._existing_paths(list(targets)))
		if clashing:
			names = ", ".join(path.rsplit("/", 1)[-1] for path in clashing)
			raise TorrentClientError(f"'{names}' already exists in {destination}")

	def _copy_move(self, paths, destination):
		"""File Station moves in the background: the job has to be polled"""
		data = _mapping(self._call(COPYMOVE_API, "start", version=3, params={
			"path": json.dumps(paths),
			"dest_folder_path": f"/{destination}",
			"remove_src": "true",
			"overwrite": "false",
			"accurate_progress": "true",
		}))
		job = data.get("taskid")
		if not job:
			raise TorrentClientError("File Station did not start the move")
		for _ in range(MOVE_POLL_ATTEMPTS):
			time.sleep(MOVE_POLL_DELAY)
			status = _mapping(self._call(COPYMOVE_API, "status", version=3, params={"taskid": job}))
			if status.get("finished"):
				failures = _sequence(status.get("errors"))
				if failures:
					raise TorrentClientError(f"File Station could not move the data: {failures}")
				return
		raise TorrentClientError("File Station is still moving the data")

	def _edit_destination(self, torrent_ids, destination):
		"""DSM 7 dropped edit from version 1 of the Task API, DSM 6 only has
		that one, so both are tried"""
		params = {"id": ",".join(str(i) for i in torrent_ids), "destination": destination}
		try:
			data = self._call(TASK_API, "edit", version=2, params=params, timeout=MOVE_RPC_TIMEOUT)
		except SynologyApiError as e:
			if e.code != 103:  # The method does not exist in this version
				raise
			data = self._call(TASK_API, "edit", version=1, params=params, timeout=MOVE_RPC_TIMEOUT)
		self._check_items(data, "move")

	def get_download_dirs(self):
		try:
			tasks = self._list_tasks(additional="detail")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting download dirs: {e}")
		counts = Counter()
		for t in tasks:
			directory = _mapping(_mapping(t.get("additional")).get("detail")).get("destination", "")
			if directory:
				counts[directory.rstrip("/") or "/"] += 1
		default_dir = self.get_default_download_dir()
		if default_dir:
			counts[default_dir.rstrip("/") or "/"] += 0
		return [d for d, _ in counts.most_common()]

	def get_default_download_dir(self):
		try:
			config = self._call(INFO_API, "getconfig") or {}
			return config.get("default_destination", "") or ""
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting default download dir: {e}")

	def get_free_space(self, path):
		if SHARE_API not in self._api_info:
			return -1
		try:
			if not path:
				path = (self._call(INFO_API, "getconfig") or {}).get("default_destination", "")
			share = (path or "").strip("/").split("/")[0]
			if not share:
				return -1
			data = self._call(SHARE_API, "list_share", version=2, params={
				"additional": json.dumps(["volume_status"])}) or {}
		except TorrentClientError:
			return -1
		for entry in _sequence(data.get("shares")):
			entry = _mapping(entry)
			if entry.get("name", "").lower() == share.lower():
				volume = _mapping(_mapping(entry.get("additional")).get("volume_status"))
				return _num(volume.get("freespace"), -1)
		return -1

	def get_settings(self):
		try:
			config = self._call(INFO_API, "getconfig") or {}
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting settings: {e}")
		down = _num(config.get("bt_max_download"))  # KB/s, 0 means unlimited
		up = _num(config.get("bt_max_upload"))
		return {
			"version": self.test_connection(),
			"alt_speed_enabled": False,
			"alt_speed_down": 0,
			"alt_speed_up": 0,
			"speed_limit_down": down if down > 0 else 0,
			"speed_limit_down_enabled": down > 0,
			"speed_limit_up": up if up > 0 else 0,
			"speed_limit_up_enabled": up > 0,
			"download_dir": config.get("default_destination", "") or "",
		}

	def set_alt_speed(self, enabled):
		raise PermanentTorrentError("Download Station does not support alternative speed limits")

	def set_speed_limit(self, direction, kbps, enabled):
		key = "bt_max_download" if direction == "down" else "bt_max_upload"
		try:
			config = self._call(INFO_API, "getconfig") or {}
			current = _num(config.get(key))
			if kbps is None:
				kbps = current if current > 0 else self._saved_limits.get(key, 0)
			if enabled and int(kbps) > 0:
				self._call(INFO_API, "setserverconfig", params={key: int(kbps)})
			else:
				if current > 0:
					self._saved_limits[key] = current  # Remember it for re-enabling
				self._call(INFO_API, "setserverconfig", params={key: 0})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting speed limit: {e}")
