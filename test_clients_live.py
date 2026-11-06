"""Live test battery for the torrent client adapters, against real torrent
managers. Run with: python3 test_clients_live.py

test_clients.py fakes each manager from what the real ones answer; this one
asks the real ones, so it is what tells when a new version of a manager
answers something else. It only talks to the managers set in the environment
and skips the rest:

  TCB_LIVE_TRANSMISSION=host:port
  TCB_LIVE_DELUGE=host:port:password           (of the Web UI)
  TCB_LIVE_QBITTORRENT=host:port:user:password
  TCB_LIVE_DELUGE_BAD_DIR   folder the Deluge daemon cannot write, for a
                            torrent in error (default /root/tcb-live-noaccess)
  TCB_LIVE_ALLOW_RESTART=1  also restarts the Deluge daemon behind its Web UI
                            with TCB_LIVE_DELUGE_RESTART (default: docker exec
                            tcb-review-deluge s6-svc -r /run/service/svc-deluged),
                            after breaking a torrent the way Deluge 2.2 does
                            when it removes one only halfway
  TCB_LIVE_TRANSMISSION_EXEC, TCB_LIVE_DELUGE_EXEC, TCB_LIVE_QBITTORRENT_EXEC
                            command that runs another one where the manager
                            keeps its downloads, as the user it runs as (for
                            example: docker exec -i -u abc tcb-review-deluge).
                            With it the battery writes the data of torrents
                            whose data the manager has to find already there

Point it only at throwaway managers: it adds torrents of its own (made up on
the spot, with a tracker that does not exist, so they never download) and
removes them with their data at the end, whatever happens."""

import base64
import hashlib
import os
import shlex
import subprocess
import sys
import threading
import time
import traceback
import uuid

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from torrent_clients import (
	DuplicateTorrentError,
	FILE_HIGH,
	FILE_LOW,
	FILE_NORMAL,
	FILE_SKIP,
	RestartRequiredError,
	TorrentStatus,
)

FAILED = []
CHECKS = [0]


def check(description, actual, expected):
	CHECKS[0] += 1
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def raised(function, *args, **kwargs):
	"""The exception the call raises, or None"""
	try:
		function(*args, **kwargs)
	except Exception as e:
		return e
	return None


def wait_for(condition, seconds=15, step=0.5):
	"""The first truthy value condition() returns within the time, or the last one"""
	deadline = time.time() + seconds
	while True:
		value = condition()
		if value or time.time() >= deadline:
			return value
		time.sleep(step)


def torrent_when(client, torrent_hash, condition, seconds=15):
	"""The torrent once condition(torrent) holds within the time, or None"""
	def ready():
		torrent = client.get_torrent(torrent_hash)
		return torrent if torrent is not None and condition(torrent) else None
	return wait_for(ready, seconds)


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


PIECE = 16384
EPISODES = [(["Show.S01E01.1080p.WEB.mkv"], 300000), (["Subs", "Show.S01E01.es.srt"], 5000),
			(["Show.S01E02.1080p.WEB.mkv"], 250000), (["info.nfo"], 1200)]


def make_torrent_with_content(label, files=EPISODES):
	"""A new torrent of its own (random content, so a hash nobody has), whose
	tracker does not exist: (data, info hash, name, paths, content)"""
	name = f"TCB.Live.{label}.S01.1080p.WEB-{uuid.uuid4().hex[:8]}"
	content = os.urandom(sum(size for _, size in files))
	pieces = b"".join(hashlib.sha1(content[i:i + PIECE]).digest() for i in range(0, len(content), PIECE))
	info = {"name": name, "piece length": PIECE, "pieces": pieces,
			"files": [{"path": path, "length": size} for path, size in files]}
	data = bencode({"announce": "http://127.0.0.1:1/announce", "info": info})
	return data, hashlib.sha1(bencode(info)).hexdigest(), name, sorted(f"{name}/{'/'.join(path)}" for path, _ in files), content


def make_torrent(label):
	"""A new multi-file torrent of its own: (data, info hash, name, paths)"""
	return make_torrent_with_content(label)[:4]


def write_data(exec_prefix, folder, name, files, content):
	"""Writes the content of a torrent into folder, where the manager looks for
	it, through the command of TCB_LIVE_*_EXEC"""
	offset = 0
	for path, size in files:
		target = "/".join([folder.rstrip("/"), name] + path)
		subprocess.run(shlex.split(exec_prefix) + ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", target],
			input=content[offset:offset + size], check=True, capture_output=True, timeout=60)
		offset += size


def delete_folder(exec_prefix, folder):
	subprocess.run(shlex.split(exec_prefix) + ["rm", "-rf", folder], capture_output=True, timeout=60)


def tidy(client, exec_prefix):
	"""Deletes the folder the managers leave behind, empty, after the torrents in it are removed"""
	default_dir = quietly(client.get_default_download_dir) if exec_prefix else None
	if default_dir:
		delete_folder(exec_prefix, f"{default_dir.rstrip('/')}/tcb-live-first")


def story(client, label, cleanup):
	"""What every manager has to do right: add, refuse a duplicate, file
	priorities, session stats and the listing. Returns the added torrent"""
	version = client.test_connection()
	print(f"  {label}: {version}")
	default_dir = client.get_default_download_dir().rstrip("/") or "/"
	first_dir = f"{default_dir}/tcb-live-first"

	data, torrent_hash, name, paths = make_torrent(label)
	torrent = client.add_torrent(torrent_data=data, download_dir=first_dir)
	cleanup.append(torrent.id)
	check(f"{label}: the added torrent is the one sent", (torrent.id, torrent.name), (torrent_hash, name))
	check(f"{label}: in the folder chosen", torrent.download_dir.rstrip("/"), first_dir)
	full = torrent_when(client, torrent_hash, lambda t: len(t.files) == 4)
	check(f"{label}: with its files", sorted(path for path, _, _ in full.files) if full else None, paths)

	# D9: a torrent the manager already has
	magnet = f"magnet:?xt=urn:btih:{torrent_hash}&dn={name}"
	magnet_b32 = f"magnet:?xt=urn:btih:{base64.b32encode(bytes.fromhex(torrent_hash)).decode()}"
	for kind, kwargs in ((".torrent", {"torrent_data": data}), ("magnet", {"magnet": magnet}), ("base32 magnet", {"magnet": magnet_b32})):
		started = time.time()
		error = raised(client.add_torrent, download_dir=f"{default_dir}/tcb-live-second", **kwargs)
		check(f"{label}: adding the same {kind} again is a DuplicateTorrentError", type(error), DuplicateTorrentError)
		check(f"{label}: naming the torrent that was there ({kind})", getattr(error, "torrent_id", None), torrent_hash)
		check(f"{label}: told at once ({kind})", time.time() - started < 4, True)
	check(f"{label}: the torrent that was there stays in its folder", client.get_torrent(torrent_hash).download_dir.rstrip("/"), first_dir)
	check(f"{label}: and is still only once in the list", [t.id for t in client.get_torrents()].count(torrent_hash), 1)

	# File priorities, one file and all of them, at every level the manager has
	levels = client.file_priority_levels
	for level in levels:
		client.set_file_priority(torrent_hash, [paths[0]], level)
		check(f"{label}: one file at {level}", wait_for(lambda: client.get_torrent(torrent_hash).file_priorities.get(paths[0]) == level, 5), True)
		client.set_file_priority(torrent_hash, paths, level)
		check(f"{label}: every file at {level}", wait_for(
			lambda: set(client.get_torrent(torrent_hash).file_priorities.values()) == {level}, 5), True)
	client.set_file_priority(torrent_hash, paths, FILE_NORMAL)
	client.set_file_priority(torrent_hash, [paths[1]], FILE_SKIP)
	client.set_file_priority(torrent_hash, [paths[2]], FILE_HIGH)
	expected = {path: FILE_NORMAL for path in paths}
	expected.update({paths[1]: FILE_SKIP, paths[2]: FILE_HIGH})
	check(f"{label}: files at different levels", wait_for(lambda: client.get_torrent(torrent_hash).file_priorities == expected, 5), True)
	client.set_file_priority(torrent_hash, paths, FILE_NORMAL)

	# Session stats and the listing
	summary = client.get_summary()
	check(f"{label}: the dashboard counts it", summary.total >= 1, True)
	check(f"{label}: bytes of this session", (summary.session_downloaded >= 0, summary.session_uploaded >= 0), (True, True))
	listed = {t.id: t for t in client.get_torrents()}
	check(f"{label}: it is in the listing", torrent_hash in listed, True)
	check(f"{label}: no ratio in the listing is negative", [t.name for t in listed.values() if t.ratio < 0], [])
	# None while it never transferred, except in qBittorrent, which counts the add as activity
	last_activity = listed[torrent_hash].last_activity
	check(f"{label}: the last activity is none or a moment of the past ({last_activity})",
		last_activity is None or 0 < last_activity <= time.time() + 60, True)
	check(f"{label}: a torrent without data is not finished", listed[torrent_hash].is_finished, False)
	return torrent_hash


def removal(client, label, torrent_ids):
	client.remove_torrents(torrent_ids, delete_data=True)
	gone = wait_for(lambda: all(client.get_torrent(torrent_id) is None for torrent_id in torrent_ids), 15)
	check(f"{label}: removed", gone, True)


def remove_quietly(client, torrent_ids):
	for torrent_id in torrent_ids:
		try:
			client.remove_torrents([torrent_id], delete_data=True)
		except Exception:
			pass


def quietly(function):
	"""What the call returns, or None when it fails (said, not raised)"""
	try:
		return function()
	except Exception as e:
		print(f"  ({type(e).__name__}: {e})")
		return None


def magnet_without_metadata(client, label, cleanup):
	"""A magnet nobody serves: it stays fetching its metadata"""
	torrent_hash = hashlib.sha1(uuid.uuid4().bytes).hexdigest()
	torrent = client.add_torrent(magnet=f"magnet:?xt=urn:btih:{torrent_hash}&dn=TCB.Live.Magnet.{label}")
	cleanup.append(torrent.id)
	check(f"{label}: a magnet is added", torrent.id, torrent_hash)
	# qBittorrent queues a new torrent for a moment before it starts
	started = wait_for(lambda: client.get_torrent(torrent_hash).status == TorrentStatus.DOWNLOADING, 10)
	check(f"{label}: it downloads (its metadata)", started, True)
	check(f"{label}: with no ratio yet, which is 0", client.get_torrent(torrent_hash).ratio, 0.0)
	check(f"{label}: and it is not finished", torrent.is_finished, False)
	return torrent_hash


def almost_complete(client, label, cleanup, exec_prefix, raw_progress):
	"""A torrent whose data is all there but its last byte: the managers report
	a share of 99.9999 %, which rounds to 100, and only the bytes left say it
	is not finished. raw_progress(torrent_hash) is the manager's own figure"""
	files = [(["Show.S01E01.mkv"], 40 * PIECE), (["Show.S01E02.mkv"], 24 * PIECE + 1)]  # The last piece is 1 byte
	data, torrent_hash, name, _paths, content = make_torrent_with_content(f"{label}.Almost", files)
	folder = f"{client.get_default_download_dir().rstrip('/')}/tcb-live-almost"
	write_data(exec_prefix, folder, name, files, content[:-1] + bytes([content[-1] ^ 0xFF]))
	try:
		torrent = client.add_torrent(torrent_data=data, download_dir=folder)
		cleanup.append(torrent.id)
		# The manager checks the data it finds first
		torrent = torrent_when(client, torrent_hash, lambda t: t.progress > 50 and t.status != TorrentStatus.CHECKING, 30)
		time.sleep(1)
		raw = raw_progress(torrent_hash)
		print(f"  {label}'s own figure for a torrent lacking its last byte: {raw}")
		check(f"{label}: its own share of it rounds to 100 (what fooled the bot)", round(raw * 100, 2), 100.0)
		torrent = client.get_torrent(torrent_hash)
		check(f"{label}: a torrent lacking 1 byte is not finished", (torrent.progress, torrent.is_finished), (99.99, False))
		listed = {t.id: t for t in client.get_torrents()}[torrent_hash]
		check(f"{label}: nor in the listing", (listed.progress, listed.is_finished), (99.99, False))
		check(f"{label}: nor counted as completed in the dashboard", client.get_summary().completed, sum(1 for t in client.get_torrents() if t.is_finished))
		removal(client, label, [torrent_hash])
		cleanup.remove(torrent_hash)
	finally:
		delete_folder(exec_prefix, folder)


def run(label, function):
	print(f"{label}:")
	step(label, function)


def step(label, function):
	"""What the story returns. One that blows up fails without stopping the next ones"""
	try:
		return function()
	except Exception:
		CHECKS[0] += 1
		FAILED.append(f"{label} stopped:\n{traceback.format_exc()}")
		return None


def transmission_live(address):
	from torrent_clients.transmission_client import TransmissionClient
	host, port = address.rsplit(":", 1)
	client = TransmissionClient(host, int(port))
	exec_prefix = os.environ.get("TCB_LIVE_TRANSMISSION_EXEC")
	cleanup = []
	try:
		story(client, "Transmission", cleanup)
		# D6: Transmission reports -1 for a magnet without metadata
		magnet_without_metadata(client, "Transmission", cleanup)
		summary = client.get_summary()
		check("Transmission: stats of all its sessions", (summary.total_downloaded >= 0, summary.session_seconds >= 0,
			summary.total_seconds >= 0, summary.session_count >= 1), (True, True, True, True))
		if exec_prefix:
			almost_complete(client, "Transmission", cleanup, exec_prefix,
				lambda torrent_hash: client.client.get_torrent(torrent_hash, arguments=["id", "percentDone"]).fields["percentDone"])
		else:
			print("  Transmission: torrents with data skipped (TCB_LIVE_TRANSMISSION_EXEC is not set)")
		removal(client, "Transmission", list(cleanup))
		cleanup.clear()
	finally:
		remove_quietly(client, cleanup)
		tidy(client, exec_prefix)


def qbittorrent_live(address):
	from torrent_clients.qbittorrent_client import QBittorrentClient
	host, port, user, password = address.split(":", 3)
	client = QBittorrentClient(host, int(port), username=user, password=password)
	exec_prefix = os.environ.get("TCB_LIVE_QBITTORRENT_EXEC")
	cleanup = []
	try:
		story_hash = story(client, "qBittorrent", cleanup)
		# qBittorrent also reads a magnet whose topic is percent-encoded (Transmission and Deluge refuse it)
		error = raised(client.add_torrent, magnet=f"magnet:?xt=urn%3Abtih%3A{story_hash}&dn=TCB.Live.Encoded")
		check("qBittorrent: adding it again as a percent-encoded magnet is a DuplicateTorrentError",
			(type(error), getattr(error, "torrent_id", None)), (DuplicateTorrentError, story_hash))
		api = lambda endpoint, **params: client.session.get(f"{client.base_url}/{endpoint}", params=params, timeout=15).json()
		check("qBittorrent: no temporary tag is left behind", [t for t in api("torrents/tags") if t.startswith("tcb-")], [])
		summary = client.get_summary()
		check("qBittorrent: bytes of all its history", (summary.total_downloaded >= 0, summary.total_uploaded >= 0), (True, True))
		# D8: a force-started magnet fetching its metadata is forcedMetaDL
		torrent_hash = magnet_without_metadata(client, "qBittorrent", cleanup)
		client._post("torrents/setForceStart", data={"hashes": torrent_hash, "value": "true"})
		forced = wait_for(lambda: [t["state"] for t in api("torrents/info", hashes=torrent_hash)] == ["forcedMetaDL"], 10)
		check("qBittorrent: a force-started magnet reports forcedMetaDL", forced, True)
		check("qBittorrent: which is downloading, not paused", client.get_torrent(torrent_hash).status, TorrentStatus.DOWNLOADING)
		if exec_prefix:
			almost_complete(client, "qBittorrent", cleanup, exec_prefix, lambda torrent_hash: api("torrents/info", hashes=torrent_hash)[0]["progress"])
		else:
			print("  qBittorrent: torrents with data skipped (TCB_LIVE_QBITTORRENT_EXEC is not set)")
		removal(client, "qBittorrent", list(cleanup))
		cleanup.clear()
	finally:
		remove_quietly(client, cleanup)
		tidy(client, exec_prefix)


def unfinished_again(client, torrent_hash, paths):
	"""Pauses the torrent, skips every file (Deluge sees it finished) and wants one again"""
	torrent_when(client, torrent_hash, lambda t: len(t.files) == len(paths))
	client.pause_torrents([torrent_hash])
	client.set_file_priority(torrent_hash, paths, FILE_SKIP)
	finished = wait_for(lambda: client._call("core.get_torrent_status", torrent_hash, ["is_finished"]).get("is_finished"), 10)
	check("Deluge: a torrent with every file skipped is finished for Deluge", finished, True)
	client.set_file_priority(torrent_hash, [paths[0]], FILE_NORMAL)
	time.sleep(2)


def removed_whole(client, what, torrent_hash, cleanup):
	error = raised(client.remove_torrents, [torrent_hash], delete_data=True)
	check(f"Deluge: {what} is removed", error, None)
	if error is None or isinstance(error, RestartRequiredError):
		cleanup.remove(torrent_hash)  # Removed, or only the daemon restart clears it
	check(f"Deluge: whole, {what} is no longer in Deluge", torrent_hash in client._call("core.get_session_state"), False)
	started = time.time()
	check(f"Deluge: the listing still answers after removing {what}", raised(client.get_torrents), None)
	check(f"Deluge: at once after removing {what}", time.time() - started < 3, True)


def deluge_live(address):
	from torrent_clients.deluge_client import DelugeClient
	host, port, password = address.split(":", 2)
	client = DelugeClient(host, int(port), password=password)
	exec_prefix = os.environ.get("TCB_LIVE_DELUGE_EXEC")
	cleanup = []
	try:
		torrent_hash = story(client, "Deluge", cleanup)
		paths = sorted(path for path, _, _ in client.get_torrent(torrent_hash).files)

		# D7: Deluge calls 5 High
		client._call("core.set_torrent_options", [torrent_hash], {"file_priorities": [5, 4, 1, 0]})
		by_index = sorted(client._call("core.get_torrent_status", torrent_hash, ["files"])["files"], key=lambda f: f["index"])
		priorities = client.get_torrent(torrent_hash).file_priorities
		check("Deluge: priorities 5, 4, 1 and 0 are high, normal, low and skip",
			[priorities[f["path"]] for f in by_index], [FILE_HIGH, FILE_NORMAL, FILE_LOW, FILE_SKIP])

		# D5: changes at the same time on one torrent are all kept
		for attempt in range(5):
			client.set_file_priority(torrent_hash, paths, FILE_NORMAL)
			threads = [threading.Thread(target=client.set_file_priority, args=(torrent_hash, [path], FILE_SKIP)) for path in paths]
			for thread in threads:
				thread.start()
			for thread in threads:
				thread.join()
			kept = set(client.get_torrent(torrent_hash).file_priorities.values()) == {FILE_SKIP}
			check(f"Deluge: four simultaneous changes are all kept (round {attempt + 1})", kept, True)
		client.set_file_priority(torrent_hash, paths, FILE_NORMAL)

		# D6: Deluge reports -1 while nothing is downloaded
		check("Deluge: the ratio of a torrent with nothing downloaded is 0",
			(client._call("core.get_torrent_status", torrent_hash, ["ratio"])["ratio"], client.get_torrent(torrent_hash).ratio), (-1.0, 0.0))

		# D1: a torrent in error is not complete, whatever Deluge's progress says
		bad_dir = os.environ.get("TCB_LIVE_DELUGE_BAD_DIR", "/root/tcb-live-noaccess")
		data, errored_hash, _name, _paths = make_torrent("Deluge.Error")
		errored = client.add_torrent(torrent_data=data, download_dir=bad_dir)
		cleanup.append(errored.id)
		errored = torrent_when(client, errored_hash, lambda t: t.status == TorrentStatus.ERROR, 20)
		check(f"Deluge: a torrent in {bad_dir} ends in error", errored.status if errored else None, TorrentStatus.ERROR)
		if errored:
			raw = client._call("core.get_torrent_status", errored_hash, ["progress", "total_done", "message"])
			print(f"  Deluge's own view of it: {raw}")
			check("Deluge: its own progress of it is 100 (what used to fool the bot)", raw["progress"], 100.0)
			check("Deluge: its progress is 0 here", errored.progress, 0.0)
			check("Deluge: it is not finished", errored.is_finished, False)
			listed = {t.id: t for t in client.get_torrents()}
			check("Deluge: nor in the listing", (listed[errored_hash].progress, listed[errored_hash].is_finished), (0.0, False))
			check("Deluge: and the dashboard does not count it as completed",
				client.get_summary().completed, sum(1 for t in listed.values() if t.is_finished))

		# Deluge 2.2 forgets a finished torrent from its queue of unfinished ones,
		# and does not put it back when a skipped file is wanted again while it
		# is paused or complete. It removes such a torrent from libtorrent (and
		# its data) and then fails, leaving a broken entry that keeps it from
		# listing until the daemon restarts: the adapter gets it ready first.
		# Each story goes on its own, so the daemon restart that clears what a
		# failed one breaks always comes
		folder = f"{client.get_default_download_dir().rstrip('/')}/tcb-live-first"
		step("Deluge: paused torrent unfinished again", lambda: deluge_paused_removal(client, folder, cleanup))
		if exec_prefix:
			step("Deluge: almost complete torrent", lambda: almost_complete(client, "Deluge", cleanup, exec_prefix,
				lambda torrent_hash: client._call("core.get_torrent_status", torrent_hash, ["progress"])["progress"] / 100))
			step("Deluge: complete torrent unfinished again", lambda: deluge_complete_removal(client, exec_prefix, cleanup))
		else:
			print("  Deluge: torrents with data skipped (TCB_LIVE_DELUGE_EXEC is not set)")
		if os.environ.get("TCB_LIVE_ALLOW_RESTART") == "1":
			refused = step("Deluge: torrent removed only halfway", lambda: deluge_half_removed(client, folder, cleanup))
			step("Deluge: daemon restart", lambda: deluge_restart(client, torrent_hash, refused, cleanup))
		else:
			print("  Deluge: daemon restart skipped (TCB_LIVE_ALLOW_RESTART is not 1)")

		removal(client, "Deluge", list(cleanup))
		cleanup.clear()
	finally:
		if os.environ.get("TCB_LIVE_ALLOW_RESTART") == "1" and quietly(lambda: broken_entries(client)):
			# A failed story left a torrent removed only halfway: only a restart clears it
			print("  Deluge: restarting its daemon to clear what a failed story left half removed")
			restart_deluge_daemon()
			time.sleep(3)
			client = quietly(lambda: DelugeClient(host, int(port), password=password)) or client
		remove_quietly(client, cleanup)
		tidy(client, exec_prefix)


def deluge_paused_removal(client, folder, cleanup):
	data, paused_hash, _name, paused_paths = make_torrent("Deluge.Paused")
	cleanup.append(client.add_torrent(torrent_data=data, download_dir=folder).id)
	unfinished_again(client, paused_hash, paused_paths)
	check("Deluge: a paused torrent with every file skipped, then one wanted again, is not finished for Deluge",
		client._call("core.get_torrent_status", paused_hash, ["state", "is_finished"]), {"state": "Paused", "is_finished": False})
	removed_whole(client, "a paused torrent unfinished again", paused_hash, cleanup)


def deluge_complete_removal(client, exec_prefix, cleanup):
	"""A complete torrent with a file skipped and wanted again"""
	folder = f"{client.get_default_download_dir().rstrip('/')}/tcb-live-complete"
	data, complete_hash, name, complete_paths, content = make_torrent_with_content("Deluge.Complete")
	write_data(exec_prefix, folder, name, EPISODES, content)
	try:
		cleanup.append(client.add_torrent(torrent_data=data, download_dir=folder).id)
		seeding = torrent_when(client, complete_hash, lambda t: t.is_finished and t.status == TorrentStatus.SEEDING, 30)
		check("Deluge: a torrent whose data is already there is complete", seeding is not None, True)
		client.set_file_priority(complete_hash, [complete_paths[0]], FILE_SKIP)
		time.sleep(1)
		client.set_file_priority(complete_hash, [complete_paths[0]], FILE_NORMAL)
		time.sleep(1)
		check("Deluge: a file of it skipped and wanted again leaves it not finished for Deluge, though complete",
			client._call("core.get_torrent_status", complete_hash, ["is_finished", "is_seed"]), {"is_finished": False, "is_seed": True})
		removed_whole(client, "a complete torrent unfinished again", complete_hash, cleanup)
	finally:
		delete_folder(exec_prefix, folder)


def deluge_half_removed(client, folder, cleanup):
	"""A torrent removed only halfway, as Deluge leaves one removed by another
	client, or one that downloaded data since the daemon started. Returns the
	torrent and the magnet hash refused meanwhile, to add them after the restart"""
	from torrent_clients.deluge_client import QUEUE_LOST
	data, broken_hash, _name, broken_paths = make_torrent("Deluge.Broken")
	cleanup.append(client.add_torrent(torrent_data=data, download_dir=folder).id)
	unfinished_again(client, broken_hash, broken_paths)
	failures = client._call("core.remove_torrents", [broken_hash], True)  # Straight to Deluge
	cleanup.remove(broken_hash)  # Only the daemon restart clears it now
	check("Deluge: removing it straight away removes it only halfway", [QUEUE_LOST in str(failure) for failure in failures], [True])
	error = raised(client.remove_torrents, [broken_hash], delete_data=True)
	check("Deluge: removing it again says its daemon has to restart, where Deluge says nothing", type(error), RestartRequiredError)
	started = time.time()
	check("Deluge: so does the listing", type(raised(client.get_torrents)), RestartRequiredError)
	check("Deluge: at once, where Deluge would never answer", time.time() - started < 3, True)
	check("Deluge: and the dashboard", type(raised(client.get_summary)), RestartRequiredError)
	refused_hash = hashlib.sha1(uuid.uuid4().bytes).hexdigest()
	error = raised(client.add_torrent, magnet=f"magnet:?xt=urn:btih:{refused_hash}&dn=TCB.Live.Refused")
	check("Deluge: an add is refused, Deluge would lose it when restarted", type(error), RestartRequiredError)
	check("Deluge: the broken torrent is gone for the bot", client.get_torrent(broken_hash), None)
	check("Deluge: it is still in Deluge, broken", broken_entries(client), [broken_hash])
	return data, broken_hash, refused_hash


def deluge_restart(client, torrent_hash, refused, cleanup):
	"""D2: the daemon restarts while its Web UI stays up"""
	check("Deluge: the daemon restarts", restart_deluge_daemon(), 0)
	time.sleep(2)
	check("Deluge: the Web UI is left disconnected", client._rpc("web.connected"), False)
	started = time.time()
	recovered = wait_for(lambda: torrent_hash in [t.id for t in quietly(client.get_torrents) or []], 60, step=1)
	print(f"  Deluge: back after {time.time() - started:.1f}s")
	check("Deluge: the same client works again after the restart", recovered, True)
	started = time.time()
	client.get_torrents()
	check("Deluge: and its next calls are fast", time.time() - started < 3, True)
	check("Deluge: nothing broken is left after the restart", broken_entries(client), [])
	if refused:
		data, broken_hash, refused_hash = refused
		added = client.add_torrent(magnet=f"magnet:?xt=urn:btih:{refused_hash}&dn=TCB.Live.Refused")
		cleanup.append(added.id)
		check("Deluge: and adds work again", added.id, refused_hash)
		added = client.add_torrent(torrent_data=data)
		cleanup.append(added.id)
		check("Deluge: even of the torrent that was broken, which is a new one now", (added.id, client.get_torrent(broken_hash) is not None),
			(broken_hash, True))
		check("Deluge: and is listed", broken_hash in [t.id for t in client.get_torrents()], True)


def broken_entries(client):
	"""Torrents Deluge removed only halfway: still in its session, but asking
	for them fails on their gone libtorrent handle"""
	broken = []
	for torrent_id in client._rpc("core.get_session_state") or []:
		try:
			client._rpc("core.get_torrent_status", torrent_id, ["name"])
		except Exception as e:
			if "invalid torrent handle" in str(e):
				broken.append(torrent_id)
	return broken


def restart_deluge_daemon():
	command = os.environ.get("TCB_LIVE_DELUGE_RESTART", "docker exec tcb-review-deluge s6-svc -r /run/service/svc-deluged")
	return subprocess.run(shlex.split(command), capture_output=True, text=True).returncode


MANAGERS = (
	("Transmission", "TCB_LIVE_TRANSMISSION", transmission_live),
	("Deluge", "TCB_LIVE_DELUGE", deluge_live),
	("qBittorrent", "TCB_LIVE_QBITTORRENT", qbittorrent_live),
)

for label, variable, function in MANAGERS:
	address = os.environ.get(variable)
	if not address:
		print(f"{label}: skipped ({variable} is not set)")
		continue
	run(label, lambda: function(address))

if FAILED:
	print(f"\n{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({CHECKS[0]} checks)")
