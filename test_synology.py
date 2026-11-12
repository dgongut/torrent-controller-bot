"""Test battery for the Synology Download Station client. Run with: python3 test_synology.py

There is no NAS to test against, so the client talks to a fake DSM: a
requests.Session that answers the SYNO.* calls. Everything above it is the
real SynologyClient (discovery, login, the transport and all the logic), and
time is virtual: the sleeps of the client move a clock that the background
moves of File Station follow, so a move of hours takes no time at all.

The fake behaves the way the client's own docstrings and the README describe
the NAS, so what is checked is the client against that understanding:
- Changing the destination of a task only records it: the data stays where it
  is. A finished task answers success and ignores the change until it is
  started again, and a finished HTTP download never takes it.
- A complete task started again while its data is not where it points breaks
  (README: "breaks on the next check DSM makes"). Those land in dsm.broken.
- Pausing a task that is not running rejects the whole batch.
- The files of a torrent are listed relative to its root folder, which is
  never named, and a magnet lists no files until its metadata arrives.
- File Station moves file by file in the background, a file that is being
  copied is already there in part, the source folder goes once it is empty,
  and a move within the same shared folder is instant. A restart of the NAS
  stops the jobs running then, and File Station forgets them.

Each fake NAS has a config folder of its own, the one the bot keeps across
restarts: connecting to it again is a restart of the bot.

DSM cannot run in a container, so the closest thing to one is the last part:
the same fake DSM served over HTTP on localhost, with the real requests.Session
of the client talking to it (form encoding, multipart uploads, and the session
cookie DSM wants on them)."""

import atexit
import email.parser
import email.policy
import http.cookies
import http.server
import json
import os
import posixpath
import shutil
import sys
import tempfile
import threading
import types
import warnings
from collections import Counter
from urllib.parse import parse_qs, urlparse

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# The urllib3 of macOS complains about its LibreSSL on import
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests

# For the bot itself, loaded at the end to try its automatic mover against the NAS
os.environ.setdefault("TELEGRAM_TOKEN", "0:fake")
os.environ.setdefault("TELEGRAM_ADMIN", "1")
os.environ.setdefault("LOCALE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale"))

import config
from torrent_clients import synology_client as sc
from torrent_clients.base import (
	FILE_HIGH,
	FILE_LOW,
	FILE_NORMAL,
	FILE_SKIP,
	DuplicateTorrentError,
	PermanentTorrentError,
	TorrentClientError,
	TorrentStatus,
)

FAILED = []
CHECKS = [0]


def check(description, actual, expected):
	CHECKS[0] += 1
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def raised(function, *args, **kwargs):
	"""The exception the call raises, None when it returns"""
	try:
		function(*args, **kwargs)
	except Exception as e:
		return e
	return None


# ---------------------------------------------------------------------------
# FAKE DSM
# ---------------------------------------------------------------------------

GB = 1024 ** 3
MB = 1024 ** 2
DIR = None  # What a folder holds in FakeDSM.fs, where a file holds its size
BASE = "http://nas.lan:5000/webapi"
CONFIG_ROOT = tempfile.mkdtemp(prefix="tcb-synology-")  # Never the real /config
atexit.register(shutil.rmtree, CONFIG_ROOT, ignore_errors=True)
HOST_PATH = BASE.split("://", 1)[1]
RUNNING = ("downloading", "seeding", "waiting", "hash_checking", "finishing")

AUTH, INFO, TASK, TASK2 = sc.AUTH_API, sc.INFO_API, sc.TASK_API, sc.TASK2_API
STAT, BT_FILE, LIST, DELETE = sc.STAT_API, sc.BT_FILE_API, sc.SHARE_API, sc.DELETE_API
FOLDER, COPYMOVE = sc.FOLDER_API, sc.COPYMOVE_API

DSM7_APIS = {  # name: (path, minVersion, maxVersion)
	AUTH: ("entry.cgi", 1, 7),
	INFO: ("DownloadStation/info.cgi", 1, 2),
	TASK: ("DownloadStation/task.cgi", 1, 3),
	TASK2: ("entry.cgi", 1, 2),
	STAT: ("DownloadStation/statistic.cgi", 1, 1),
	BT_FILE: ("entry.cgi", 1, 2),
	LIST: ("entry.cgi", 1, 2),
	DELETE: ("entry.cgi", 1, 2),
	FOLDER: ("entry.cgi", 1, 2),
	COPYMOVE: ("entry.cgi", 1, 3),
}
DSM6_APIS = dict(DSM7_APIS, **{AUTH: ("auth.cgi", 1, 6), TASK: ("DownloadStation/task.cgi", 1, 1)})
del DSM6_APIS[TASK2], DSM6_APIS[BT_FILE]


class Clock:
	def __init__(self):
		self.now = 0.0

	def sleep(self, seconds):
		self.now += seconds


class FakeResponse:
	def __init__(self, payload, status_code=200):
		self.payload = payload
		self.status_code = status_code

	def json(self):
		if self.payload is None:
			raise ValueError("not JSON")  # A web page that is not the DSM API
		return self.payload


def ok(data=None):
	return FakeResponse({"success": True, "data": data})


def err(code, errors=None):
	error = {"code": code}
	if errors:
		error["errors"] = errors
	return FakeResponse({"success": False, "error": error})


class FakeSession:
	"""What the client gets instead of requests.Session"""

	def __init__(self, dsm):
		self.dsm = dsm
		self.verify = True
		self.cookies = {}

	def get(self, url, params=None, timeout=None):
		return self.dsm.handle(self, url, dict(params or {}))

	def post(self, url, data=None, files=None, timeout=None):
		return self.dsm.handle(self, url, dict(data or {}), files)


def split_ids(value):
	return [i for i in str(value or "").split(",") if i]


def share(path):
	return path.strip("/").split("/")[0]


class FakeDSM:
	def __init__(self, dsm6=False, missing=(), rate=None, fail_mode="data"):
		self.clock = Clock()
		self.apis = dict(DSM6_APIS if dsm6 else DSM7_APIS)
		for name in missing:
			del self.apis[name]
		self.user, self.password = "bot", "secret"
		self.login_error = 0
		self.logins = 0
		self.sid = None
		self.config = {"default_destination": "downloads", "bt_max_download": 0, "bt_max_upload": 0}
		self.fs = {}  # Absolute path: size of a file, or DIR
		self.free = {}  # Shared folder: bytes File Station can still write into it, unlimited when missing
		self.rate = rate  # Bytes per second of a move between shared folders, None for instant
		self.fail_mode = fail_mode  # How a failed move is reported: "data" (in the status) or "api" (error 1001)
		self.tasks = {}
		self.next_id = 1
		self.jobs = {}
		self.calls = []
		self.counts = Counter()
		self.drop = {}  # (api, method): numbers of the calls that never reach the NAS
		self.lose = {}  # (api, method): numbers of the calls done but whose answer is lost
		self.torrents = {}  # Content of a .torrent file: what its task turns out to be
		self.magnets = {}  # Magnet link: what its task turns out to be
		self.broken = []  # Ids of the tasks started again while their data was not where they point
		self.deleted = []  # Paths deleted through File Station
		self.ds_moves_data = False  # Download Station moves the data itself when the destination changes ("background": slowly)
		self.background = {}  # Moves Download Station makes by itself, which File Station knows nothing about
		self.refuse_code = 0  # Error a finished task answers to a new destination, 0 to ignore it silently
		self.ignore_edits = set()  # Ids of tasks that answer success to a new destination and keep theirs
		self.report_uri = True
		self.normalize_uri = False  # Report a magnet as Download Station rewrites it, with a hex hash
		self.bt_no_index = False  # DS2 BT.File items without an index
		self.bt_reverse = False  # DS2 BT.File items in another order than their indexes
		self.forget_finished_jobs = False
		self.reboot_at = None  # Virtual second at which the NAS restarts and File Station forgets its jobs
		self.hide_jobs = False  # File Station answers that it does not know the jobs, which keep running
		self.refuse_start = 0  # Error File Station answers to the start of a move, 0 to start it
		self.io_errors = {}  # Path of a file: error File Station hits moving it, and goes on with the next
		self.denied = set()  # APIs the user has no permission for
		self.on_create = None  # Called when a task is created, before creating it
		self.config_dir = tempfile.mkdtemp(dir=CONFIG_ROOT)  # What the bot keeps across restarts
		for folder in ("/downloads", "/video", "/video/library"):
			self.mkdir(folder)

	# -- File system -----------------------------------------------------

	def mkdir(self, path):
		parts = path.strip("/").split("/")
		for i in range(1, len(parts) + 1):
			self.fs.setdefault("/" + "/".join(parts[:i]), DIR)

	def put(self, path, size):
		self.mkdir(posixpath.dirname(path))
		self.fs[path] = size

	def isdir(self, path):
		return path in self.fs and self.fs[path] is DIR

	def under(self, path):
		return [p for p in self.fs if p == path or p.startswith(path + "/")]

	def files_under(self, path):
		return sorted(p for p in self.under(path) if self.fs[p] is not DIR)

	def remove(self, path):
		for p in self.under(path):
			del self.fs[p]

	def move_tree(self, source, folder):
		"""Moves source into folder, merging with what is already there"""
		target = f"{folder.rstrip('/')}/{posixpath.basename(source)}"
		for path in sorted(self.under(source)):
			value = self.fs.pop(path)
			if value is DIR:
				self.mkdir(target + path[len(source):])
			else:
				self.put(target + path[len(source):], value)

	def size_under(self, path):
		return sum(self.fs[p] for p in self.files_under(path))

	# -- Download Station tasks ------------------------------------------

	def add_task(self, title, destination="downloads", files=(), status="seeding", kind="bt", on_disk=True,
			uri="", trackers=(), listed=True, with_root=False, **extra):
		"""files: (name, size) or (name, size, downloaded), relative to the root
		of the content: the folder named after the title, or the file itself
		for a torrent of one file named like the task"""
		task_id = f"dbid_{self.next_id}"
		self.next_id += 1
		entries = []
		for entry in files:
			done = entry[2] if len(entry) > 2 else entry[1]
			entries.append({"name": entry[0], "size": entry[1], "done": done, "priority": "normal", "wanted": True})
		t = {
			"id": task_id, "type": kind, "title": title, "status": status, "destination": destination,
			"files": entries, "uri": uri, "create_time": 1700000000 + self.next_id, "completed_time": 0,
			"uploaded": 0, "speed_down": 0, "speed_up": 0, "seeders": 0, "leechers": 0, "seedelapsed": 0,
			"trackers": list(trackers), "error_detail": "", "listed": listed, "with_root": with_root,
		}
		t.update(extra)
		if not t["completed_time"] and self.complete(t):
			t["completed_time"] = 1700005000
		self.tasks[task_id] = t
		if on_disk:
			for f in entries:
				if f["done"] > 0:
					self.put(self.file_path(t, f), f["done"])
		return task_id

	@staticmethod
	def single(t):
		"""A torrent of one file, which is its content. Download Station may
		title it otherwise (single_file), after the name in its magnet"""
		return len(t["files"]) == 1 and (t["files"][0]["name"] == t["title"] or t.get("single_file", False))

	def root(self, t):
		return f"/{t['destination']}/{t['files'][0]['name'] if self.single(t) else t['title']}"

	def file_path(self, t, f):
		return self.root(t) if self.single(t) else f"{self.root(t)}/{f['name']}"

	@staticmethod
	def complete(t):
		wanted = [f for f in t["files"] if f["wanted"]]
		return bool(wanted) and all(f["done"] >= f["size"] for f in wanted)

	def intact(self, t):
		"""The data of the task is complete where the task points"""
		return all(self.fs.get(self.file_path(t, f)) == f["done"] for f in t["files"] if f["done"] > 0)

	def start(self, t):
		"""A task started again checks its data where it points"""
		if self.complete(t) and not self.intact(t):
			t["status"], t["error_detail"] = "error", "missing_files"
			self.broken.append(t["id"])
		else:
			t["status"] = "seeding" if self.complete(t) else "downloading"

	def show(self, t, additional):
		"""The task as Download Station lists it"""
		if t.get("metadata_in") is not None:
			t["metadata_in"] -= 1
			if t["metadata_in"] <= 0:
				spec = t.pop("metadata")
				t["metadata_in"] = None
				t["title"] = spec["title"]
				t["files"] = [{"name": n, "size": s, "done": 0, "priority": "normal", "wanted": True}
					for n, s in spec["files"]]
		parts = set(filter(None, (additional or "").split(",")))
		size = t.get("size", sum(f["size"] for f in t["files"]))
		out = {
			"id": t["id"], "type": t["type"], "username": "bot", "title": t["title"],
			"size": str(size),  # Download Station answers some numbers as strings
			"status": t["status"],
		}
		if t["status"] == "error":
			out["status_extra"] = {"error_detail": t["error_detail"]}
		additional = {}
		if "detail" in parts:
			additional["detail"] = {
				"destination": t["destination"], "uri": t["uri"] if self.report_uri else "",
				"create_time": t["create_time"], "completed_time": t["completed_time"],
				"connected_seeders": t["seeders"], "connected_leechers": t["leechers"],
				"seedelapsed": t["seedelapsed"], "priority": "auto", "total_peers": 0,
			}
		if "transfer" in parts:
			additional["transfer"] = {
				"size_downloaded": str(sum(f["done"] for f in t["files"])), "size_uploaded": str(t["uploaded"]),
				"speed_download": t["speed_down"], "speed_upload": t["speed_up"],
			}
		if "file" in parts:
			additional["file"] = [] if not t["listed"] else [{
				"filename": f"{t['title']}/{f['name']}" if t["with_root"] else f["name"],
				"size": f["size"], "size_downloaded": f["done"], "priority": f["priority"], "wanted": f["wanted"],
			} for f in t["files"]]
		if "tracker" in parts:
			additional["tracker"] = [{"url": url, "status": "Success"} for url in t["trackers"]]
		if additional:
			out["additional"] = additional
		return out

	def settle(self):
		"""A task restarted from its finished state checks its data, then seeds"""
		for t in self.tasks.values():
			if t.pop("checking", False):
				self.start(t)

	def create_from_link(self, link, destination):
		if self.on_create:
			self.on_create()
		query = parse_qs(urlparse(link).query)
		topic = (query.get("xt") or [""])[0]
		info_hash = topic.rpartition(":")[2].lower()
		spec = self.magnets.get(link, {})
		info_hash = spec.get("hash", info_hash)
		uri = f"magnet:?xt=urn:btih:{info_hash}" if self.normalize_uri else link
		title = (query.get("dn") or [info_hash])[0]
		duplicate = any(t.get("hash") == info_hash for t in self.tasks.values())
		task_id = self.add_task(title, destination, status="error" if duplicate else "downloading",
			on_disk=False, uri=uri, hash=info_hash)
		if duplicate:
			self.tasks[task_id]["error_detail"] = "torrent_duplicate"
		elif spec.get("files"):
			self.tasks[task_id].update(metadata=spec, metadata_in=spec.get("after", 2))
		return task_id

	def create_from_torrent(self, content, destination):
		if self.on_create:
			self.on_create()
		spec = self.torrents[content]
		duplicate = any(t.get("hash") == spec["hash"] for t in self.tasks.values())
		# Titled after the uploaded file until Download Station reads the metadata
		task_id = self.add_task("upload.torrent", destination, status="error" if duplicate else "downloading",
			on_disk=False, uri="upload.torrent", hash=spec["hash"])
		if duplicate:
			self.tasks[task_id]["error_detail"] = "torrent_duplicate"
		else:
			self.tasks[task_id].update(metadata=spec, metadata_in=spec.get("after", 2))
		return task_id

	# -- File Station moves ----------------------------------------------

	def advance_jobs(self):
		for job in [*self.jobs.values(), *self.background.values()]:
			if job["finished"] or job["forgotten"]:
				continue
			# A restart of the NAS stops the jobs running then, for good
			end = self.reboot_at if self.reboot_at is not None and job["start"] < self.reboot_at else None
			now = self.clock.now if end is None else min(self.clock.now, end)
			budget = float("inf") if job["instant"] else (now - job["start"]) * self.rate
			while job["queue"]:
				source, target = job["queue"][0]
				size = self.fs[source]
				room = self.free.get(share(target))
				if source in self.io_errors:
					job["queue"].pop(0)
					job["errors"].append({"code": self.io_errors[source], "path": source})  # Left where it was
					continue
				if room is not None and room < size:
					job["errors"].append({"code": 416, "path": source})  # No space left on device
					job["finished"] = True
					break
				if job["moved"] + size > budget:
					self.put(target, int(budget - job["moved"]))  # Being copied: there, in part
					break
				job["queue"].pop(0)
				if target in self.fs and job["overwrite"] == "false" and target not in job["partial"]:
					continue  # Skipped: the source stays
				self.put(target, self.fs.pop(source))
				job["moved"] += size
				if room is not None:
					self.free[share(target)] -= size
			if job["queue"]:
				job["partial"].add(job["queue"][0][1])
			for source in job["sources"]:
				if self.isdir(source) and not self.files_under(source):
					self.remove(source)  # The emptied source folder goes too
			if not job["queue"]:
				job["finished"] = True
			if job["finished"] and self.forget_finished_jobs:
				job["forgotten"] = True
			if end is not None and self.clock.now >= end:
				job["forgotten"] = True

	def start_job(self, params):
		paths = json.loads(params["path"])
		folder = params["dest_folder_path"]
		if self.refuse_start:
			return err(self.refuse_start)
		if not self.isdir(folder) or any(p not in self.fs for p in paths):
			return err(408)
		job_id = f"FileStation_{len(self.jobs) + 1:04d}"
		self.jobs[job_id] = self.new_job(job_id, paths, folder, params.get("overwrite"))
		self.advance_jobs()
		return ok({"taskid": job_id})

	def new_job(self, job_id, paths, folder, overwrite):
		queue = []
		for path in paths:
			parent = posixpath.dirname(path)
			queue += [(f, folder + f[len(parent):]) for f in self.files_under(path)]
		return {
			"id": job_id, "start": self.clock.now, "queue": queue, "sources": paths, "dest": folder,
			"moved": 0, "total": sum(self.fs[f] for f, _ in queue), "finished": False, "errors": [],
			"overwrite": overwrite, "forgotten": False, "partial": set(),
			"instant": self.rate is None or all(share(p) == share(folder) for p in paths),
		}

	def job_status(self, params):
		job = self.jobs.get(params.get("taskid"))
		if job is None or job["forgotten"] or self.hide_jobs:
			return err(599)
		if job["finished"] and job["errors"]:
			if self.fail_mode == "api":
				return err(1001, job["errors"])
			return ok({"finished": True, "errors": job["errors"], "dest_folder_path": job["dest"],
				"processed_size": job["moved"], "total": job["total"]})
		return ok({
			"finished": job["finished"], "progress": round(job["moved"] / job["total"], 4) if job["total"] else 1,
			"processed_size": job["moved"], "total": job["total"], "dest_folder_path": job["dest"],
			"path": job["queue"][0][0] if job["queue"] else "",
		})

	# -- Transport -------------------------------------------------------

	def handle(self, session, url, params, files=None):
		api, method = params.get("api"), params.get("method")
		self.advance_jobs()
		self.calls.append(types.SimpleNamespace(
			url=url, api=api, method=method, version=params.get("version"), params=params, files=files))
		self.counts[(api, method)] += 1
		number = self.counts[(api, method)]
		if number in self.drop.get((api, method), ()):
			raise requests.exceptions.ConnectTimeout(f"{api}.{method} never reached the NAS")
		response = self.answer(session, url, api, method, params, files)
		if number in self.lose.get((api, method), ()):
			raise requests.exceptions.ReadTimeout(f"Read timed out on {api}.{method}")
		return response

	def answer(self, session, url, api, method, params, files):
		where = url.split("://", 1)[-1]  # Over http or https alike
		if api == "SYNO.API.Info":
			if where != f"{HOST_PATH}/query.cgi":
				return FakeResponse(None, 404)
			wanted = params.get("query", "").split(",")
			return ok({name: {"path": path, "minVersion": low, "maxVersion": high}
				for name, (path, low, high) in self.apis.items() if name in wanted})
		if api not in self.apis:
			return err(102)
		if where != f"{HOST_PATH}/{self.apis[api][0]}":
			return FakeResponse(None, 404)
		if api == AUTH:
			return self.login(session, params)
		# A multipart request is known by the session cookie, the others by _sid
		sid = session.cookies.get("id") if files else params.get("_sid")
		if not self.sid or sid != self.sid:
			return err(119)
		if api in self.denied:
			return err(105)  # Whatever the session
		if api == INFO:
			if method == "getinfo":
				return ok({"version": 4750, "version_string": "4.0.1-4750", "is_manager": True})
			if method == "getconfig":
				return ok(dict(self.config))
			if method == "setserverconfig":
				for key in ("bt_max_download", "bt_max_upload"):
					if key in params:
						self.config[key] = int(params[key])
				return ok()
		if api == STAT:
			return ok({"speed_download": sum(t["speed_down"] for t in self.tasks.values()),
				"speed_upload": sum(t["speed_up"] for t in self.tasks.values())})
		if api == TASK:
			return self.task_api(method, params, files)
		if api == TASK2 and method == "create":
			destination = json.loads(params["destination"])
			if not self.isdir(f"/{destination}"):
				return err(403)
			if json.loads(params["type"]) == "file":
				content = files[json.loads(params["file"])[0]][1]
				return ok({"task_id": [self.create_from_torrent(content, destination)], "list_id": []})
			return ok({"task_id": [self.create_from_link(json.loads(params["url"])[0], destination)], "list_id": []})
		if api == BT_FILE:
			return self.bt_file_api(method, params)
		if api == LIST:
			if method == "getinfo":
				sizes = "size" in json.loads(params.get("additional") or "[]")
				return ok({"files": [
					dict({"path": p, "name": posixpath.basename(p), "isdir": self.fs[p] is DIR},
						**({"additional": {"size": self.fs[p] or 0}} if sizes else {})) if p in self.fs
					else {"path": p, "code": 408} for p in json.loads(params["path"])]})
			if method == "list_share":
				shares = sorted(p for p in self.fs if p.count("/") == 1 and self.fs[p] is DIR)
				return ok({"shares": [{
					"name": p[1:], "path": p, "isdir": True,
					"additional": {"volume_status": {"freespace": self.free.get(p[1:], 500 * GB), "totalspace": 4000 * GB}},
				} for p in shares], "total": len(shares), "offset": 0})
		if api == FOLDER and method == "create":
			for parent, name in zip(json.loads(params["folder_path"]), json.loads(params["name"])):
				if not self.isdir(f"/{share(parent)}"):
					return err(1100)  # Only DSM can create a shared folder
				self.mkdir(f"{parent}/{name}")
			return ok({"folders": []})
		if api == DELETE and method == "delete":
			paths = json.loads(params["path"])
			if any(p not in self.fs for p in paths):
				return err(408)
			for path in paths:
				self.remove(path)
			self.deleted += paths
			return ok()
		if api == COPYMOVE:
			if method == "start":
				return self.start_job(params)
			if method == "status":
				return self.job_status(params)
		return err(103)

	def login(self, session, params):
		if self.login_error:
			return err(self.login_error)
		if params.get("account") != self.user or params.get("passwd") != self.password:
			return err(400)
		self.logins += 1
		self.sid = f"sid-{self.logins}"
		session.cookies["id"] = self.sid  # What the Set-Cookie of the answer does
		return ok({"sid": self.sid, "is_portal_port": False})

	def task_api(self, method, params, files):
		ids = split_ids(params.get("id"))
		if method == "list":
			out = [self.show(t, params.get("additional")) for t in self.tasks.values()]
			self.settle()
			return ok({"tasks": out, "total": len(out), "offset": 0})
		if method == "getinfo":
			if any(i not in self.tasks for i in ids):
				return err(404)  # One unknown id fails the whole batch
			out = [self.show(self.tasks[i], params.get("additional")) for i in ids]
			self.settle()
			return ok({"tasks": out})
		if method == "create":
			destination = params.get("destination") or self.config["default_destination"]
			if not self.isdir(f"/{destination}"):
				return err(403)
			if files:
				self.create_from_torrent(files["file"][1], destination)
			else:
				self.create_from_link(params["uri"], destination)
			return ok()
		if method == "pause":
			if any(self.tasks[i]["status"] not in RUNNING for i in ids if i in self.tasks):
				return err(405)  # One task that cannot do it fails the whole batch
			for i in ids:
				if i in self.tasks:
					self.tasks[i]["status"] = "paused"
			return ok([{"id": i, "error": 0 if i in self.tasks else 544} for i in ids])
		if method == "resume":
			items = []
			for i in ids:
				t = self.tasks.get(i)
				if t is None:
					items.append({"id": i, "error": 544})
				elif t["status"] in ("paused", "error"):
					self.start(t)
					items.append({"id": i, "error": 0})
				elif t["status"] == "finished" and t["type"] == "bt":
					t["status"], t["checking"] = "hash_checking", True
					items.append({"id": i, "error": 0})
				else:
					items.append({"id": i, "error": 405})
			return ok(items)
		if method == "delete":
			for i in ids:
				self.tasks.pop(i, None)
			return ok([{"id": i, "error": 0} for i in ids])
		if method == "edit":
			destination = params["destination"].strip("/")
			if not self.isdir(f"/{destination}"):
				return err(100)  # Download Station only says "unknown error"
			items = []
			for i in ids:
				t = self.tasks.get(i)
				if t is None:
					items.append({"id": i, "error": 544})
					continue
				if t["status"] == "finished":
					items.append({"id": i, "error": self.refuse_code})  # And it keeps its destination
					continue
				if i in self.ignore_edits:
					items.append({"id": i, "error": 0})
					continue
				old_root = self.root(t) if t["files"] else None
				t["destination"] = destination
				if self.ds_moves_data == "background" and old_root and old_root in self.fs:
					job_id = f"DownloadStation_{len(self.background) + 1}"
					self.background[job_id] = self.new_job(job_id, [old_root], f"/{destination}", "false")
				elif self.ds_moves_data and old_root and old_root in self.fs:
					self.move_tree(old_root, f"/{destination}")
				items.append({"id": i, "error": 0})
			return ok(items)
		return err(103)

	def bt_file_api(self, method, params):
		t = self.tasks.get(json.loads(params["task_id"]))
		if t is None:
			return err(404)
		if method == "list":
			items = [{"index": n, "name": f["name"], "size": f["size"], "size_downloaded": f["done"],
				"wanted": f["wanted"], "priority": f["priority"]} for n, f in enumerate(t["files"])]
			if self.bt_no_index:
				for item in items:
					del item["index"]
			if self.bt_reverse:
				items.reverse()
			return ok({"items": items, "total": len(items)})
		if method == "set":
			for n in json.loads(params["index"]):
				t["files"][n]["wanted"] = json.loads(params["wanted"])
				if "priority" in params:
					t["files"][n]["priority"] = json.loads(params["priority"])
			return ok()
		return err(103)

	# -- Inspection --------------------------------------------------------

	def where(self, task_id):
		return self.tasks.get(task_id, {}).get("destination")  # None once the task is gone

	def status(self, task_id):
		return self.tasks.get(task_id, {}).get("status")

	def made(self, api, method):
		return [c for c in self.calls if (c.api, c.method) == (api, method)]


CURRENT = {}

# The real client, with its HTTP session going to the fake DSM and its sleeps to its clock
sc.requests = types.SimpleNamespace(Session=lambda: FakeSession(CURRENT["dsm"]))
sc.time = types.SimpleNamespace(sleep=lambda seconds: CURRENT["dsm"].clock.sleep(seconds))


def nas(**options):
	dsm = FakeDSM(**options)
	CURRENT["dsm"] = dsm
	return dsm


def connect(dsm=None, **kwargs):
	"""A bot starting against the NAS, with the config folder of that NAS"""
	dsm = dsm or CURRENT["dsm"]
	CURRENT["dsm"] = dsm
	config.CONFIG_PATH = dsm.config_dir
	return sc.SynologyClient("nas.lan", 5000, "bot", "secret", **kwargs)


def saved_moves(dsm):
	"""What the bot wrote down about the moves that have not ended, None when nothing"""
	path = os.path.join(dsm.config_dir, sc.MOVES_FILE)
	if not os.path.exists(path):
		return None
	with open(path, encoding="utf-8") as file:
		return json.load(file)


def episodes(count=4, size=30 * GB):
	return [(f"E{n:02d}.mkv", size) for n in range(1, count + 1)]


# ---------------------------------------------------------------------------
# LOGIN AND API DISCOVERY
# ---------------------------------------------------------------------------

dsm = nas()
client = connect(dsm)
discovery, login = dsm.calls[0], dsm.calls[1]
check("the APIs are discovered first, at query.cgi of the API path", discovery.url, f"{BASE}/query.cgi")
check("every API the client uses is asked for", sorted(discovery.params["query"].split(",")), sorted(sc.DISCOVER_APIS))
check("then it logs in at the path DSM gave for the auth API", (login.api, login.method, login.url), (AUTH, "login", f"{BASE}/entry.cgi"))
check("with the user, a session id format and the highest version both know",
	(login.params["account"], login.params["passwd"], login.params["format"], login.params["session"], login.version),
	("bot", "secret", "sid", "DownloadStation", 3))
check("the session id is kept", client.sid, dsm.sid)
check("DSM 7 can choose the files of a task", client.file_priority_levels, (FILE_SKIP, FILE_LOW, FILE_NORMAL, FILE_HIGH))
check("the version of Download Station", client.test_connection(), "Download Station 4.0.1-4750")
call = dsm.calls[-1]
check("calls carry the session id and go to the path of their API",
	(call.params["_sid"], call.url, call.version), (dsm.sid, f"{BASE}/DownloadStation/info.cgi", 1))

dsm.sid = None  # The session expired on the NAS
check("an expired session logs in again and the call goes through", client.test_connection(), "Download Station 4.0.1-4750")
check("only once", dsm.logins, 2)
check("with the new session id", client.sid, "sid-2")

dsm6 = nas(dsm6=True)
client6 = connect(dsm6)
check("DSM 6 logs in at auth.cgi", dsm6.calls[1].url, f"{BASE}/auth.cgi")
check("DSM 6 cannot choose the files of a task", client6.file_priority_levels, ())
check("so asking for it is refused for good",
	type(raised(client6.set_file_priority, "dbid_1", ["a.mkv"], FILE_SKIP)), PermanentTorrentError)
check("an API the NAS does not have is not called",
	str(raised(client6._call, BT_FILE, "list")), f"{BT_FILE} is not available on this NAS")

e = raised(connect, nas(), api_path="/transmission/rpc")
check("an API path left over from another manager is explained",
	(type(e), "/webapi" in str(e), "HTTP 404" in str(e)), (TorrentClientError, True, True))
e = raised(connect, nas(missing=[TASK]))
check("a NAS without Download Station is explained", str(e), "Download Station is not installed or not running on this NAS")
dsm = nas()
dsm.password = "other"
check("a wrong password is explained", str(raised(connect, dsm)), "Synology login failed: Wrong user or password")
dsm = nas()
dsm.login_error = 403
check("so is an account with two step verification", "two step verification" in str(raised(connect, dsm)), True)
dsm = nas()
client = connect(dsm, protocol="https")
check("https talks to the self signed certificate of DSM", (client.base_url, client.session.verify),
	("https://nas.lan:5000/webapi", False))


# ---------------------------------------------------------------------------
# LISTING, SUMMARY AND SETTINGS
# ---------------------------------------------------------------------------

dsm = nas()
ubuntu = dsm.add_task("Ubuntu 24.04", files=[("ubuntu.iso", 1000)], uploaded=2000, speed_up=30, seeders=1, leechers=2,
	trackers=["udp://tracker.example.org:1337/announce", "http://tracker.example.org/announce", "https://other.example.net/a"],
	seedelapsed=3600)
debian = dsm.add_task("Debian netinst", destination="video/library", files=[("netinst.iso", 1000, 250)],
	status=2, speed_down=50)  # Some DSM versions give the status as a number
finished = dsm.add_task("Old.Movie.2001", files=[("Old.Movie.2001.mkv", 700)], status="finished")
failing = dsm.add_task("Broken.Download", files=[("a.bin", 100, 10)], status="error", error_detail="disk_full")
picked = dsm.add_task("Picked.Pack", files=[("wanted.mkv", 400), ("skipped.mkv", 600, 0)], completed_time=1700009000)
queued = dsm.add_task("Waiting.One", files=[("w.bin", 100, 0)], status="waiting")
nearly = dsm.add_task("Nearly.Done", files=[("big.mkv", 50000 * MB, 49998 * MB)], status="downloading", create_time=1)
client = connect(dsm)

torrents = client.get_torrents()
by_id = {t.id: t for t in torrents}
check("the listing asks for the trackers too", dsm.made(TASK, "list")[-1].params["additional"], "detail,transfer,tracker")
check("newest first", [t.id for t in torrents], [queued, picked, failing, finished, debian, ubuntu, nearly])
check("statuses, numeric ones too", [by_id[i].status for i in (ubuntu, debian, finished, failing, queued)],
	[TorrentStatus.SEEDING, TorrentStatus.DOWNLOADING, TorrentStatus.PAUSED, TorrentStatus.ERROR, TorrentStatus.QUEUED])
check("progress and time left", (by_id[debian].progress, by_id[debian].eta), (25.0, 15))
check("a task with files left out is complete once Download Station says so", (by_id[picked].progress, by_id[picked].is_finished), (100.0, True))
check("a task with bytes still missing is never 100 %, however little is left (49,998 of 50,000 MB)",
	(by_id[nearly].progress, by_id[nearly].is_finished), (99.99, False))
check("nor in its detail", client.get_torrent(nearly).progress, 99.99)
check("ratio, peers and seeding time", (by_id[ubuntu].ratio, by_id[ubuntu].peers, by_id[ubuntu].seeding_time), (2.0, 3, 3600))
check("tracker hosts, once each", by_id[ubuntu].trackers, ["tracker.example.org", "other.example.net"])
check("the reason of an error", by_id[failing].error_message, "The disk is full")
check("the folder and the dates", (by_id[debian].download_dir, by_id[debian].added_date), ("video/library", dsm.tasks[debian]["create_time"]))
check("the listing has no files", by_id[ubuntu].files, [])
check("search by name", [t.id for t in client.get_torrents(query="ubuntu")], [ubuntu])
check("search by folder", [t.id for t in client.get_torrents(query="video/library")], [debian])
check("filter by status", [t.id for t in client.get_torrents(status=TorrentStatus.SEEDING)], [picked, ubuntu])

summary = client.get_summary()
check("summary counts", (summary.total, summary.counts[TorrentStatus.SEEDING], summary.counts[TorrentStatus.PAUSED]), (7, 2, 1))
check("summary completed and active, the nearly done one not among the completed", (summary.completed, summary.uploading, summary.downloading), (3, 1, 1))
check("summary speeds come from the statistics", (summary.download_rate, summary.upload_rate), (50, 30))
check("free space of the shared folder of the default destination", summary.free_space, 500 * GB)
check("no transfer statistics on Download Station", summary.session_downloaded, -1)
dsm.free["video"] = 123
check("free space of a folder is the one of its shared folder", client.get_free_space("video/library/x"), 123)
check("folders in use, most used first, and the default one", client.get_download_dirs(), ["downloads", "video/library"])

settings = client.get_settings()
check("settings", (settings["download_dir"], settings["speed_limit_down_enabled"], settings["alt_speed_enabled"]), ("downloads", False, False))
client.set_speed_limit("down", 500, True)
check("a speed limit goes to the server config", dsm.config["bt_max_download"], 500)
client.set_speed_limit("down", None, False)
check("turning it off sets 0", dsm.config["bt_max_download"], 0)
client.set_speed_limit("down", None, True)
check("and turning it on again brings it back", dsm.config["bt_max_download"], 500)

dsm.calls.clear()
client.pause_torrents([ubuntu, finished, failing])
check("only what is running is paused: one stopped task would fail the batch",
	[c.params["id"] for c in dsm.made(TASK, "pause")], [ubuntu])
raised(client.resume_torrents, [ubuntu, debian])
check("only what is stopped is resumed", [c.params["id"] for c in dsm.made(TASK, "resume")], [ubuntu])
check("and it seeds again", dsm.status(ubuntu), "seeding")


# ---------------------------------------------------------------------------
# DETAIL OF A TASK, WITH THE PRIORITIES OF ITS FILES (v1.13)
# ---------------------------------------------------------------------------

dsm = nas()
show = dsm.add_task("Show.S01", files=[("b.mkv", 300), ("A.mkv", 200), ("c/d.srt", 10), ("e.nfo", 5, 0), ("f.txt", 1)],
	seeders=4, leechers=1, seedelapsed=120, completed_time=1700005000)
dsm.tasks[show]["files"][0]["priority"] = "high"
dsm.tasks[show]["files"][1]["priority"] = "low"
dsm.tasks[show]["files"][2]["priority"] = "skip"
dsm.tasks[show]["files"][3]["wanted"] = False
dsm.tasks[show]["files"][4]["priority"] = "auto"
client = connect(dsm)
info = client.get_torrent(show)
check("detail asks for files and trackers", dsm.made(TASK, "getinfo")[-1].params["additional"], "detail,transfer,file,tracker")
check("files sorted by path, with size and what is downloaded", info.files,
	[("A.mkv", 200, 200), ("b.mkv", 300, 300), ("c/d.srt", 10, 10), ("e.nfo", 5, 0), ("f.txt", 1, 1)])
check("priorities: high, low, skip as priority or as not wanted, anything else normal", info.file_priorities,
	{"b.mkv": FILE_HIGH, "A.mkv": FILE_LOW, "c/d.srt": FILE_SKIP, "e.nfo": FILE_SKIP, "f.txt": FILE_NORMAL})
check("peers and seeding time", (info.seeders, info.leechers, info.peers, info.seeding_time), (4, 1, 5, 120))
check("dates", (info.added_date, info.completed_date), (dsm.tasks[show]["create_time"], 1700005000))
check("a task that is not there", client.get_torrent("dbid_404"), None)


# ---------------------------------------------------------------------------
# ADDING A TORRENT FILE AND A MAGNET
# ---------------------------------------------------------------------------

dsm = nas()
content = b"d4:infod4:name14:Big.Buck.Bunnyee"
dsm.torrents[content] = {"hash": "aa" * 20, "title": "Big.Buck.Bunny", "files": [("bbb.mkv", 900), ("bbb.srt", 10)]}
client = connect(dsm)
info = client.add_torrent(torrent_data=content, download_dir="/video/library/")
create = dsm.made(TASK2, "create")[0]
check("DSM 7 uploads a .torrent through DownloadStation2, with JSON encoded values",
	{k: create.params[k] for k in ("type", "file", "destination", "create_list")},
	{"type": '"file"', "file": '["torrent"]', "destination": '"video/library"', "create_list": "false"})
check("the content goes in the part named in file", (list(create.files), create.files["torrent"][0], create.files["torrent"][1]),
	(["torrent"], "upload.torrent", content))
check("a multipart call is known by the session cookie, not by _sid", "_sid" in create.params, False)
check("the added torrent comes back with its real name once Download Station reads it",
	(info.name, info.download_dir, info.total_size, info.status), ("Big.Buck.Bunny", "video/library", 910, TorrentStatus.DOWNLOADING))
check("it is the task DownloadStation2 reported", info.id, sorted(dsm.tasks)[-1])

e = raised(client.add_torrent, torrent_data=content)
check("a torrent already there is refused for good, as a duplicate", type(e), DuplicateTorrentError)
check("naming the task that was already there", str(e), "Error adding torrent: The torrent is already in Download Station: Big.Buck.Bunny")
check("whose id it carries, found by the name inside the .torrent file", getattr(e, "torrent_id", None), info.id)
check("and the task Download Station made for it is cleaned up", len(dsm.tasks), 1)

dsm6 = nas(dsm6=True)
dsm6.torrents[content] = dsm.torrents[content]
client6 = connect(dsm6)
info = client6.add_torrent(torrent_data=content)
create = dsm6.made(TASK, "create")[0]
check("DSM 6 uploads through the Task API, to the default destination",
	(list(create.files), create.params["destination"], info.name, info.download_dir),
	(["file"], "downloads", "Big.Buck.Bunny", "downloads"))

dsm = nas()
magnet = "magnet:?xt=urn:btih:" + "cd" * 20 + "&dn=Night+of+the+Living+Dead&tr=udp%3A%2F%2Ftracker.example.org%3A1337"
client = connect(dsm)
info = client.add_torrent(magnet=magnet)
create = dsm.made(TASK, "create")[0]
check("a magnet goes through the Task API, to the default destination",
	(create.params["uri"], create.params["destination"]), (magnet, "downloads"))
check("and without metadata comes back as Download Station names it",
	(info.name, info.files, info.total_size, info.download_dir), ("Night of the Living Dead", [], 0, "downloads"))

dsm = nas()
client = connect(dsm)
info = client.add_torrent(magnet=magnet, download_dir="video/Classics")
check("a destination that does not exist is created and the add retried",
	(dsm.isdir("/video/Classics"), info.download_dir, len(dsm.made(TASK, "create"))), (True, "video/Classics", 2))
check("by File Station, under its shared folder",
	{k: dsm.made(FOLDER, "create")[0].params[k] for k in ("folder_path", "name", "force_parent")},
	{"folder_path": '["/video"]', "name": '["Classics"]', "force_parent": "true"})
e = raised(client.add_torrent, magnet=magnet, download_dir="nosuchshare/x")
check("a shared folder that does not exist cannot be created",
	str(e), "Error adding torrent: Synology error on SYNO.DownloadStation2.Task.create: Destination does not exist")


# ---------------------------------------------------------------------------
# REMOVING, WITH AND WITHOUT THE DATA
# ---------------------------------------------------------------------------

dsm = nas()
folder_task = dsm.add_task("Show.S02", files=[("E01.mkv", 100), ("Subs/E01.srt", 1)])
single_task = dsm.add_task("film.mkv", files=[("film.mkv", 300)])
kept_task = dsm.add_task("Kept", files=[("k.bin", 5)])
client = connect(dsm)
raised(client.remove_torrents, [kept_task])
check("removing the task alone leaves its data", (kept_task in dsm.tasks, "/downloads/Kept/k.bin" in dsm.fs), (False, True))
check("without File Station", dsm.made(DELETE, "delete"), [])
raised(client.remove_torrents, [folder_task, single_task], delete_data=True)
check("removing with the data deletes the folder of a torrent and the file of a single file one",
	sorted(dsm.deleted), ["/downloads/Show.S02", "/downloads/film.mkv"])
check("recursively", dsm.made(DELETE, "delete")[0].params["recursive"], "true")
check("both tasks are gone", (folder_task in dsm.tasks, single_task in dsm.tasks), (False, False))
check("and nothing else is touched", sorted(p for p in dsm.fs if p.startswith("/downloads/")), ["/downloads/Kept", "/downloads/Kept/k.bin"])

dsm = nas(missing=[DELETE])
task = dsm.add_task("Show.S03", files=[("E01.mkv", 100)])
client = connect(dsm)
e = raised(client.remove_torrents, [task], delete_data=True)
check("without File Station the data cannot be deleted, and the task is not removed either",
	(str(e), task in dsm.tasks), ("Deleting the downloaded data needs File Station, which is not available for this user", True))


# ---------------------------------------------------------------------------
# MOVING
# ---------------------------------------------------------------------------

dsm = nas(rate=100 * MB)
show = dsm.add_task("Show.S01.1080p", files=[("E01.mkv", 700 * MB), ("E02.mkv", 700 * MB), ("Subs/E01.srt", 1)])
client = connect(dsm)
dsm.calls.clear()
raised(client.move_torrents, [show], "/video/library/")
steps = [(c.api, c.method) for c in dsm.calls if (c.api, c.method) in
	{(TASK, "pause"), (TASK, "edit"), (COPYMOVE, "start"), (TASK, "resume")}]
check("a seeding torrent is stopped, pointed at the new folder, its data moved, and started again",
	steps, [(TASK, "pause"), (TASK, "edit"), (COPYMOVE, "start"), (TASK, "resume")])
start = dsm.made(COPYMOVE, "start")[0]
check("File Station moves its folder, without overwriting anything",
	{k: start.params[k] for k in ("path", "dest_folder_path", "remove_src", "overwrite")},
	{"path": '["/downloads/Show.S01.1080p"]', "dest_folder_path": "/video/library", "remove_src": "true", "overwrite": "false"})
check("the data is at the destination and not in the old folder",
	(dsm.size_under("/video/library/Show.S01.1080p"), "/downloads/Show.S01.1080p" in dsm.fs), (1400 * MB + 1, False))
check("the task points there and seeds", (dsm.where(show), dsm.status(show), dsm.broken), ("video/library", "seeding", []))
check("it waited for File Station to finish", dsm.clock.now >= 14, True)
check("the move is over", client._moves, {})
dsm.calls.clear()
raised(client.move_torrents, [show], "video/library")
check("moving it where it already is does nothing", [(c.api, c.method) for c in dsm.calls if c.method in ("edit", "start", "pause")], [])

dsm = nas()
show = dsm.add_task("Show.S04", files=[("E01.mkv", 100)])
client = connect(dsm)
raised(client.move_torrents, [show], "video/New Shows")
check("a destination that does not exist is created first",
	(dsm.isdir("/video/New Shows"), dsm.where(show), "/video/New Shows/Show.S04/E01.mkv" in dsm.fs), (True, "video/New Shows", True))

dsm = nas()
done = dsm.add_task("Finished.Torrent", files=[("f.mkv", 100)], status="finished")
client = connect(dsm)
raised(client.move_torrents, [done], "video/library")
check("a finished torrent is restarted so that it takes the new destination, and its data moved",
	(dsm.where(done), "/video/library/Finished.Torrent/f.mkv" in dsm.fs, dsm.broken), ("video/library", True, []))

dsm = nas()
dsm.ds_moves_data = True
show = dsm.add_task("Show.S05", files=[("E01.mkv", 100)])
client = connect(dsm)
raised(client.move_torrents, [show], "video/library")
check("when Download Station moves the data by itself File Station is not asked",
	(dsm.made(COPYMOVE, "start"), "/video/library/Show.S05/E01.mkv" in dsm.fs, dsm.status(show)), ([], True, "seeding"))

dsm = nas(rate=115 * MB)
dsm.ds_moves_data = "background"
film = dsm.add_task("Film.2021", files=[("Film.2021.mkv", 30 * GB)])
client = connect(dsm)
check("when Download Station moves the data by itself, in the background, the move waits for it",
	raised(client.move_torrents, [film], "video/library"), None)
check("without File Station moving it as well, and the task seeds from there once it is all there",
	(dsm.made(COPYMOVE, "start"), dsm.fs.get("/video/library/Film.2021/Film.2021.mkv"), "/downloads/Film.2021" in dsm.fs,
		dsm.status(film), dsm.broken), ([], 30 * GB, False, "seeding", []))

dsm = nas()
fresh = dsm.add_task("Just.Started", files=[("a.mkv", 100, 0)], status="downloading", on_disk=False)
halfway = dsm.add_task("Half.Way", files=[("b.mkv", 100, 40)], status="downloading", on_disk=False)
client = connect(dsm)
raised(client.move_torrents, [fresh, halfway], "video/library")
check("a task with nothing on disk yet only changes its destination",
	(dsm.where(fresh), dsm.made(COPYMOVE, "start"), dsm.status(fresh)), ("video/library", [], "downloading"))
check("and so does an unfinished one whose pieces are not in its folder",
	(dsm.where(halfway), dsm.status(halfway)), ("video/library", "downloading"))

dsm = nas()
show = dsm.add_task("Show.S06", files=[("E01.mkv", 100)])
dsm.put("/video/library/Show.S06/other.mkv", 5)
client = connect(dsm)
e = raised(client.move_torrents, [show], "video/library")
check("moving on top of something already there is refused for good",
	(type(e), str(e)), (PermanentTorrentError, "Error moving torrents: 'Show.S06' already exists in video/library"))
check("before touching anything", (dsm.where(show), dsm.status(show), dsm.made(TASK, "pause")), ("downloads", "seeding", []))

dsm = nas()
dsm.mkdir("/video/other")
one = dsm.add_task("Same.Name", files=[("a.mkv", 100)])
two = dsm.add_task("Same.Name", destination="video/other", files=[("b.mkv", 100)])
client = connect(dsm)
e = raised(client.move_torrents, [one, two], "video/library")
check("two contents of the same name are not moved into one folder, for good",
	(type(e), str(e), dsm.made(TASK, "pause")),
	(PermanentTorrentError, "Error moving torrents: 'Same.Name' would end up twice in video/library", []))

dsm = nas()
show = dsm.add_task("Show.S08", files=[("E01.mkv", 100)])
client = connect(dsm)
e = raised(client.move_torrents, [show], "nosuchshare/Shows")
check("a destination in a shared folder that does not exist is refused for good",
	(type(e), str(e), dsm.where(show), dsm.made(TASK, "pause")),
	(PermanentTorrentError, "Error moving torrents: The destination folder 'nosuchshare/Shows' does not exist and cannot be created",
		"downloads", []))
dsm.drop[(FOLDER, "create")] = {dsm.counts[(FOLDER, "create")] + 1}
e = raised(client.move_torrents, [show], "video/Shows")
check("a folder File Station does not answer about is something to try again",
	(type(e), "CreateFolder.create" in str(e), dsm.where(show), dsm.made(TASK, "pause")), (TorrentClientError, True, "downloads", []))
check("which then creates it and moves the data there", (raised(client.move_torrents, [show], "video/Shows"), dsm.where(show),
	"/video/Shows/Show.S08/E01.mkv" in dsm.fs), (None, "video/Shows", True))
dsm.drop[(LIST, "getinfo")] = {dsm.counts[(LIST, "getinfo")] + 1}
e = raised(client.move_torrents, [show], "video/library")
check("a File Station that does not answer is something to try again",
	(type(e), "File Station cannot tell what is on disk" in str(e), dsm.where(show), dsm.status(show)),
	(TorrentClientError, True, "video/Shows", "seeding"))
check("which then works", (raised(client.move_torrents, [show], "video/library"), dsm.where(show)), (None, "video/library"))

dsm = nas(missing=[LIST])
show = dsm.add_task("Show.S09", files=[("E01.mkv", 100)])
magnet_only = dsm.add_task("Magnet.Only", status="downloading", on_disk=False)
client = connect(dsm)
e = raised(client.move_torrents, [show], "video/library")
check("without File Station the data cannot be moved, and the user is told for good",
	(type(e), str(e)), (PermanentTorrentError,
		"Error moving torrents: File Station is not available for this user, and it is needed to see the data on disk"))
check("before touching anything, so nothing breaks", (dsm.where(show), dsm.status(show), dsm.made(TASK, "pause"), dsm.broken),
	("downloads", "seeding", [], []))
raised(client.move_torrents, [magnet_only], "video/library")
check("a task with no data of its own is still pointed elsewhere", dsm.where(magnet_only), "video/library")

dsm = nas()
dsm.denied = {LIST}
show = dsm.add_task("Show.S10", files=[("E01.mkv", 100)])
client = connect(dsm)
e = raised(client.move_torrents, [show], "video/library")
check("a user File Station refuses cannot move the data, and is told for good",
	(type(e), str(e)), (PermanentTorrentError, "Error moving torrents: File Station cannot tell what is on disk: Synology session "
		"error on SYNO.FileStation.List.getinfo: The logged in user has no permission for this API"))
check("after logging in again once, without touching anything", (dsm.logins, dsm.where(show), dsm.status(show)), (2, "downloads", "seeding"))

check("File Station error texts", [client._describe_error(COPYMOVE, code) for code in (412, 413, 414, 416, 420)],
	["The file name is too long", "The file name is too long", "File already exists", "No space left on device",
		"Illegal file name on a FAT file system"])


# ---------------------------------------------------------------------------
# S1: A MOVE IS ONLY DONE WHEN THE DATA IS AT THE DESTINATION
# ---------------------------------------------------------------------------

def seasons(**options):
	"""Four seasons in 4K seeding in downloads, 120 GB each, moved to another
	shared folder at 115 MB/s: the 480 GB take 71 minutes, longer than the
	hour a single attempt waits for File Station"""
	dsm = nas(rate=115 * MB, **options)
	ids = [dsm.add_task(f"Show S0{n} 2160p", files=episodes()) for n in range(1, 5)]
	return dsm, connect(dsm), ids


# The move outlives the wait of the first attempt
dsm, client, ids = seasons()
e = raised(client.move_torrents, ids, "video/library")
check("a move File Station is still doing is not reported as done", type(e), TorrentClientError)
check("but as something to try again", str(e), "Error moving torrents: File Station is still moving the data to video/library")
check("no task was started while its data was being moved", (dsm.broken, {dsm.status(i) for i in ids}), ([], {"paused"}))
check("File Station is still at it", [job["finished"] for job in dsm.jobs.values()], [False])
check("with the data in both folders", (dsm.size_under("/video/library") > 0, dsm.size_under("/downloads") > 0), (True, True))
e = raised(client.remove_torrents, [ids[3]], delete_data=True)
check("its data cannot be deleted under File Station's feet", (type(e), ids[3] in dsm.tasks, dsm.deleted), (TorrentClientError, True, []))
e = raised(client.resume_torrents, ids)
check("nor can the user start the tasks before their data is all there: a refusal, saying what to do", (type(e), str(e)),
	(PermanentTorrentError, "Error resuming torrents: the data of 'Show S01 2160p', 'Show S02 2160p', 'Show S03 2160p', "
		"'Show S04 2160p' is still being moved to video/library, and it cannot start before it is all there: "
		"order the same move again to follow it to its end"))
check("so none of them breaks", (dsm.broken, {dsm.status(i) for i in ids}), ([], {"paused"}))
other = dsm.add_task("Other.Paused", destination="video", files=[("o.bin", 10)], status="paused")
e = raised(client.resume_torrents, [*ids, other])
check("resuming them along with other tasks starts the others alone",
	(dsm.status(other), {dsm.status(i) for i in ids}, dsm.broken), ("seeding", {"paused"}, []))
check("and says which ones were held and that the others did start", (type(e), str(e)),
	(PermanentTorrentError, "Error resuming torrents: the data of 'Show S01 2160p', 'Show S02 2160p', 'Show S03 2160p', "
		"'Show S04 2160p' is still being moved to video/library, and it cannot start before it is all there: "
		"order the same move again to follow it to its end. The other torrents were resumed"))
polls = dsm.counts[(COPYMOVE, "status")]
dsm.clock.sleep(60)  # The bot tries again a minute later
check("the retry waits for that move instead of taking the tasks for moved", raised(client.move_torrents, ids, "video/library"), None)
check("it did wait", (dsm.counts[(COPYMOVE, "status")] > polls, all(job["finished"] for job in dsm.jobs.values())), (True, True))
check("for the same job, without starting another", len(dsm.jobs), 1)
check("all the data is at the destination", (dsm.size_under("/video/library"), dsm.size_under("/downloads")), (480 * GB, 0))
check("and the tasks seed from there", ({dsm.where(i) for i in ids}, {dsm.status(i) for i in ids}, dsm.broken),
	({"video/library"}, {"seeding"}, []))
check("the move is over", client._moves, {})

# A second order to another folder waits for the first move, then moves on from there
dsm, client, ids = seasons()
dsm.mkdir("/video/other")
check("first order still running", type(raised(client.move_torrents, ids, "video/library")), TorrentClientError)
dsm.clock.sleep(60)
check("a new order to another folder waits for it and then moves the data on", raised(client.move_torrents, ids, "video/other"), None)
check("ending up where the last order said", ({dsm.where(i) for i in ids}, dsm.size_under("/video/other"), dsm.size_under("/video/library/Show S01 2160p")),
	({"video/other"}, 480 * GB, 0))
check("seeding, and nothing broken", ({dsm.status(i) for i in ids}, dsm.broken), ({"seeding"}, []))

# File Station stops half way, saying so in the data of the status or as error 1001 of the status call
NO_ROOM = ("Error moving torrents: File Station could not move the data to video/library: No space left on device "
	"(/downloads/Show S02 2160p/E03.mkv). Moved: 'Show S01 2160p'. "
	"Left in their folder: 'Show S03 2160p' (downloads), 'Show S04 2160p' (downloads). "
	"Stopped half moved, part in their folder and part in video/library: 'Show S02 2160p' (downloads). "
	"Moving them again finishes the move")
for mode in ("data", "api"):
	dsm, client, ids = seasons(fail_mode=mode)
	dsm.free["video"] = 200 * GB  # Room for one season and a half
	e = raised(client.move_torrents, ids, "video/library")
	label = f"(failure reported as {'status data' if mode == 'data' else 'error 1001'})"
	check(f"a move that stopped half way is a failure the user is told about {label}", type(e), PermanentTorrentError)
	check(f"saying why and where the data of each task is {label}", str(e), NO_ROOM)
	s01, s02, s03, s04 = ids
	check(f"the season that made it seeds from the destination {label}",
		(dsm.where(s01), dsm.status(s01), dsm.size_under("/video/library/Show S01 2160p")), ("video/library", "seeding", 120 * GB))
	check(f"the ones File Station did not get to are back where they were, seeding {label}",
		[(dsm.where(i), dsm.status(i), dsm.size_under(f"/downloads/{dsm.tasks[i]['title']}")) for i in (s03, s04)],
		[("downloads", "seeding", 120 * GB)] * 2)
	check(f"the half moved one stays stopped, not broken {label}",
		(dsm.status(s02), dsm.size_under("/downloads/Show S02 2160p"), dsm.size_under("/video/library/Show S02 2160p"), dsm.broken),
		("paused", 60 * GB, 60 * GB, []))
	e = raised(client.resume_torrents, [s02])
	check(f"the user cannot start the half moved one {label}", (type(e), str(e), dsm.status(s02), dsm.broken),
		(PermanentTorrentError, "Error resuming torrents: the move of 'Show S02 2160p' to video/library stopped half way: "
			"order the same move again to finish it", "paused", []))
	e = raised(client.move_torrents, ids, "video/library")
	check(f"asking again does not take it for moved: File Station is asked for what is left, which stops the same way {label}",
		(type(e), str(e), len(dsm.jobs), dsm.status(s02)),
		(PermanentTorrentError, "Error moving torrents: File Station could not move the data to video/library: No space left on "
			"device (/downloads/Show S02 2160p/E03.mkv). Stopped half moved, part in their folder and part in video/library: "
			"'Show S02 2160p' (downloads). Moving them again finishes the move", 2, "paused"))
	check(f"what is left goes over the partial copies, and nothing else {label}",
		{k: dsm.made(COPYMOVE, "start")[-1].params[k] for k in ("path", "overwrite")},
		{"path": '["/downloads/Show S02 2160p"]', "overwrite": "true"})

# Once there is room, ordering the move again finishes it
dsm.free["video"] = 2000 * GB
check("once there is room, ordering the move again finishes it", raised(client.move_torrents, [s02], "video/library"), None)
check("with all of the season at the destination, seeding",
	(dsm.size_under("/video/library/Show S02 2160p"), "/downloads/Show S02 2160p" in dsm.fs, dsm.status(s02), dsm.broken),
	(120 * GB, False, "seeding", []))
check("and nothing left pending", (client._moves, saved_moves(dsm)), ({}, None))

# Once the user moves the rest by hand, the next order finds it moved and starts it again
dsm, client, ids = seasons()
dsm.free["video"] = 200 * GB
raised(client.move_torrents, ids, "video/library")
s02 = ids[1]
dsm.move_tree("/downloads/Show S02 2160p", "/video/library")
check("the half moved task is done once its data is all at the destination", raised(client.move_torrents, [s02], "video/library"), None)
check("without File Station moving anything", len(dsm.jobs), 1)
check("and it seeds again", (dsm.status(s02), dsm.broken, client._moves), ("seeding", [], {}))

# An order to another folder puts the data of the half moved task together first, then moves it on
dsm, client, ids = seasons()
dsm.free["video"] = 200 * GB
dsm.mkdir("/video/other")
raised(client.move_torrents, ids, "video/library")
dsm.free["video"] = 2000 * GB
s02 = ids[1]
check("a half moved task can be ordered somewhere else", raised(client.move_torrents, [s02], "video/other"), None)
check("where all its data ends up, seeding",
	(dsm.where(s02), dsm.size_under("/video/other/Show S02 2160p"), dsm.size_under("/video/library/Show S02 2160p"),
		dsm.size_under("/downloads/Show S02 2160p"), dsm.status(s02), dsm.broken),
	("video/other", 120 * GB, 0, 0, "seeding", []))

# Deleting a half moved task with its data deletes both halves
dsm, client, ids = seasons()
dsm.free["video"] = 200 * GB
raised(client.move_torrents, ids, "video/library")
raised(client.remove_torrents, [ids[1]], delete_data=True)
check("deleting a half moved task with its data deletes both halves",
	sorted(dsm.deleted), ["/downloads/Show S02 2160p", "/video/library/Show S02 2160p"])
check("and forgets its move", (client._moves, saved_moves(dsm)), ({}, None))
check("the others keep their data", (dsm.size_under("/video/library/Show S01 2160p"), dsm.size_under("/downloads/Show S03 2160p")), (120 * GB, 120 * GB))

# Several files fail
dsm = nas(rate=115 * MB)
show = dsm.add_task("Show S05", files=episodes(3, 10 * GB))
client = connect(dsm)
dsm.io_errors = {"/downloads/Show S05/E01.mkv": 417, "/downloads/Show S05/E03.mkv": 417}
e = raised(client.move_torrents, [show], "video/library")
check("several files that fail are summed up", (type(e), str(e)),
	(PermanentTorrentError, "Error moving torrents: File Station could not move the data to video/library: Input/output error "
		"(/downloads/Show S05/E01.mkv) and 1 more. Stopped half moved, part in their folder and part in video/library: "
		"'Show S05' (downloads). Moving them again finishes the move"))
dsm.io_errors = {}
check("and moving them again once the disk is fine finishes the move", raised(client.move_torrents, [show], "video/library"), None)
check("with every file at the destination", (dsm.files_under("/video/library/Show S05"), dsm.status(show), dsm.broken),
	([f"/video/library/Show S05/E0{n}.mkv" for n in (1, 2, 3)], "seeding", []))

# File Station refuses to start the move
dsm = nas(rate=115 * MB)
film = dsm.add_task("Film A", files=[("a.mkv", GB)])
client = connect(dsm)
dsm.refuse_start = 1002
e = raised(client.move_torrents, [film], "video/library")
check("a move File Station refuses to start is a failure the user is told about", (type(e), str(e)),
	(PermanentTorrentError, "Error moving torrents: File Station could not move the data to video/library: An error occurred "
		"at the destination. Left in their folder: 'Film A' (downloads)"))
check("with the task back where it was, seeding, and nothing pending",
	(dsm.where(film), dsm.status(film), dsm.broken, client._moves), ("downloads", "seeding", [], {}))

# A task whose data File Station did not get to, which cannot be pointed back either
dsm = nas(rate=115 * MB)
shows = [dsm.add_task(f"Show S0{n}", files=episodes(2, 30 * GB)) for n in (1, 2)]
client = connect(dsm)
dsm.free["video"] = 70 * GB  # Room for the first season alone
dsm.drop[(TASK, "edit")] = {2}  # 1: both to the destination, 2: the second one back
e = raised(client.move_torrents, shows, "video/library")
check("a task left pointing at the destination with its data in its folder is told apart", (type(e), str(e)),
	(PermanentTorrentError, "Error moving torrents: File Station could not move the data to video/library: No space left on "
		"device (/downloads/Show S02/E01.mkv). Moved: 'Show S01'. Stopped, with the data in their folder but pointing at "
		"video/library: 'Show S02' (downloads). Moving them again finishes the move"))
check("and stays stopped, where nothing breaks it",
	[(dsm.where(i), dsm.status(i)) for i in shows] + [dsm.size_under("/downloads/Show S02"), dsm.broken],
	[("video/library", "seeding"), ("video/library", "paused"), 60 * GB, []])
dsm.free["video"] = 2000 * GB
check("once there is room, moving it again moves its data to where it points", raised(client.move_torrents, [shows[1]], "video/library"), None)
check("and it seeds from there", (dsm.size_under("/video/library/Show S02"), dsm.status(shows[1]), dsm.broken), (60 * GB, "seeding", []))

# ...or it was pointed back, but the answer saying so was lost
dsm = nas(rate=115 * MB)
shows = [dsm.add_task(f"Show S0{n}", files=episodes(2, 30 * GB)) for n in (1, 2)]
client = connect(dsm)
dsm.free["video"] = 70 * GB
dsm.lose[(TASK, "edit")] = {2}
e = raised(client.move_torrents, shows, "video/library")
check("a task whose way back went unanswered is taken to point at the destination still",
	("Stopped, with the data in their folder but pointing at video/library: 'Show S02' (downloads)" in str(e),
		dsm.where(shows[1]), dsm.status(shows[1])), (True, "downloads", "paused"))
dsm.free["video"] = 2000 * GB
dsm.ignore_edits = {shows[1]}  # And for now Download Station does not take another folder for it
e = raised(client.move_torrents, [shows[1]], "video/library")
check("its data moved there, a task that does not take the destination is not started where its data is not",
	(type(e), str(e), dsm.where(shows[1]), dsm.status(shows[1]), dsm.size_under("/video/library/Show S02"), dsm.broken),
	(PermanentTorrentError, "Error moving torrents: Download Station does not take video/library as the folder of the data "
		"moved there. Stopped, with the data in video/library but pointing elsewhere: 'Show S02'. Moving them again finishes "
		"the move", "downloads", "paused", 60 * GB, []))
dsm.ignore_edits = set()
check("moving it again points it there", raised(client.move_torrents, [shows[1]], "video/library"), None)
check("and only then starts it", (dsm.where(shows[1]), dsm.status(shows[1]), dsm.broken, client._moves), ("video/library", "seeding", [], {}))

dsm = nas(rate=115 * MB)
shows = [dsm.add_task(f"Show S0{n}", files=episodes(2, 30 * GB)) for n in (1, 2)]
client = connect(dsm)
dsm.free["video"] = 70 * GB
dsm.lose[(TASK, "edit")] = {2}
raised(client.move_torrents, shows, "video/library")
dsm.free["video"] = 2000 * GB
dsm.ignore_edits = {shows[1]}
raised(client.move_torrents, [shows[1]], "video/library")
check("deleting with its data a task left pointing away from its data deletes the data where it is",
	(raised(client.remove_torrents, [shows[1]], delete_data=True), dsm.deleted, shows[1] in dsm.tasks, client._moves),
	(None, ["/video/library/Show S02"], False, {}))


def films(**options):
	"""Two films of 30 GB to another shared folder at 115 MB/s: under nine minutes"""
	dsm = nas(rate=115 * MB, **options)
	ids = [dsm.add_task(f"Film {n} (2020)", files=[(f"Film {n}.mkv", 30 * GB)]) for n in (1, 2)]
	return dsm, connect(dsm), ids


def seeding_there(dsm, ids):
	return ({dsm.where(i) for i in ids}, {dsm.status(i) for i in ids}, dsm.size_under("/video/library"), dsm.size_under("/downloads"), dsm.broken)


SEEDING_THERE = ({"video/library"}, {"seeding"}, 60 * GB, 0, [])

# One status poll goes unanswered
dsm, client, ids = films()
dsm.drop[(COPYMOVE, "status")] = {10}
e = raised(client.move_torrents, ids, "video/library")
check("a lost poll is something to try again", (type(e), "CopyMove.status" in str(e)), (TorrentClientError, True))
check("while the tasks stay stopped", ({dsm.status(i) for i in ids}, dsm.broken), ({"paused"}, []))
dsm.clock.sleep(60)
check("the retry follows the same job to its end", (raised(client.move_torrents, ids, "video/library"), len(dsm.jobs)), (None, 1))
check("and then starts the tasks", seeding_there(dsm, ids), SEEDING_THERE)

# The answer to the start of the move is lost, but File Station started it
dsm, client, ids = films()
dsm.lose[(COPYMOVE, "start")] = {1}
e = raised(client.move_torrents, ids, "video/library")
check("a move whose start went unanswered is something to try again", type(e), TorrentClientError)
check("with the tasks stopped", {dsm.status(i) for i in ids}, {"paused"})
dsm.clock.sleep(60)
check("the retry follows the data on disk to the end, without starting another move",
	(raised(client.move_torrents, ids, "video/library"), len(dsm.jobs), dsm.counts[(COPYMOVE, "start")]), (None, 1, 1))
check("and the tasks seed from there", seeding_there(dsm, ids), SEEDING_THERE)

# The start of the move never reached File Station
dsm, client, ids = films()
dsm.drop[(COPYMOVE, "start")] = {1}
e = raised(client.move_torrents, ids, "video/library")
check("a start that never got there is something to try again", (type(e), {dsm.status(i) for i in ids}), (TorrentClientError, {"paused"}))
dsm.clock.sleep(60)
before = dsm.clock.now
check("the retry starts the move once the data has stayed still for a while",
	(raised(client.move_torrents, ids, "video/library"), len(dsm.jobs), dsm.counts[(COPYMOVE, "start")]), (None, 1, 2))
check("a while that is not cut short", dsm.jobs.get("FileStation_0001", {}).get("start", before) - before >= sc.WATCH_STILL, True)
check("and the tasks seed from there", seeding_there(dsm, ids), SEEDING_THERE)

# The answer to the start is lost, and the move stops for lack of room
dsm, client, ids = films()
dsm.lose[(COPYMOVE, "start")] = {1}
dsm.free["video"] = 40 * GB  # One film fits
raised(client.move_torrents, ids, "video/library")
dsm.clock.sleep(60)
e = raised(client.move_torrents, ids, "video/library")
check("what a job nobody could ask about left untouched is moved again, which tells why it fails", (type(e), str(e)),
	(PermanentTorrentError, "Error moving torrents: File Station could not move the data to video/library: No space left on "
		"device (/downloads/Film 2 (2020)/Film 2.mkv). Moved: 'Film 1 (2020)'. Left in their folder: 'Film 2 (2020)' (downloads)"))
check("with each film seeding where its data is", ([(dsm.where(i), dsm.status(i)) for i in ids], dsm.broken, client._moves),
	([("video/library", "seeding"), ("downloads", "seeding")], [], {}))

# File Station forgets a job that ended well: the disk tells
dsm, client, ids = films()
dsm.forget_finished_jobs = True
check("a job File Station forgot once done is checked on disk", raised(client.move_torrents, ids, "video/library"), None)
check("and found moved", seeding_there(dsm, ids), SEEDING_THERE)

# File Station answers that it does not know a job that is still moving the data
dsm, client, ids = films()
dsm.hide_jobs = True
check("a job File Station does not own up to is followed on disk to its end", raised(client.move_torrents, ids, "video/library"), None)
check("without starting another, and the films seed from the destination", (len(dsm.jobs), seeding_there(dsm, ids)), (1, SEEDING_THERE))

dsm = nas(rate=115 * MB)
remux = dsm.add_task("Film.2160p.Remux", files=[("Film.2160p.Remux.mkv", 120 * GB)])  # Longer than WATCH_STILL to copy
client = connect(dsm)
dsm.hide_jobs = True
check("a file whose copy outlasts the wait for stillness is not taken for stopped while it grows",
	raised(client.move_torrents, [remux], "video/library"), None)
check("and the film seeds whole from the destination",
	(dsm.fs.get("/video/library/Film.2160p.Remux/Film.2160p.Remux.mkv"), dsm.status(remux), dsm.broken, len(dsm.jobs)),
	(120 * GB, "seeding", [], 1))

# The NAS restarts half way: File Station forgets the job with the data in two folders
STOPPED = ("Error moving torrents: File Station could not move the data to video/library: the move stopped half way (the NAS "
	"may have restarted). Left in their folder: 'Film 2 (2020)' (downloads). Stopped half moved, part in their folder and "
	"part in video/library: 'Film 1 (2020)' (downloads). Moving them again finishes the move")
dsm, client, ids = films()
dsm.reboot_at = 200
e = raised(client.move_torrents, ids, "video/library")
check("a job lost half way ends, once the data stays still, as a failure the user is told about", (type(e), str(e)),
	(PermanentTorrentError, STOPPED))
check("after watching the disk for a while", dsm.clock.now >= 200 + sc.WATCH_STILL, True)
film1, film2 = ids
check("the half moved film stays stopped, the one File Station did not get to seeds where it was",
	([(dsm.where(i), dsm.status(i)) for i in ids], dsm.broken), ([("video/library", "paused"), ("downloads", "seeding")], []))
check("ordering it again moves what is left over its partial copy", raised(client.move_torrents, [film1], "video/library"), None)
start = dsm.made(COPYMOVE, "start")[-1].params
check("File Station is asked for that alone", (start["path"], start["overwrite"], len(dsm.jobs)), ('["/downloads/Film 1 (2020)"]', "true", 2))
check("and the film seeds whole from the destination",
	(dsm.fs.get("/video/library/Film 1 (2020)/Film 1.mkv"), "/downloads/Film 1 (2020)" in dsm.fs, dsm.status(film1), dsm.broken),
	(30 * GB, False, "seeding", []))

dsm, client, ids = films()
dsm.reboot_at = 200
raised(client.move_torrents, ids, "video/library")
raised(client.remove_torrents, [ids[0]], delete_data=True)
check("deleting a film left half way by a restart of the NAS deletes both halves",
	sorted(dsm.deleted), ["/downloads/Film 1 (2020)", "/video/library/Film 1 (2020)"])

# Download Station takes the new folder, but its answer is lost: the tasks go back
dsm, client, ids = films()
dsm.lose[(TASK, "edit")] = {1}
e = raised(client.move_torrents, ids, "video/library")
check("a destination change that went unanswered is something to try again", type(e), TorrentClientError)
check("after pointing the tasks back to where their data is, and starting them",
	({dsm.where(i) for i in ids}, {dsm.status(i) for i in ids}, dsm.made(COPYMOVE, "start"), dsm.broken, client._moves),
	({"downloads"}, {"seeding"}, [], [], {}))
check("so the retry moves them for real", (raised(client.move_torrents, ids, "video/library"), dsm.size_under("/video/library")), (None, 60 * GB))

# ...and pointing them back fails too: they stay stopped, and the retry carries on with the move
dsm, client, ids = films()
dsm.lose[(TASK, "edit")] = {1}
dsm.drop[(TASK, "edit")] = {2}
e = raised(client.move_torrents, ids, "video/library")
check("when they cannot be pointed back either they stay stopped",
	(type(e), {dsm.where(i) for i in ids}, {dsm.status(i) for i in ids}, dsm.broken), (TorrentClientError, {"video/library"}, {"paused"}, []))
dsm.clock.sleep(60)
check("and the retry does not take them for moved: it moves their data", raised(client.move_torrents, ids, "video/library"), None)
check("which ends at the destination, seeding", seeding_there(dsm, ids), SEEDING_THERE)

dsm, client, ids = films()
dsm.lose[(TASK, "edit")] = {1}
dsm.drop[(TASK, "edit")] = {2}
raised(client.move_torrents, ids, "video/library")
raised(client.remove_torrents, [ids[0]], delete_data=True)
check("deleting one of them with its data deletes it from the folder it never left", dsm.deleted, ["/downloads/Film 1 (2020)"])


# ---------------------------------------------------------------------------
# S2: A REFUSAL LEAVES EVERYTHING AS IT WAS
# ---------------------------------------------------------------------------

for code in (0, 407):
	dsm = nas()
	dsm.refuse_code = code
	bt = dsm.add_task("Ubuntu.Pack", files=[("ubuntu-1.iso", 100), ("ubuntu-2.iso", 100)])
	web = dsm.add_task("debian.iso", kind="http", files=[("debian.iso", 50)], status="finished", listed=False)
	client = connect(dsm)
	e = raised(client.move_torrents, [bt, web], "video/library")
	label = "silently" if not code else f"with error {code}"
	why = "it considers that download over" if not code else "Setting the destination failed"
	check(f"a finished download refusing the new folder ({label}) stops the whole move", (type(e), str(e)),
		(PermanentTorrentError, f"Error moving torrents: Download Station refuses to change the destination of "
			f"'debian.iso': {why}. The data was left where it was"))
	check(f"the torrent that took the change is pointed back ({label})", dsm.where(bt), "downloads")
	check(f"its data never moved ({label})", (dsm.made(COPYMOVE, "start"), dsm.size_under("/downloads/Ubuntu.Pack")), ([], 200))
	check(f"and it seeds again, intact ({label})", (dsm.status(bt), dsm.broken), ("seeding", []))
	check(f"the download that refused is as it was ({label})", (dsm.where(web), dsm.status(web)), ("downloads", "finished"))
	check(f"nothing is left pending ({label})", client._moves, {})
	check(f"so the torrent alone can be moved afterwards ({label})", raised(client.move_torrents, [bt], "video/library"), None)
	check(f"and it is ({label})", (dsm.where(bt), dsm.size_under("/video/library/Ubuntu.Pack"), dsm.status(bt)), ("video/library", 200, "seeding"))

# Pointing the torrent back fails too
dsm = nas()
bt = dsm.add_task("Ubuntu.Pack", files=[("ubuntu-1.iso", 100), ("ubuntu-2.iso", 100)])
web = dsm.add_task("debian.iso", kind="http", files=[("debian.iso", 50)], status="finished", listed=False)
client = connect(dsm)
dsm.drop[(TASK, "edit")] = {3}  # 1: both, 2: the one that refused, again, 3: the torrent back
e = raised(client.move_torrents, [bt, web], "video/library")
check("a torrent that cannot be pointed back is named, and only it", (type(e), str(e)),
	(PermanentTorrentError, "Error moving torrents: Download Station refuses to change the destination of 'debian.iso': "
		"it considers that download over. The data was left where it was, but 'Ubuntu.Pack' could not be pointed back to "
		"its folder and was left stopped"))
check("it stays stopped and in its move, while the download that refused is left as it was",
	(dsm.where(bt), dsm.status(bt), sorted(client._moves), dsm.where(web), dsm.status(web), dsm.broken),
	("video/library", "paused", [bt], "downloads", "finished", []))
check("so moving the torrent alone works at once", raised(client.move_torrents, [bt], "video/library"), None)
check("with its data", (dsm.size_under("/video/library/Ubuntu.Pack"), dsm.status(bt), dsm.broken, client._moves),
	(200, "seeding", [], {}))

dsm = nas()
pack_a = dsm.add_task("Pack.A", files=[("a-1.iso", 100), ("a-2.iso", 100)])
pack_b = dsm.add_task("Pack.B", files=[("b-1.iso", 100), ("b-2.iso", 100)])
web = dsm.add_task("debian.iso", kind="http", files=[("debian.iso", 50)], status="finished", listed=False)
client = connect(dsm)
dsm.drop[(TASK, "edit")] = {3}
e = raised(client.move_torrents, [pack_a, pack_b, web], "video/library")
check("several of them are named together", str(e).endswith(
	". The data was left where it was, but 'Pack.A', 'Pack.B' could not be pointed back to their folders and were left stopped"), True)
raised(client.remove_torrents, [pack_a], delete_data=True)
check("deleting one of them with its data deletes it from the folder its data never left", dsm.deleted, ["/downloads/Pack.A"])
check("and the other one stays in the move", sorted(client._moves), [pack_b])


# ---------------------------------------------------------------------------
# S3: ONLY WHAT THE FILE LIST OF A TASK SHOWS IS ITS CONTENT
# ---------------------------------------------------------------------------

def movie_pair():
	"""A folder torrent holding a single file, next to a torrent of that very
	file: Download Station lists both files as Movie.2024.mkv"""
	dsm = nas()
	folder = dsm.add_task("Movie.2024", files=[("Movie.2024.mkv", 500)])
	single = dsm.add_task("Movie.2024.mkv", files=[("Movie.2024.mkv", 400)])
	return dsm, connect(dsm), folder, single


dsm, client, folder, single = movie_pair()
check("the content of the folder torrent is its folder",
	client._content_root(client.get_torrent(folder)), ("/downloads/Movie.2024", ["/downloads/Movie.2024/Movie.2024.mkv"]))
check("the content of the single file torrent is the file", client._content_root(client.get_torrent(single)),
	("/downloads/Movie.2024.mkv", ["/downloads/Movie.2024.mkv"]))
raised(client.remove_torrents, [folder], delete_data=True)
check("deleting the folder torrent with its data deletes its folder only", dsm.deleted, ["/downloads/Movie.2024"])
check("the other torrent keeps its file", ("/downloads/Movie.2024.mkv" in dsm.fs, single in dsm.tasks), (True, True))
raised(client.remove_torrents, [single], delete_data=True)
check("which goes with its own torrent", dsm.deleted, ["/downloads/Movie.2024", "/downloads/Movie.2024.mkv"])

dsm, client, folder, single = movie_pair()
raised(client.move_torrents, [folder], "video/library")
check("moving the folder torrent moves its folder", dsm.made(COPYMOVE, "start")[0].params["path"], '["/downloads/Movie.2024"]')
check("and leaves the other file where it is",
	("/video/library/Movie.2024/Movie.2024.mkv" in dsm.fs, dsm.fs.get("/downloads/Movie.2024.mkv"), dsm.where(single)),
	(True, 400, "downloads"))

dsm = nas()
dsm.put("/downloads/Breaking Bad/S01E01.mkv", 700)  # Someone's library, not the magnet's
magnet_task = dsm.add_task("Breaking Bad", status="downloading", on_disk=False,
	uri="magnet:?xt=urn:btih:" + "ef" * 20 + "&dn=Breaking+Bad")
client = connect(dsm)
check("a magnet without metadata has no content of its own", client._content_root(client.get_torrent(magnet_task)), None)
raised(client.move_torrents, [magnet_task], "video/library")
check("moving it moves nothing on disk", (dsm.made(COPYMOVE, "start"), dsm.where(magnet_task)), ([], "video/library"))
raised(client.move_torrents, [magnet_task], "downloads")
raised(client.remove_torrents, [magnet_task], delete_data=True)
check("deleting it with its data deletes nothing", (dsm.made(DELETE, "delete"), "/downloads/Breaking Bad/S01E01.mkv" in dsm.fs), ([], True))

dsm = nas()
dsm.put("/downloads/Breaking Bad/Season 1/S01E01.mkv", 700)
waiting = dsm.add_task("Breaking Bad", files=[("S01E01.mkv", 700, 0), ("S01E02.mkv", 700, 0)], status="downloading", on_disk=False)
client = connect(dsm)
raised(client.remove_torrents, [waiting], delete_data=True)
check("a folder of the same name without the files of the task is not its content",
	(dsm.made(DELETE, "delete"), "/downloads/Breaking Bad/Season 1/S01E01.mkv" in dsm.fs), ([], True))

dsm = nas()
partial = dsm.add_task("Show.S03", files=[("E01.mkv", 100, 0), ("E02.mkv", 100, 40)], status="downloading")
rooted = dsm.add_task("Show.S04", files=[("E01.mkv", 100)], with_root=True)
client = connect(dsm)
check("the files with data are the ones looked for", client._content_root(client.get_torrent(partial))[1],
	["/downloads/Show.S03/E02.mkv", "/downloads/Show.S03/E01.mkv"])
check("a list that names the root folder is understood too", client._content_root(client.get_torrent(rooted)),
	("/downloads/Show.S04", ["/downloads/Show.S04/Show.S04/E01.mkv", "/downloads/Show.S04/E01.mkv"]))
raised(client.remove_torrents, [partial, rooted], delete_data=True)
check("and both are deleted", sorted(dsm.deleted), ["/downloads/Show.S03", "/downloads/Show.S04"])

# A torrent of one file that Download Station titles otherwise: no folder of
# that title holds it, so the file itself is its content, when nothing says
# that file is someone else's
dsm = nas()
movie = dsm.add_task("Some Movie 2024", files=[("Some.Movie.2024.1080p.mkv", 100)], single_file=True)
client = connect(dsm)
check("a torrent of one file titled otherwise is found by its file",
	client._content_on_disk([client.get_torrent(movie)]), {movie: "/downloads/Some.Movie.2024.1080p.mkv"})
raised(client.move_torrents, [movie], "video/library")
check("and moved", (dsm.where(movie), dsm.status(movie), sorted(p for p in dsm.fs if "Some" in p), dsm.broken),
	("video/library", "seeding", ["/video/library/Some.Movie.2024.1080p.mkv"], []))
raised(client.remove_torrents, [movie], delete_data=True)
check("and deleted with its data", dsm.deleted, ["/video/library/Some.Movie.2024.1080p.mkv"])

dsm = nas()
owner = dsm.add_task("X.mkv", files=[("X.mkv", 100)])  # The torrent of that very file
other = dsm.add_task("Some Movie", files=[("X.mkv", 100)], single_file=True, on_disk=False)  # Its own data is gone
client = connect(dsm)
e = raised(client.move_torrents, [other], "video/library")
check("a file another task of the folder goes by is not taken for its data", (type(e), str(e)),
	(PermanentTorrentError, "Error moving torrents: Cannot find on disk the data of 'Some Movie' (downloads): nothing was moved"))
check("so the finished download is not pointed elsewhere without it, and nothing is touched",
	(dsm.fs.get("/downloads/X.mkv"), dsm.where(other), dsm.status(other), dsm.made(TASK, "pause"), dsm.made(TASK, "edit")),
	(100, "downloads", "seeding", [], []))
raised(client.remove_torrents, [other], delete_data=True)
check("nor is that file deleted with it", ("/downloads/X.mkv" in dsm.fs, dsm.deleted, other in dsm.tasks), (True, [], False))

dsm = nas()
dsm.put("/downloads/Y.mkv", 50)  # Someone's file of that name
other = dsm.add_task("Some Film", files=[("Y.mkv", 100)], single_file=True, on_disk=False)
client = connect(dsm)
e = raised(client.move_torrents, [other], "video/library")
check("nor a file of that name and another size", (type(e), dsm.fs.get("/downloads/Y.mkv"), dsm.where(other)),
	(PermanentTorrentError, 50, "downloads"))

dsm = nas()
show = dsm.add_task("Show S01", files=[("E01.mkv", 100), ("E02.mkv", 100)], on_disk=False)
for name in ("E01.mkv", "E02.mkv"):
	dsm.put(f"/downloads/Show.S01.1080p/{name}", 100)  # The folder on disk goes by another name
client = connect(dsm)
e = raised(client.move_torrents, [show], "video/library")
check("a finished download whose data is not under its name is not moved", (type(e), str(e)),
	(PermanentTorrentError, "Error moving torrents: Cannot find on disk the data of 'Show S01' (downloads): nothing was moved"))
check("nor pointed elsewhere, where it would break",
	(dsm.where(show), dsm.status(show), dsm.made(TASK, "pause"), dsm.made(TASK, "edit"), dsm.broken, client._moves),
	("downloads", "seeding", [], [], [], {}))


# ---------------------------------------------------------------------------
# S4: A MAGNET ADD TAKES ITS OWN TASK
# ---------------------------------------------------------------------------

check("magnet keys: hex hash and name", sc._magnet_keys("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=La+Casa%20de%20Papel"),
	({"abcdef0123456789abcdef0123456789abcdef01"}, {"la casa de papel"}))
check("magnet keys: a base32 hash is the same hash", sc._magnet_keys("magnet:?xt=urn:btih:" + "VPG66AJDIVTYTK6N54ASGRLHRGV433YB"),
	({"abcdef0123456789abcdef0123456789abcdef01"}, set()))
check("magnet keys: a v2 torrent", sc._magnet_keys("magnet:?xt=urn:btmh:1220" + "ab" * 32), ({"1220" + "ab" * 32}, set()))
check("magnet keys: not a magnet", sc._magnet_keys("https://example.org/file.torrent"), (set(), set()))

ours = "magnet:?xt=urn:btih:" + "12" * 20 + "&dn=Our.Show.S01"


def someone_else_adds(dsm):
	def add():
		if not any(t["title"] == "Someone.Elses.Download" for t in dsm.tasks.values()):
			dsm.add_task("Someone.Elses.Download", files=[("x.iso", 10, 0)], status="downloading", on_disk=False,
				uri="magnet:?xt=urn:btih:" + "99" * 20 + "&dn=Someone.Elses.Download")
	dsm.on_create = add


for variant in ("link", "hash", "name"):
	dsm = nas()
	someone_else_adds(dsm)
	dsm.normalize_uri = variant == "hash"
	dsm.report_uri = variant != "name"
	client = connect(dsm)
	info = client.add_torrent(magnet=ours)
	check(f"a magnet add takes its own task, not the one someone else added first (by its {variant})",
		(info.name, info.id == next(i for i, t in dsm.tasks.items() if t["title"] == "Our.Show.S01")), ("Our.Show.S01", True))

dsm = nas()
someone_else_adds(dsm)
base32 = "magnet:?xt=urn:btih:" + "VPG66AJDIVTYTK6N54ASGRLHRGV433YB"
dsm.magnets[base32] = {"hash": "abcdef0123456789abcdef0123456789abcdef01"}
dsm.normalize_uri = True
client = connect(dsm)
info = client.add_torrent(magnet=base32)
check("a base32 magnet is found by its hash when Download Station rewrites it in hex", info.name, "abcdef0123456789abcdef0123456789abcdef01")

dsm = nas()
someone_else_adds(dsm)
dsm.magnets[ours] = {"title": "Our.Show.S01.1080p", "files": [("E01.mkv", 500), ("E02.mkv", 500)], "after": 3}
client = connect(dsm)
info = client.add_torrent(magnet=ours)
check("and waits for its metadata", (info.name, info.total_size, len(info.files)), ("Our.Show.S01.1080p", 1000, 2))

dsm = nas()
client = connect(dsm)
first = client.add_torrent(magnet=magnet)
dsm.drop[(TASK, "list")] = {dsm.counts[(TASK, "list")] + 1}  # The list from before the add cannot be read
e = raised(client.add_torrent, magnet=magnet)
check("a magnet added twice while the list from before cannot be read is still taken for a duplicate",
	(type(e), str(e), sorted(dsm.tasks)),
	(DuplicateTorrentError, "Error adding torrent: The torrent is already in Download Station: Night of the Living Dead", [first.id]))
check("of the task that was there, not of the one the failed add made", getattr(e, "torrent_id", None), first.id)

dsm = nas()
dsm.report_uri = False  # Found by title alone
client = connect(dsm)
first = client.add_torrent(magnet=magnet)
e = raised(client.add_torrent, magnet=magnet)
check("a duplicate magnet is found by the name in the link when the link of the task is not reported",
	(type(e), getattr(e, "torrent_id", None), sorted(dsm.tasks)), (DuplicateTorrentError, first.id, [first.id]))

dsm = nas()
dsm.normalize_uri = True  # Download Station rewrites the link: only the hash tells
renamed = "magnet:?xt=urn:btih:" + "cd" * 20 + "&dn=Another+Name"
client = connect(dsm)
first = client.add_torrent(magnet=magnet)
e = raised(client.add_torrent, magnet=renamed)
check("a duplicate magnet of another name is found by its info hash",
	(type(e), getattr(e, "torrent_id", None)), (DuplicateTorrentError, first.id))

dsm = nas()
dsm.report_uri = False
hidden = dsm.add_task("Titled.Otherwise", files=[("x.mkv", 10)], hash="ef" * 20)
client = connect(dsm)
e = raised(client.add_torrent, magnet="magnet:?xt=urn:btih:" + "ef" * 20)
check("a duplicate whose task cannot be told is still a duplicate, of no known task",
	(type(e), str(e), getattr(e, "torrent_id", "missing"), sorted(dsm.tasks)),
	(DuplicateTorrentError, "Error adding torrent: The torrent is already in Download Station", None, [hidden]))


# ---------------------------------------------------------------------------
# S5: FILES OF A TASK, BY INDEX OR BY POSITION
# ---------------------------------------------------------------------------

def bt_set():
	return dsm.made(BT_FILE, "set")[-1].params


dsm = nas()
pack = dsm.add_task("Pack", files=[("a.mkv", 10), ("b.mkv", 10), ("c.mkv", 10)])
client = connect(dsm)
client.set_file_priority(pack, ["b.mkv", "c.mkv"], FILE_SKIP)
check("the list of files is asked to DownloadStation2", dsm.made(BT_FILE, "list")[-1].params["task_id"], json.dumps(pack))
check("files left out: by index, not wanted, no priority",
	{k: bt_set().get(k) for k in ("task_id", "index", "wanted", "priority")},
	{"task_id": json.dumps(pack), "index": "[1, 2]", "wanted": "false", "priority": None})
check("and the detail shows it", client.get_torrent(pack).file_priorities, {"a.mkv": FILE_NORMAL, "b.mkv": FILE_SKIP, "c.mkv": FILE_SKIP})
client.set_file_priority(pack, ["c.mkv"], FILE_HIGH)
check("a priority makes the file wanted again", {k: bt_set()[k] for k in ("index", "wanted", "priority")},
	{"index": "[2]", "wanted": "true", "priority": '"high"'})
check("which the detail shows", client.get_torrent(pack).file_priorities["c.mkv"], FILE_HIGH)

dsm.bt_reverse = True
client.set_file_priority(pack, ["a.mkv"], FILE_LOW)
check("items that carry an index are known by it, whatever their order", bt_set()["index"], "[0]")
dsm.bt_reverse = False
dsm.bt_no_index = True
client.set_file_priority(pack, ["b.mkv", "c.mkv"], FILE_LOW)
check("items without an index are known by their place in the list, not all as the first one", bt_set()["index"], "[1, 2]")
check("so the right files change", client.get_torrent(pack).file_priorities, {"a.mkv": FILE_LOW, "b.mkv": FILE_LOW, "c.mkv": FILE_LOW})
e = raised(client.set_file_priority, pack, ["nope.mkv"], FILE_SKIP)
check("files the task does not have are refused for good", (type(e), str(e)), (PermanentTorrentError, f"No such files in task {pack}"))
e = raised(client.set_file_priority, "dbid_404", ["a.mkv"], FILE_SKIP)
check("so is a task that is not there", (type(e), str(e)), (PermanentTorrentError, "Task not found: dbid_404"))


# ---------------------------------------------------------------------------
# A MOVE THAT HAS NOT ENDED OUTLIVES A RESTART OF THE BOT
# ---------------------------------------------------------------------------

dsm, client, ids = seasons()
raised(client.move_torrents, ids, "video/library")
saved = saved_moves(dsm) or {}
written = (saved.get("moves") or [{}])[0]
check("a move still going on is written down in the config folder of the bot",
	(saved.get("nas"), len(saved.get("moves") or []), written.get("stage"), [t["id"] for t in written.get("tasks", [])], written.get("job")),
	(BASE, 1, "wait", ids, "FileStation_0001"))
restarted = connect(dsm)
check("a restarted bot knows the move has not ended",
	(sorted(restarted._moves), getattr(restarted._moves.get(ids[0]), "stage", None)), (sorted(ids), "wait"))
e = raised(restarted.resume_torrents, ids)
check("so it does not let the tasks start before their data is there", (type(e), dsm.broken, {dsm.status(i) for i in ids}),
	(PermanentTorrentError, [], {"paused"}))
e = raised(restarted.remove_torrents, [ids[0]], delete_data=True)
check("nor deletes their data under File Station's feet", (type(e), dsm.deleted), (TorrentClientError, []))
check("nor takes them for moved when the move is ordered again: it waits for the same job",
	(raised(restarted.move_torrents, ids, "video/library"), len(dsm.jobs)), (None, 1))
check("which ends with the data at the destination and the tasks seeding",
	(dsm.size_under("/video/library"), dsm.size_under("/downloads"), {dsm.status(i) for i in ids}, dsm.broken),
	(480 * GB, 0, {"seeding"}, []))
check("and nothing left written down", (restarted._moves, saved_moves(dsm)), ({}, None))

# Restarted while File Station forgets the job (it went with the session of the bot, say)
dsm, client, ids = seasons()
raised(client.move_torrents, ids, "video/library")
dsm.hide_jobs = True
restarted = connect(dsm)
check("the move is followed on disk to its end", raised(restarted.move_torrents, ids, "video/library"), None)
check("with every season seeding from the destination", (dsm.size_under("/video/library"), {dsm.status(i) for i in ids}, dsm.broken),
	(480 * GB, {"seeding"}, []))

# A move that stopped half way is remembered too
dsm, client, ids = seasons()
dsm.free["video"] = 200 * GB
raised(client.move_torrents, ids, "video/library")
restarted = connect(dsm)
check("after a restart, only the half moved task is pending", sorted(restarted._moves), [ids[1]])
e = raised(restarted.resume_torrents, [ids[1]])
check("and it is still kept from starting", (type(e), "stopped half way" in str(e), dsm.status(ids[1])), (PermanentTorrentError, True, "paused"))
dsm.free["video"] = 2000 * GB
check("until moving it again finishes the move", raised(restarted.move_torrents, [ids[1]], "video/library"), None)
check("whole at the destination, seeding", (dsm.size_under("/video/library/Show S02 2160p"), dsm.status(ids[1]), dsm.broken, saved_moves(dsm)),
	(120 * GB, "seeding", [], None))

dsm, client, ids = seasons()
dsm.free["video"] = 200 * GB
raised(client.move_torrents, ids, "video/library")
del dsm.tasks[ids[1]]  # Removed in DSM while the bot was down
restarted = connect(dsm)
check("tasks that are no longer in Download Station are not remembered", (restarted._moves, saved_moves(dsm)), ({}, None))

dsm, client, ids = seasons()
dsm.free["video"] = 200 * GB
raised(client.move_torrents, ids, "video/library")
dsm.tasks[ids[1]]["title"] = "Another.Download"  # Download Station reinstalled, handing out the same id to another task
restarted = connect(dsm)
check("nor is another task that has the same id now", (restarted._moves, saved_moves(dsm)), ({}, None))

dsm, client, ids = seasons()
dsm.free["video"] = 200 * GB
raised(client.move_torrents, ids, "video/library")
dsm.drop[(TASK, "list")] = {dsm.counts[(TASK, "list")] + 1}  # Download Station does not answer while the bot starts
restarted = connect(dsm)
check("a move taken back counts until Download Station can say whose tasks are still there", sorted(restarted._moves), [ids[1]])
e = raised(restarted.resume_torrents, [ids[1]])
check("so its task is kept from starting", (type(e), dsm.status(ids[1]), dsm.broken), (PermanentTorrentError, "paused", []))

# Restarted while File Station is still moving, which then finishes with nobody driving the move
dsm, client, ids = seasons()
dsm.tasks[ids[3]]["status"] = "paused"  # Stopped before the move: the move does not start it again
raised(client.move_torrents, ids, "video/library")
restarted = connect(dsm)
dsm.clock.sleep(3600)  # File Station ends the job meanwhile
dsm.advance_jobs()
check("File Station did end it", ([job["finished"] for job in dsm.jobs.values()], dsm.size_under("/downloads")), ([True], 0))
e = raised(restarted.resume_torrents, ids)
check("resuming then finishes the move instead of refusing", e, None)
check("every task points at the destination and seeds, none broken",
	({dsm.where(i) for i in ids}, {dsm.status(i) for i in ids}, dsm.broken), ({"video/library"}, {"seeding"}, []))
check("and the move is over, also on disk", (restarted._moves, saved_moves(dsm)), ({}, None))
check("without starting another File Station job", len(dsm.jobs), 1)

# The same while the bot kept running: its attempt gave up waiting, and nothing drives the move again
dsm, client, ids = seasons()
raised(client.move_torrents, ids, "video/library")
dsm.clock.sleep(3600)
check("a move whose attempt gave up is finished by resuming once File Station is done", raised(client.resume_torrents, ids[:2]), None)
check("its tasks seed from the destination", ({dsm.where(i) for i in ids}, {dsm.status(i) for i in ids}, dsm.broken),
	({"video/library"}, {"seeding"}, []))

dsm, client, ids = seasons()
raised(client.move_torrents, ids, "video/library")
saved = saved_moves(dsm) or {}
saved["nas"] = "http://other-nas:5000/webapi"
with open(os.path.join(dsm.config_dir, sc.MOVES_FILE), "w", encoding="utf-8") as file:
	json.dump(saved, file)
check("the moves of another NAS are not taken for this one's", connect(dsm)._moves, {})
with open(os.path.join(dsm.config_dir, sc.MOVES_FILE), "w", encoding="utf-8") as file:
	file.write("{ not json")
check("nor is a file that cannot be read a reason not to start", connect(dsm)._moves, {})


# ---------------------------------------------------------------------------
# OVER REAL HTTP: THE REQUESTS SESSION OF THE CLIENT AGAINST THE FAKE DSM
# ---------------------------------------------------------------------------

class DSMHandler(http.server.BaseHTTPRequestHandler):
	"""Serves the current fake DSM the way DSM speaks HTTP: parameters in the
	query string or in a form, uploads as multipart, and the session id in a
	cookie set at login"""

	def log_message(self, *args):
		pass

	def do_GET(self):
		url = urlparse(self.path)
		self.serve(url.path, {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()})

	def do_POST(self):
		body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
		kind = self.headers.get("Content-Type", "")
		if not kind.startswith("multipart/form-data"):
			self.serve(urlparse(self.path).path, {k: v[-1] for k, v in parse_qs(body.decode(), keep_blank_values=True).items()})
			return
		message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(f"Content-Type: {kind}\r\n\r\n".encode() + body)
		params, files = {}, {}
		for part in message.iter_parts():
			name = part.get_param("name", header="content-disposition")
			if part.get_filename():
				files[name] = (part.get_filename(), part.get_payload(decode=True), part.get_content_type())
			else:
				params[name] = part.get_payload(decode=True).decode()
		self.serve(urlparse(self.path).path, params, files)

	def serve(self, path, params, files=None):
		jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
		session = types.SimpleNamespace(cookies={name: morsel.value for name, morsel in jar.items()})
		before = dict(session.cookies)
		response = CURRENT["dsm"].handle(session, f"http://{HOST_PATH.split('/')[0]}{path}", params, files)
		page = response.payload is None
		body = b"<html>Not the DSM API</html>" if page else json.dumps(response.payload).encode()
		self.send_response(response.status_code)
		self.send_header("Content-Type", "text/html" if page else "application/json")
		for name, value in session.cookies.items():
			if before.get(name) != value:
				self.send_header("Set-Cookie", f"{name}={value}; path=/")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DSMHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
sc.requests = requests  # The real session this time
try:
	dsm = nas()
	sintel = b"d4:infod4:name9:Sintel.4Kee"
	dsm.torrents[sintel] = {"hash": "bb" * 20, "title": "Sintel.4K", "files": [("sintel.mkv", 800), ("sintel.srt", 2)]}
	tears = b"d4:infod4:name15:Tears.of.Steelee"
	dsm.torrents[tears] = {"hash": "cc" * 20, "title": "Tears.of.Steel", "files": [("tears.mkv", 600)]}
	show = dsm.add_task("Show.S07", files=[("E01.mkv", 100), ("E02.mkv", 100)])
	config.CONFIG_PATH = dsm.config_dir
	client = sc.SynologyClient("127.0.0.1", server.server_address[1], "bot", "secret")
	check("over HTTP: discovery and login", (client.sid, dsm.logins, client.test_connection()), ("sid-1", 1, "Download Station 4.0.1-4750"))
	check("over HTTP: listing", [t.name for t in client.get_torrents()], ["Show.S07"])
	info = client.add_torrent(torrent_data=sintel, download_dir="video/library")
	upload = dsm.made(TASK2, "create")[0]
	check("over HTTP: a .torrent goes up as multipart, known by the session cookie set at login",
		(info.name, info.total_size, dsm.logins, "_sid" in upload.params, upload.files["torrent"][:2]),
		("Sintel.4K", 802, 1, False, ("upload.torrent", sintel)))
	info = client.add_torrent(magnet=magnet)
	check("over HTTP: a magnet", (info.name, info.download_dir), ("Night of the Living Dead", "downloads"))
	raised(client.move_torrents, [show], "video/library")
	check("over HTTP: a move", (dsm.where(show), dsm.status(show), "/video/library/Show.S07/E01.mkv" in dsm.fs),
		("video/library", "seeding", True))
	client.set_file_priority(show, ["E02.mkv"], FILE_SKIP)
	check("over HTTP: file priorities", client.get_torrent(show).file_priorities, {"E01.mkv": FILE_NORMAL, "E02.mkv": FILE_SKIP})
	raised(client.remove_torrents, [show], delete_data=True)
	check("over HTTP: removing with the data", (show in dsm.tasks, dsm.deleted), (False, ["/video/library/Show.S07"]))
	dsm.sid = None  # The session expires on the NAS right before an upload
	info = client.add_torrent(torrent_data=tears)
	check("over HTTP: an upload after the session expired logs in again and goes up with the new cookie",
		(info.name, dsm.logins, len(dsm.made(TASK2, "create"))), ("Tears.of.Steel", 2, 2))
	check("over HTTP: everything is there", sorted(t.name for t in client.get_torrents()),
		["Night of the Living Dead", "Sintel.4K", "Tears.of.Steel"])
finally:
	sc.requests = types.SimpleNamespace(Session=lambda: FakeSession(CURRENT["dsm"]))
	server.shutdown()


# ---------------------------------------------------------------------------
# THE AUTOMATIC MOVER
# ---------------------------------------------------------------------------
# The whole bot against the NAS: Download Station tells its folders without
# the leading slash, its free space by shared folder, and its move only
# returns once File Station has the data in the new folder

import importlib.util
import time as real_time
import auto_mover
import torrent_clients

dsm = nas()
dsm.free = {"downloads": 35 * GB, "video": 2000 * GB}
old_a = dsm.add_task("Movie.A", destination="downloads/PELICULAS", files=[("a.mkv", 30 * GB)], completed_time=1700001000)
old_b = dsm.add_task("Show.B.S01", destination="downloads/SERIES/Show B", files=episodes(3, 10 * GB), completed_time=1700002000)
old_c = dsm.add_task("Movie.C", destination="downloads/PELICULAS", files=[("c.mkv", 30 * GB)], completed_time=1700003000)
left = dsm.add_task("Movie.D", destination="downloads/PELICULAS", files=[("d.mkv", 30 * GB)], completed_time=1700004000)
recent = dsm.add_task("Movie.New", destination="downloads/PELICULAS", files=[("n.mkv", 30 * GB)], completed_time=int(real_time.time()) - 3600)
elsewhere = dsm.add_task("Movie.Kept", destination="video/library", files=[("k.mkv", 30 * GB)], completed_time=1700000500)
mover_client = connect(dsm)
torrent_clients.create_client = lambda config_module: mover_client
_spec = importlib.util.spec_from_file_location("tcbot", os.path.join(os.path.dirname(os.path.abspath(__file__)), "torrent-controller-bot.py"))
bot = importlib.util.module_from_spec(_spec)
sys.modules["tcbot"] = bot
_spec.loader.exec_module(bot)
MOVER_SENT, MOVER_EDITS = [], []
bot.bot.send_message = lambda chat_id, text, **k: (MOVER_SENT.append(text), types.SimpleNamespace(message_id=len(MOVER_SENT)))[1]
bot.bot.edit_message_text = lambda text, chat_id, message_id, **k: MOVER_EDITS.append(text) or True
bot.AUTO_MOVER_WATCH_SECONDS = 0.01
bot.AUTO_MOVER_SETTLE_SECONDS = 0


def mover_over():
	deadline = real_time.time() + 10
	while bot._auto_mover_job[0] and real_time.time() < deadline:
		real_time.sleep(0.01)
	return bot._auto_mover_job[0] is None


check("the mover: a folder typed with slashes is stored the way Download Station writes it",
	(bot.set_mover_field("source", "/downloads/"), bot.set_mover_field("target", "/video"),
	bot.bot_settings.get("auto_mover_source"), bot.bot_settings.get("auto_mover_target")), (None, None, "downloads", "video"))
bot.bot_settings.set("auto_mover_min_free_gb", 40)
bot.bot_settings.set("auto_mover_wanted_free_gb", 100)
bot.bot_settings.set("auto_mover_min_days", 7)
text, _markup = bot.build_settings("mover")
check("the mover: the screen tells the free space of each shared folder",
	(f"<code>downloads</code> · {bot.get_text('MOVER_FREE', bot.sizeof_fmt(35 * GB))}" in text,
	f"<code>video</code> · {bot.get_text('MOVER_FREE', bot.sizeof_fmt(2000 * GB))}" in text), (True, True))
plan = bot.auto_mover_plan()
check("the mover: under the minimum, the oldest tasks of the source are picked", (plan.outcome, [t.id for t in plan.torrents]), ("move", [old_a, old_b, old_c]))
bot.bot_settings.set("auto_mover", True)
bot.auto_mover_tick()
check("the mover: the move ends", mover_over(), True)
check("the mover: each task points at the same subfolder of the target, created when missing",
	(dsm.where(old_a), dsm.where(old_b), dsm.where(old_c), dsm.isdir("/video/SERIES/Show B")),
	("video/PELICULAS", "video/SERIES/Show B", "video/PELICULAS", True))
check("the mover: with its data, and seeding again",
	("/video/PELICULAS/Movie.A/a.mkv" in dsm.fs, "/downloads/PELICULAS/Movie.A" in dsm.fs, dsm.size_under("/video/SERIES/Show B/Show.B.S01"),
	dsm.status(old_a), dsm.status(old_b), dsm.broken), (True, False, 30 * GB, "seeding", "seeding", []))
check("the mover: the others stay where they were", (dsm.where(left), dsm.where(recent), dsm.where(elsewhere)),
	("downloads/PELICULAS", "downloads/PELICULAS", "video/library"))
check("the mover: the notice tells them moved", bot.get_text("MOVER_DONE", bot.get_text("MOVER_TORRENTS", 3), bot.sizeof_fmt(90 * GB), "downloads", "video") in MOVER_EDITS[-1], True)

# Something of the same name already in the target: Download Station is not asked to overwrite it
dsm.free["downloads"] = 35 * GB
dsm.put("/video/PELICULAS/Movie.D/other.mkv", 5)
bot.bot_settings.set("auto_mover", True)
bot.auto_mover_tick()
check("the mover: a move the NAS refuses ends too", mover_over(), True)
check("the mover: the task stays in the source with its data", (dsm.where(left), "/downloads/PELICULAS/Movie.D/d.mkv" in dsm.fs, dsm.status(left)), ("downloads/PELICULAS", True, "seeding"))
# In a message of its own: an edit of the notice would tell nobody
check("the mover: it is told as not moved, with why", (bot.get_text("MOVER_FAILED", bot.get_text("MOVER_TORRENTS_ONE")) in MOVER_SENT[-1], "already exists" in MOVER_SENT[-1]), (True, True))
check("the mover: and turned off", bot.bot_settings.get("auto_mover"), False)


if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({CHECKS[0]} checks)")
