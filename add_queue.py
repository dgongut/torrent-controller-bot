"""
Adds that could not reach the torrent manager because it was not answering
(timeouts, connection refused...), kept to be sent again once it does.

Each queued add is a pair of files in CONFIG_PATH/add_queue: <id>.json with
what the add was for and, for a .torrent, <id>.torrent with its content. A
link was already downloaded into a .torrent (or turned into a magnet) before
the add, so nothing here depends on the link still working. The files are
deleted as soon as the add is finished, whatever its outcome, and they
survive a restart of the bot.

This module only keeps the queue: the bot decides what is temporary, does the
adds and tells the user (see the ADD QUEUE section of the bot).
"""

import json
import os
import threading
import time
import uuid
from logger import warning

MAX_AGE = 24 * 3600  # Seconds an add is retried before giving up on it
# Seconds to wait after each failure in a row: a busy manager is left alone a
# little longer every time, so the retries never add to its load
RETRY_DELAYS = (30, 60, 120, 300, 600)


class AddQueue:
	def __init__(self, path):
		self.path = path
		self._lock = threading.Lock()
		self._items = None  # {id: item}, loaded on first use
		self._busy = set()  # Ids being added right now
		self._failures = 0  # Failures in a row, for the delay before the next try
		self._next_try = 0.0
		self._wake = threading.Event()

	# -- persistence ---------------------------------------------------------

	def _json_file(self, item_id):
		return os.path.join(self.path, f"{item_id}.json")

	def _data_file(self, item_id):
		return os.path.join(self.path, f"{item_id}.torrent")

	def _load(self):
		if self._items is not None:
			return
		items = {}
		try:
			names = os.listdir(self.path)
		except FileNotFoundError:
			names = []
		except OSError as e:
			warning(f"Cannot read the add queue in {self.path}: {e}")
			names = []
		for name in names:
			if not name.endswith(".json"):
				continue
			try:
				with open(os.path.join(self.path, name), "r", encoding="utf-8") as file:
					item = json.load(file)
				if not isinstance(item, dict) or item.get("id") != name[:-len(".json")]:
					raise ValueError("not an add of the queue")
				items[item["id"]] = item
			except Exception as e:
				warning(f"Cannot read the queued add {name}, it is left out: {e}")
		# A .torrent without its .json is what a crash between the two writes
		# leaves behind: nothing can be done with it
		for name in names:
			if name.endswith(".torrent") and name[:-len(".torrent")] not in items:
				self._delete(name[:-len(".torrent")])
		self._items = items

	def _write(self, item, data=None):
		os.makedirs(self.path, exist_ok=True)
		if data is not None:
			tmp = self._data_file(item["id"]) + ".tmp"
			with open(tmp, "wb") as file:
				file.write(data)
			os.replace(tmp, self._data_file(item["id"]))
		tmp = self._json_file(item["id"]) + ".tmp"
		with open(tmp, "w", encoding="utf-8") as file:
			json.dump(item, file, indent=4)
		os.replace(tmp, self._json_file(item["id"]))

	def _delete(self, item_id):
		for path in (self._json_file(item_id), self._data_file(item_id)):
			try:
				os.remove(path)
			except FileNotFoundError:
				pass
			except OSError as e:
				warning(f"Cannot delete {path}: {e}")

	# -- the queue -----------------------------------------------------------

	def put(self, name, magnet, data, ids, context, started=None, resolve=False):
		"""Queues an add and returns it. ids are the info hashes the torrent
		can get once added, context what the add was for (destination, chat...),
		started when the first try was made. resolve: queued without trying,
		so the context still has to be worked out with the manager.
		Raises OSError when it cannot be stored: an add that would be lost on
		a restart must not be told as queued"""
		item = {
			"id": uuid.uuid4().hex[:12],
			"name": name or "",
			"magnet": magnet,
			"has_data": data is not None,
			"ids": [str(i).lower() for i in ids or []],
			"context": dict(context or {}),
			"message_id": None,
			"created": time.time(),
		}
		item["started"] = started or item["created"]
		item["resolve"] = bool(resolve)
		with self._lock:
			self._load()
			self._write(item, data)
			self._items[item["id"]] = item
			# It was queued because the manager just failed: it is left alone
			# for a while before the first retry. Something new sent while the
			# queue waits longer than that brings the next try forward: the
			# user is there, and may have seen the manager is back
			soon = time.time() + RETRY_DELAYS[0]
			self._next_try = soon if self._next_try <= time.time() else min(self._next_try, soon)
		self._wake.set()
		return dict(item)

	def set_message(self, item_id, message_id):
		"""Remembers the message that tells the add is queued, which is the one
		that changes to tell how it ended"""
		with self._lock:
			self._load()
			item = self._items.get(item_id)
			if item is None:
				return
			item["message_id"] = message_id
			try:
				self._write(item)
			except OSError as e:
				warning(f"Cannot store the message of the queued add {item['name']}: {e}")

	def items(self):
		"""The queued adds, oldest first"""
		with self._lock:
			self._load()
			return [dict(item) for item in sorted(self._items.values(), key=lambda i: i["created"])]

	def __len__(self):
		with self._lock:
			self._load()
			return len(self._items)

	def get(self, item_id):
		with self._lock:
			self._load()
			item = self._items.get(item_id)
			return dict(item) if item else None

	def data(self, item_id):
		"""The content of the .torrent of an add, None for a magnet"""
		try:
			with open(self._data_file(item_id), "rb") as file:
				return file.read()
		except FileNotFoundError:
			return None

	def find(self, torrent_id):
		"""The queued add whose torrent is torrent_id, or None"""
		torrent_id = str(torrent_id).lower()
		with self._lock:
			self._load()
			for item in sorted(self._items.values(), key=lambda i: i["created"]):
				if torrent_id in item["ids"]:
					return dict(item)
		return None

	def take(self, item_id):
		"""Removes an add from the queue and returns it, None when it was not
		there any more (someone else finished it first). Whoever gets it is the
		one that tells how it ended"""
		with self._lock:
			return self._take(item_id)

	def _take(self, item_id):
		self._load()
		item = self._items.pop(item_id, None)
		if item is not None:
			self._delete(item_id)
		return dict(item) if item else None

	def cancel(self, item_id):
		"""Takes an add the user no longer wants. "busy" when it is being sent
		right now (its result is about to land), None when it is gone"""
		with self._lock:
			if item_id in self._busy:
				return "busy"
			return self._take(item_id)

	def is_busy(self, item_id):
		with self._lock:
			return item_id in self._busy

	def begin(self, item_id):
		"""Marks an add as being sent, so it can no longer be cancelled. False
		when it is not queued any more (cancelled, or seen landing): then it
		must not be sent"""
		with self._lock:
			self._load()
			if item_id not in self._items:
				return False
			self._busy.add(item_id)
			return True

	def end(self, item_id):
		with self._lock:
			self._busy.discard(item_id)

	# -- pacing --------------------------------------------------------------

	def failed(self):
		"""The manager still does not answer: waits longer before the next try"""
		with self._lock:
			delay = RETRY_DELAYS[min(self._failures, len(RETRY_DELAYS) - 1)]
			self._failures += 1
			self._next_try = time.time() + delay
			return delay

	def succeeded(self):
		"""The manager answered again: what is queued goes now"""
		with self._lock:
			self._failures = 0
			self._next_try = 0.0
		self._wake.set()

	def wait_time(self):
		"""Seconds until the next try may be made, 0 when it may be made now"""
		with self._lock:
			return max(0.0, self._next_try - time.time())

	def wait(self, timeout):
		"""Sleeps up to timeout seconds, less when something is queued"""
		self._wake.wait(timeout)
		self._wake.clear()
