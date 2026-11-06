"""Synology Download Station implementation of the TorrentClient interface (DSM Web API)

Download Station has no rename and no recheck API, so those capabilities are
switched off and the bot hides the corresponding buttons. It has no turtle mode
either. Deleting the downloaded data is done through File Station, because the
Download Station API only removes the task.

Moving the data is done through File Station too, and a big move outlives the
call that ordered it. Until it ends, its tasks stay stopped and pointing at a
folder that does not hold all of their data yet, so the moves that have not
ended are written down in the config folder of the bot: a restart of the bot
does not take those tasks for moved.
"""

import base64
import json
import os
import threading
import time
from collections import Counter
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import requests
import urllib3

from torrent_clients.base import (
	FILE_HIGH,
	FILE_LOW,
	FILE_NORMAL,
	FILE_SKIP,
	DuplicateTorrentError,
	PermanentTorrentError,
	SessionSummary,
	TorrentClient,
	TorrentClientError,
	TorrentInfo,
	TorrentStatus,
	_bencode_entries,
	capped_progress,
	expected_torrent_ids,
	query_matcher,
	sort_files,
)

REQUEST_TIMEOUT = 20
MOVE_RPC_TIMEOUT = 60
UPLOAD_TIMEOUT = 120
MOVE_POLL_DELAY = 2  # Seconds between polls of a File Station move
MOVE_POLL_ATTEMPTS = 1800  # Enough for a big torrent moved across volumes
WATCH_DELAY = 10  # Seconds between looks at the disk while no File Station job can be asked about a move
WATCH_ATTEMPTS = 360  # An hour of them, like the polls of a job, before the attempt gives up
WATCH_STILL = 900  # Seconds the data has to stay as it is before nothing is taken to be moving it
WATCH_FILES = 100  # Files of each task looked at to see its data move
DRIVE_STEPS = 8  # Stages a move goes through in one attempt, more than it ever needs
MOVES_FILE = "download_station_moves.json"  # The moves that have not ended, in the config folder of the bot
PAUSE_SETTLE_DELAY = 1  # Seconds given to Download Station to let go of the files
DESTINATION_ATTEMPTS = 8  # Polls waiting for a destination change to be applied
DESTINATION_DELAY = 1
RESTART_ATTEMPTS = 10  # Polls waiting for a task to come out of its finished state
NEW_TASK_ATTEMPTS = 20  # Polls waiting for the created task to show up in the list
METADATA_ATTEMPTS = 6  # Polls waiting for the real name and size of the new task
NEW_TASK_DELAY = 0.5
EVIDENCE_FILES = 3  # Files of a task looked for on disk to tell its content from someone else's

AUTH_API = "SYNO.API.Auth"
INFO_API = "SYNO.DownloadStation.Info"
TASK_API = "SYNO.DownloadStation.Task"
TASK2_API = "SYNO.DownloadStation2.Task"  # DSM 7: needed to upload .torrent files
STAT_API = "SYNO.DownloadStation.Statistic"
BT_FILE_API = "SYNO.DownloadStation2.Task.BT.File"  # DSM 7: which files of a task are downloaded
SHARE_API = "SYNO.FileStation.List"
DELETE_API = "SYNO.FileStation.Delete"
FOLDER_API = "SYNO.FileStation.CreateFolder"
COPYMOVE_API = "SYNO.FileStation.CopyMove"

DISCOVER_APIS = (
	AUTH_API, INFO_API, TASK_API, TASK2_API, STAT_API, BT_FILE_API,
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
# Of those, the one that does not go away by logging in again: the user is
# not allowed to use that API
NO_PERMISSION = 105

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
	410: "Failed to connect to a network file system",
	411: "Read-only file system",
	412: "The file name is too long",
	413: "The file name is too long",  # For an encrypted file system
	414: "File already exists",
	415: "Disk quota exceeded",
	416: "No space left on device",
	417: "Input/output error",
	418: "Illegal name or path",
	419: "Illegal file name",
	420: "Illegal file name on a FAT file system",
	421: "Device or resource busy",
	599: "No such task of the file operation",
	900: "Failed to delete the files",
	1000: "Failed to copy the files",
	1001: "Failed to move the files",
	1002: "An error occurred at the destination",
	1003: "Cannot overwrite or skip the existing file",
	1004: "A file cannot overwrite a folder with the same name, nor a folder a file",
	1006: "Cannot move a file or folder with special characters to a FAT32 file system",
	1007: "Cannot move a file bigger than 4 GB to a FAT32 file system",
	1100: "Failed to create the folder",
	1101: "The number of folders to create has reached the system limit",
}

# File Station answers the status of a move that went wrong with one of these
# codes, and the errors object of the answer says which files failed and why
COPYMOVE_FAILED = (1000, 1001, 1002, 1003, 1004, 1006, 1007)
NO_SUCH_JOB = 599  # File Station does not know the background job (any more)

# Where the data of a task is while it is moved (see SynologyClient._disk_states)
MOVED = "moved"  # All of it at the destination, or nothing on disk to move
UNTOUCHED = "untouched"  # All of it still where it was
SPLIT = "split"  # Anything in between

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


def _magnet_keys(link):
	"""Info hashes (lowercase hex, or the multihash of a v2 torrent) and
	display names (casefolded) of a magnet link: what tells its task apart"""
	if not (link or "").strip().lower().startswith("magnet:"):
		return set(), set()
	params = parse_qs(urlparse(link.strip()).query)
	hashes = set()
	for topic in params.get("xt", []):
		kind, _, value = topic.rpartition(":")
		kind = kind.lower()
		if kind == "urn:btih" and len(value) == 32:
			try:
				value = base64.b32decode(value.upper()).hex()  # The short form of the same hash
			except ValueError:
				continue
		if kind in ("urn:btih", "urn:btmh") and value:
			hashes.add(value.lower())
	names = {name.strip().casefold() for name in params.get("dn", []) if name.strip()}
	return hashes, names


def _torrent_name(torrent_data):
	"""Name of the torrent inside the content of a .torrent file, which
	Download Station titles its task after. None when it cannot be read"""
	if not isinstance(torrent_data, (bytes, bytearray)):
		return None
	try:
		for key, start, end in _bencode_entries(torrent_data):
			if key != b"info":
				continue
			info = bytes(torrent_data[start:end])
			for name_key, name_start, name_end in _bencode_entries(info):
				if name_key == b"name":
					value = info[name_start:name_end]
					return value[value.index(b":") + 1:].decode("utf-8", "replace").strip() or None
			return None
	except (ValueError, IndexError, RecursionError):
		return None
	return None


def _quoted(tasks):
	return ", ".join(repr(t.name) for t in tasks)


def _placed(tasks):
	"""Names of tasks with the folder each one was in before the move"""
	return ", ".join(f"{t.name!r} ({(t.download_dir or '').strip('/')})" for t in tasks)


def _moves_path():
	"""File where the moves that have not ended are written down: in the
	folder of the settings of the bot, the one that survives a restart of
	its container. None when there is no such folder to write in"""
	try:
		import config
	except ImportError:
		return None
	return os.path.join(config.CONFIG_PATH, MOVES_FILE)


class SynologyApiError(TorrentClientError):
	"""Error reported by DSM, keeping the code so the caller can react to it,
	and the errors object some APIs add about the files that failed"""

	def __init__(self, message, code, details=None):
		super().__init__(message)
		self.code = code
		self.details = details or []


class SessionExpired(Exception):
	"""Internal marker: the DSM session id is no longer valid"""

	def __init__(self, code):
		super().__init__(f"Synology session expired (code {code})")
		self.code = code


class _Move:
	"""A move of the data of some tasks that has not ended. It outlives the
	call that started it whenever that call cannot tell how it ended (File
	Station still moving the data, or not answering) and when it stopped half
	way: until then the tasks only look like they are at the destination, so
	the next move of any of them carries on with this one instead of trusting
	where they point"""

	POINT = "point"  # The tasks still have to be pointed at the destination
	COPY = "copy"  # File Station has to be asked to move what is still in the old folders
	WAIT = "wait"  # File Station is moving it
	FAILED = "failed"  # It stopped half way: the data of the tasks left is in two folders
	DONE = "done"

	def __init__(self, destination, tasks, sources, active):
		self.destination = destination
		self.tasks = tasks  # TorrentInfo of the tasks as they were before the move
		self.sources = sources  # {task id: path of its content before the move}
		self.active = active  # Ids stopped for the move, started again once their data is in place
		self.stage = self.POINT
		self.job = None  # Id of the File Station job moving the data, None when there is none to ask
		self.failure = ""  # What went wrong, once it stopped half way
		self.again = False  # Moving again what a failed attempt left, over the partial copies it made
		self.lock = threading.Lock()  # One attempt at a time takes it forward

	def trim(self, ids):
		"""Leaves in the move only the tasks whose ids are given. A move with
		none left is over"""
		self.tasks = [t for t in self.tasks if t.id in ids]
		self.sources = {i: path for i, path in self.sources.items() if i in ids}
		self.active = [i for i in self.active if i in ids]
		if not self.tasks:
			self.stage = self.DONE

	def dump(self):
		"""What is written down about the move, for restore"""
		return {
			"destination": self.destination,
			"tasks": [{"id": t.id, "name": t.name, "folder": (t.download_dir or "").strip("/")} for t in self.tasks],
			"sources": dict(self.sources),
			"active": list(self.active),
			"stage": self.stage,
			"job": self.job,
			"failure": self.failure,
			"again": self.again,
		}

	@classmethod
	def restore(cls, data):
		"""The move dump wrote down. Raises ValueError, KeyError or TypeError
		when that is not what data holds"""
		tasks = [TorrentInfo(id=str(t["id"]), name=str(t["name"]), status=TorrentStatus.PAUSED, download_dir=str(t["folder"]))
			for t in data["tasks"]]
		move = cls(str(data["destination"]), tasks, {str(i): str(path) for i, path in data["sources"].items()},
			[str(i) for i in data["active"]])
		move.stage = data["stage"]
		if move.stage not in (cls.POINT, cls.COPY, cls.WAIT, cls.FAILED) or not tasks or not move.destination:
			raise ValueError("not a move that has not ended")
		move.job = data.get("job") or None
		move.failure = str(data.get("failure") or "")
		move.again = bool(data.get("again"))
		return move


class SynologyClient(TorrentClient):
	reports_last_activity = False
	skipped_files_in_size = True
	supports_alt_speed = False  # Download Station has no alternative speed mode
	supports_rename = False  # No rename API for tasks or files
	supports_verify = False  # No recheck API
	supports_ratio_limit = False  # Only a global seeding ratio in the Download Station settings
	supports_torrent_speed_limit = False  # Only global speed limits
	supports_reannounce = False  # No API to reach the trackers on demand

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
		self._moves = {}  # Task id -> _Move of its data that has not ended
		self._moves_lock = threading.RLock()
		self._moves_file = _moves_path()
		self._restored = set()  # Moves taken back from a previous run whose tasks are still to be checked
		self._discover()
		self._login()
		if BT_FILE_API not in self._api_info:
			self.file_priority_levels = ()  # DSM 6 cannot choose the files of a task
		self._load_moves()

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
			error = _mapping(data.get("error"))
			code = _num(error.get("code"), 0)
			if code in SESSION_EXPIRED_CODES:
				raise SessionExpired(code)
			raise SynologyApiError(
				f"Synology error on {api}.{method}: {self._describe_error(api, code)}", code,
				_sequence(error.get("errors")))
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
				# A fresh session that is refused all the same: the code tells
				# whether the user is not allowed to use the API at all
				raise SynologyApiError(
					f"Synology session error on {api}.{method}: {self._describe_error(api, e.code)}", e.code)

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
		# adds up to its size. Its completion stamp is what settles it. Until
		# then the bytes still missing keep it under 100: rounded, 49,998 of
		# 50,000 MB would read as finished, and the bot announces that once
		if _num(detail.get("completed_time")) > 0:
			progress = 100.0
		elif total_size > 0:
			progress = capped_progress(downloaded / total_size * 100, total_size - downloaded)
		else:
			progress = 0.0
		eta = -1
		if download_rate > 0 and total_size > downloaded:
			eta = int((total_size - downloaded) / download_rate)

		files = []
		priorities = {}
		if full:
			for f in _sequence(additional.get("file")):
				f = _mapping(f)
				files.append((f.get("filename", ""), _num(f.get("size")), _num(f.get("size_downloaded"))))
				priority = f.get("priority")
				priorities[f.get("filename", "")] = (FILE_SKIP if f.get("wanted") is False or priority == "skip"
					else priority if priority in (FILE_LOW, FILE_NORMAL, FILE_HIGH) else FILE_NORMAL)
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
			seeding_time=_num(detail.get("seedelapsed"), -1),
			peers=seeders + leechers,
			seeders=seeders,
			leechers=leechers,
			download_dir=detail.get("destination", "") or "",
			error_message=error_message,
			added_date=_num(detail.get("create_time"), None) or None,
			completed_date=_num(detail.get("completed_time"), None) or None,
			files=files,
			file_priorities=priorities,
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
		matches = query_matcher(query)
		for t in tasks:
			info = self._to_info(t)
			if matches and not matches(info.name, info.download_dir):
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
		(no File Station permission, the missing folder is a shared folder,
		which only DSM itself can create, or File Station refuses it), and
		raises TorrentClientError when File Station does not answer"""
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
		except SynologyApiError:
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
			if e.code not in DESTINATION_MISSING:
				raise
			try:
				created = self._ensure_destination(destination)
			except TorrentClientError:
				created = False
			if not created:
				raise e
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

	def _new_task(self, known_ids, magnet=None):
		"""Id of the task this add created, among the ones that were not in the
		list before. Someone else may have added another task meanwhile, so
		for a magnet it has to be the task of that magnet: the one with that
		link or info hash, or else titled after the name in the link, which is
		how Download Station calls it until the metadata arrives. New tasks
		are looked for with the cheapest listing there is: a library with
		thousands of torrents is polled several times while the task shows up"""
		new = [str(t.get("id")) for t in self._list_tasks(additional=None)
			if t.get("id") and str(t.get("id")) not in known_ids]
		if not magnet or not new:
			return new[0] if new else None
		hashes, names = _magnet_keys(magnet)
		# Newest first: when the list from before the add could not be read,
		# a task of the same magnet that was already there counts as new too,
		# and the one this add created is the latest
		tasks = sorted(self._raw_tasks(new, "detail"), reverse=True,
			key=lambda t: _num(_mapping(_mapping(t.get("additional")).get("detail")).get("create_time")))
		for t in tasks:
			uri = (_mapping(_mapping(t.get("additional")).get("detail")).get("uri") or "").strip()
			if uri and (uri == magnet.strip() or hashes & _magnet_keys(uri)[0]):
				return str(t.get("id"))
		for t in tasks:
			title = (t.get("title") or "").strip().casefold()
			if title and (title in names or title in hashes):
				return str(t.get("id"))
		return None

	def _wait_for_task(self, task_id, known_ids, magnet=None):
		"""A new task is titled after the uploaded file or the link until
		Download Station parses the metadata, so it is polled until the real
		name and size arrive. A magnet has no metadata to parse yet, so the
		wait gives up quickly and the placeholder is returned, like the other
		torrent managers do"""
		info = None
		for _ in range(NEW_TASK_ATTEMPTS):
			if not task_id:
				task_id = self._new_task(known_ids, magnet)
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
		info = self._wait_for_task(task_id, known_ids, magnet)
		# A torrent that is already there does not fail: Download Station
		# creates a task in error state that has to be cleaned up
		if info.status == TorrentStatus.ERROR and info.error_message == ERROR_DETAILS[DUPLICATE_ERROR]:
			try:
				self._call(TASK_API, "delete", params={"id": info.id, "force_complete": "false"})
			except TorrentClientError:
				pass
			# The bot shows the task that was already there, so it is looked for
			try:
				existing = self._duplicated(info, magnet, torrent_data)
			except TorrentClientError:
				existing = None
			message = f"Error adding torrent: {ERROR_DETAILS[DUPLICATE_ERROR]}"
			if existing:
				message += f": {existing.get('title')}"
			raise DuplicateTorrentError(message, torrent_id=str(existing.get("id")) if existing else None)
		return info

	def _duplicated(self, failed, magnet, torrent_data):
		"""The raw task already in Download Station that the add of a magnet
		or a .torrent file failed as a duplicate of, None when it cannot be
		told. Download Station does not say which one it is, nor report the
		info hash of a task, so it is the task whose link has the same info
		hash, or else the one titled like the torrent: after the name inside
		the .torrent file or in the link, or like the failed task. The tasks
		that failed as duplicates themselves are never it"""
		hashes = set(expected_torrent_ids(magnet=magnet, torrent_data=torrent_data))
		names = set()
		if magnet:
			magnet_hashes, names = _magnet_keys(magnet)
			hashes |= magnet_hashes
		name = _torrent_name(torrent_data)
		if name:
			names.add(name.casefold())
		if (failed.name or "").strip() and failed.name != "upload.torrent":
			names.add(failed.name.strip().casefold())
		tasks = [t for t in self._list_tasks(additional="detail") if str(t.get("id")) != failed.id
			and _mapping(t.get("status_extra")).get("error_detail") != DUPLICATE_ERROR]
		# Oldest first: of two tasks of the same torrent, the one that was there first
		tasks.sort(key=lambda t: _num(_mapping(_mapping(t.get("additional")).get("detail")).get("create_time")))
		for t in tasks:
			uri = (_mapping(_mapping(t.get("additional")).get("detail")).get("uri") or "").strip()
			if hashes and uri and hashes & _magnet_keys(uri)[0]:
				return t
		for t in tasks:
			title = (t.get("title") or "").strip().casefold()
			if title and (title in names or title in hashes):
				return t
		return None

	def _content_root(self, info):
		"""Where the content of a task is on disk according to its own file
		list, with the paths whose presence proves it: (root, evidence), or
		None when the list does not tell. Download Station lists the files of
		a torrent relative to its root folder without ever naming that folder,
		so the folder can only take the title of the task; a single file
		listed under the very title of the task is a torrent of that one file.
		A task that lists no files yet (a magnet still without its metadata)
		has nothing of its own on disk, whatever is there under its name"""
		directory = (info.download_dir or "").strip("/")
		title = (info.name or "").strip("/")
		files = [f for f in info.files if (f[0] or "").strip("/")]
		if not directory or not title or not files:
			return None
		root = f"/{directory}/{title}"
		if len(files) == 1 and files[0][0].strip("/") == title:
			return root, [root]
		# A folder of that name proves nothing by itself: the files that
		# already have data, the likeliest to be on disk, have to be in it
		evidence = []
		for path, _size, _done in sorted(files, key=lambda f: f[2] <= 0)[:EVIDENCE_FILES]:
			path = path.strip("/")
			evidence.append(f"{root}/{path}")
			if path.startswith(f"{title}/"):
				evidence.append(f"/{directory}/{path}")  # In case the list does name the root folder
		return root, evidence

	def _stat(self, paths, strict=False, sizes=False):
		"""The paths that really are on disk, asked to File Station: {path:
		size in bytes, None for a folder or when sizes are not asked}. A
		missing path comes back as an entry with an error code and no name.
		Unless strict, a File Station failure reads as nothing being there,
		which is only fine where finding nothing is the safe answer. Strict,
		it raises PermanentTorrentError when the user cannot use File Station"""
		if not paths:
			return {}
		if SHARE_API not in self._api_info:
			if strict:
				raise PermanentTorrentError(
					"File Station is not available for this user, and it is needed to see the data on disk")
			return {}
		params = {"path": json.dumps(list(dict.fromkeys(paths)))}
		if sizes:
			params["additional"] = json.dumps(["size"])
		try:
			data = self._call(SHARE_API, "getinfo", version=2, params=params)
		except TorrentClientError as e:
			if not strict:
				return {}
			denied = isinstance(e, SynologyApiError) and e.code == NO_PERMISSION
			raise (PermanentTorrentError if denied else TorrentClientError)(f"File Station cannot tell what is on disk: {e}")
		found = {}
		for entry in map(_mapping, _sequence(_mapping(data).get("files"))):
			if entry.get("name") and entry.get("path"):
				size = None if entry.get("isdir") else _mapping(entry.get("additional")).get("size")
				found[entry["path"]] = None if size is None else _num(size, None)
		return found

	def _existing_paths(self, paths, strict=False):
		"""The paths that really are on disk (see _stat)"""
		return set(self._stat(paths, strict))

	def _content_on_disk(self, tasks):
		"""Path of the content on disk of each of the given tasks, {task id:
		path}. Only what the file list of the task proves to be its own
		counts, so a task that has not written anything yet, or whose title
		happens to name something else, has nothing there"""
		roots = {t.id: self._content_root(t) for t in tasks}
		roots = {task_id: root for task_id, root in roots.items() if root}
		existing = self._existing_paths(
			[path for root, evidence in roots.values() for path in (root, *evidence)], strict=True)
		found = {
			task_id: root for task_id, (root, evidence) in roots.items()
			if root in existing and any(path in existing for path in evidence)}
		found.update(self._lone_files([t for t in tasks if t.id in roots and t.id not in found]))
		return found

	def _lone_files(self, tasks):
		"""Content of torrents of a single file whose task goes by a title of
		its own (it may keep the name of its magnet), which the folder of that
		title does not hold: {task id: path of the file}. The file is only
		taken when it is complete with its very size and no other task of
		that folder goes by its name, so that nothing that may be someone
		else's is taken for it"""
		candidates = {}
		for t in tasks:
			directory = (t.download_dir or "").strip("/")
			if len(t.files) != 1 or not directory:
				continue
			name, size, done = t.files[0]
			name = (name or "").strip("/")
			if name and "/" not in name and 0 < size <= done:
				candidates[t.id] = (f"/{directory}/{name}", size)
		if not candidates:
			return {}
		found = self._stat([path for path, _size in candidates.values()], strict=True, sizes=True)
		candidates = {i: path for i, (path, size) in candidates.items() if found.get(path) == size}
		if not candidates:
			return {}
		named = {}
		for t in self._list_tasks(additional="detail"):
			folder = (_mapping(_mapping(t.get("additional")).get("detail")).get("destination") or "").strip("/")
			named.setdefault(f"/{folder}/{(t.get('title') or '').strip('/')}", set()).add(str(t.get("id")))
		return {i: path for i, path in candidates.items() if not named.get(path, set()) - {i}}

	def _content_anywhere(self, infos, moves):
		"""Paths of the content on disk of the given tasks. The data of a task
		whose move has not ended can be where it points, in the folder it
		left, or part in each, so that move has it looked for in both"""
		paths = list(self._content_on_disk(infos).values())
		for move in moves:
			before = {t.id: t.download_dir for t in move.tasks}
			mine = [info for info in infos if info.id in before]
			if mine:
				paths += self._content_on_disk([replace(info, download_dir=before[info.id]) for info in mine]).values()
				paths += self._content_on_disk([replace(info, download_dir=move.destination) for info in mine]).values()
		return list(dict.fromkeys(paths))

	def _delete_paths(self, paths):
		"""Deletes through File Station whatever of the given paths is still on
		disk: Download Station only ever deletes the task"""
		existing = self._existing_paths(paths, strict=True)
		targets = [path for path in paths if path in existing]
		if targets:
			self._call(DELETE_API, "delete", version=2, params={
				"path": json.dumps(targets), "recursive": "true"}, timeout=MOVE_RPC_TIMEOUT)

	def remove_torrents(self, torrent_ids, delete_data=False):
		torrent_ids = [str(i) for i in torrent_ids]
		moves = self._moves_of(torrent_ids)
		targets = []
		if delete_data:
			if DELETE_API not in self._api_info:
				raise TorrentClientError(
					"Deleting the downloaded data needs File Station, which is not available for this user")
			busy = [t for move in moves if move.stage in (_Move.COPY, _Move.WAIT)
				for t in move.tasks if t.id in torrent_ids]
			if busy:
				raise TorrentClientError(
					f"Error removing torrents: File Station may still be moving the data of {_quoted(busy)}, "
					f"which can be deleted once the move is over (moving it again follows it to the end)")
			# What is whose can only be told while the tasks are still there
			try:
				targets = self._content_anywhere(self._tasks_by_id(torrent_ids), moves)
			except TorrentClientError as e:
				kind = PermanentTorrentError if isinstance(e, PermanentTorrentError) else TorrentClientError
				raise kind(f"Error removing torrents: {e}")
		try:
			self._check_items(self._call(TASK_API, "delete", params={
				"id": ",".join(torrent_ids), "force_complete": "false"}),
				"remove", nothing_to_do_is_fine=True)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error removing torrents: {e}")
		for move in moves:
			self._keep(move, [t.id for t in move.tasks if t.id not in torrent_ids])
		if targets:
			try:
				self._delete_paths(targets)
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
		ids = [str(i) for i in torrent_ids]
		# The data of a task whose move has not ended is not all where the task
		# points yet: started now, Download Station would find it missing and
		# break the task. The move starts it again once its data is in place
		moves = self._moves_of(ids)
		if moves:
			self._settle(moves)
			moves = self._moves_of(ids)
		held = [(move, t) for move in moves for t in move.tasks if t.id in ids]
		free = [i for i in ids if i not in {t.id for _move, t in held}]
		targets = self._filter_by_status(free, RESUMABLE_STATUSES) if free else []
		if targets:
			try:
				self._check_items(self._call(TASK_API, "resume", params={"id": ",".join(targets)}), "resume")
			except TorrentClientError as e:
				raise TorrentClientError(f"Error resuming torrents: {e}")
		if held:
			# A refusal, not a failure to reach the NAS: asking again does not
			# change it, so the bot has to tell the user what does. The rest did
			# start, and the user must not take the held ones for started too
			message = f"Error resuming torrents: {self._held(held)}"
			if free:
				message += ". The other torrents were resumed"
			raise PermanentTorrentError(message)

	def _settle(self, moves):
		"""Ends the given moves whose data File Station already got entirely
		to the destination, the way the move itself would have, so that their
		tasks can start. Nothing drives a move again but a new order of it:
		one taken back from a previous run of the bot, or whose attempt gave
		up while File Station was still at it, would keep its tasks from
		starting long after its data is in place. A move that is being driven
		right now is left to that attempt, and one whose tasks may not point
		at the destination yet to the next order. Anything that cannot be told
		leaves the move as it is, with its tasks held"""
		for move in moves:
			if move.stage in (_Move.POINT, _Move.DONE) or not move.lock.acquire(blocking=False):
				continue
			try:
				if all(state == MOVED for state in self._disk_states(move).values()):
					self._finish(move, "")
			except TorrentClientError:
				pass  # Still held: the refusal says how to end the move
			finally:
				move.lock.release()

	@staticmethod
	def _held(held):
		"""Why the tasks of moves that have not ended cannot start, from
		[(move, task)], with what ends the move"""
		parts = []
		for move in dict.fromkeys(move for move, _t in held):
			names = _quoted(t for m, t in held if m is move)
			if move.stage == _Move.FAILED:
				parts.append(f"the move of {names} to {move.destination} stopped half way: order the same move "
					f"again to finish it")
			else:
				parts.append(f"the data of {names} is still being moved to {move.destination}, and it cannot "
					f"start before it is all there: order the same move again to follow it to its end")
		return ". ".join(parts)

	def verify_torrent(self, torrent_id):
		raise PermanentTorrentError("Download Station cannot verify the data of a task")

	def set_file_priority(self, torrent_id, paths, priority):
		if not self.file_priority_levels:
			raise PermanentTorrentError("This Download Station cannot choose the files of a task")
		try:
			files = (self._call(BT_FILE_API, "list", version=2, params={
				"task_id": json.dumps(str(torrent_id)), "offset": 0, "limit": -1}) or {}).get("items") or []
			wanted = set(paths)
			# A file is known by its index, or, when an item does not carry
			# one, by its place in the list, as the other managers number them
			indexes = [
				_num(f.get("index"), position) for position, f in enumerate(map(_mapping, files))
				if f.get("name") in wanted]
			if not indexes:
				raise PermanentTorrentError(f"No such files in task {torrent_id}")
			params = {"task_id": json.dumps(str(torrent_id)), "index": json.dumps(indexes),
				"wanted": json.dumps(priority != FILE_SKIP)}
			if priority != FILE_SKIP:
				params["priority"] = json.dumps(priority)
			self._call(BT_FILE_API, "set", version=2, params=params)
		except PermanentTorrentError:
			raise
		except SynologyApiError as e:
			if e.code == UNKNOWN_TASK:
				raise PermanentTorrentError(f"Task not found: {torrent_id}")
			raise TorrentClientError(f"Error setting the priority of the files of {torrent_id}: {e}")

	def rename_torrent(self, torrent_id, new_name):
		raise PermanentTorrentError("Download Station cannot rename tasks")

	def rename_file(self, torrent_id, old_path, new_name):
		raise PermanentTorrentError("Download Station cannot rename files inside a task")

	def rename_folder(self, torrent_id, old_path, new_name):
		raise PermanentTorrentError("Download Station cannot rename folders inside a task")

	def move_torrents(self, torrent_ids, new_dir):
		"""Changing the destination of a task only tells Download Station where
		the data still to come has to go: whatever is already on disk stays
		where it is (the API answers success all the same). So the content is
		moved with File Station and the task is pointed at the new folder.
		It only returns once the data really is there. While File Station is
		still moving it, or cannot be asked how that went, a plain
		TorrentClientError says so and the tasks stay stopped: the next call
		with any of them carries that move on instead of starting another,
		also after a restart of the bot"""
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
		# A move of these tasks that has not ended is seen through first: until
		# then they only look like they are where they point
		for move in self._moves_of(ids):
			self._drive(move)
		self._require_folder(destination)
		tasks = [t for t in self._tasks_by_id(ids) if (t.download_dir or "").strip("/") != destination]
		if not tasks:
			return  # Everything is already there
		sources = self._content_on_disk(tasks)
		# A finished download has its data in its folder. Not finding it there
		# means it goes by another name, and pointing the task elsewhere
		# without its data would only break it
		lost = [t for t in tasks if t.id not in sources and self._complete(t)]
		if lost:
			raise PermanentTorrentError(f"Cannot find on disk the data of {_placed(lost)}: nothing was moved")
		self._refuse_collisions(destination, list(sources.values()))

		# A task that is running holds its files: it is stopped for the move and
		# started again once its data is where the task points
		active = [t.id for t in tasks if t.status in PAUSABLE_STATUSES]
		if active:
			self._check_items(self._call(TASK_API, "pause", params={"id": ",".join(active)}), "pause")
			time.sleep(PAUSE_SETTLE_DELAY)
		move = _Move(destination, tasks, sources, active)
		self._remember(move)
		self._drive(move)

	@staticmethod
	def _complete(info):
		"""Whether Download Station finished downloading the task, so that its
		data has to be in its folder. Where an unfinished download keeps the
		pieces it has is up to Download Station, so not finding them there is
		no reason to refuse moving it"""
		return (bool(info.completed_date or info.status == TorrentStatus.SEEDING)
			and any(done > 0 for _path, _size, done in info.files))

	def _drive(self, move):
		"""Takes a move as far as it can go. Returns once the data of every task
		is where the task points, raises PermanentTorrentError when the move
		went wrong in a way the user has to be told about, and a plain
		TorrentClientError while how it ends is not known yet: the move then
		stays registered with its tasks stopped, for the next attempt to carry
		on from there"""
		with move.lock:
			if move.stage == _Move.FAILED:
				# Ordered again after stopping half way: File Station is asked
				# for what is still in the old folders, over the partial copies
				# the failed attempt left at the destination
				self._set(move, stage=_Move.COPY, again=True)
			steps = {_Move.POINT: self._point_tasks, _Move.COPY: self._copy_rest, _Move.WAIT: self._wait_copy}
			for _ in range(DRIVE_STEPS):
				step = steps.get(move.stage)
				if not step:
					return  # Over
				step(move)
			raise TorrentClientError(f"The move to {move.destination} did not end")

	def _point_tasks(self, move):
		"""Points every task of a move at its destination. Download Station is
		asked first: it records the new destination and, depending on the kind
		of task, moves the data by itself. Nothing is touched on disk until it
		confirms the change, and when it refuses it for any of the tasks the
		whole move is called off, so that a refusal leaves everything exactly
		as it was"""
		ids = [t.id for t in move.tasks]
		expected = dict.fromkeys(ids, move.destination)
		try:
			reasons = self._edit_destination(ids, move.destination)
			fresh, refused = self._destination_state(expected)
			if refused:
				# Download Station ignores the change for a task it considers
				# over. Starting it and stopping it again takes it out of that
				# state, at the price of a full data check, so it is only done
				# for the tasks that need it
				self._restart(refused)
				reasons.update(self._edit_destination(refused, move.destination))
				fresh, refused = self._destination_state(expected)
		except TorrentClientError:
			# Which tasks took the change is not known: the ones that did go back
			self._call_off(move)
			raise
		if refused:
			task = next(t for t in fresh if t.id == refused[0])
			code = reasons.get(task.id)
			why = (ITEM_ERRORS.get(code) or self._describe_error(TASK_API, code)) if code else "it considers that download over"
			stuck = self._call_off(move)
			message = f"Download Station refuses to change the destination of {task.name!r}: {why}. The data was left where it was"
			if len(stuck) == 1:
				message += f", but {_quoted(stuck)} could not be pointed back to its folder and was left stopped"
			elif stuck:
				message += f", but {_quoted(stuck)} could not be pointed back to their folders and were left stopped"
			raise PermanentTorrentError(message)
		self._set(move, stage=_Move.COPY)

	def _call_off(self, move):
		"""Undoes a move before any of its data was moved: the tasks are pointed
		back to where they were and the ones stopped for it start again.
		Returns the tasks that could not be pointed back: they stay stopped,
		and in the move, for the next attempt to finish what was started"""
		stuck = self._roll_back(move.tasks)
		self._resume([i for i in move.active if i not in stuck])
		self._keep(move, stuck)
		return list(move.tasks)

	def _roll_back(self, tasks):
		"""Points tasks back to the folder they were in before a move. Returns
		the ids of the ones that may still point somewhere else: the ones that
		took the change when Download Station does not confirm they went back,
		and all of them when it cannot even tell which ones took it"""
		expected = {t.id: (t.download_dir or "").strip("/") for t in tasks}
		try:
			current = {t.id: (t.download_dir or "").strip("/") for t in self._tasks_by_id(list(expected))}
		except TorrentClientError:
			return list(expected)
		wrong = {i: folder for i, folder in expected.items() if i in current and current[i] != folder}
		if not wrong:
			return []
		try:
			for folder in dict.fromkeys(wrong.values()):
				self._edit_destination([i for i in wrong if wrong[i] == folder], folder)
			return self._destination_state(wrong)[1]
		except TorrentClientError:
			return list(wrong)

	def _copy_rest(self, move):
		"""Asks File Station to move whatever data of a move is still in the
		old folders"""
		states = self._disk_states(move)
		if SPLIT in states.values() and not move.again:
			# Data in two places before File Station was asked means that
			# something is moving it already (Download Station by itself, or
			# a job whose start went unanswered): the disk tells how it ends
			self._set(move, stage=_Move.WAIT, job=None)
			return
		pending = list(dict.fromkeys(move.sources[t.id] for t in move.tasks if states[t.id] != MOVED))
		if not pending:
			self._finish(move, "")  # Download Station moved it by itself, or there was nothing on disk
			return
		# From here on a job may be moving the data, even if its start goes
		# unanswered: no other one is started until the disk says it ended
		self._set(move, stage=_Move.WAIT, job=None)
		try:
			data = _mapping(self._call(COPYMOVE_API, "start", version=3, params={
				"path": json.dumps(pending),
				"dest_folder_path": f"/{move.destination}",
				"remove_src": "true",
				# What a failed attempt left at the destination are partial
				# copies of what is still in the old folder: nothing of these
				# tasks was there before the move (see _refuse_collisions)
				"overwrite": "true" if move.again else "false",
				"accurate_progress": "true",
			}))
		except SynologyApiError as e:
			self._finish(move, self._copy_failure(e.code, e.details))  # Refused: nothing was moved
			return
		if data.get("taskid"):
			self._set(move, job=data["taskid"])

	def _wait_copy(self, move):
		"""Follows the File Station job of a move until it ends"""
		if not move.job:
			self._watch(move)
			return
		for _ in range(MOVE_POLL_ATTEMPTS):
			time.sleep(MOVE_POLL_DELAY)
			try:
				status = _mapping(self._call(COPYMOVE_API, "status", version=3, params={"taskid": move.job}))
			except SynologyApiError as e:
				if e.code in COPYMOVE_FAILED:
					self._finish(move, self._copy_failure(e.code, e.details))
					return
				if e.code != NO_SUCH_JOB:
					raise
				# File Station does not know the job (any more): only the disk
				# can tell how far it got
				self._set(move, job=None)
				self._watch(move)
				return
			if status.get("finished"):
				errors = _sequence(status.get("errors"))
				self._finish(move, self._copy_failure(None, errors) if errors else "")
				return
		raise TorrentClientError(f"File Station is still moving the data to {move.destination}")

	def _watch(self, move):
		"""Follows on disk the data of a move that no File Station job can be
		asked about: File Station forgot the job (the NAS restarted, say), or
		its start went unanswered. The sizes of the files of the tasks at
		both ends change while anything moves them, so once nothing changes
		for WATCH_STILL nothing is moving them: a move that left data in two
		folders stopped half way, and one that did not get to touch it is
		started again"""
		files = self._watched_files(move)
		seen, still = None, 0
		for _ in range(WATCH_ATTEMPTS):
			disk = self._snapshot(move, files)
			states = self._states(move, disk)
			if all(state == MOVED for state in states.values()):
				self._finish(move, "")
				return
			still = still + WATCH_DELAY if disk == seen else 0
			seen = disk
			if still >= WATCH_STILL:
				if SPLIT in states.values():
					self._finish(move, "the move stopped half way (the NAS may have restarted)")
				else:
					self._set(move, stage=_Move.COPY)
				return
			time.sleep(WATCH_DELAY)
		raise TorrentClientError(
			f"File Station may still be moving the data of {_quoted(t for t in move.tasks if t.id in move.sources)}")

	def _watched_files(self, move):
		"""Paths of the files of each task of a move inside its content,
		{task id: paths}: what its data is moved file by file"""
		if not move.sources:
			return {}
		files = {}
		for t in self._tasks_by_id(list(move.sources)):
			title = (t.name or "").strip("/")
			names = []
			for path, _size, _done in t.files[:WATCH_FILES]:
				path = (path or "").strip("/")
				names.append(path)
				if title and path.startswith(f"{title}/"):
					names.append(path[len(title) + 1:])  # In case the list names the root folder
			files[t.id] = names
		return files

	def _finish(self, move, failure):
		"""Ends a move once nothing is moving its data, from where the disk says
		the data of each task is: the tasks whose data is at the destination
		start again, the ones whose data was not touched are pointed back to
		where they were, and the ones left half way stay stopped and in the
		move, so that no later call takes them for moved. failure is what File
		Station reported, empty when it reported nothing wrong"""
		states = self._disk_states(move)
		untouched = [t for t in move.tasks if states[t.id] == UNTOUCHED]
		stuck = self._roll_back(untouched) if untouched else []
		# A task only starts again where its data is. One whose data got to
		# the destination may not point there (a move carried on after its
		# task was pointed back, or its folder changed by hand): it is
		# pointed there first, and left stopped when that cannot be done
		astray = self._point([t for t in move.tasks if states[t.id] == MOVED], move.destination)
		fine = {t.id for t in move.tasks if (states[t.id] == MOVED and t.id not in astray)
			or (states[t.id] == UNTOUCHED and t.id not in stuck)}
		self._resume([i for i in move.active if i in fine])
		if len(fine) == len(move.tasks) and all(state == MOVED for state in states.values()):
			self._keep(move, [])
			return
		message = self._move_failure(move, failure, states, stuck, astray)
		move.stage, move.failure, move.again = _Move.FAILED, failure, False
		self._keep(move, [t.id for t in move.tasks if t.id not in fine])
		raise PermanentTorrentError(message)

	def _point(self, tasks, destination):
		"""Makes sure the given tasks point at destination. Returns the ids of
		the ones Download Station does not take it for. When it does not
		answer, TorrentClientError leaves the end of the move to the next
		attempt, which finds everything as it was"""
		if not tasks:
			return []
		wrong = [t.id for t in self._tasks_by_id([t.id for t in tasks]) if (t.download_dir or "").strip("/") != destination]
		if not wrong:
			return []
		self._edit_destination(wrong, destination)
		return self._destination_state(dict.fromkeys(wrong, destination))[1]

	def _move_failure(self, move, failure, states, stuck, astray):
		"""What the user is told about a move that went wrong: why, and where
		the data of each of its tasks ended up"""
		moved = [t for t in move.tasks if states[t.id] == MOVED and t.id not in astray]
		back = [t for t in move.tasks if states[t.id] == UNTOUCHED and t.id not in stuck]
		left = [t for t in move.tasks if states[t.id] == UNTOUCHED and t.id in stuck]
		split = [t for t in move.tasks if states[t.id] == SPLIT]
		elsewhere = [t for t in move.tasks if states[t.id] == MOVED and t.id in astray]
		if elsewhere and not (failure or split or left or back):
			parts = [f"Download Station does not take {move.destination} as the folder of the data moved there"]
		else:
			parts = [f"File Station could not move the data to {move.destination}: "
				f"{failure or 'File Station left part of the data behind'}"]
		if moved:
			parts.append(f"Moved: {_quoted(moved)}")
		if back:
			parts.append(f"Left in their folder: {_placed(back)}")
		if split:
			parts.append(f"Stopped half moved, part in their folder and part in {move.destination}: {_placed(split)}")
		if left:
			parts.append(f"Stopped, with the data in their folder but pointing at {move.destination}: {_placed(left)}")
		if elsewhere:
			parts.append(f"Stopped, with the data in {move.destination} but pointing elsewhere: {_quoted(elsewhere)}")
		if split or left or elsewhere:
			parts.append("Moving them again finishes the move")
		return ". ".join(parts)

	def _copy_failure(self, code, errors):
		"""Why a File Station move went wrong, from the code of the answer and
		its errors object about the files that failed"""
		reasons = []
		for item in errors:
			if isinstance(item, dict):
				reason = self._describe_error(COPYMOVE_API, _num(item.get("code")))
				reasons.append(f"{reason} ({item['path']})" if item.get("path") else reason)
			else:
				reasons.append(str(item))
		if not reasons:
			reasons.append(self._describe_error(COPYMOVE_API, code) if code else "unknown error")
		return reasons[0] + (f" and {len(reasons) - 1} more" if len(reasons) > 1 else "")

	def _disk_states(self, move):
		"""Where the data of each task of a move is (see _states)"""
		return self._states(move, self._snapshot(move))

	def _snapshot(self, move, files=None):
		"""What File Station sees of the data of a move, {path: size} (see
		_stat): the content of each task in its old folder and at the
		destination, and with files ({task id: paths inside its content})
		each of those files at both ends, whose sizes change while they move"""
		paths = []
		for i, source in move.sources.items():
			target = self._target(move.destination, source)
			paths += [source, target]
			for name in (files or {}).get(i, ()):
				paths += [f"{source}/{name}", f"{target}/{name}"]
		return self._stat(paths, strict=True, sizes=files is not None)

	def _states(self, move, disk):
		"""Where the data of each task of a move is, {task id: MOVED, UNTOUCHED
		or SPLIT}, told by what disk (a _snapshot) has in its old folder and
		in the destination. A task that had nothing on disk had nothing to
		move"""
		states = {}
		for t in move.tasks:
			source = move.sources.get(t.id)
			if not source:
				states[t.id] = MOVED
			elif self._target(move.destination, source) in disk:
				states[t.id] = SPLIT if source in disk else MOVED
			else:
				states[t.id] = UNTOUCHED if source in disk else SPLIT
		return states

	@staticmethod
	def _target(destination, path):
		"""Where the content at path ends up when moved into destination"""
		return f"/{destination}/{path.rsplit('/', 1)[-1]}"

	def _resume(self, ids):
		"""Starts again the tasks stopped for a move. Telling how the move went
		matters more than this, so a failure here is left out"""
		if not ids:
			return
		try:
			self._call(TASK_API, "resume", params={"id": ",".join(ids)})
		except TorrentClientError:
			pass

	def _remember(self, move):
		with self._moves_lock:
			for t in move.tasks:
				self._moves[t.id] = move
		self._save_moves()

	def _set(self, move, **changes):
		"""Takes a move to another stage (or changes the job moving its data),
		and writes it down"""
		for name, value in changes.items():
			setattr(move, name, value)
		self._save_moves()

	def _keep(self, move, ids):
		"""Leaves in a move only the given tasks, the ones whose move has not
		ended. A move with none left is over"""
		ids = set(ids)
		with self._moves_lock:
			for t in move.tasks:
				if t.id not in ids and self._moves.get(t.id) is move:
					del self._moves[t.id]
			move.trim(ids)
		self._save_moves()

	def _moves_of(self, ids):
		"""The moves that have not ended of any of the given tasks"""
		self._check_restored()
		with self._moves_lock:
			return list(dict.fromkeys(self._moves[i] for i in ids if i in self._moves))

	def _save_moves(self):
		"""Writes down the moves that have not ended, so that a restart of the
		bot carries them on. Not being able to only loses that, so it never
		fails what is being done"""
		if not self._moves_file:
			return
		with self._moves_lock:
			moves = [move.dump() for move in dict.fromkeys(self._moves.values())]
			try:
				if not moves:
					if os.path.exists(self._moves_file):
						os.remove(self._moves_file)
					return
				os.makedirs(os.path.dirname(self._moves_file), exist_ok=True)
				temporary = f"{self._moves_file}.tmp"
				with open(temporary, "w", encoding="utf-8") as file:
					json.dump({"nas": self.base_url, "moves": moves}, file, indent=1)
				os.replace(temporary, self._moves_file)
			except OSError:
				pass

	def _load_moves(self):
		"""Takes back the moves a previous run of the bot left on this NAS
		without an end"""
		try:
			with open(self._moves_file, encoding="utf-8") as file:
				saved = json.load(file)
		except (TypeError, OSError, ValueError):
			return  # Nothing written down, or nothing that can be read
		if not isinstance(saved, dict) or saved.get("nas") != self.base_url:
			return
		with self._moves_lock:
			for data in _sequence(saved.get("moves")):
				try:
					move = _Move.restore(data)
				except (AttributeError, KeyError, TypeError, ValueError):
					continue
				self._restored.add(move)
				for t in move.tasks:
					self._moves[t.id] = move
		self._check_restored()

	def _check_restored(self):
		"""Leaves out of the moves taken back from a previous run the tasks that
		are no longer the ones that were moved: gone from Download Station, or
		another task under the same id, which a reinstalled Download Station
		hands out again. Until Download Station can be asked they all count,
		since taking a task for moved is what breaks it"""
		if not self._restored:
			return
		try:
			current = {str(t.get("id")): t for t in self._list_tasks(additional="detail")}
		except TorrentClientError:
			return
		with self._moves_lock:
			for move in self._restored:
				same = {t.id for t in move.tasks if self._same_task(t, current.get(t.id), move.destination)}
				for t in move.tasks:
					if t.id not in same and self._moves.get(t.id) is move:
						del self._moves[t.id]
				move.trim(same)
			self._restored.clear()
		self._save_moves()

	@staticmethod
	def _same_task(info, raw, destination):
		"""Whether the raw task Download Station lists now is the task info of
		a move to destination: same title, pointing at its old folder or at
		the destination"""
		if not raw:
			return False
		folder = (_mapping(_mapping(raw.get("additional")).get("detail")).get("destination") or "").strip("/")
		return raw.get("title") == info.name and folder in ((info.download_dir or "").strip("/"), destination)

	def _destination_state(self, expected):
		"""Download Station applies a destination change in the background, and
		answers success even for the tasks it ends up ignoring, so the change
		has to be waited for and checked. expected maps the id of each task to
		the folder it should point at. Returns the tasks with their new state
		and the ids that did not take the change"""
		fresh, refused = [], []
		for _ in range(DESTINATION_ATTEMPTS):
			fresh = self._tasks_by_id(list(expected))
			refused = [t.id for t in fresh if (t.download_dir or "").strip("/") != expected[t.id]]
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

	def _raw_tasks(self, ids, additional):
		"""The given tasks as Download Station answers them, in a single call
		when it can be done. Ids that are not there any more are left out"""
		try:
			data = self._call(TASK_API, "getinfo", params={"id": ",".join(ids), "additional": additional})
			tasks = [_mapping(t) for t in _sequence(_mapping(data).get("tasks"))]
		except SynologyApiError as e:
			if e.code != UNKNOWN_TASK:
				raise
			# One stale id makes Download Station refuse the whole batch, so the
			# tasks are picked out of the listing instead
			wanted = {str(i) for i in ids}
			tasks = [t for t in self._list_tasks(additional=additional) if str(t.get("id")) in wanted]
		return [t for t in tasks if t.get("title")]

	def _tasks_by_id(self, ids):
		"""Full info of the given tasks. Ids that are not there any more are
		left out of the result"""
		return [self._to_info(t, full=True) for t in self._raw_tasks(ids, "detail,file")]

	def _require_folder(self, destination):
		"""Makes sure the destination folder is there before moving into it.
		Without File Station that is left to Download Station, which refuses
		a destination that does not exist"""
		if SHARE_API not in self._api_info or self._existing_paths([f"/{destination}"], strict=True):
			return
		if not self._ensure_destination(destination):
			raise PermanentTorrentError(
				f"The destination folder '{destination}' does not exist and cannot be created")

	def _refuse_collisions(self, destination, sources):
		"""Moving on top of something already there would either overwrite the
		content or be silently skipped, so it is refused instead, and so is
		moving two contents of the same name into the same folder. Nothing
		has been touched yet, and trying again would find the same"""
		names = Counter(path.rsplit("/", 1)[-1] for path in set(sources))
		twice = sorted(name for name, count in names.items() if count > 1)
		if twice:
			raise PermanentTorrentError(f"'{', '.join(twice)}' would end up twice in {destination}")
		clashing = sorted(self._existing_paths([self._target(destination, path) for path in sources], strict=True))
		if clashing:
			names = ", ".join(path.rsplit("/", 1)[-1] for path in clashing)
			raise PermanentTorrentError(f"'{names}' already exists in {destination}")

	def _edit_destination(self, torrent_ids, destination):
		"""DSM 7 dropped edit from version 1 of the Task API, DSM 6 only has
		that one, so both are tried. Returns the error code Download Station
		answered for each task that refused the change; it can also answer
		success and ignore it, which only the state of the task tells"""
		params = {"id": ",".join(str(i) for i in torrent_ids), "destination": destination}
		try:
			data = self._call(TASK_API, "edit", version=2, params=params, timeout=MOVE_RPC_TIMEOUT)
		except SynologyApiError as e:
			if e.code != 103:  # The method does not exist in this version
				raise
			data = self._call(TASK_API, "edit", version=1, params=params, timeout=MOVE_RPC_TIMEOUT)
		items = [_mapping(i) for i in _sequence(data)]
		return {str(i.get("id")): _num(i.get("error")) for i in items if _num(i.get("error"))}

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
