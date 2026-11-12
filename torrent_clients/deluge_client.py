"""Deluge implementation of the TorrentClient interface (Web UI JSON-RPC API)"""

import base64
import re
import threading
import time
from collections import Counter
from itertools import count
from urllib.parse import urlparse

import requests
from requests import exceptions as requests_errors

from torrent_clients.base import (
	DuplicateTorrentError,
	FILE_HIGH,
	FILE_LOW,
	FILE_NORMAL,
	FILE_SKIP,
	PermanentTorrentError,
	RATIO_MODE_NONE,
	RATIO_MODE_TORRENT,
	RestartRequiredError,
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
# Asking the Web UI whether it is connected to the daemon: it answers that by
# itself, at once, so a longer wait only means it does not answer at all
PROBE_TIMEOUT = 5
TRACKER_ERROR_PREFIX = "Error: "  # tracker_status is "Announce OK", "Error: <tracker answer>"...

# Codes of the errors the Web UI answers by itself. The daemon's own errors
# come with another code and the exception in the message
ERROR_NOT_AUTHENTICATED = 1
ERROR_UNKNOWN_METHOD = 2  # Also what every daemon method gets while the Web UI was never connected

# How the daemon refuses a torrent it already has, naming it
DUPLICATE_RE = re.compile(r"already (?:in session|being added) \(([0-9a-fA-F]{40})\)")

# What Deluge 2.2 answers when it removes a torrent only halfway (see _prepare_removal)
QUEUE_LOST = "is not in queued torrents set"
REMOVAL_KEYS = ["is_finished", "is_seed", "state", "total_payload_download", "file_priorities"]
# Times a removal looks, 0.2 s apart, whether the torrents are ready for it
REMOVAL_POLLS = 15


class DelugeRPCError(TorrentClientError):
	"""Error answered by the Deluge Web UI, with its code"""

	def __init__(self, message, code=None):
		super().__init__(message)
		self.code = code


class DelugeConnectionError(TorrentClientError):
	"""Nothing answers at the address of the Web UI"""
	pass


def _not_authenticated(error):
	return getattr(error, "code", None) == ERROR_NOT_AUTHENTICATED or "Not authenticated" in str(error)


def _unknown_method(error):
	return isinstance(error, DelugeRPCError) and (
		error.code == ERROR_UNKNOWN_METHOD or (error.code is None and str(error).endswith(": Unknown method")))


def _number(value):
	return isinstance(value, (int, float)) and not isinstance(value, bool)


def _half_removed(torrent_ids):
	return RestartRequiredError(
		f"Deluge did not finish removing {', '.join(sorted(torrent_ids))}: until its daemon restarts it can neither "
		"list its torrents nor save the ones added. Restart the Deluge daemon")


def _progress(t):
	"""Share of the wanted bytes that are downloaded, 0-100. Deluge's own
	progress is the one of whatever the torrent is doing: 100 for any torrent
	in error, the copy while it moves its data, so it cannot tell whether the
	download is complete. Nor can total_done, which counts the pieces of
	skipped files. Like qBittorrent and Transmission, a torrent with nothing
	wanted is at 0, and it is only 100 once every wanted byte is there"""
	wanted, remaining = t.get("total_wanted"), t.get("total_remaining")
	if not _number(wanted) or not _number(remaining):
		return capped_progress(t.get("progress", 0), None)  # Deluge 1.3 has no total_remaining
	if wanted <= 0:
		return 0.0
	return capped_progress((wanted - remaining) * 100.0 / wanted, remaining)


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
	"name", "state", "progress", "total_wanted", "total_remaining", "download_payload_rate",
	"upload_payload_rate", "save_path", "time_added", "tracker_host", "message",
	"ratio", "time_since_transfer",
	# How long ago it finished, which the automatic mover goes by
	"completed_time", "seeding_time",
]

# Normalized file priority -> Deluge's, the values its own interface uses
FILE_PRIORITY_VALUES = {FILE_SKIP: 0, FILE_LOW: 1, FILE_NORMAL: 4, FILE_HIGH: 7}


def _file_priority(value):
	"""Deluge file priority (libtorrent's, 0 to 7) -> normalized one. Deluge
	names them Skip (0), Low (1-3), Normal (4) and High (5-7)"""
	try:
		value = int(value)
	except (TypeError, ValueError):
		return FILE_NORMAL
	if value <= 0:
		return FILE_SKIP
	if value < 4:
		return FILE_LOW
	return FILE_HIGH if value >= 5 else FILE_NORMAL


FULL_KEYS = LIGHT_KEYS + [
	"total_done", "total_uploaded", "eta", "num_peers", "num_seeds",
	"files", "file_progress", "file_priorities", "trackers",
	"stop_ratio", "stop_at_ratio", "total_seeds", "total_peers",
	"distributed_copies", "tracker_status", "private",
	"max_download_speed", "max_upload_speed",
]


class DelugeClient(TorrentClient):
	supports_alt_speed = False
	ratio_limit_copied_on_add = True

	def __init__(self, host, port, password=None, protocol="http"):
		self.base_url = f"{protocol}://{host}:{port}/json"
		self.password = password or ""
		self.session = requests.Session()
		self._id_counter = count(1)
		self._saved_limits = {}
		self._login_lock = threading.Lock()
		# Times it logged in again, and times the Web UI did not even say whether
		# it was connected, so a failed call can tell whether another one
		# already found out what was wrong
		self._logins = 0
		self._dead_probes = 0
		self._file_priority_lock = threading.Lock()
		self._broken = set()  # Torrents Deluge removed only halfway, until its daemon restarts
		self._login()

	def _rpc(self, method, *params, timeout=REQUEST_TIMEOUT):
		payload = {"method": method, "params": list(params), "id": next(self._id_counter)}
		try:
			response = self.session.post(self.base_url, json=payload, timeout=timeout)
		except requests_errors.ConnectionError as e:  # ConnectTimeout is one too
			raise DelugeConnectionError(f"Deluge request failed ({method}): {e}")
		except Exception as e:
			raise TorrentClientError(f"Deluge request failed ({method}): {e}")
		if response.status_code >= 400:
			raise TorrentClientError(f"Deluge error {response.status_code} on {method}")
		try:
			data = response.json()
		except ValueError:
			data = None
		if not isinstance(data, dict):
			# A reverse proxy or a login page in front of the Web UI answers with a web page
			raise TorrentClientError(
				f"Deluge answered {method} with something that is not its API (HTTP {response.status_code}): "
				"is a proxy or a login page in between?")
		error = data.get("error")
		if error:
			if isinstance(error, dict):
				raise DelugeRPCError(f"Deluge error on {method}: {error.get('message', error)}", error.get("code"))
			raise DelugeRPCError(f"Deluge error on {method}: {error}")
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
		"""RPC that gets back what the Web UI lost: its login, when the web
		session expires, and the daemon, when it restarts on its own (the Web UI
		stays up but disconnected, and every call to the daemon hangs until it
		times out, for as long as nobody connects it again)"""
		seen = (self._logins, self._dead_probes)
		try:
			return self._rpc(method, *params, timeout=timeout)
		except TorrentClientError as e:
			if not self._recover(e, seen):
				raise
		return self._rpc(method, *params, timeout=timeout)

	def _recover(self, error, seen):
		"""Logs in and connects to the daemon again when losing that is what
		made a call fail. seen is (_logins, _dead_probes) from before the call.
		Returns whether the call is worth retrying"""
		if isinstance(error, DelugeConnectionError):
			return False  # Nothing answers there: there is nothing to reconnect to
		with self._login_lock:
			logins, dead_probes = seen
			if self._logins != logins:
				return True  # Another call reconnected while this one was failing
			if self._dead_probes != dead_probes:
				return False  # Another call found the Web UI not answering: asking it again would only wait as long
			if not _not_authenticated(error):
				try:
					if self._rpc("web.connected", timeout=PROBE_TIMEOUT):
						return False  # Nothing was lost: the call failed on its own
				except TorrentClientError as e:
					if not _not_authenticated(e):
						self._dead_probes += 1
						return False  # The Web UI does not answer either: nothing to reconnect to
			try:
				self._login()
			except TorrentClientError as e:
				raise TorrentClientError(f"{error} (reconnecting to Deluge failed: {e})")
			self._logins += 1
			return True

	def _to_info(self, torrent_hash, t, full=False):
		files = []
		priorities = {}
		if full:
			progress = t.get("file_progress") or []
			file_priorities = t.get("file_priorities") or []
			for i, f in enumerate(t.get("files") or []):
				size = f.get("size", 0)
				done = progress[i] if i < len(progress) else 0
				files.append((f.get("path", ""), size, int(size * done)))
				index = f.get("index", i)
				if index < len(file_priorities):
					priorities[f.get("path", "")] = _file_priority(file_priorities[index])
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
		tracker_status = t.get("tracker_status", "") or ""
		since_transfer = t.get("time_since_transfer", -1)  # Deluge 2.0+, -1 never
		return TorrentInfo(
			id=torrent_hash,
			name=t.get("name", ""),
			status=STATUS_MAP.get(state, TorrentStatus.PAUSED),
			progress=_progress(t),
			total_size=t.get("total_wanted", 0),
			downloaded=t.get("total_done", 0),
			uploaded=t.get("total_uploaded", 0),
			download_rate=t.get("download_payload_rate", 0),
			upload_rate=t.get("upload_payload_rate", 0),
			eta=eta if eta and eta > 0 else -1,
			ratio=clean_ratio(t.get("ratio", 0)),  # -1 while nothing is downloaded
			ratio_limit=t.get("stop_ratio", -1) if t.get("stop_at_ratio") else -1,
			download_limit=int(t["max_download_speed"]) if (t.get("max_download_speed") or -1) > 0 else -1,
			upload_limit=int(t["max_upload_speed"]) if (t.get("max_upload_speed") or -1) > 0 else -1,
			# Deluge copies the global setting into each torrent when adding it
			ratio_limit_mode=(RATIO_MODE_TORRENT if t.get("stop_at_ratio") else RATIO_MODE_NONE) if "stop_at_ratio" in t else "",
			seeding_time=t.get("seeding_time", -1),
			peers=t.get("num_peers", 0) + t.get("num_seeds", 0),
			seeders=t.get("num_seeds", 0),
			leechers=t.get("num_peers", 0),
			swarm_seeders=t.get("total_seeds", -1),
			swarm_leechers=t.get("total_peers", -1),
			availability=t.get("distributed_copies", -1),
			download_dir=t.get("save_path", "") or "",
			error_message=t.get("message", "") if state == "Error" else "",
			tracker_error=tracker_status[len(TRACKER_ERROR_PREFIX):] if tracker_status.startswith(TRACKER_ERROR_PREFIX) else "",
			private=bool(t.get("private", False)),
			added_date=t.get("time_added", None),
			completed_date=t.get("completed_time", 0) or None,  # Deluge 2.0+, 0 while not done
			last_activity=int(time.time()) - since_transfer if since_transfer is not None and since_transfer >= 0 else None,
			files=files,
			file_priorities=priorities,
			trackers=trackers,
		)

	def _torrents_status(self, keys, filter_dict=None):
		self._check_broken()
		return self._call("core.get_torrents_status", filter_dict or {}, keys) or {}

	def _check_broken(self):
		"""Raises RestartRequiredError while a torrent Deluge removed only
		halfway is still there: until the daemon restarts it does not answer a
		listing that includes it, nor does it save anything added meanwhile"""
		if not self._broken:
			return
		self._broken = {torrent_id for torrent_id in list(self._broken) if self._still_broken(torrent_id)}
		if self._broken:
			raise _half_removed(self._broken)

	def _still_broken(self, torrent_id):
		"""Whether a torrent Deluge removed only halfway is still there: asking
		for it fails on its gone libtorrent handle. Once the daemon restarts it
		is not there, or it is a new torrent, added again, that answers"""
		try:
			self._call("core.get_torrent_status", torrent_id, ["name"])
		except DelugeRPCError:
			return True
		return False

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
			torrents = self._torrents_status([
				"state", "progress", "total_wanted", "total_remaining", "upload_payload_rate", "download_payload_rate"])
			session = self._call(
				"core.get_session_status", ["payload_download_rate", "payload_upload_rate",
					"total_payload_download", "total_payload_upload"]) or {}
			free_space = self.get_free_space(None)
		except RestartRequiredError:
			raise
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting summary: {e}")

		counts = Counter()
		completed = 0
		uploading = 0
		downloading = 0
		for t in torrents.values():
			counts[STATUS_MAP.get(t.get("state", ""), TorrentStatus.PAUSED)] += 1
			if _progress(t) >= 100:
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
			# Deluge only counts since the daemon started, it keeps no history
			session_downloaded=int(session.get("total_payload_download", -1)),
			session_uploaded=int(session.get("total_payload_upload", -1)),
		)

	def get_torrents(self, status=None, query=None):
		try:
			torrents = self._torrents_status(LIGHT_KEYS)
		except RestartRequiredError:
			raise
		except TorrentClientError as e:
			raise TorrentClientError(f"Error listing torrents: {e}")

		result = []
		matches = query_matcher(query)
		for torrent_hash, t in torrents.items():
			if matches and not matches(t.get("name", ""), t.get("save_path", "")):
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
			if isinstance(e, DelugeRPCError) and torrent_id in self._broken:
				return None  # Deluge dropped it, only its broken entry is left
			raise TorrentClientError(f"Error getting torrent {torrent_id}: {e}")
		if not t:
			return None
		return self._to_info(torrent_id, t, full=True)

	def add_torrent(self, magnet=None, torrent_data=None, download_dir=None, category=None):
		options = {}
		if download_dir:
			options["download_location"] = download_dir
		try:
			# Deluge would not keep it past the restart it needs
			self._check_broken()
			try:
				if magnet:
					torrent_hash = self._call("core.add_torrent_magnet", magnet, options)
				elif torrent_data:
					filedump = base64.b64encode(torrent_data).decode("ascii")
					torrent_hash = self._call("core.add_torrent_file", "upload.torrent", filedump, options)
				else:
					raise TorrentClientError("No magnet or torrent data provided")
			except TorrentClientError as e:
				# The daemon refuses a torrent it already has, and names it
				duplicate = DUPLICATE_RE.search(str(e))
				if duplicate:
					torrent_id = duplicate.group(1).lower()
					raise DuplicateTorrentError(f"Torrent already in Deluge: {torrent_id}", torrent_id=torrent_id)
				raise
			if not torrent_hash:
				# Deluge 1.3 answers nothing instead
				existing = self._existing_torrent(magnet, torrent_data)
				if existing:
					raise DuplicateTorrentError(f"Torrent already in Deluge: {existing}", torrent_id=existing)
				raise TorrentClientError("Torrent added but no hash returned")
			for _ in range(10):
				info = self.get_torrent(torrent_hash)
				if info and info.name:
					return info
				time.sleep(0.5)
			info = self.get_torrent(torrent_hash)
			if info is None:
				raise TorrentClientError(f"Torrent added but not found: {torrent_hash}")
			return info
		except TorrentClientError:
			raise
		except Exception as e:
			raise TorrentClientError(f"Error adding torrent: {e}")

	def _existing_torrent(self, magnet, torrent_data):
		"""Id of the torrent Deluge already has that the magnet or .torrent
		would be, or None"""
		for torrent_id in expected_torrent_ids(magnet, torrent_data):
			try:
				if self._call("core.get_torrent_status", torrent_id, ["name"]):
					return torrent_id
			except TorrentClientError:
				pass
		return None

	def remove_torrents(self, torrent_ids, delete_data=False):
		torrent_ids = list(torrent_ids)
		try:
			skipped = self._prepare_removal(torrent_ids)
			try:
				failures = self._call("core.remove_torrents", torrent_ids, bool(delete_data)) or []
			except TorrentClientError as e:
				# Only a daemon without the method (Deluge 1.3) removes them one by
				# one: after any other failure some of them may be gone already
				if not _unknown_method(e):
					self._give_back_priorities(skipped)
					raise
				for torrent_id in torrent_ids:
					self._call("core.remove_torrent", torrent_id, bool(delete_data))
				failures = []
			# A torrent Deluge removed only halfway is still in the session, whether
			# it said so or, told again, it said nothing
			in_session = self._session_ids()
			if in_session is None:
				broken = {str(failure[0]) for failure in failures if isinstance(failure, (list, tuple)) and failure and QUEUE_LOST in str(failure)}
			else:
				broken = {torrent_id for torrent_id in torrent_ids if torrent_id in in_session}
			if broken:
				self._broken = self._broken | broken
				raise _half_removed(broken)
			if failures:
				raise TorrentClientError(f"Could not remove: {failures}")
		except RestartRequiredError:
			raise
		except TorrentClientError as e:
			raise TorrentClientError(f"Error removing torrents: {e}")

	def _prepare_removal(self, torrent_ids):
		"""Gets the torrents ready for Deluge 2.2 to remove them whole.
		Deluge keeps the torrents that are not finished in a queue of its own,
		and a finished torrent that stops being finished, because one of its
		skipped files is wanted again, only goes back there if that happens
		while it downloads: not while it is paused or queued, nor when it has
		all its data already. Removing it then drops it from libtorrent (and
		deletes its data) and fails, leaving a broken entry behind that keeps
		Deluge from listing its torrents and from saving the ones added until
		its daemon restarts. A finished torrent is removed without looking at
		that queue, so a torrent that has not downloaded anything since the
		daemon started is finished first by skipping every file, or put back
		in the queue by a recheck when it has all its data. Both are silent
		then: with data downloaded in this session Deluge would announce it as
		finished (to its plugins, or moving it to the completed folder), so
		such a torrent is removed as it is.
		Returns {id: file priorities} of the torrents whose files it skipped,
		to give them back if the removal fails"""
		skipped, waiting = {}, []
		for torrent_id in torrent_ids:
			if torrent_id in self._broken:
				continue
			try:
				t = self._call("core.get_torrent_status", torrent_id, REMOVAL_KEYS) or {}
				if t.get("is_finished") is not False or t.get("total_payload_download") != 0:
					continue
				priorities = t.get("file_priorities") or []
				if t.get("is_seed") and t.get("state") in ("Seeding", "Paused", "Queued"):
					self._call("core.force_recheck", [torrent_id])
				elif priorities and t.get("state") in ("Downloading", "Paused", "Queued"):
					self._call("core.set_torrent_options", [torrent_id], {"file_priorities": [0] * len(priorities)})
					skipped[torrent_id] = list(priorities)
				else:
					continue
			except DelugeRPCError:
				continue  # Deluge answered about this one (a broken torrent): it is removed as it is
			except TorrentClientError:
				break  # Deluge does not answer: the removal itself will tell
			waiting.append(torrent_id)
		for _ in range(REMOVAL_POLLS):
			if not waiting:
				break
			time.sleep(0.2)
			try:
				status = self._call("core.get_torrents_status", {"id": waiting}, ["is_finished", "state"]) or {}
			except TorrentClientError:
				break
			# A rechecked torrent is back in the queue as soon as it is checking
			waiting = [torrent_id for torrent_id in waiting
				if not (status.get(torrent_id) or {}).get("is_finished") and (status.get(torrent_id) or {}).get("state") != "Checking"]
		return skipped

	def _give_back_priorities(self, skipped):
		"""Puts back the file priorities _prepare_removal changed, after a removal that failed"""
		for torrent_id, priorities in skipped.items():
			try:
				self._call("core.set_torrent_options", [torrent_id], {"file_priorities": priorities})
			except DelugeRPCError:
				continue  # Removed after all
			except TorrentClientError:
				return  # Deluge does not answer: the rest would only wait as long

	def _session_ids(self):
		"""Ids of every torrent in Deluge, None when it does not say. It answers
		this one even while a broken torrent keeps it from listing"""
		try:
			return set(self._call("core.get_session_state") or [])
		except TorrentClientError:
			return None

	def set_ratio_limit(self, torrent_ids, limit):
		try:
			if limit is None:
				# No torrent follows the global setting in Deluge: it is copied
				# into each one when added, so going back to it is copying it again
				config = self._call("core.get_config_values", ["stop_seed_at_ratio", "stop_seed_ratio"]) or {}
				options = {"stop_at_ratio": bool(config.get("stop_seed_at_ratio")), "stop_ratio": float(config.get("stop_seed_ratio", 2.0))}
			elif limit < 0:
				options = {"stop_at_ratio": False}
			else:
				options = {"stop_at_ratio": True, "stop_ratio": float(limit)}
			self._call("core.set_torrent_options", list(torrent_ids), options)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting ratio limit: {e}")

	def set_torrent_speed_limit(self, torrent_ids, direction, kbps):
		key = "max_download_speed" if direction == "down" else "max_upload_speed"
		try:
			self._call("core.set_torrent_options", list(torrent_ids), {key: float(kbps) if kbps > 0 else -1.0})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting speed limit: {e}")

	def reannounce_torrents(self, torrent_ids):
		try:
			self._call("core.force_reannounce", list(torrent_ids))
		except TorrentClientError as e:
			raise TorrentClientError(f"Error reannouncing: {e}")

	def get_global_ratio_limit(self):
		try:
			config = self._call("core.get_config_values", ["stop_seed_at_ratio", "stop_seed_ratio"]) or {}
		except TorrentClientError as e:
			raise TorrentClientError(f"Error getting ratio limit: {e}")
		return float(config.get("stop_seed_ratio", -1)) if config.get("stop_seed_at_ratio") else -1

	def set_global_ratio_limit(self, limit):
		try:
			if limit < 0:
				self._call("core.set_config", {"stop_seed_at_ratio": False})
			else:
				self._call("core.set_config", {"stop_seed_at_ratio": True, "stop_seed_ratio": float(limit)})
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting ratio limit: {e}")

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

	def set_file_priority(self, torrent_id, paths, priority):
		try:
			# Deluge only takes the priorities of every file at once, by index, so
			# two changes at the same time would each write back the list as it
			# was before the other one, and one of them would be lost
			with self._file_priority_lock:
				t = self._call("core.get_torrent_status", torrent_id, ["files", "file_priorities"])
				if not t:
					raise PermanentTorrentError(f"Torrent not found: {torrent_id}")
				current = list(t.get("file_priorities") or [])
				wanted = set(paths)
				indexes = [f.get("index", i) for i, f in enumerate(t.get("files") or []) if f.get("path") in wanted]
				indexes = [i for i in indexes if i < len(current)]
				if not indexes:
					raise PermanentTorrentError(f"No such files in torrent {torrent_id}")
				for index in indexes:
					current[index] = FILE_PRIORITY_VALUES[priority]
				self._call("core.set_torrent_options", [torrent_id], {"file_priorities": current})
		except PermanentTorrentError:
			raise
		except TorrentClientError as e:
			raise TorrentClientError(f"Error setting the priority of the files of {torrent_id}: {e}")

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

	def rename_folder(self, torrent_id, old_path, new_name):
		parent = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
		new_path = f"{parent}/{new_name}" if parent else new_name
		try:
			# Deluge names folders with their trailing slash
			self._call("core.rename_folder", torrent_id, f"{old_path}/", f"{new_path}/")
		except TorrentClientError as e:
			raise TorrentClientError(f"Error renaming folder {old_path}: {e}")

	def move_torrents(self, torrent_ids, new_dir):
		try:
			self._call("core.move_storage", list(torrent_ids), new_dir, timeout=MOVE_RPC_TIMEOUT)
		except TorrentClientError as e:
			raise TorrentClientError(f"Error moving torrents: {e}")

	def get_download_dirs(self):
		try:
			torrents = self._torrents_status(["save_path"])
		except RestartRequiredError:
			raise
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
