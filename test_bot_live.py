"""The bot against real torrent managers. Run with: python3 test_bot_live.py

Opt-in: it only talks to the managers named in the environment, and does
nothing without them (throwaway containers are the place for it, it adds and
deletes torrents):

	TCB_LIVE_TRANSMISSION=127.0.0.1:9091
	TCB_LIVE_DELUGE=127.0.0.1:8112:<web password>
	TCB_LIVE_QBITTORRENT=127.0.0.1:8080:<user>:<password>
	TCB_LIVE_DATA=<dir holding BigReview/big.bin>  (optional: the recheck checks)
	TCB_LIVE_DATA_DIR=<that same folder as the manager sees it> (default /downloads)

The bot is loaded like in test_callbacks.py but with the real client, and a
fake Telegram that records what would be sent. What is checked is what only a
real manager can tell: what it does with what the bot asks, and what it
reports back to the monitor.

Test data is generated on the fly: a small multi-file torrent whose tracker
does not exist (nothing is downloaded), and optionally an 800 MB complete one
built by make_big_torrent() from the same seed as the data in TCB_LIVE_DATA."""

import hashlib
import importlib.util
import os
import random
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

os.environ.setdefault("TELEGRAM_TOKEN", "0:fake")
os.environ.setdefault("TELEGRAM_ADMIN", "1")
os.environ.setdefault("LOCALE_PATH", os.path.join(REPO, "locale"))

import config
config.CONFIG_PATH = tempfile.mkdtemp()  # Never touch the real /config

import torrent_clients
from torrent_clients import TorrentStatus

FAILED = []
CHECKS = [0]
CHAT_ID = 1


def check(description, actual, expected):
	CHECKS[0] += 1
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def bencode(value):
	if isinstance(value, int):
		return b"i%de" % value
	if isinstance(value, bytes):
		return b"%d:%s" % (len(value), value)
	if isinstance(value, str):
		return bencode(value.encode())
	if isinstance(value, list):
		return b"l" + b"".join(bencode(item) for item in value) + b"e"
	return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


def make_torrent(name, files, piece=16384, seed=1):
	"""(data of a .torrent, its info hash) for files [(path parts, size)]"""
	rng = random.Random(seed)
	content = b"".join(rng.randbytes(size) for _, size in files)
	pieces = b"".join(hashlib.sha1(content[i:i + piece]).digest() for i in range(0, len(content), piece))
	info = {"name": name, "piece length": piece, "pieces": pieces, "files": [{"path": p, "length": s} for p, s in files]}
	return bencode({"announce": "http://127.0.0.1:1/announce", "info": info}), hashlib.sha1(bencode(info)).hexdigest()


def make_big_torrent():
	"""The torrent of BigReview/big.bin as make_big.py writes it (seed 7, 200 pieces of 4 MiB)"""
	piece = 4 * 1024 * 1024
	rng = random.Random(7)
	hashes = [hashlib.sha1(rng.randbytes(piece)).digest() for _ in range(200)]
	info = {"name": "BigReview", "piece length": piece, "pieces": b"".join(hashes), "files": [{"path": ["big.bin"], "length": 200 * piece}]}
	return bencode({"announce": "http://127.0.0.1:1/announce", "info": info}), hashlib.sha1(bencode(info)).hexdigest()


def live_clients():
	clients = []
	if os.environ.get("TCB_LIVE_TRANSMISSION"):
		host, port = os.environ["TCB_LIVE_TRANSMISSION"].split(":")[:2]
		clients.append(("transmission", dict(TORRENT_CLIENT="transmission", TORRENT_CLIENT_HOST=host, TORRENT_CLIENT_PORT=port)))
	if os.environ.get("TCB_LIVE_DELUGE"):
		host, port, password = os.environ["TCB_LIVE_DELUGE"].split(":", 2)
		clients.append(("deluge", dict(TORRENT_CLIENT="deluge", TORRENT_CLIENT_HOST=host, TORRENT_CLIENT_PORT=port,
									TORRENT_CLIENT_PASSWORD=password)))
	if os.environ.get("TCB_LIVE_QBITTORRENT"):
		host, port, user, password = os.environ["TCB_LIVE_QBITTORRENT"].split(":", 3)
		clients.append(("qbittorrent", dict(TORRENT_CLIENT="qbittorrent", TORRENT_CLIENT_HOST=host, TORRENT_CLIENT_PORT=port,
										TORRENT_CLIENT_USER=user, TORRENT_CLIENT_PASSWORD=password)))
	return clients


def load_bot(settings):
	"""A fresh bot module against the real manager described by settings"""
	for key, value in settings.items():
		setattr(config, key, value)
	spec = importlib.util.spec_from_file_location(f"tcbot_{settings['TORRENT_CLIENT']}", os.path.join(REPO, "torrent-controller-bot.py"))
	bot = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(bot)
	sent = []
	next_id = [1000]

	def send(chat_id, text, **kwargs):
		next_id[0] += 1
		sent.append((text, kwargs.get("reply_markup")))
		return types.SimpleNamespace(message_id=next_id[0])

	bot.bot.send_message = send
	bot.bot.edit_message_text = lambda text, chat_id, message_id, **kwargs: sent.append((text, kwargs.get("reply_markup"))) or True
	bot.bot.delete_message = lambda *a, **k: True
	bot.bot.edit_message_reply_markup = lambda *a, **k: True
	bot.bot.answer_callback_query = lambda *a, **k: True
	bot._live_refresher = lambda *a, **k: None
	bot.notify = lambda text, chat_id=None, thread_id=None, reply_markup=None: sent.append((text, reply_markup)) or types.SimpleNamespace(message_id=0)
	return bot, sent


def callbacks(markup):
	return [b.callback_data for row in (markup.keyboard if markup else []) for b in row]


def wait_for(condition, seconds=20):
	end = time.time() + seconds
	while time.time() < end:
		value = condition()
		if value:
			return value
		time.sleep(0.5)
	return condition()


def run(label, settings):
	bot, sent = load_bot(settings)
	client = bot.client
	data_dir = os.environ.get("TCB_LIVE_DATA_DIR", "/downloads")
	small, small_hash = make_torrent("Live.Review.Show.S01.1080p.WEB-GRP", [
		(["Live.Review.Show.S01E01.1080p.WEB.mkv"], 300000), (["Subs", "Live.Review.Show.S01E01.es.srt"], 5000),
		(["Live.Review.Show.S01E02.1080p.WEB.mkv"], 250000), (["info.nfo"], 1200)])
	other, other_hash = make_torrent("Live.Review.Other.2024.1080p", [(["Live.Review.Other.2024.1080p.mkv"], 200000)], seed=2)
	for leftover in (small_hash, other_hash):
		try:
			client.remove_torrents([leftover], delete_data=True)
		except torrent_clients.TorrentClientError:
			pass
	time.sleep(1)

	# Adding through the bot, and adding it again
	pending_id = bot.new_pending_torrent("Live.Review.Show.S01.1080p.WEB-GRP", data=small)
	bot.dispatch_callback(CHAT_ID, 1, 1, bot.build_call("addTo", pending_id, bot.get_dir_id(data_dir)))
	added = wait_for(lambda: client.get_torrent(small_hash))
	check(f"[{label}] the bot adds the torrent", bool(added), True)
	pending_id = bot.new_pending_torrent("Live.Review.Show.S01.1080p.WEB-GRP", data=small)
	sent.clear()
	bot.dispatch_callback(CHAT_ID, 1, 1, bot.build_call("addTo", pending_id, bot.get_dir_id(data_dir)))
	check(f"[{label}] adding it again says it is already there",
		sent[-1][0], bot.get_text("ADD_DUPLICATE", bot.html.escape(added.name)) if added else None)

	# Files: leave the whole folder out, then take one file back, through the buttons
	ctx = bot.new_nav_context(small_hash, config.FILTER_ALL, 0)
	torrent = client.get_torrent(small_hash)
	root = bot.torrent_folders(torrent).index(bot.torrent_root(torrent))
	bot.dispatch_callback(CHAT_ID, 1, 1, f"{bot.build_call('folderPrio', ctx, root, 0)}|s")
	torrent = wait_for(lambda: (lambda t: t if t and set(t.file_priorities.values()) == {"skip"} else None)(client.get_torrent(small_hash)))
	check(f"[{label}] 'download none' leaves every file out", sorted(set(torrent.file_priorities.values())) if torrent else None, ["skip"])
	torrent = client.get_torrent(small_hash)
	video = next(i for i, (p, _, _) in enumerate(torrent.files) if p.endswith("E02.1080p.WEB.mkv"))
	bot.dispatch_callback(CHAT_ID, 1, 1, bot.file_call("filePrio", ctx, root, 0, video, torrent.files[video][0], "d"))
	torrent = wait_for(lambda: (lambda t: t if t and t.file_priorities.get(t.files[video][0]) != "skip" else None)(client.get_torrent(small_hash)))
	check(f"[{label}] a file taken back is downloaded again", torrent.file_priorities.get(torrent.files[video][0]) if torrent else None, "normal")
	check(f"[{label}] and the screen says so", bot.get_text("FILE_DOWNLOADED") in sent[-1][0], True)

	# A mass delete deletes what its confirmation counted
	bot.dispatch_callback(CHAT_ID, 1, 1, bot.build_call("mass", "delete", config.FILTER_ALL))
	# Without its data: what matters is which torrents go, and the data of a
	# complete test torrent may be in the same manager
	confirm = next(c for c in callbacks(sent[-1][1]) if c.startswith("confirmMass|delete|") and c.endswith("|0"))
	pending_id = bot.new_pending_torrent("Live.Review.Other.2024.1080p", data=other)
	bot.dispatch_callback(CHAT_ID, 1, 1, bot.build_call("addTo", pending_id, bot.get_dir_id(data_dir)))
	wait_for(lambda: client.get_torrent(other_hash))
	bot.dispatch_callback(CHAT_ID, 1, 1, confirm)
	gone = wait_for(lambda: client.get_torrent(small_hash) is None)
	check(f"[{label}] confirming deletes what was counted", gone, True)
	check(f"[{label}] and not the torrent added after the confirmation", client.get_torrent(other_hash) is not None, True)
	client.remove_torrents([other_hash], delete_data=True)

	# A recheck of a complete torrent is not announced twice
	if os.environ.get("TCB_LIVE_DATA"):
		big, big_hash = make_big_torrent()
		try:
			client.remove_torrents([big_hash], delete_data=False)
		except torrent_clients.TorrentClientError:
			pass
		time.sleep(1)
		client.add_torrent(torrent_data=big, download_dir=data_dir)
		done = wait_for(lambda: (lambda t: t if t and t.is_finished else None)(client.get_torrent(big_hash)), 60)
		check(f"[{label}] the complete torrent is seen complete", bool(done), True)
		states = bot.poll_torrents(None)
		sent.clear()
		time.sleep(5)  # qBittorrent ignores a recheck asked right after its own first check
		client.verify_torrent(big_hash)
		# Polled as fast as the manager answers: a fast disk checks 800 MB in a second
		seen = set()
		end = time.time() + 30
		while time.time() < end:
			states = bot.poll_torrents(states)
			seen.update(t.status for t in client.get_torrents() if t.id == big_hash)
			if TorrentStatus.SEEDING in seen and len(seen) > 1:
				break
		states = bot.poll_torrents(states)
		check(f"[{label}] the recheck went through checking", TorrentStatus.CHECKING in seen or TorrentStatus.QUEUED in seen, True)
		check(f"[{label}] and was not announced as a new completion", [t for t, _ in sent if bot.get_text("NOTIFY_COMPLETED", "")[:12] in t], [])
		client.remove_torrents([big_hash], delete_data=False)

	# Deluge: a torrent in error is not complete (it used to report 100 %)
	if label == "deluge":
		client._call("core.set_config", {"pre_allocate_storage": True})
		client.add_torrent(torrent_data=other, download_dir="/root/not-writable")
		errored = wait_for(lambda: (lambda t: t if t and t.status == TorrentStatus.ERROR else None)(client.get_torrent(other_hash)))
		check("[deluge] a torrent that cannot write is in error", errored.status if errored else None, TorrentStatus.ERROR)
		check("[deluge] and is not complete", errored.is_finished if errored else None, False)
		client._call("core.set_config", {"pre_allocate_storage": False})
		client.remove_torrents([other_hash], delete_data=False)


clients = live_clients()
if not clients:
	print("No managers configured (TCB_LIVE_TRANSMISSION / TCB_LIVE_DELUGE / TCB_LIVE_QBITTORRENT): nothing to test")
	raise SystemExit(0)
for name, settings in clients:
	run(name, settings)

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print(f"ALL TESTS OK ({CHECKS[0]} checks against {', '.join(name for name, _ in clients)})")
