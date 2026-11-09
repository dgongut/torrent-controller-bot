"""Test battery for the torrent client adapters. Run with: python3 test_clients.py

The adapters are the only part of the bot that talks to the torrent managers,
and every manager has its own way of saying the same thing: a torrent in error
that Deluge calls 100 % done, a ratio of -1 that means 'none yet', a duplicate
that Transmission hands back as if it were new, a removal Deluge leaves half
done. None of that shows up against the fake client of the other batteries,
so here each adapter runs for real against a fake of the manager's own API,
built from what the real ones answer (Transmission 4.1.4, Deluge 2.2.0 and
qBittorrent 5.3.0): the requests.Session of Deluge and qBittorrent and the
transmission_rpc.Client of Transmission are replaced, and nothing touches the
network. test_clients_live.py tells the same stories to real managers.

Every story starts from a fresh fake, and one that blows up fails without
stopping the others."""

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import types

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import requests
import transmission_rpc

from torrent_clients import (
	DuplicateTorrentError,
	FILE_HIGH,
	FILE_LOW,
	FILE_NORMAL,
	FILE_SKIP,
	PermanentTorrentError,
	RestartRequiredError,
	TorrentClientError,
	TorrentStatus,
	expected_torrent_ids,
)
from torrent_clients import deluge_client, qbittorrent_client, transmission_client
from torrent_clients.base import capped_progress, clean_ratio

FAILED = []
CHECKS = [0]


def check(description, actual, expected):
	CHECKS[0] += 1
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def scenario(function):
	"""Runs a story right away. An exception fails it instead of stopping the
	battery, so one broken story never hides the rest"""
	try:
		function()
	except Exception:
		CHECKS[0] += 1
		FAILED.append(f"{function.__name__} stopped:\n{traceback.format_exc()}")
	return function


def raised(function, *args, **kwargs):
	"""The exception the call raises, or None"""
	try:
		function(*args, **kwargs)
	except Exception as e:
		return e
	return None


# The adds poll the manager while it registers a new torrent: no waiting here
qbittorrent_client.time = types.SimpleNamespace(sleep=lambda seconds: None, time=time.time)
deluge_client.time = types.SimpleNamespace(sleep=lambda seconds: None, time=time.time)


# ---------------------------------------------------------------------------
# TEST TORRENTS
# ---------------------------------------------------------------------------

def bencode(value):
	if isinstance(value, int):
		return b"i%de" % value
	if isinstance(value, bytes):
		return b"%d:%s" % (len(value), value)
	if isinstance(value, str):
		return bencode(value.encode())
	if isinstance(value, list):
		return b"l" + b"".join(bencode(item) for item in value) + b"e"
	if isinstance(value, dict):
		return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
	raise TypeError(value)


INFO = {"name": "Review.Show.S01.1080p.WEB-GRP", "piece length": 16384, "pieces": b"\x01" * 60,
		"files": [{"path": ["E01.mkv"], "length": 30000}, {"path": ["E02.mkv"], "length": 15000}]}
TORRENT_DATA = bencode({"announce": "http://127.0.0.1:1/announce", "info": INFO})
TORRENT_HASH = hashlib.sha1(bencode(INFO)).hexdigest()
MAGNET = f"magnet:?xt=urn:btih:{TORRENT_HASH}&dn=Review"
MAGNET_B32 = f"magnet:?xt=urn:btih:{base64.b32encode(bytes.fromhex(TORRENT_HASH)).decode()}&dn=Review"
OTHER_HASH = hashlib.sha1(b"another torrent").hexdigest()
OTHER_MAGNET = f"magnet:?xt=urn:btih:{OTHER_HASH}&dn=Other"
LOGIN_PAGE = "<!DOCTYPE html><html><head><title>Sign in</title></head><body>SSO</body></html>"


def http_response(status=200, payload=None, text="", content_type="text/plain; charset=UTF-8"):
	"""A real requests Response, so .json() fails exactly like it does on a web page"""
	response = requests.models.Response()
	response.status_code = status
	if payload is not None:
		text = json.dumps(payload)
		content_type = "application/json"
	response._content = text.encode("utf-8")
	response.encoding = "utf-8"
	response.headers["Content-Type"] = content_type
	return response


# ---------------------------------------------------------------------------
# INFO HASHES AND RATIOS (base.py)
# ---------------------------------------------------------------------------

@scenario
def info_hashes():
	check("the id of a .torrent is the SHA-1 of its info dictionary", expected_torrent_ids(torrent_data=TORRENT_DATA), [TORRENT_HASH])
	check("the id of a magnet in hex, whatever its case", expected_torrent_ids(magnet=MAGNET.replace(TORRENT_HASH, TORRENT_HASH.upper())), [TORRENT_HASH])
	check("the id of a magnet in base32", expected_torrent_ids(magnet=MAGNET_B32), [TORRENT_HASH])
	check("the id of a magnet whose topic is percent-encoded", expected_torrent_ids(magnet=f"magnet:?xt=urn%3Abtih%3A{TORRENT_HASH.upper()}&dn=Review"),
		[TORRENT_HASH])
	check("in lowercase too", expected_torrent_ids(magnet=f"magnet:?xt=urn%3abtih%3a{TORRENT_HASH}"), [TORRENT_HASH])
	check("a topic hidden in the name is not one", expected_torrent_ids(magnet=f"magnet:?dn=a%26xt%3Durn%3Abtih%3A{OTHER_HASH}&xt=urn:btih:{TORRENT_HASH}"),
		[TORRENT_HASH])
	check("a magnet without an info hash has no id", expected_torrent_ids(magnet="magnet:?dn=nothing"), [])
	check("nor one whose hash cannot be read", expected_torrent_ids(magnet="magnet:?xt=urn:btih:zzzz"), [])
	v2 = "ab" * 32
	check("a v2 magnet is also named after its SHA-256 cut to 40 digits",
		expected_torrent_ids(magnet=f"magnet:?xt=urn:btih:{TORRENT_HASH}&xt=urn:btmh:1220{v2}"), [TORRENT_HASH, v2[:40]])
	check("the v1 id first, whatever the order of the topics (it is Transmission's id)",
		expected_torrent_ids(magnet=f"magnet:?xt=urn:btmh:1220{v2}&xt=urn:btih:{TORRENT_HASH}"), [TORRENT_HASH, v2[:40]])
	hybrid = dict(INFO, **{"meta version": 2, "file tree": {"E01.mkv": {"": {"length": 30000}}}})
	check("a hybrid .torrent can have either of its two ids", expected_torrent_ids(torrent_data=bencode({"info": hybrid})),
		[hashlib.sha1(bencode(hybrid)).hexdigest(), hashlib.sha256(bencode(hybrid)).hexdigest()[:40]])
	started = time.time()
	for broken in (b"", b"garbage", b"d4:info", b"d4:infod4:name-3:abcee", b"d4:infod4:name99:abcee", b"l" * 50000, "d4:infodee"):
		check(f"broken torrent data {broken[:20]!r} has no id", expected_torrent_ids(torrent_data=broken), [])
	check("and never hangs on it (a negative length steps backwards)", time.time() - started < 2, True)


@scenario
def progress_cap():
	# Rounding to 2 decimals must never make a torrent finished while bytes are missing
	check("30 MB missing out of 1 TB is not 100 %", capped_progress(99.997, 30 * 10 ** 6), 99.99)
	check("nothing missing is 100 %", capped_progress(100.0, 0), 100.0)
	check("a manager that does not say what is left keeps its own figure", capped_progress(99.997, None), 100.0)
	check("a normal progress is only rounded", capped_progress(42.4242, 10), 42.42)
	check("never below 0 or above 100", (capped_progress(-3, 5), capped_progress(100.4, 0)), (0.0, 100.0))
	check("junk is 0", capped_progress("junk", 5), 0.0)


@scenario
def ratios():
	for sentinel in (-1, -1.0, -2, None, "", "nan?"):
		check(f"the ratio sentinel {sentinel!r} is 0", clean_ratio(sentinel), 0.0)
	check("a real ratio is kept, rounded", clean_ratio(1.23456), 1.23)


# ---------------------------------------------------------------------------
# DELUGE: fake Web UI in front of a fake daemon
# ---------------------------------------------------------------------------

class DaemonError(Exception):
	"""An exception of the daemon, which the Web UI hands over wrapped in a Failure"""

	def __init__(self, kind, message):
		super().__init__(message)
		self.kind = kind


def deluge_torrent(name, state="Downloading", wanted=1000, remaining=None, done=None, progress=None, ratio=-1.0,
				files=None, priorities=None, since_transfer=-1, message="OK", save_path="/downloads", added=1700000000,
				downloaded_now=0):
	"""A torrent as the daemon reports it. progress is Deluge's own: the one of
	whatever the torrent is doing (100 for any error, the copy while moving).
	is_finished and queued are Deluge's own bookkeeping (see FakeDelugeWeb),
	downloaded_now its total_payload_download: bytes since the daemon started"""
	remaining = wanted if remaining is None else remaining
	done = wanted - remaining if done is None else done
	if progress is None:
		progress = 100.0 if state == "Error" else (100.0 * (wanted - remaining) / wanted if wanted else 100.0)
	files = files if files is not None else [{"index": 0, "path": f"{name}/a.mkv", "size": wanted, "offset": 0}]
	complete = wanted > 0 and remaining == 0
	return {"name": name, "state": state, "progress": progress, "total_wanted": wanted, "total_remaining": remaining,
			"total_done": done, "total_size": wanted, "download_payload_rate": 0, "upload_payload_rate": 0,
			"save_path": save_path, "time_added": added, "tracker_host": "tracker.test", "message": message,
			"ratio": ratio, "time_since_transfer": since_transfer, "total_uploaded": 0, "eta": 0, "num_peers": 0,
			"num_seeds": 0, "files": files, "file_progress": [0.0] * len(files),
			"file_priorities": list(priorities if priorities is not None else [4] * len(files)),
			"trackers": [{"url": "http://tracker.test/announce"}], "is_finished": complete, "queued": not complete,
			"is_seed": complete and state != "Error", "total_payload_download": downloaded_now}


# What a broken torrent can still be asked in a listing: its other keys need its gone libtorrent handle
HANDLE_FREE_KEYS = {"is_finished", "state", "name", "save_path", "is_seed", "total_payload_download"}


class FakeDelugeWeb:
	"""Deluge's Web UI (json_api) and its daemon, as Deluge 2.2.0 answers them.
	It also stands for the requests.Session the client talks through"""

	def __init__(self):
		self.finished_events = []  # Torrents Deluge announced as finished (TorrentFinishedEvent)
		self.password = "deluge"
		self.logged_in = False
		self.connected = False
		self.was_connected = False
		self.daemon_up = True
		self.page = None  # A web page answered instead of the API (a proxy in front)
		self.torrents = {}
		self.calls = []
		self.missing = set()  # Daemon methods it lacks (Deluge 1.3)
		self.fail = {}  # method -> exception the request raises, or DaemonError the daemon answers
		self.read_barrier = None
		self.hash_of = {TORRENT_DATA: TORRENT_HASH}
		self.session_status = {"payload_download_rate": 10, "payload_upload_rate": 5,
							"total_payload_download": 3000, "total_payload_upload": 2000}
		self.timeouts = []  # (method, timeout) of every request
		self.unreachable = False  # Nothing listens at the address
		self.hung = None  # Barrier the requests wait on before timing out: the Web UI itself hangs
		self.alerts = []  # What libtorrent tells the daemon a moment later

	def restart_daemon(self):
		"""The daemon restarts on its own: the Web UI stays up but disconnected,
		and every call to the daemon hangs instead of answering. Whatever was
		left of a torrent removed halfway is gone after it"""
		self.connected = False
		self.torrents = {torrent_id: t for torrent_id, t in self.torrents.items() if not t.get("broken")}

	def post(self, url, json=None, timeout=None):
		method, params, request_id = json["method"], json["params"], json["id"]
		self.calls.append(method)
		self.timeouts.append((method, timeout))

		def reply(result=None, error=None):
			return http_response(payload={"result": result, "error": error, "id": request_id})

		if self.unreachable:
			raise requests.exceptions.ConnectionError("Failed to establish a new connection: [Errno 111] Connection refused")
		if self.hung is not None:
			try:
				self.hung.wait()
			except threading.BrokenBarrierError:
				pass
			raise requests.exceptions.ReadTimeout(f"Read timed out. (read timeout={timeout})")
		if self.page is not None:
			return http_response(text=self.page, content_type="text/html")
		while self.alerts:
			self.alerts.pop(0)()
		if method == "auth.login":
			self.logged_in = params[0] == self.password
			return reply(self.logged_in)
		if not self.logged_in:
			return reply(error={"message": "Not authenticated", "code": 1})
		if method == "web.connected":
			return reply(self.connected)
		if method == "web.get_hosts":
			return reply([["c89d8c2d40fe4f159143cad91d059731", "127.0.0.1", 58846, "localclient"]])
		if method == "web.connect":
			if not self.daemon_up:
				return reply(error={"message": "Failure: [Failure instance: Traceback: ConnectionRefusedError]", "code": 4})
			self.connected = self.was_connected = True
			return reply(["core.get_torrents_status"])
		if not self.connected:
			if self.was_connected:
				raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")
			return reply(error={"message": "Unknown method", "code": 2})
		failure = self.fail.get(method)
		if failure is not None and not isinstance(failure, DaemonError):
			raise failure
		handler = getattr(self, "m_" + method.replace(".", "_"), None)
		if handler is None or method in self.missing:
			return reply(error={"message": "Unknown method", "code": 2})
		try:
			if failure is not None:
				raise failure
			return reply(handler(*params))
		except DaemonError as e:
			return reply(error={"code": 4, "message": "Failure: [Failure instance: Traceback (failure with no frames): "
								f"<class 'deluge.error.{e.kind}'>: {e}\n]"})

	def m_daemon_get_version(self):
		return "2.2.0"

	def m_core_get_torrents_status(self, filter_dict, keys):
		ids = filter_dict.get("id", list(self.torrents)) if filter_dict else list(self.torrents)
		torrents = {torrent_id: self.torrents[torrent_id] for torrent_id in ids if torrent_id in self.torrents}
		if any(t.get("broken") for t in torrents.values()) and set(keys) - HANDLE_FREE_KEYS:
			# Deluge 2.2 never answers a listing that asks a broken torrent's handle
			raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")
		return {torrent_id: {key: t[key] for key in keys if key in t} for torrent_id, t in torrents.items()}

	def m_core_get_session_state(self):
		return list(self.torrents)

	def m_core_get_torrent_status(self, torrent_id, keys, diff=False):
		t = self.torrents.get(torrent_id)
		if t and t.get("broken"):
			raise DaemonError("WrappedException", "invalid torrent handle used [libtorrent:20]")
		status = {key: json_copy(t[key]) for key in keys if key in t} if t else {}
		if self.read_barrier is not None:
			# Nobody gets an answer until two readers have read: unless the
			# client lets one in at a time, both read the same priorities
			try:
				self.read_barrier.wait()
			except threading.BrokenBarrierError:
				pass
		return status

	def m_core_get_session_status(self, keys):
		return {key: value for key, value in self.session_status.items() if key in keys}

	def m_core_get_free_space(self, path=None):
		return 5000

	def m_core_get_config_values(self, keys):
		return {key: value for key, value in {"download_location": "/downloads"}.items() if key in keys}

	def m_core_add_torrent_file(self, filename, filedump, options):
		return self._add(self.hash_of[base64.b64decode(filedump)], options)

	def m_core_add_torrent_magnet(self, magnet, options):
		value = re.search(r"btih:([0-9A-Za-z]+)", magnet).group(1)
		return self._add(value.lower() if len(value) == 40 else base64.b32decode(value).hex(), options)

	def _add(self, torrent_id, options):
		if torrent_id in self.torrents:
			raise DaemonError("AddTorrentError", f"Torrent already in session ({torrent_id}).")
		self.torrents[torrent_id] = deluge_torrent("Review.Show.S01.1080p.WEB-GRP", save_path=options.get("download_location", "/downloads"))
		return torrent_id

	def m_core_remove_torrents(self, torrent_ids, remove_data):
		errors = []
		for torrent_id in torrent_ids:
			try:
				self._remove(torrent_id)
			except DaemonError as e:
				errors.append([torrent_id, str(e)])
		return errors

	def m_core_remove_torrent(self, torrent_id, remove_data):
		return self._remove(torrent_id)

	def _remove(self, torrent_id):
		"""Deluge 2.2's TorrentManager.remove(): it drops the torrent from
		libtorrent before it looks for it in its queue of unfinished torrents"""
		t = self.torrents.get(torrent_id)
		if t is None:
			raise DaemonError("InvalidTorrentError", f"torrent_id {torrent_id} not in session.")
		if t.get("broken"):
			return False  # Its handle is gone: libtorrent refuses it, and Deluge says nothing
		if not t["is_finished"] and not t["queued"]:
			t["broken"] = True
			raise DaemonError("InvalidTorrentError", f"{torrent_id} is not in queued torrents set.")
		del self.torrents[torrent_id]
		return True

	def m_core_set_torrent_options(self, torrent_ids, options):
		for torrent_id in torrent_ids:
			t = self.torrents[torrent_id]
			if t.get("broken"):
				raise DaemonError("WrappedException", "invalid torrent handle used [libtorrent:20]")
			if "file_priorities" in options:
				old, new = t["file_priorities"], list(options["file_priorities"])
				t["file_priorities"] = new
				if any(before == 0 and after > 0 for before, after in zip(old, new)):
					# Torrent.set_file_priorities: wanting a skipped file again unfinishes it,
					# and only a state change seen while it downloads puts it back in the queue
					t["is_finished"] = False
					if t["state"] == "Downloading" and not t["is_seed"]:
						t["queued"] = True
				if not any(new) and not t["is_finished"] and not t["is_seed"] and t["state"] != "Error":
					# Nothing wanted: libtorrent finishes it (not a seed, which never changes state)
					self.alerts.append(lambda t=t: self._finished(t))

	def _finished(self, t):
		"""on_alert_torrent_finished: announced as finished only with data downloaded since the daemon started"""
		if not t["is_finished"] and t["total_payload_download"]:
			self.finished_events.append(t["name"])
		t["is_finished"], t["queued"] = True, False

	def m_core_force_recheck(self, torrent_ids):
		for torrent_id in torrent_ids:
			t = self.torrents[torrent_id]
			state = t["state"]
			# The state change to Checking puts it back in the queue; a seed finishes again once checked
			t.update(state="Checking", queued=True, is_finished=False)
			self.alerts.append(lambda t=t, state=state: (self._finished(t), t.update(state=state)))

	def m_core_move_storage(self, torrent_ids, dest):
		return None


def json_copy(value):
	return json.loads(json.dumps(value))


def make_deluge(web):
	deluge_client.requests = types.SimpleNamespace(Session=lambda: web)
	try:
		return deluge_client.DelugeClient("deluge.test", 8112, password="deluge")
	finally:
		deluge_client.requests = requests


def fresh_deluge(torrents=None):
	web = FakeDelugeWeb()
	web.torrents = torrents or {}
	return web, make_deluge(web)


def daemon_calls(web):
	return [method for method in web.calls if not method.startswith(("web.", "auth."))]


@scenario
def deluge_login():
	web, deluge = fresh_deluge()
	check("Deluge logs in and connects the Web UI to the daemon", web.calls, ["auth.login", "web.connected", "web.get_hosts", "web.connect"])


@scenario
def deluge_progress():
	# D1: the progress comes from the wanted bytes, never from Deluge's own number
	web, deluge = fresh_deluge({
		"e" * 40: deluge_torrent("Errored", state="Error", wanted=556200, done=0, message="Permission denied"),
		"m" * 40: deluge_torrent("Moving", state="Moving", wanted=1000, remaining=0, progress=37.5),
		"s" * 40: deluge_torrent("Skipped files", wanted=1000, remaining=400, done=900, progress=60.0),
		"n" * 40: deluge_torrent("Nothing wanted", state="Seeding", wanted=0, remaining=0, done=500, progress=100.0),
		"t" * 40: deluge_torrent("Almost", wanted=10 ** 12, remaining=1),
		"c" * 40: deluge_torrent("Complete", state="Seeding", wanted=1000, remaining=0, ratio=1.234),
	})
	listed = {t.name: t for t in deluge.get_torrents()}
	for name, progress in (("Errored", 0.0), ("Moving", 100.0), ("Skipped files", 60.0), ("Nothing wanted", 0.0),
						("Almost", 99.99), ("Complete", 100.0)):
		check(f"Deluge listing: progress of '{name}'", listed[name].progress, progress)
		torrent_id = next(i for i, t in web.torrents.items() if t["name"] == name)
		check(f"Deluge detail: progress of '{name}'", deluge.get_torrent(torrent_id).progress, progress)
	check("a torrent in error with nothing downloaded is not finished (Deluge says 100 %)", listed["Errored"].is_finished, False)
	check("it shows as an error, with Deluge's message", (listed["Errored"].status, listed["Errored"].error_message),
		(TorrentStatus.ERROR, "Permission denied"))
	check("a torrent with every file skipped is not finished, as in qBittorrent and Transmission", listed["Nothing wanted"].is_finished, False)
	check("one byte missing of a terabyte is not rounded up to finished", listed["Almost"].is_finished, False)
	check("the dashboard counts as completed only what has every wanted byte", deluge.get_summary().completed, 2)
	legacy = deluge_torrent("Deluge 1.3", progress=42.0)
	del legacy["total_remaining"]
	web.torrents = {"l" * 40: legacy}
	check("Deluge 1.3, without total_remaining, keeps its own progress", deluge.get_torrents()[0].progress, 42.0)

	# D6: no negative ratio, so sorting by ratio puts 'no data' at 0
	check("Deluge's -1 (nothing downloaded) is ratio 0", listed["Errored"].ratio, 0.0)
	check("a real Deluge ratio is kept", listed["Complete"].ratio, 1.23)
	check("no Deluge ratio is negative", min(t.ratio for t in listed.values()) >= 0, True)


@scenario
def deluge_last_activity():
	web, deluge = fresh_deluge({"a" * 40: deluge_torrent("Active", since_transfer=120),
								"b" * 40: deluge_torrent("Never", since_transfer=-1)})
	listed = {t.name: t for t in deluge.get_torrents()}
	check("Deluge last activity is the time of the last transfer", abs(listed["Active"].last_activity - (time.time() - 120)) <= 2, True)
	check("a torrent that never transferred has no last activity", listed["Never"].last_activity, None)


DELUGE_FILES = [{"index": i, "path": f"Show/E0{i}.mkv", "size": 100, "offset": 100 * i} for i in range(5)]


@scenario
def deluge_file_priorities():
	# D7: Deluge names 1-3 Low, 4 Normal and 5-7 High
	check("Deluge priorities 0-7", [deluge_client._file_priority(value) for value in range(8)],
		[FILE_SKIP, FILE_LOW, FILE_LOW, FILE_LOW, FILE_NORMAL, FILE_HIGH, FILE_HIGH, FILE_HIGH])
	web, deluge = fresh_deluge({"f" * 40: deluge_torrent("Show", files=DELUGE_FILES, priorities=[0, 1, 4, 5, 7], wanted=400)})
	check("Deluge file priorities of a torrent", deluge.get_torrent("f" * 40).file_priorities,
		{"Show/E00.mkv": FILE_SKIP, "Show/E01.mkv": FILE_LOW, "Show/E02.mkv": FILE_NORMAL, "Show/E03.mkv": FILE_HIGH, "Show/E04.mkv": FILE_HIGH})
	deluge.set_file_priority("f" * 40, ["Show/E00.mkv", "Show/E02.mkv"], FILE_HIGH)
	check("Deluge writes every priority, changing only the files asked for", web.torrents["f" * 40]["file_priorities"], [7, 1, 7, 5, 7])
	deluge.set_file_priority("f" * 40, ["Show/E04.mkv"], FILE_SKIP)
	deluge.set_file_priority("f" * 40, ["Show/E03.mkv"], FILE_LOW)
	deluge.set_file_priority("f" * 40, ["Show/E01.mkv"], FILE_NORMAL)
	check("skip, low and normal", web.torrents["f" * 40]["file_priorities"], [7, 4, 7, 1, 0])
	check("a file that is not in the torrent is a permanent error",
		type(raised(deluge.set_file_priority, "f" * 40, ["Show/nope.mkv"], FILE_LOW)), PermanentTorrentError)
	check("so is a torrent that is not there",
		type(raised(deluge.set_file_priority, "0" * 40, ["Show/E00.mkv"], FILE_LOW)), PermanentTorrentError)


class NoLock:
	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


@scenario
def deluge_concurrent_priorities():
	# D5: two changes at the same time on the same torrent both survive
	def change_two_files_at_once(web, client):
		web.torrents["f" * 40]["file_priorities"] = [4, 4, 4, 4, 4]
		web.read_barrier = threading.Barrier(2, timeout=0.5)
		threads = [threading.Thread(target=client.set_file_priority, args=("f" * 40, [path], FILE_SKIP))
				for path in ("Show/E00.mkv", "Show/E04.mkv")]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()
		web.read_barrier = None
		return web.torrents["f" * 40]["file_priorities"]

	web, deluge = fresh_deluge({"f" * 40: deluge_torrent("Show", files=DELUGE_FILES, wanted=500)})
	check("two simultaneous priority changes on one torrent are both kept", change_two_files_at_once(web, deluge), [0, 4, 4, 4, 0])
	unlocked = make_deluge(web)
	unlocked._file_priority_lock = NoLock()
	check("(without the lock one of them is lost: the race is real)", change_two_files_at_once(web, unlocked) == [0, 4, 4, 4, 0], False)


@scenario
def deluge_session_stats():
	web, deluge = fresh_deluge()
	summary = deluge.get_summary()
	check("Deluge session stats", (summary.session_downloaded, summary.session_uploaded, summary.download_rate, summary.upload_rate),
		(3000, 2000, 10, 5))
	check("Deluge keeps no history", (summary.total_downloaded, summary.total_uploaded, summary.session_seconds), (-1, -1, -1))


@scenario
def deluge_daemon_restart():
	# D2: the daemon restarts while the Web UI stays up
	web, deluge = fresh_deluge({"c" * 40: deluge_torrent("Complete", state="Seeding", wanted=1000, remaining=0)})
	web.restart_daemon()
	web.calls.clear()
	check("after a daemon restart the next call still works", [t.name for t in deluge.get_torrents()], ["Complete"])
	check("by connecting the Web UI to the daemon again and retrying once", web.calls,
		["core.get_torrents_status", "web.connected", "auth.login", "web.connected", "web.get_hosts", "web.connect", "core.get_torrents_status"])
	web.calls.clear()
	deluge.get_torrents()
	check("and the calls after it go straight through", web.calls, ["core.get_torrents_status"])

	web.calls.clear()
	web.logged_in = False  # The web session expired
	check("an expired web session logs in again", [t.name for t in deluge.get_torrents()], ["Complete"])
	check("without asking whether it is connected first", web.calls,
		["core.get_torrents_status", "auth.login", "web.connected", "core.get_torrents_status"])

	web.calls.clear()
	web.fail["core.move_storage"] = requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")
	error = raised(deluge.move_torrents, ["c" * 40], "/elsewhere")
	check("a call that fails while connected fails", isinstance(error, TorrentClientError), True)
	check("and is not repeated (moving twice is not harmless)", daemon_calls(web), ["core.move_storage"])
	check("nor does it log in again", "auth.login" in web.calls, False)
	del web.fail["core.move_storage"]

	web.restart_daemon()
	web.daemon_up = False
	error = raised(deluge.get_torrents)
	check("while the daemon is still down the call fails", isinstance(error, TorrentClientError), True)
	check("saying the reconnection failed too", "reconnecting to Deluge failed" in str(error), True)
	web.daemon_up = True
	check("and once it is back the next call reconnects", [t.name for t in deluge.get_torrents()], ["Complete"])

	web.calls.clear()
	seen = (deluge._logins, deluge._dead_probes)
	deluge._logins += 1  # Another thread reconnected while this call was failing
	check("a call that failed while another one reconnected just retries", deluge._recover(TorrentClientError("timed out"), seen), True)
	check("without reconnecting once more", web.calls, [])


@scenario
def deluge_web_ui_gone():
	# D2, when it is the Web UI that fails: reconnecting cannot help, so failing must not take longer
	web, deluge = fresh_deluge({"c" * 40: deluge_torrent("Complete", state="Seeding", wanted=1000, remaining=0)})
	web.unreachable = True
	web.calls.clear()
	error = raised(deluge.get_torrents)
	check("nothing listening at the Web UI's address is an error", isinstance(error, TorrentClientError), True)
	check("at once, without asking whether it is connected", web.calls, ["core.get_torrents_status"])
	web.unreachable = False

	# A Web UI that hangs: every request times out, four calls at the same time
	web.hung = threading.Barrier(4, timeout=2)
	web.calls.clear()
	web.timeouts.clear()
	errors = []
	threads = [threading.Thread(target=lambda: errors.append(raised(deluge.get_torrents))) for _ in range(4)]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()
	web.hung = None
	check("four calls to a hung Web UI all fail", [isinstance(error, TorrentClientError) for error in errors], [True] * 4)
	check("asking it once whether it is connected, not once per call", web.calls.count("web.connected"), 1)
	check("with a short timeout: it answers that by itself", [timeout for method, timeout in web.timeouts if method == "web.connected"],
		[deluge_client.PROBE_TIMEOUT])
	check("and none of them logs in again", "auth.login" in web.calls, False)
	check("once it answers again, so does the client", [t.name for t in deluge.get_torrents()], ["Complete"])
	web.restart_daemon()
	check("and it still reconnects after a later daemon restart", [t.name for t in deluge.get_torrents()], ["Complete"])


@scenario
def deluge_not_the_api():
	# D3: a reverse proxy or a login page answering instead of Deluge
	web, deluge = fresh_deluge()
	web.page = LOGIN_PAGE
	error = raised(deluge.get_torrents)
	check("Deluge behind a login page: a TorrentClientError", isinstance(error, TorrentClientError), True)
	check("that says what is probably in between", "proxy or a login page" in str(error), True)
	check("the dashboard too", isinstance(raised(deluge.get_summary), TorrentClientError), True)
	check("also when it is there from the start", isinstance(raised(make_deluge, web), TorrentClientError), True)


def removals(web):
	return [method for method in web.calls if method in ("core.remove_torrents", "core.remove_torrent")]


@scenario
def deluge_remove():
	# D4: only a daemon without remove_torrents (Deluge 1.3) removes one by one
	web, deluge = fresh_deluge({"a" * 40: deluge_torrent("A"), "b" * 40: deluge_torrent("B")})
	web.calls.clear()
	deluge.remove_torrents(["a" * 40, "b" * 40], delete_data=True)
	check("Deluge 2 removes them in one call", (removals(web), web.torrents), (["core.remove_torrents"], {}))

	web.torrents = {"a" * 40: deluge_torrent("A"), "b" * 40: deluge_torrent("B")}
	web.fail["core.remove_torrents"] = requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")
	web.calls.clear()
	error = raised(deluge.remove_torrents, ["a" * 40, "b" * 40], delete_data=True)
	check("a removal that times out is an error", isinstance(error, TorrentClientError), True)
	check("and never falls back to removing them one by one", removals(web), ["core.remove_torrents"])
	check("the files skipped to get them ready are wanted again", [web.torrents[i]["file_priorities"] for i in ("a" * 40, "b" * 40)], [[4], [4]])
	check("so they keep downloading", [web.torrents[i]["queued"] for i in ("a" * 40, "b" * 40)], [True, True])
	del web.fail["core.remove_torrents"]

	web.calls.clear()
	error = raised(deluge.remove_torrents, ["a" * 40, "0" * 40], delete_data=False)
	check("a torrent Deluge could not remove is an error", "Could not remove" in str(error), True)
	check("a plain one, it is not that Deluge needs a restart", type(error), TorrentClientError)
	check("without falling back either", removals(web), ["core.remove_torrents"])

	web.torrents = {"a" * 40: deluge_torrent("A"), "b" * 40: deluge_torrent("B")}
	web.missing.add("core.remove_torrents")
	web.calls.clear()
	deluge.remove_torrents(["a" * 40, "b" * 40])
	check("Deluge 1.3 removes them one by one", (removals(web), web.torrents),
		(["core.remove_torrents", "core.remove_torrent", "core.remove_torrent"], {}))


def unfinished_again(state="Paused", **kwargs):
	"""A torrent whose files were all skipped (so Deluge saw it finished) and one of
	them wanted again while it was paused or queued: Deluge does not put it back in
	its queue of unfinished torrents"""
	t = deluge_torrent("Unfinished again", state=state, files=DELUGE_FILES[:2], priorities=[4, 0], wanted=100, **kwargs)
	t.update(is_finished=False, queued=False)
	return t


def complete_unfinished_again(**kwargs):
	"""A complete torrent (a seed) with a file skipped and wanted again: Deluge no
	longer sees it finished, and libtorrent never tells it otherwise"""
	t = deluge_torrent("Complete", state="Seeding", files=DELUGE_FILES[:2], wanted=200, remaining=0, **kwargs)
	t.update(is_finished=False, queued=False)
	return t


@scenario
def deluge_half_removal():
	# Deluge 2.2 drops a torrent from libtorrent (and its data) before it looks
	# for it in its queue, and keeps a broken entry when it is not there
	web, deluge = fresh_deluge({"p" * 40: unfinished_again("Paused"), "q" * 40: unfinished_again("Queued"),
								"s" * 40: complete_unfinished_again(), "n" * 40: deluge_torrent("New"),
								"o" * 40: deluge_torrent("Other", state="Seeding", wanted=10, remaining=0)})
	error = raised(deluge.remove_torrents, ["p" * 40], delete_data=True)
	check("a paused torrent unfinished again is removed whole", (error, "p" * 40 in web.torrents), (None, False))
	check("by skipping every file first, which finishes it", web.calls.count("core.set_torrent_options"), 1)
	error = raised(deluge.remove_torrents, ["q" * 40, "n" * 40])
	check("so is a queued one, along with a plain one", (error, "q" * 40 in web.torrents, "n" * 40 in web.torrents), (None, False, False))
	web.calls.clear()
	error = raised(deluge.remove_torrents, ["s" * 40])
	check("a complete torrent unfinished again is removed whole", (error, "s" * 40 in web.torrents), (None, False))
	check("put back in the queue by a recheck: skipping its files would not finish it", web.calls.count("core.force_recheck"), 1)
	check("none of them announced as finished (nothing was downloaded)", web.finished_events, [])
	check("the listing still answers", [t.name for t in deluge.get_torrents()], ["Other"])

	# Not touched: a torrent in error, a magnet without metadata, a finished one, one checking
	web.torrents.update({"e" * 40: dict(unfinished_again("Error"), queued=True),
						"m" * 40: deluge_torrent("Magnet", files=[], priorities=[]),
						"c" * 40: dict(unfinished_again("Checking"), queued=True)})
	web.calls.clear()
	error = raised(deluge.remove_torrents, ["e" * 40, "m" * 40, "o" * 40, "c" * 40])
	check("torrents that need nothing are removed as they are", [m for m in web.calls if m in ("core.set_torrent_options", "core.force_recheck")], [])
	check("all of them", (error, web.torrents), (None, {}))

	# With data downloaded since the daemon started, finishing it would set off
	# Deluge's 'torrent finished' (its plugins, the move to the completed folder)
	web.torrents = {"d" * 40: unfinished_again("Paused", downloaded_now=50), "o" * 40: deluge_torrent("Other"),
					"z" * 40: complete_unfinished_again(downloaded_now=200)}
	web.calls.clear()
	error = raised(deluge.remove_torrents, ["d" * 40], delete_data=True)
	check("such a torrent is left as it is", (web.calls.count("core.set_torrent_options"), web.finished_events), (0, []))
	check("and when Deluge removes it only halfway, the error says to restart its daemon", type(error), RestartRequiredError)
	check("(the message too)", "Restart the Deluge daemon" in str(error) and "d" * 40 in str(error), True)
	check("its broken entry stays in Deluge", web.torrents["d" * 40].get("broken"), True)
	web.calls.clear()
	error = raised(deluge.get_torrents)
	check("listing says the same at once, instead of waiting for a Deluge that never answers", type(error), RestartRequiredError)
	check("without asking for the listing", "core.get_torrents_status" in web.calls, False)
	check("so does the dashboard", type(raised(deluge.get_summary)), RestartRequiredError)
	check("and the folders", type(raised(deluge.get_download_dirs)), RestartRequiredError)
	check("an add is refused: Deluge would lose it when restarted", type(raised(deluge.add_torrent, magnet=MAGNET)), RestartRequiredError)
	check("nothing was added", TORRENT_HASH in web.torrents, False)
	check("the torrent is gone for the bot", deluge.get_torrent("d" * 40), None)
	check("other torrents still answer", deluge.get_torrent("o" * 40).name, "Other")
	web.calls.clear()
	error = raised(deluge.remove_torrents, ["d" * 40])
	check("removing it again says the same, where Deluge says nothing", type(error), RestartRequiredError)
	error = raised(deluge.remove_torrents, ["z" * 40])
	check("as does a complete torrent with data downloaded in this session", type(error), RestartRequiredError)
	check("which was not rechecked either", web.calls.count("core.force_recheck"), 0)
	check("naming it", "z" * 40 in str(error), True)
	web.restart_daemon()
	web.torrents["d" * 40] = deluge_torrent("Added again")  # By hand, in Deluge's own interface
	check("once the daemon restarts, the same torrent added again is found", deluge.get_torrent("d" * 40).name, "Added again")
	check("the listing works again", sorted(t.name for t in deluge.get_torrents()), ["Added again", "Other"])
	check("and so do adds", deluge.add_torrent(magnet=MAGNET).id, TORRENT_HASH)


@scenario
def deluge_add_not_found():
	web, deluge = fresh_deluge()
	web.m_core_add_torrent_magnet = lambda magnet, options: OTHER_HASH  # Answers a hash it never lists
	error = raised(deluge.add_torrent, magnet=OTHER_MAGNET)
	check("an add Deluge never lists is an error, not None", (type(error), "added but not found" in str(error)), (TorrentClientError, True))


@scenario
def deluge_duplicates():
	# D9: a torrent Deluge already has
	web, deluge = fresh_deluge()
	added = deluge.add_torrent(torrent_data=TORRENT_DATA, download_dir="/downloads/first")
	check("Deluge adds a new torrent", (added.id, added.download_dir), (TORRENT_HASH, "/downloads/first"))
	for label, kwargs in (("the same .torrent", {"torrent_data": TORRENT_DATA}), ("its magnet", {"magnet": MAGNET}),
						("its magnet in base32", {"magnet": MAGNET_B32})):
		error = raised(deluge.add_torrent, download_dir="/downloads/second", **kwargs)
		check(f"Deluge: adding {label} again is a DuplicateTorrentError", type(error), DuplicateTorrentError)
		check(f"naming the torrent that was there ({label})", getattr(error, "torrent_id", None), TORRENT_HASH)
	check("which keeps its folder", web.torrents[TORRENT_HASH]["save_path"], "/downloads/first")

	web.m_core_add_torrent_file = lambda filename, filedump, options: None  # Deluge 1.3 answers nothing
	error = raised(deluge.add_torrent, torrent_data=TORRENT_DATA)
	check("Deluge 1.3 answering nothing for a torrent it has is a DuplicateTorrentError", type(error), DuplicateTorrentError)
	check("found by its info hash", getattr(error, "torrent_id", None), TORRENT_HASH)
	del web.torrents[TORRENT_HASH]
	error = raised(deluge.add_torrent, torrent_data=TORRENT_DATA)
	check("answering nothing for a torrent it does not have is a plain error", type(error), TorrentClientError)


# ---------------------------------------------------------------------------
# QBITTORRENT: fake Web API v2
# ---------------------------------------------------------------------------

def qbit_torrent(torrent_hash, name, state="downloading", progress=0.5, ratio=0.0, tags="", save_path="/downloads",
				last_activity=0, added_on=1700000000, size=1000, left=None):
	return {"hash": torrent_hash, "name": name, "state": state, "progress": progress, "size": size, "total_size": size,
			"amount_left": round(size * (1 - progress)) if left is None else left,
			"downloaded": 500, "uploaded": 0, "dlspeed": 0, "upspeed": 0, "eta": 8640000, "ratio": ratio,
			"max_ratio": -1, "ratio_limit": -2, "dl_limit": 0, "up_limit": 0, "seeding_time": 0, "max_seeding_time": -1,
			"num_leechs": 0, "num_seeds": 0, "availability": 0, "save_path": save_path, "category": "", "auto_tmm": False,
			"tracker": "http://tracker.test/announce", "added_on": added_on, "completion_on": 0,
			"last_activity": last_activity, "tags": tags, "private": False}


class FakeQbit:
	"""qBittorrent's Web API v2 as 5.3.0 answers it. add_answer switches to how
	older versions answer an add. It also stands for the requests.Session"""

	def __init__(self):
		self.torrents = {}
		self.files = {}
		self.calls = []
		self.add_answer = "json"  # "json" (5.x), "fails" or "ok" (older versions)
		self.hidden_from_tag = 0  # Polls by tag a new torrent stays invisible in
		self.page = None  # A web page answered instead of the API (a proxy in front)
		self.version = "v5.3.0"
		self.tags = set()
		self.hash_of = {TORRENT_DATA: TORRENT_HASH, MAGNET: TORRENT_HASH, MAGNET_B32: TORRENT_HASH, OTHER_MAGNET: OTHER_HASH}

	def post(self, url, data=None, timeout=None, **kwargs):
		return self.request("POST", url, data=data, timeout=timeout, **kwargs)

	def request(self, method, url, timeout=None, params=None, data=None, files=None):
		endpoint = url.split("/api/v2/", 1)[1]
		self.calls.append(endpoint)
		if self.page is not None and endpoint != "auth/login":
			return http_response(text=self.page, content_type="text/html")
		return getattr(self, "e_" + endpoint.replace("/", "_"))(params or {}, data or {}, files)

	def e_auth_login(self, params, data, files):
		return http_response(204)

	def e_app_version(self, params, data, files):
		return http_response(text=self.version)

	def e_torrents_tags(self, params, data, files):
		return http_response(payload=sorted(self.tags))

	def e_torrents_deleteTags(self, params, data, files):
		for tag in data["tags"].split(","):
			self.tags.discard(tag)
			for t in self.torrents.values():
				t["tags"] = ", ".join(x for x in t["tags"].split(", ") if x and x != tag)
		return http_response()

	def e_torrents_info(self, params, data, files):
		torrents = list(self.torrents.values())
		if "hashes" in params:
			torrents = [t for t in torrents if t["hash"] in params["hashes"].split("|")]
		if "tag" in params:
			if self.hidden_from_tag > 0:
				self.hidden_from_tag -= 1
				torrents = []
			torrents = [t for t in torrents if params["tag"] in t["tags"].split(", ")]
		return http_response(payload=torrents)

	def e_torrents_files(self, params, data, files):
		return http_response(payload=self.files.get(params["hash"], []))

	def e_torrents_trackers(self, params, data, files):
		return http_response(payload=[{"url": "http://tracker.test/announce", "status": 2, "num_seeds": 3,
									"num_leeches": 1, "msg": ""}])

	def e_torrents_add(self, params, data, files):
		key = data.get("urls") or files["torrents"][1]
		torrent_hash = self.hash_of.get(key)
		if torrent_hash is None or torrent_hash in self.torrents:
			if self.add_answer == "json":
				return http_response(409, text="Conflict")
			if self.add_answer == "fails" or torrent_hash is None:
				return http_response(text="Fails.")
			return http_response(text="Ok.")  # Some versions take a duplicate without a word
		self.tags.add(data["tags"])
		self.torrents[torrent_hash] = qbit_torrent(torrent_hash, "Review.Show.S01.1080p.WEB-GRP", tags=data["tags"],
												save_path=data.get("savepath", "/downloads"))
		if self.add_answer == "json":
			return http_response(payload={"added_torrent_ids": [torrent_hash], "failure_count": 0, "pending_count": 0, "success_count": 1})
		return http_response(text="Ok.")

	def e_torrents_filePrio(self, params, data, files):
		for index in data["id"].split("|"):
			self.files[data["hash"]][int(index)]["priority"] = data["priority"]
		return http_response()

	def e_transfer_info(self, params, data, files):
		return http_response(payload={"dl_info_speed": 10, "up_info_speed": 5, "dl_info_data": 3000, "up_info_data": 2000})

	def e_sync_maindata(self, params, data, files):
		return http_response(payload={"server_state": {"free_space_on_disk": 5000, "alltime_dl": 90000, "alltime_ul": 80000}})

	def e_transfer_speedLimitsMode(self, params, data, files):
		return http_response(text="0")

	def e_app_preferences(self, params, data, files):
		return http_response(payload={"save_path": "/downloads", "dl_limit": 0, "up_limit": 0})

	def e_torrents_categories(self, params, data, files):
		return http_response(payload={})


def make_qbit(api):
	qbittorrent_client.requests = types.SimpleNamespace(Session=lambda: api)
	try:
		return qbittorrent_client.QBittorrentClient("qbit.test", 8080, username="admin", password="secret")
	finally:
		qbittorrent_client.requests = requests


def fresh_qbit(torrents=None):
	api = FakeQbit()
	api.torrents = torrents or {}
	return api, make_qbit(api)


@scenario
def qbit_states():
	# D8: every state qBittorrent 5.3.0 has (the list of its own web UI) means something
	states = {
		"forcedDL": TorrentStatus.DOWNLOADING, "metaDL": TorrentStatus.DOWNLOADING, "forcedMetaDL": TorrentStatus.DOWNLOADING,
		"downloading": TorrentStatus.DOWNLOADING, "stalledDL": TorrentStatus.DOWNLOADING, "forcedUP": TorrentStatus.SEEDING,
		"uploading": TorrentStatus.SEEDING, "stalledUP": TorrentStatus.SEEDING, "stoppedDL": TorrentStatus.PAUSED,
		"stoppedUP": TorrentStatus.PAUSED, "queuedDL": TorrentStatus.QUEUED, "queuedUP": TorrentStatus.QUEUED,
		"checkingDL": TorrentStatus.CHECKING, "checkingUP": TorrentStatus.CHECKING, "queuedForChecking": TorrentStatus.CHECKING,
		"checkingResumeData": TorrentStatus.CHECKING, "moving": TorrentStatus.CHECKING, "error": TorrentStatus.ERROR,
		"missingFiles": TorrentStatus.ERROR,
	}
	for state, status in states.items():
		check(f"qBittorrent state {state}", qbittorrent_client.STATUS_MAP.get(state), status)
	api, qbit = fresh_qbit({"f" * 40: qbit_torrent("f" * 40, "Forced magnet", state="forcedMetaDL", progress=0)})
	check("a force-started magnet fetching its metadata is downloading, not paused",
		[t.status for t in qbit.get_torrents()], [TorrentStatus.DOWNLOADING])


@scenario
def qbit_listing():
	api, qbit = fresh_qbit({"a" * 40: qbit_torrent("a" * 40, "Active", ratio=1.5, last_activity=1700000500),
							"n" * 40: qbit_torrent("n" * 40, "Never", ratio=0, last_activity=0)})
	listed = {t.name: t for t in qbit.get_torrents()}
	check("qBittorrent ratio in the listing", (listed["Active"].ratio, listed["Never"].ratio), (1.5, 0.0))
	check("qBittorrent last activity in the listing", (listed["Active"].last_activity, listed["Never"].last_activity), (1700000500, None))


@scenario
def qbit_progress():
	# progress of a big torrent lacking a few MB rounds to 100: the bytes left say otherwise
	api, qbit = fresh_qbit({"a" * 40: qbit_torrent("a" * 40, "Almost", progress=0.99997, size=10 ** 12, left=30 * 10 ** 6),
							"d" * 40: qbit_torrent("d" * 40, "Done", state="stalledUP", progress=1, size=10 ** 12, left=0)})
	listed = {t.name: t for t in qbit.get_torrents()}
	check("qBittorrent: 30 MB missing out of 1 TB is not finished", (listed["Almost"].progress, listed["Almost"].is_finished), (99.99, False))
	check("not in the detail either", qbit.get_torrent("a" * 40).is_finished, False)
	check("a complete one is", (listed["Done"].progress, listed["Done"].is_finished), (100.0, True))
	check("the dashboard counts only the complete one", qbit.get_summary().completed, 1)


@scenario
def qbit_file_priorities():
	# 0 is not downloaded, 1 normal, 6 high and 7 maximal
	api, qbit = fresh_qbit({"a" * 40: qbit_torrent("a" * 40, "Show")})
	api.files = {"a" * 40: [{"index": i, "name": f"Show/E0{i}.mkv", "size": 100, "progress": 0, "priority": p}
							for i, p in enumerate((0, 1, 6, 7))]}
	check("qBittorrent file priorities of a torrent", qbit.get_torrent("a" * 40).file_priorities,
		{"Show/E00.mkv": FILE_SKIP, "Show/E01.mkv": FILE_NORMAL, "Show/E02.mkv": FILE_HIGH, "Show/E03.mkv": FILE_HIGH})
	check("qBittorrent has no low priority", qbit.file_priority_levels, (FILE_SKIP, FILE_NORMAL, FILE_HIGH))
	qbit.set_file_priority("a" * 40, ["Show/E00.mkv", "Show/E03.mkv"], FILE_NORMAL)
	check("qBittorrent changes only the files asked for", [f["priority"] for f in api.files["a" * 40]], [1, 1, 6, 1])
	qbit.set_file_priority("a" * 40, ["Show/E01.mkv"], FILE_SKIP)
	qbit.set_file_priority("a" * 40, ["Show/E00.mkv"], FILE_HIGH)
	check("skip and high", [f["priority"] for f in api.files["a" * 40]], [6, 0, 6, 1])
	check("a file that is not in the torrent is a permanent error",
		type(raised(qbit.set_file_priority, "a" * 40, ["nope"], FILE_HIGH)), PermanentTorrentError)


@scenario
def qbit_session_stats():
	api, qbit = fresh_qbit()
	summary = qbit.get_summary()
	check("qBittorrent session stats", (summary.session_downloaded, summary.session_uploaded, summary.total_downloaded,
		summary.total_uploaded, summary.free_space), (3000, 2000, 90000, 80000, 5000))


@scenario
def qbit_not_the_api():
	# D3: a reverse proxy or a login page answering 200 with a web page
	api, qbit = fresh_qbit({"a" * 40: qbit_torrent("a" * 40, "Show")})
	api.page = LOGIN_PAGE
	for label, call in (("the listing", qbit.get_torrents), ("a torrent", lambda: qbit.get_torrent("a" * 40)),
						("the dashboard", qbit.get_summary), ("the folders", qbit.get_download_dirs),
						("the settings", qbit.get_settings), ("the version", qbit.test_connection),
						("the ratio limit", qbit.get_global_ratio_limit), ("the categories", qbit.get_categories),
						("the default folder", qbit.get_default_download_dir),
						("a file priority", lambda: qbit.set_file_priority("a" * 40, ["x"], FILE_SKIP)),
						("a speed limit", lambda: qbit.set_speed_limit("down", None, True)),
						("an add", lambda: qbit.add_torrent(magnet=OTHER_MAGNET))):
		error = raised(call)
		check(f"qBittorrent behind a login page: {label} is a TorrentClientError", isinstance(error, TorrentClientError), True)
	check("free space is unknown instead of failing", qbit.get_free_space("/downloads"), -1)
	api.page = None
	api.version = "<html>"
	check("a version that is not one is refused", isinstance(raised(qbit.test_connection), TorrentClientError), True)
	api.version = "v5.3.0"
	check("the real version is read", qbit.test_connection(), "qBittorrent 5.3.0")


@scenario
def qbit_duplicates_5x():
	# D9: qBittorrent 5.x tells what it added, and answers 409 to a torrent it has
	api, qbit = fresh_qbit()
	added = qbit.add_torrent(torrent_data=TORRENT_DATA, download_dir="/downloads/first")
	check("qBittorrent 5.x adds a torrent", (added.id, added.download_dir), (TORRENT_HASH, "/downloads/first"))
	check("taking its hash from the answer, without looking for the tag", api.calls.count("torrents/info"), 1)
	check("the temporary tag is gone", (api.tags, api.torrents[TORRENT_HASH]["tags"]), (set(), ""))
	encoded = f"magnet:?xt=urn%3Abtih%3A{TORRENT_HASH}&dn=Review"
	api.hash_of[encoded] = TORRENT_HASH
	for label, kwargs in (("the same .torrent", {"torrent_data": TORRENT_DATA}), ("its magnet", {"magnet": MAGNET}),
						("its magnet in base32", {"magnet": MAGNET_B32}), ("its magnet percent-encoded", {"magnet": encoded})):
		api.calls.clear()
		error = raised(qbit.add_torrent, download_dir="/downloads/second", **kwargs)
		check(f"qBittorrent 5.x: adding {label} again is a DuplicateTorrentError", type(error), DuplicateTorrentError)
		check(f"naming the torrent that was there ({label})", getattr(error, "torrent_id", None), TORRENT_HASH)
		check(f"at once, without waiting for it to show up ({label})", api.calls.count("torrents/info"), 1)
	check("which keeps its folder", api.torrents[TORRENT_HASH]["save_path"], "/downloads/first")
	error = raised(qbit.add_torrent, magnet="magnet:?xt=urn:btih:zzzz")
	check("a magnet qBittorrent cannot read is a permanent error", type(error), PermanentTorrentError)
	check("no temporary tag is left behind", api.tags, set())


@scenario
def qbit_duplicates_older():
	# Before 5.x the answer is Ok. or Fails., and the new torrent is found by its temporary tag
	api, qbit = fresh_qbit({TORRENT_HASH: qbit_torrent(TORRENT_HASH, "Review.Show.S01.1080p.WEB-GRP")})
	api.add_answer = "fails"
	error = raised(qbit.add_torrent, magnet=MAGNET)
	check("'Fails.' for a torrent it has is a DuplicateTorrentError", (type(error), getattr(error, "torrent_id", None)),
		(DuplicateTorrentError, TORRENT_HASH))
	check("'Fails.' for anything else is a permanent error", type(raised(qbit.add_torrent, magnet="magnet:?xt=urn:btih:zzzz")),
		PermanentTorrentError)
	check("a new torrent is found by its temporary tag", qbit.add_torrent(magnet=OTHER_MAGNET).id, OTHER_HASH)
	del api.torrents[OTHER_HASH]

	api.add_answer = "ok"
	error = raised(qbit.add_torrent, magnet=MAGNET)
	check("a duplicate taken without a word is a DuplicateTorrentError", (type(error), getattr(error, "torrent_id", None)),
		(DuplicateTorrentError, TORRENT_HASH))
	api.hidden_from_tag = 10
	check("a new torrent slow to show up under its tag is still the new one", qbit.add_torrent(magnet=OTHER_MAGNET).id, OTHER_HASH)
	api.e_torrents_add = lambda params, data, files: http_response(text="Ok.")  # Says Ok. and adds nothing
	check("an add that never shows up is an error",
		type(raised(qbit.add_torrent, magnet=f"magnet:?xt=urn:btih:{'9' * 40}")), TorrentClientError)
	check("no temporary tag is left behind", api.tags, set())


# ---------------------------------------------------------------------------
# TRANSMISSION: fake transmission_rpc.Client
# ---------------------------------------------------------------------------

def transmission_torrent(torrent_id, torrent_hash, name, status=4, percent=0.5, ratio=0.0, activity=0,
						download_dir="/downloads", files=None, size=1000, left=None):
	files = files or [{"name": f"{name}/a.mkv", "length": 1000, "bytesCompleted": 500}]
	left = round(size * (1 - percent)) if left is None else left
	return {"id": torrent_id, "hashString": torrent_hash, "name": name, "status": status, "error": 0, "errorString": "",
			"percentDone": percent, "sizeWhenDone": size, "leftUntilDone": left, "rateDownload": 0, "rateUpload": 0,
			"downloadDir": download_dir,
			"addedDate": 1700000000 + torrent_id, "trackers": [{"announce": "http://tracker.test/announce"}],
			"uploadRatio": ratio, "activityDate": activity, "totalSize": 1000, "downloadedEver": 500, "uploadedEver": 0,
			"eta": -1, "peersConnected": 0, "peersSendingToUs": 0, "peersGettingFromUs": 0, "files": files,
			"fileStats": [{"bytesCompleted": f["bytesCompleted"], "wanted": True, "priority": 0} for f in files],
			"priorities": [0] * len(files), "wanted": [1] * len(files), "isFinished": False, "secondsSeeding": 0,
			"doneDate": 0, "seedRatioLimit": 2, "seedRatioMode": 0, "downloadLimit": 0, "downloadLimited": False,
			"uploadLimit": 0, "uploadLimited": False, "isPrivate": False, "trackerStats": []}


class FakeTransmissionRPC:
	"""transmission_rpc.Client as it behaves against Transmission 4.1.4: it
	hands back a torrent the daemon already had (torrent-duplicate) exactly
	like a new one, and the daemon leaves that one where it was"""

	def __init__(self):
		self.torrents = {}
		self.calls = []
		self.next_id = 1
		self.hash_of = {TORRENT_DATA: TORRENT_HASH, MAGNET: TORRENT_HASH, MAGNET_B32: TORRENT_HASH}
		self.stats = {"current-stats": {"downloadedBytes": 3000, "uploadedBytes": 2000, "secondsActive": 60},
					"cumulative-stats": {"downloadedBytes": 90000, "uploadedBytes": 80000, "secondsActive": 6000, "sessionCount": 7}}

	def add(self, fields):
		self.torrents[fields["hashString"]] = fields

	def get_torrents(self, ids=None, arguments=None, timeout=None):
		return [transmission_rpc.Torrent(fields=dict(t)) for t in self.torrents.values() if ids is None or t["hashString"] in ids]

	def get_torrent(self, torrent_id, arguments=None, timeout=None):
		if torrent_id not in self.torrents:
			raise KeyError(torrent_id)
		return transmission_rpc.Torrent(fields=dict(self.torrents[torrent_id]))

	def add_torrent(self, torrent, timeout=None, download_dir=None, **kwargs):
		torrent_hash = self.hash_of[torrent]
		if torrent_hash not in self.torrents:
			self.add(transmission_torrent(self.next_id, torrent_hash, "Review.Show.S01.1080p.WEB-GRP",
										download_dir=download_dir or "/downloads/complete"))
			self.next_id += 1
		existing = self.torrents[torrent_hash]
		return transmission_rpc.Torrent(fields={key: existing[key] for key in ("id", "name", "hashString")})

	def change_torrent(self, ids, timeout=None, **kwargs):
		self.calls.append((ids, kwargs))

	def get_session(self, timeout=None):
		return transmission_rpc.Session(fields={"version": "4.1.4 (a6d907d9ea)", "download-dir": "/downloads/complete",
												"alt-speed-enabled": False, "seedRatioLimited": False, "seedRatioLimit": 2})

	def session_stats(self, timeout=None):
		return transmission_rpc.SessionStats(fields=self.stats)

	def free_space(self, path, timeout=None):
		return 5000


def fresh_transmission():
	rpc = FakeTransmissionRPC()
	transmission_client.transmission_rpc = types.SimpleNamespace(Client=lambda **kwargs: rpc)
	try:
		return rpc, transmission_client.TransmissionClient("transmission.test", 9091)
	finally:
		transmission_client.transmission_rpc = transmission_rpc


@scenario
def seeding_time_limits():
	# The seeding time limit stops a torrent besides the ratio: the automatic removal tells it apart from a pause
	torrents = {}
	for torrent_hash, limit in (("a" * 40, 120), ("b" * 40, -1)):
		torrent = qbit_torrent(torrent_hash, "Limits", state="stoppedUP", progress=1.0)
		torrent["max_seeding_time"] = limit
		torrents[torrent_hash] = torrent
	_api, qbit = fresh_qbit(torrents)
	check("qBittorrent reports its seeding time limit in seconds", qbit.get_torrent("a" * 40).seeding_time_limit, 7200)
	check("none is -1", qbit.get_torrent("b" * 40).seeding_time_limit, -1)
	rpc, transmission = fresh_transmission()
	rpc.add(transmission_torrent(1, "0" * 40, "Limits", status=0, percent=1.0))
	check("Transmission has no seeding time limit", transmission.get_torrent("0" * 40).seeding_time_limit, -1)


@scenario
def transmission_duplicates():
	# D9: Transmission hands back the torrent it already had
	rpc, transmission = fresh_transmission()
	added = transmission.add_torrent(torrent_data=TORRENT_DATA, download_dir="/downloads/first")
	check("Transmission adds a new torrent", (added.id, added.download_dir), (TORRENT_HASH, "/downloads/first"))
	for label, kwargs in (("the same .torrent", {"torrent_data": TORRENT_DATA}), ("its magnet", {"magnet": MAGNET}),
						("its magnet in base32", {"magnet": MAGNET_B32})):
		error = raised(transmission.add_torrent, download_dir="/downloads/second", **kwargs)
		check(f"Transmission: adding {label} again is a DuplicateTorrentError", type(error), DuplicateTorrentError)
		check(f"naming the torrent that was there ({label})", getattr(error, "torrent_id", None), TORRENT_HASH)
	check("which keeps its folder", rpc.torrents[TORRENT_HASH]["downloadDir"], "/downloads/first")
	check("adding nothing is an error", isinstance(raised(transmission.add_torrent), TorrentClientError), True)


@scenario
def transmission_listing():
	# D6: Transmission's -1 (none yet) and -2 (infinite) are not ratios
	rpc, transmission = fresh_transmission()
	rpc.add(transmission_torrent(1, "1" * 40, "Magnet without metadata", ratio=-1, activity=0))
	rpc.add(transmission_torrent(2, "2" * 40, "Infinite", ratio=-2, activity=1700000500))
	rpc.add(transmission_torrent(3, "3" * 40, "Seeded", status=6, percent=1.0, ratio=2.345, activity=1700000600))
	listed = {t.name: t for t in transmission.get_torrents()}
	check("Transmission ratio -1 is 0", listed["Magnet without metadata"].ratio, 0.0)
	check("Transmission ratio -2 is 0", listed["Infinite"].ratio, 0.0)
	check("a real Transmission ratio is kept", listed["Seeded"].ratio, 2.35)
	check("the detail never shows a negative ratio either", transmission.get_torrent("1" * 40).ratio, 0.0)
	check("Transmission last activity in the listing", (listed["Seeded"].last_activity, listed["Magnet without metadata"].last_activity),
		(1700000600, None))


@scenario
def transmission_progress():
	# percentDone of a big torrent lacking a few MB rounds to 100: the bytes left say otherwise
	rpc, transmission = fresh_transmission()
	rpc.add(transmission_torrent(1, "1" * 40, "Almost", percent=0.99997, size=10 ** 12, left=30 * 10 ** 6))
	rpc.add(transmission_torrent(2, "2" * 40, "Done", status=6, percent=1.0, size=10 ** 12, left=0))
	listed = {t.name: t for t in transmission.get_torrents()}
	check("Transmission: 30 MB missing out of 1 TB is not finished", (listed["Almost"].progress, listed["Almost"].is_finished), (99.99, False))
	check("not in the detail either", transmission.get_torrent("1" * 40).is_finished, False)
	check("a complete one is", (listed["Done"].progress, listed["Done"].is_finished), (100.0, True))
	check("the dashboard counts only the complete one", transmission.get_summary().completed, 1)


@scenario
def transmission_file_priorities():
	# Unwanted files are skipped, the rest are low, normal or high
	rpc, transmission = fresh_transmission()
	files = [{"name": f"Show/E0{i}.mkv", "length": 100, "bytesCompleted": 0} for i in range(4)]
	fields = transmission_torrent(4, "4" * 40, "Show", files=files)
	fields.update(priorities=[0, -1, 0, 1], wanted=[0, 1, 1, 1])
	rpc.add(fields)
	check("Transmission file priorities of a torrent", transmission.get_torrent("4" * 40).file_priorities,
		{"Show/E00.mkv": FILE_SKIP, "Show/E01.mkv": FILE_LOW, "Show/E02.mkv": FILE_NORMAL, "Show/E03.mkv": FILE_HIGH})
	transmission.set_file_priority("4" * 40, ["Show/E01.mkv", "Show/E03.mkv"], FILE_SKIP)
	transmission.set_file_priority("4" * 40, ["Show/E00.mkv"], FILE_HIGH)
	transmission.set_file_priority("4" * 40, ["Show/E02.mkv"], FILE_LOW)
	transmission.set_file_priority("4" * 40, ["Show/E01.mkv"], FILE_NORMAL)
	check("Transmission skips files and gives the rest their priority, by index", rpc.calls, [
		("4" * 40, {"files_unwanted": [1, 3]}),
		("4" * 40, {"files_wanted": [0], "priority_high": [0]}),
		("4" * 40, {"files_wanted": [2], "priority_low": [2]}),
		("4" * 40, {"files_wanted": [1], "priority_normal": [1]}),
	])
	check("a file that is not in the torrent is a permanent error",
		type(raised(transmission.set_file_priority, "4" * 40, ["nope"], FILE_LOW)), PermanentTorrentError)


@scenario
def transmission_session_stats():
	# This session, all of them, the time running and the times started
	rpc, transmission = fresh_transmission()
	summary = transmission.get_summary()
	check("Transmission session stats", (summary.session_downloaded, summary.session_uploaded, summary.session_seconds,
		summary.total_downloaded, summary.total_uploaded, summary.total_seconds, summary.session_count),
		(3000, 2000, 60, 90000, 80000, 6000, 7))


# qBittorrent only knows the free space of its default save path: another
# folder may be on another disk, so its space is unknown
free_qbit = qbittorrent_client.QBittorrentClient.__new__(qbittorrent_client.QBittorrentClient)
free_qbit._get_json = lambda endpoint, params=None, kind=None: (
	{"save_path": "/downloads/"} if endpoint == "app/preferences" else {"server_state": {"free_space_on_disk": 111}})
check("qbit: free space of the default save path", free_qbit.get_free_space("/downloads"), 111)
check("qbit: and of a folder inside it", free_qbit.get_free_space("/downloads/Films"), 111)
check("qbit: a folder elsewhere is unknown", free_qbit.get_free_space("/mnt/other_disk"), -1)
check("qbit: a folder only sharing the start is elsewhere", free_qbit.get_free_space("/downloads2"), -1)

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({CHECKS[0]} checks)")
