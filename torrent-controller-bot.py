import base64
import hashlib
import html
import json
import math
import os
import re
import requests
import socket
import sys
import telebot
import threading
import time
import uuid
from collections import Counter
from config import *
from datetime import datetime
from urllib.parse import urljoin
from telebot.types import ForceReply
from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup
from logger import debug, error, warning
from message_queue import MessageQueue, describe_error
from name_parser import DEFAULT_MOVIE_TEMPLATE, DEFAULT_SERIES_TEMPLATE, DEFAULT_SEASON_PACK_TEMPLATE, SUBTITLE_EXTENSIONS, TemplateError, VALID_EXTENSIONS, companion_subtitle_name, is_sample_file, series_auto_dir, series_dir_candidates, series_identity, suggest_file_name, suggest_folder_name, suggest_name, validate_template
from torrent_clients import DuplicateTorrentError, RestartRequiredError, expected_torrent_ids, FILE_HIGH, FILE_LOW, FILE_NORMAL, FILE_SKIP, PermanentTorrentError, RATIO_MODE_GLOBAL, RATIO_MODE_NONE, RATIO_MODE_TORRENT, TorrentClientError, TorrentStatus, content_root, create_client
import auto_mover
from add_queue import AddQueue, MAX_AGE as ADD_QUEUE_MAX_AGE
import bot_settings
import config as _config_module
import telemetry
import tracker_limits

VERSION = "1.16.0"

if LANGUAGE.lower() not in ("es", "en"):
	error("LANGUAGE only can be ES/EN")
	sys.exit(1)

# MODULO DE TRADUCCIONES
_locale_cache = {}

def load_locale(locale):
	"""Load locale with caching to avoid repeated file I/O"""
	if locale not in _locale_cache:
		with open(f"{LOCALE_PATH}/{locale}.json", "r", encoding="utf-8") as file:
			_locale_cache[locale] = json.load(file)
	return _locale_cache[locale]

def get_text(key, *args):
	"""Get translated text with caching"""
	messages = load_locale(LANGUAGE.lower())
	if key in messages:
		translated_text = messages[key]
	else:
		messages_en = load_locale("en")
		if key in messages_en:
			warning(f"key ['{key}'] is not in locale {LANGUAGE}")
			translated_text = messages_en[key]
		else:
			error(f"key ['{key}'] is not in locale {LANGUAGE} or EN")
			return f"key ['{key}'] is not in locale {LANGUAGE} or EN"

	if args:
		# One pass: a value that itself contains "$2" (a torrent called
		# "Price$2Off") must not get the second argument put inside it
		def argument(match):
			index = int(match.group(1))
			return str(args[index - 1]) if 1 <= index <= len(args) else match.group(0)
		translated_text = _PLACEHOLDER.sub(argument, translated_text)

	return translated_text


_PLACEHOLDER = re.compile(r"\$(\d+)")


def parse_name(filename):
	"""Suggests a name using the user templates (or the defaults).
	Returns None when no suggestion can be produced"""
	return suggest_name(
		filename,
		template_movie=bot_settings.get("template_movie") or None,
		template_series=bot_settings.get("template_series") or None,
		template_season=bot_settings.get("template_season") or None,
		season_prefix="T" if LANGUAGE.lower() == "es" else "S",
	)


def parse_file_name(filename, parent_name, single_video, folder_name=None):
	"""Same as parse_name but for a file inside a torrent, using the torrent
	name (and the folder holding the file) as context"""
	return suggest_file_name(
		filename,
		parent_name=parent_name,
		single_video=single_video,
		folder_name=folder_name,
		template_movie=bot_settings.get("template_movie") or None,
		template_series=bot_settings.get("template_series") or None,
		template_season=bot_settings.get("template_season") or None,
		season_prefix="T" if LANGUAGE.lower() == "es" else "S",
	)


def parse_folder_name(folder_name, parent_name):
	"""Same as parse_name but for a folder inside a torrent (a season folder),
	using the torrent name as context"""
	return suggest_folder_name(
		folder_name,
		parent_name=parent_name,
		template_movie=bot_settings.get("template_movie") or None,
		template_series=bot_settings.get("template_series") or None,
		template_season=bot_settings.get("template_season") or None,
		season_prefix="T" if LANGUAGE.lower() == "es" else "S",
	)


for problem in CONFIG_WARNINGS:
	warning(problem)
for removed in REMOVED_VARIABLES:
	if os.environ.get(removed):
		warning(f"{removed} is no longer used: screens repaint themselves every {DASHBOARD_REFRESH_SECONDS} s "
				f"for {DASHBOARD_REFRESH_DURATION} s, then the update button takes over")

# Initial variable validation
if TELEGRAM_TOKEN is None or TELEGRAM_TOKEN == '':
	error("You need to configure the bot token with the TELEGRAM_TOKEN variable")
	sys.exit(1)
if TELEGRAM_ADMIN is None or TELEGRAM_ADMIN == '':
	error("You need to configure the chatId of the user who will interact with the bot with the TELEGRAM_ADMIN variable")
	sys.exit(1)
if str(ANONYMOUS_USER_ID) in str(TELEGRAM_ADMIN).split(','):
	error("You cannot be anonymous to control the bot. In the variable TELEGRAM_ADMIN you have to put your user id.")
	sys.exit(1)
if TELEGRAM_GROUP is None or TELEGRAM_GROUP == '':
	if len(str(TELEGRAM_ADMIN).split(',')) > 1:
		error("Multiple administrators can only be specified if used in a group (using the TELEGRAM_GROUP variable)")
		sys.exit(1)

try:
	TELEGRAM_THREAD = int(str(TELEGRAM_THREAD).strip()) if str(TELEGRAM_THREAD).strip() else 1
except:
	error(f"The variable TELEGRAM_THREAD is the thread within a supergroup, it is a numeric value. It has been set to {TELEGRAM_THREAD}.")
	sys.exit(1)

ADMIN_IDS = [x.strip() for x in str(TELEGRAM_ADMIN).split(',')]

bot = telebot.TeleBot(TELEGRAM_TOKEN)
message_queue = MessageQueue(delay_between_messages=0.3)

try:
	client = create_client(_config_module)
	client_version = client.test_connection()
	debug(f"Connected to {client_version}")
except TorrentClientError as e:
	error(str(e))
	sys.exit(1)


# ---------------------------------------------------------------------------
# GENERIC HELPERS
# ---------------------------------------------------------------------------

STATUS_EMOJI = {
	TorrentStatus.DOWNLOADING: "📥",
	TorrentStatus.SEEDING: "🌱",
	TorrentStatus.PAUSED: "⏸️",
	TorrentStatus.QUEUED: "⏳",
	TorrentStatus.CHECKING: "🔍",
	TorrentStatus.ERROR: "❌",
}

STATUS_TEXT_KEY = {
	TorrentStatus.DOWNLOADING: "STATUS_DOWNLOADING",
	TorrentStatus.SEEDING: "STATUS_SEEDING",
	TorrentStatus.PAUSED: "STATUS_PAUSED",
	TorrentStatus.QUEUED: "STATUS_QUEUED",
	TorrentStatus.CHECKING: "STATUS_CHECKING",
	TorrentStatus.ERROR: "STATUS_ERROR",
}

FILTER_TO_STATUS = {
	FILTER_DOWNLOADING: TorrentStatus.DOWNLOADING,
	FILTER_SEEDING: TorrentStatus.SEEDING,
	FILTER_PAUSED: TorrentStatus.PAUSED,
	FILTER_QUEUED: TorrentStatus.QUEUED,
	FILTER_CHECKING: TorrentStatus.CHECKING,
	FILTER_ERROR: TorrentStatus.ERROR,
}


def sizeof_fmt(num, suffix="B"):
	if num is None or num < 0:
		return "?"
	for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
		if abs(num) < 1024.0:
			return f"{num:3.1f}{unit}{suffix}"
		num /= 1024.0
	return f"{num:.1f}Yi{suffix}"


def format_eta(seconds):
	if seconds is None or seconds < 0:
		return "-"
	if seconds < 60:
		return f"{seconds}s"
	minutes, _ = divmod(seconds, 60)
	hours, minutes = divmod(minutes, 60)
	days, hours = divmod(hours, 24)
	if days > 0:
		return f"{days}d {hours}h"
	if hours > 0:
		return f"{hours}h {minutes}m"
	return f"{minutes}m"


def truncate(text, length=MAX_NAME_LENGTH_IN_BUTTON):
	return text if len(text) <= length else text[:length - 1] + "…"


def truncate_dir(path, length=30):
	return path if len(path) <= length else "…" + path[-(length - 1):]


def build_call(command, *args):
	call = "|".join([command] + [str(a) for a in args])
	# Telegram rejects the whole keyboard with BUTTON_DATA_INVALID over 64 bytes
	size = len(call.encode("utf-8"))
	if size > MAX_CALLBACK_DATA_BYTES:
		warning(f"callback_data too long ({size} bytes), Telegram will reject the keyboard: {call}")
	return call


def pagination_row(page, pages, *base):
	"""Prev/next buttons plus a central one (current page) that asks which page
	to jump to. base is the callback prefix that receives the page as last arg"""
	base_call = build_call(*base)
	prev_call = f"{base_call}|{page - 1}" if page > 0 else build_call("noop")
	next_call = f"{base_call}|{page + 1}" if page < pages - 1 else build_call("noop")
	return [
		InlineKeyboardButton("⬅️", callback_data=prev_call),
		InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=build_call("goto", new_page_context(base_call), page, pages)),
		InlineKeyboardButton("➡️", callback_data=next_call),
	]


def is_authorized(user_id, chat_id):
	if str(user_id) not in ADMIN_IDS:
		return False
	if TELEGRAM_GROUP and str(chat_id) != str(TELEGRAM_GROUP) and str(chat_id) not in ADMIN_IDS:
		return False
	return True


_HTML_TAG = re.compile(r"<(/?)([a-z]+)[^>]*>")


def _pending_close_tags(text):
	"""The HTML a cut would leave open, as the closing tags it still needs"""
	stack = []
	for closing, tag in _HTML_TAG.findall(text):
		if closing:
			if stack and stack[-1] == tag:
				stack.pop()
		else:
			stack.append(tag)
	return "".join(f"</{tag}>" for tag in reversed(stack))


def clamp_text(text):
	"""A torrent or file name long enough to push a screen past the Telegram
	limit would only get the message rejected, and the screen would silently
	stay as it was, so it is cut here instead. The cut prefers a line boundary
	because every line closes its own tags, and whatever is still open at the
	cut is closed so Telegram can parse what is left"""
	if len(text) <= MAX_MESSAGE_LENGTH:
		return text
	marker = f"\n{get_text('MESSAGE_TRUNCATED')}"
	budget = MAX_MESSAGE_LENGTH - len(marker)
	cut = text[:budget]
	if "\n" in cut:
		cut = cut[:cut.rindex("\n")]
	closing = ""
	# Making room for the closing tags can uncover another one, and the screens
	# never nest HTML more than a couple of levels deep
	for _ in range(8):
		cut = re.sub(r"<[^>]*$", "", cut)  # Never leave half a tag behind
		closing = _pending_close_tags(cut)
		if len(cut) + len(closing) <= budget:
			break
		cut = cut[:budget - len(closing)]
	warning(f"Message truncated: {len(text)} characters exceed the Telegram limit")
	return cut + closing + marker


def normalize_thread(thread_id):
	"""Telegram addresses the General topic of a forum by omitting the thread,
	so both it (thread 1) and a chat without topics normalize to None"""
	try:
		thread_id = int(thread_id)
	except (TypeError, ValueError):
		return None
	return thread_id if thread_id > 1 else None


def message_thread(message):
	"""The forum topic a message is in, normalized, or None. Outside a forum a
	reply also carries message_thread_id (the id of the message that started
	the reply chain, which every answer to a ForceReply prompt is), and
	sending to it fails with "message thread not found": only a topic message
	(is_topic_message) is really in a topic"""
	if message is None or not getattr(message, "is_topic_message", False):
		return None
	return normalize_thread(getattr(message, "message_thread_id", None))


def send_message(chat_id, text, reply_markup=None, thread_id=None):
	kwargs = {"parse_mode": "HTML", "reply_markup": reply_markup, "disable_web_page_preview": True}
	thread_id = normalize_thread(thread_id)
	if thread_id:
		kwargs["message_thread_id"] = thread_id
	return message_queue.enqueue_and_wait(bot.send_message, chat_id, clamp_text(text), **kwargs)


def edit_message(chat_id, message_id, text, reply_markup=None):
	try:
		return bot.edit_message_text(clamp_text(text), chat_id, message_id, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)
	except Exception as e:
		if "message is not modified" not in str(e):
			warning(f"Cannot edit message {message_id}: {e}")
		return None


def send_notice(chat_id, text, thread_id=None):
	"""A message with nothing to press that goes away by itself: what was
	just done, told without leaving anything behind to close"""
	sent = send_message(chat_id, text, thread_id=thread_id)
	if sent:
		timer = threading.Timer(NOTICE_SECONDS, delete_message, (chat_id, sent.message_id))
		timer.daemon = True
		timer.start()
	return sent


def delete_message(chat_id, message_id):
	"""Returns whether the message is gone. Telegram refuses to delete a
	message older than 48 hours"""
	try:
		bot.delete_message(chat_id, message_id)
		return True
	except Exception:
		return False


def remove_markup(chat_id, message_id):
	"""Takes the buttons away from a message. A second tap on a button that
	already did it gets "message is not modified" from Telegram, which must
	not be painted over the message as an error"""
	try:
		bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
	except Exception as e:
		if "message is not modified" not in str(e):
			warning(f"Cannot remove the buttons of message {message_id}: {e}")


def notify(text, chat_id=None, thread_id=None, reply_markup=None):
	"""Sends a bot-initiated notification. A conversation that originated it
	takes precedence, so the answer lands in the topic where it was asked;
	otherwise it goes to the configured destination: the group (and its
	TELEGRAM_THREAD) if set, or the first admin. Returns the message sent,
	None when it could not be sent"""
	if chat_id is not None:
		return send_message(chat_id, text, reply_markup=reply_markup, thread_id=thread_id)
	target = TELEGRAM_GROUP if TELEGRAM_GROUP else ADMIN_IDS[0]
	return send_message(target, text, reply_markup=reply_markup, thread_id=TELEGRAM_THREAD)


# ---------------------------------------------------------------------------
# IN-MEMORY CONTEXTS
# ---------------------------------------------------------------------------

_contexts_lock = threading.Lock()

# Search contexts: short id -> {"query": str, "ts": float}
search_contexts = {}

# Tracker filter contexts: short id -> {"tracker": str, "ts": float}
tracker_contexts = {}

# Category filter contexts: short id -> {"category": str, "ts": float}
category_contexts = {}

# Pending torrents waiting for a download dir: id -> {"magnet"/"data", "name", "ts"}
pending_torrents = {}
# Pending ids whose add is under way (the folder was chosen)
_adding = set()

# Navigation contexts: short id -> {"torrent_id", "filter_key", "page", "ts"}
nav_contexts = {}

# Pagination contexts: short id -> {"base": callback prefix, "ts": float}
page_contexts = {}

# Pending text inputs: (chat_id, user_id) -> {"action": str, "ts": float, "thread": int|None, ...}
pending_inputs = {}

# What a confirmation was shown about, so pressing it acts on exactly that
# (the torrents a mass action counted, the ones a replace listed): short id
# -> {"ts": float, ...}. The id starts with a letter of its own kind
action_contexts = {}

# Folders and categories behind the short ids of their buttons. The id is a
# hash of the name, so it is the same in every run: buttons outlive restarts
_dir_by_id = {}
_cat_by_id = {}

# Live screens (the dashboard, a list, a torrent, files) repainting themselves.
# live_views: (chat_id, thread_id) -> {"message_id": int, "generation": int},
# the one live screen of each chat or topic, so Telegram never gets more edits
# than one. live_messages: (chat_id, message_id) -> generation of the refresher
# painting that message; a button pressed on it bumps it, which stops that
# refresher wherever it was registered
live_views = {}
live_messages = {}
_live_generation = [0]
_live_views_lock = threading.Lock()


def _stable_id(text):
	"""Short id of a folder or a category for callback_data, the same in every
	run. Handing them out in order of appearance made an old button point at
	another folder after a restart, and move the data there"""
	return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


DIR_IDS_FILE = os.path.join(_config_module.CONFIG_PATH, "dir_ids.json")
_dir_ids_loaded = [False]


def _load_dir_ids():
	"""Folders that had a button before a restart. Some exist nowhere else
	yet (the folder of a new season, offered before anything is in it)"""
	if _dir_ids_loaded[0]:
		return
	_dir_ids_loaded[0] = True
	try:
		with open(DIR_IDS_FILE, "r", encoding="utf-8") as file:
			stored = json.load(file)
		for path in stored if isinstance(stored, list) else []:
			if isinstance(path, str):
				_dir_by_id.setdefault(_stable_id(path), path)
	except FileNotFoundError:
		pass
	except Exception as e:
		warning(f"Cannot read {DIR_IDS_FILE}: {e}")


def _save_dir_ids():
	try:
		os.makedirs(_config_module.CONFIG_PATH, exist_ok=True)
		tmp_file = DIR_IDS_FILE + ".tmp"
		with open(tmp_file, "w", encoding="utf-8") as file:
			json.dump(list(_dir_by_id.values())[-MAX_REMEMBERED_DIRS:], file)
		os.replace(tmp_file, DIR_IDS_FILE)
	except Exception as e:
		warning(f"Cannot write {DIR_IDS_FILE}: {e}")


def get_dir_id(path):
	dir_id = _stable_id(path)
	with _contexts_lock:
		_load_dir_ids()
		if _dir_by_id.get(dir_id) != path:
			_dir_by_id[dir_id] = path
			_save_dir_ids()
	return dir_id


def get_dir_by_id(dir_id):
	"""The folder of a button, None when unknown. The folders that had a
	button are remembered across restarts, and a miss also learns the
	known folders again"""
	with _contexts_lock:
		_load_dir_ids()
		found = _dir_by_id.get(dir_id)
	if found is None:
		for directory in get_known_dirs():
			get_dir_id(directory)
		with _contexts_lock:
			found = _dir_by_id.get(dir_id)
	return found


def get_cat_id(category):
	cat_id = _stable_id(category)
	with _contexts_lock:
		_cat_by_id[cat_id] = category
	return cat_id


def get_cat_by_id(cat_id):
	with _contexts_lock:
		found = _cat_by_id.get(cat_id)
	if found is None:
		for name in [""] + [name for name, _ in get_known_categories()]:
			get_cat_id(name)
		with _contexts_lock:
			found = _cat_by_id.get(cat_id)
	return found


def new_action_context(kind, **data):
	"""Short id (kind + 6 hex) for what a confirmation is about"""
	with _contexts_lock:
		now = time.time()
		for key in [k for k, v in action_contexts.items() if now - v["ts"] > SEARCH_CONTEXT_TTL]:
			del action_contexts[key]
		ctx_id = f"{kind}{uuid.uuid4().hex[:6]}"
		action_contexts[ctx_id] = dict(data, ts=now)
		return ctx_id


def get_action_context(ctx_id, kind):
	"""The data of an action context of that kind, or None when it expired, the
	bot restarted, or ctx_id is something else (a button from an older version)"""
	if not ctx_id or not ctx_id.startswith(kind):
		return None
	with _contexts_lock:
		ctx = action_contexts.get(ctx_id)
		if ctx is None or time.time() - ctx["ts"] > SEARCH_CONTEXT_TTL:
			return None
		return dict(ctx)


def new_search_context(query):
	with _contexts_lock:
		now = time.time()
		for key in [k for k, v in search_contexts.items() if now - v["ts"] > SEARCH_CONTEXT_TTL]:
			del search_contexts[key]
		ctx_id = uuid.uuid4().hex[:6]
		search_contexts[ctx_id] = {"query": query, "ts": now}
		return ctx_id


def get_search_query(ctx_id):
	with _contexts_lock:
		ctx = search_contexts.get(ctx_id)
		if ctx:
			ctx["ts"] = time.time()
			return ctx["query"]
		return None


def new_tracker_context(tracker):
	with _contexts_lock:
		now = time.time()
		for key in [k for k, v in tracker_contexts.items() if now - v["ts"] > SEARCH_CONTEXT_TTL]:
			del tracker_contexts[key]
		ctx_id = uuid.uuid4().hex[:6]
		tracker_contexts[ctx_id] = {"tracker": tracker, "ts": now}
		return ctx_id


def new_category_context(category):
	with _contexts_lock:
		now = time.time()
		for key in [k for k, v in category_contexts.items() if now - v["ts"] > SEARCH_CONTEXT_TTL]:
			del category_contexts[key]
		ctx_id = uuid.uuid4().hex[:6]
		category_contexts[ctx_id] = {"category": category, "ts": now}
		return ctx_id


def get_category_filter(ctx_id):
	with _contexts_lock:
		ctx = category_contexts.get(ctx_id)
		if ctx:
			ctx["ts"] = time.time()
			return ctx["category"]
		return None


def get_tracker_filter(ctx_id):
	with _contexts_lock:
		ctx = tracker_contexts.get(ctx_id)
		if ctx:
			ctx["ts"] = time.time()
			return ctx["tracker"]
		return None


def new_nav_context(torrent_id, filter_key, page):
	"""Short id for the (torrent, filter, page) triplet: the files and move
	screens need extra args too and everything must fit in callback_data"""
	with _contexts_lock:
		now = time.time()
		for key in [k for k, v in nav_contexts.items() if now - v["ts"] > SEARCH_CONTEXT_TTL]:
			del nav_contexts[key]
		for ctx_id, ctx in nav_contexts.items():
			if ctx["torrent_id"] == torrent_id and ctx["filter_key"] == filter_key and ctx["page"] == str(page):
				ctx["ts"] = now
				return ctx_id
		ctx_id = uuid.uuid4().hex[:6]
		nav_contexts[ctx_id] = {"torrent_id": torrent_id, "filter_key": filter_key, "page": str(page), "ts": now}
		return ctx_id


def get_nav_context(ctx_id):
	with _contexts_lock:
		ctx = nav_contexts.get(ctx_id)
		if ctx:
			ctx["ts"] = time.time()
			return ctx["torrent_id"], ctx["filter_key"], ctx["page"]
		return None


def new_page_context(base):
	"""Short id for the callback prefix of a paginated view, so the jump to
	page button always fits in callback_data"""
	with _contexts_lock:
		now = time.time()
		for key in [k for k, v in page_contexts.items() if now - v["ts"] > SEARCH_CONTEXT_TTL]:
			del page_contexts[key]
		for ctx_id, ctx in page_contexts.items():
			if ctx["base"] == base:
				ctx["ts"] = now
				return ctx_id
		ctx_id = uuid.uuid4().hex[:6]
		page_contexts[ctx_id] = {"base": base, "ts": now}
		return ctx_id


def get_page_context(ctx_id):
	with _contexts_lock:
		ctx = page_contexts.get(ctx_id)
		if ctx:
			ctx["ts"] = time.time()
			return ctx["base"]
		return None


def new_pending_torrent(name, magnet=None, data=None):
	with _contexts_lock:
		now = time.time()
		for key in [k for k, v in pending_torrents.items() if now - v["ts"] > SEARCH_CONTEXT_TTL]:
			del pending_torrents[key]
		pending_id = uuid.uuid4().hex[:8]
		pending_torrents[pending_id] = {"name": name, "magnet": magnet, "data": data, "ts": now}
		return pending_id


def get_pending_torrent(pending_id):
	with _contexts_lock:
		return pending_torrents.get(pending_id)


def pop_pending_torrent(pending_id):
	with _contexts_lock:
		return pending_torrents.pop(pending_id, None)


# ---------------------------------------------------------------------------
# FILTER RESOLUTION
# ---------------------------------------------------------------------------

class ExpiredContext(Exception):
	"""What a screen points at is gone. screen is (text, markup) to show in its
	place, None for the generic expired search one"""

	def __init__(self, screen=None):
		super().__init__()
		self.screen = screen


def is_known_filter(filter_key):
	return filter_key in (FILTER_ALL, FILTER_COMPLETED, FILTER_UPLOADING, FILTER_DOWNLOADING_NOW) or filter_key in FILTER_TO_STATUS


def get_filter_label(filter_key):
	if not is_known_filter(filter_key) and filter_key.startswith("q"):
		query = get_search_query(filter_key[1:])
		if query is None:
			raise ExpiredContext()
		return get_text("SEARCH_RESULTS_TITLE", html.escape(query))
	if not is_known_filter(filter_key) and filter_key.startswith("t"):
		tracker = get_tracker_filter(filter_key[1:])
		if tracker is None:
			raise ExpiredContext()
		return get_text("TRACKER_RESULTS_TITLE", html.escape(tracker))
	if not is_known_filter(filter_key) and filter_key.startswith("c"):
		category = get_category_filter(filter_key[1:])
		if category is None:
			raise ExpiredContext()
		return get_text("CATEGORY_RESULTS_TITLE", html.escape(category or get_text("CATEGORY_NONE")))
	if filter_key == FILTER_ALL:
		return get_text("STATUS_ALL")
	if filter_key == FILTER_COMPLETED:
		return get_text("STATUS_COMPLETED")
	if filter_key == FILTER_UPLOADING:
		return f"🔼 {get_text('STATUS_UPLOADING')}"
	if filter_key == FILTER_DOWNLOADING_NOW:
		return f"🔽 {get_text('STATUS_DOWNLOADING_NOW')}"
	status = FILTER_TO_STATUS.get(filter_key)
	emoji = STATUS_EMOJI.get(status, "")
	return f"{emoji} {get_text(STATUS_TEXT_KEY[status])}" if status else get_text("STATUS_ALL")


def get_filtered_torrents(filter_key):
	"""Returns the list of TorrentInfo matching a filter key.
	Raises ExpiredContext if it points to an expired search or tracker filter"""
	if not is_known_filter(filter_key) and filter_key.startswith("q"):
		query = get_search_query(filter_key[1:])
		if query is None:
			raise ExpiredContext()
		return client.get_torrents(query=query)
	if not is_known_filter(filter_key) and filter_key.startswith("t"):
		tracker = get_tracker_filter(filter_key[1:])
		if tracker is None:
			raise ExpiredContext()
		return [t for t in client.get_torrents() if tracker in t.trackers]
	if not is_known_filter(filter_key) and filter_key.startswith("c"):
		# An empty category is a real filter: the torrents with no category at all
		category = get_category_filter(filter_key[1:])
		if category is None:
			raise ExpiredContext()
		return [t for t in client.get_torrents() if t.category == category]
	if filter_key == FILTER_ALL:
		return client.get_torrents()
	if filter_key == FILTER_COMPLETED:
		return [t for t in client.get_torrents() if t.is_finished]
	if filter_key == FILTER_UPLOADING:
		return [t for t in client.get_torrents() if t.upload_rate > 0]
	if filter_key == FILTER_DOWNLOADING_NOW:
		return [t for t in client.get_torrents() if t.download_rate > 0]
	status = FILTER_TO_STATUS.get(filter_key)
	if status is None:
		return client.get_torrents()
	return client.get_torrents(status=status)


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------

def transfer_stats_lines(title, downloaded, uploaded, seconds, starts):
	"""Block of the dashboard with what the manager has transferred in a period,
	nothing when it does not report it. Values below 0 are unknown"""
	if downloaded < 0 and uploaded < 0:
		return []
	lines = ["", get_text(title), get_text("DASHBOARD_STATS_TRANSFER", sizeof_fmt(downloaded), sizeof_fmt(uploaded))]
	extra = []
	if downloaded > 0 and uploaded >= 0:
		extra.append(get_text("DASHBOARD_STATS_RATIO", f"{uploaded / downloaded:.2f}"))
	if seconds >= 0:
		extra.append(get_text("DASHBOARD_STATS_TIME", format_eta(seconds)))
	if starts > 0:
		extra.append(get_text("DASHBOARD_STATS_SESSIONS", starts))
	if extra:
		lines.append(" · ".join(extra))
	return lines


def build_dashboard(refreshing):
	summary = client.get_summary()
	counts = summary.counts

	lines = [get_text("DASHBOARD_TITLE", summary.total), ""]
	for status in (TorrentStatus.DOWNLOADING, TorrentStatus.SEEDING, TorrentStatus.PAUSED,
			TorrentStatus.QUEUED, TorrentStatus.CHECKING, TorrentStatus.ERROR):
		lines.append(f"{STATUS_EMOJI[status]} {get_text(STATUS_TEXT_KEY[status])}: <b>{counts.get(status, 0)}</b>")
		if status == TorrentStatus.DOWNLOADING:
			lines.append(f"🔽 {get_text('STATUS_DOWNLOADING_NOW')}: <b>{summary.downloading}</b>")
		if status == TorrentStatus.SEEDING:
			lines.append(f"🔼 {get_text('STATUS_UPLOADING')}: <b>{summary.uploading}</b>")
	lines.append(f"✅ {get_text('STATUS_COMPLETED')}: <b>{summary.completed}</b>")
	lines.append("")
	lines.append(get_text("DASHBOARD_SPEEDS", sizeof_fmt(summary.download_rate), sizeof_fmt(summary.upload_rate)))
	if summary.free_space >= 0:
		lines.append(get_text("DASHBOARD_FREE_SPACE", sizeof_fmt(summary.free_space)))
	if summary.alt_speed_enabled:
		lines.append(get_text("DASHBOARD_TURTLE_ON"))
	for title, downloaded, uploaded, seconds, starts in (
			("DASHBOARD_STATS_SESSION", summary.session_downloaded, summary.session_uploaded, summary.session_seconds, -1),
			("DASHBOARD_STATS_TOTAL", summary.total_downloaded, summary.total_uploaded, summary.total_seconds, summary.session_count)):
		lines.extend(transfer_stats_lines(title, downloaded, uploaded, seconds, starts))
	lines.append("")
	lines.append(live_footer(refreshing))

	markup = InlineKeyboardMarkup(row_width=2)
	filter_buttons = [
		InlineKeyboardButton(f"📋 {get_text('STATUS_ALL')} ({summary.total})", callback_data=build_call("list", FILTER_ALL, 0)),
		InlineKeyboardButton(f"✅ {get_text('STATUS_COMPLETED')} ({summary.completed})", callback_data=build_call("list", FILTER_COMPLETED, 0)),
		InlineKeyboardButton(f"📥 {get_text('STATUS_DOWNLOADING')} ({counts.get(TorrentStatus.DOWNLOADING, 0)})", callback_data=build_call("list", FILTER_DOWNLOADING, 0)),
		InlineKeyboardButton(f"🔽 {get_text('STATUS_DOWNLOADING_NOW')} ({summary.downloading})", callback_data=build_call("list", FILTER_DOWNLOADING_NOW, 0)),
		InlineKeyboardButton(f"🌱 {get_text('STATUS_SEEDING')} ({counts.get(TorrentStatus.SEEDING, 0)})", callback_data=build_call("list", FILTER_SEEDING, 0)),
		InlineKeyboardButton(f"🔼 {get_text('STATUS_UPLOADING')} ({summary.uploading})", callback_data=build_call("list", FILTER_UPLOADING, 0)),
		InlineKeyboardButton(f"⏸️ {get_text('STATUS_PAUSED')} ({counts.get(TorrentStatus.PAUSED, 0)})", callback_data=build_call("list", FILTER_PAUSED, 0)),
		InlineKeyboardButton(f"⏳ {get_text('STATUS_QUEUED')} ({counts.get(TorrentStatus.QUEUED, 0)})", callback_data=build_call("list", FILTER_QUEUED, 0)),
		InlineKeyboardButton(f"🔍 {get_text('STATUS_CHECKING')} ({counts.get(TorrentStatus.CHECKING, 0)})", callback_data=build_call("list", FILTER_CHECKING, 0)),
		InlineKeyboardButton(f"❌ {get_text('STATUS_ERROR')} ({counts.get(TorrentStatus.ERROR, 0)})", callback_data=build_call("list", FILTER_ERROR, 0)),
	]
	markup.add(*filter_buttons)
	markup.add(
		InlineKeyboardButton(get_text("BUTTON_SEARCH"), callback_data=build_call("search")),
		InlineKeyboardButton(get_text("BUTTON_TRACKERS"), callback_data=build_call("trackers")),
	)
	if client.supports_categories:
		markup.add(InlineKeyboardButton(get_text("BUTTON_CATEGORIES"), callback_data=build_call("categories")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_SETTINGS"), callback_data=build_call("settings")))
	bottom = []
	if not refreshing:
		bottom.append(InlineKeyboardButton(get_text("BUTTON_UPDATE"), callback_data=build_call("refreshDashboard")))
	bottom.append(InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")))
	markup.add(*bottom)

	return "\n".join(lines), markup


def show_dashboard(chat_id, message_id=None, thread_id=None):
	"""Sends (or edits) the dashboard and starts the auto-refresh cycle"""
	try:
		text, markup = build_dashboard(refreshing=True)
	except TorrentClientError as e:
		text = client_error_text(e)
		if message_id:
			edit_message(chat_id, message_id, text)
		else:
			send_message(chat_id, text, thread_id=thread_id)
		return

	if message_id:
		edit_message(chat_id, message_id, text, markup)
	else:
		sent = send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)
		if not sent:
			return
		message_id = sent.message_id
	start_live_view(chat_id, message_id, thread_id, build_dashboard, build_call("refreshDashboard"))


# ---------------------------------------------------------------------------
# LIVE SCREENS
# ---------------------------------------------------------------------------

def live_footer(refreshing):
	if refreshing:
		return get_text("DASHBOARD_AUTOUPDATING")
	return get_text("DASHBOARD_LAST_UPDATE", datetime.now().strftime("%H:%M:%S"))


def live_buttons(refreshing, refresh_call, *buttons):
	"""Bottom row of a live screen: the update button only shows up once it has
	stopped repainting itself, on the left of the rest"""
	if refreshing:
		return list(buttons)
	return [InlineKeyboardButton(get_text("BUTTON_UPDATE"), callback_data=refresh_call)] + list(buttons)


def _live_status(key, chat_id, message_id, generation):
	"""live, stopped (a button of this message was pressed, or a newer
	refresher took the message over) or replaced (another message became the
	live screen of the chat or topic)"""
	if live_messages.get((chat_id, message_id)) != generation:
		return "stopped"
	state = live_views.get(key)
	if state and state["message_id"] != message_id:
		return "replaced"
	return "live"


def client_error_text(error):
	"""What a failure of the torrent manager says: a refusal is not a
	connection problem, and a manager that has to be restarted says so"""
	if isinstance(error, RestartRequiredError):
		return get_text("MANAGER_RESTART_REQUIRED", html.escape(str(error)))
	if isinstance(error, PermanentTorrentError):
		return get_text("ACTION_REFUSED", html.escape(str(error)))
	return get_text("CONNECTION_ERROR", html.escape(str(error)))


def live_error_screen(error, refresh_call=None):
	"""What a live screen shows when the last repaint cannot be built: the
	error and the button to try again, instead of being left saying it
	updates itself"""
	if isinstance(error, TorrentClientError):
		text = client_error_text(error)
	else:
		text = get_text("ERROR_GENERIC", html.escape(str(error)))
	markup = InlineKeyboardMarkup()
	row = [InlineKeyboardButton(get_text("BUTTON_UPDATE"), callback_data=refresh_call)] if refresh_call else []
	row.append(InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")))
	markup.row(*row)
	return text, markup


def _live_refresher(chat_id, thread_id, message_id, generation, build, refresh_call=None):
	key = (chat_id, thread_id)
	last = None

	def paint(refreshing):
		"""Builds outside the lock (the client can be slow) but edits inside it,
		so a button pressed meanwhile can never be painted over with this screen.
		Returns False when the screen is no longer live"""
		nonlocal last
		gone = False
		try:
			text, markup = build(refreshing=refreshing)
		except ExpiredContext as e:
			# What the screen showed is gone: it says so, instead of being
			# left behind still saying it updates itself
			text, markup = e.screen or (get_text("SEARCH_EXPIRED"), back_close_markup())
			gone = True
		except Exception as e:
			warning(f"Live screen refresh failed: {e}")
			if refreshing:
				return True
			text, markup = live_error_screen(e, refresh_call)
		with _live_views_lock:
			status = _live_status(key, chat_id, message_id, generation)
			if status == "stopped" or (refreshing and status == "replaced"):
				return False
			# An identical screen is not sent: it is a wasted call to Telegram
			current = (text, markup.to_json() if markup else None)
			if current != last:
				edit_message(chat_id, message_id, text, markup)
				last = current
		return not gone

	try:
		iterations = max(1, DASHBOARD_REFRESH_DURATION // DASHBOARD_REFRESH_SECONDS)
		for _ in range(iterations):
			time.sleep(DASHBOARD_REFRESH_SECONDS)
			with _live_views_lock:
				status = _live_status(key, chat_id, message_id, generation)
			if status == "stopped":
				return
			# A screen left behind by a newer one stops right away, with its update button
			if status == "replaced" or not paint(refreshing=True):
				break
		with _live_views_lock:
			if _live_status(key, chat_id, message_id, generation) == "stopped":
				return
		paint(refreshing=False)
	finally:
		with _live_views_lock:
			if live_messages.get((chat_id, message_id)) in (generation, None):
				live_messages.pop((chat_id, message_id), None)


def start_live_view(chat_id, message_id, thread_id, build, refresh_call=None):
	"""Starts repainting a screen already painted with build(refreshing=True).
	It becomes the live screen of its chat or topic, replacing the previous
	one. refresh_call repaints it by hand, offered if the last repaint fails"""
	key = (chat_id, normalize_thread(thread_id))
	with _live_views_lock:
		_live_generation[0] += 1
		generation = _live_generation[0]
		live_views[key] = {"message_id": message_id, "generation": generation}
		live_messages[(chat_id, message_id)] = generation
	threading.Thread(target=_live_refresher, args=(chat_id, key[1], message_id, generation, build, refresh_call), daemon=True).start()


def stop_live_view(chat_id, message_id, thread_id=None):
	"""Stops the refresher of that message, whatever chat or topic it was
	registered under and whether or not a newer screen replaced it: a button
	pressed on a message means what it shows is about to change"""
	with _live_views_lock:
		if (chat_id, message_id) in live_messages:
			live_messages[(chat_id, message_id)] = None


# ---------------------------------------------------------------------------
# TORRENT LIST
# ---------------------------------------------------------------------------

def list_tracker(filter_key):
	"""The tracker of a tracker filter, None for any other filter or an expired one"""
	if is_known_filter(filter_key) or not filter_key.startswith("t"):
		return None
	return get_tracker_filter(filter_key[1:])


# Orders of the torrent lists: (callback code, value of a torrent, natural
# order is the highest first). A value of None is unknown and always goes last
LIST_SORTS = {
	"added": ("a", lambda t: t.added_date, True),
	"name": ("n", lambda t: (t.name or "").casefold(), False),
	"size": ("s", lambda t: t.total_size or 0, True),
	"progress": ("p", lambda t: t.progress, True),
	"ratio": ("r", lambda t: t.ratio, True),
	"speed": ("v", lambda t: (t.download_rate or 0) + (t.upload_rate or 0), True),
	"activity": ("t", lambda t: t.last_activity, True),
}
LIST_SORT_BY_CODE = {code: key for key, (code, _, _) in LIST_SORTS.items()}


def available_sorts():
	"""The orders the torrent manager can give, the last activity is not in every listing"""
	return [key for key in LIST_SORTS if key != "activity" or client.reports_last_activity]


def current_sort():
	key = bot_settings.get("list_sort")
	if key not in available_sorts():
		key = "added"
	return key, bool(bot_settings.get("list_sort_reverse"))


def sort_label(key, reverse, short=False):
	"""The short one fits in half a row, next to the actions button of the list"""
	return get_text(f"SORT_{key.upper()}{'_REVERSE' if reverse else ''}{'_SHORT' if short else ''}")


def sort_torrents(torrents):
	"""Torrents in the order chosen for the lists. Ties keep the order of the
	manager's listing, the newest first"""
	key, reverse = current_sort()
	_, value, highest_first = LIST_SORTS[key]
	known = [t for t in torrents if value(t) is not None]
	unknown = [t for t in torrents if value(t) is None]
	return sorted(known, key=value, reverse=highest_first != reverse) + unknown


def build_sort_menu(filter_key):
	key, reverse = current_sort()
	text = get_text("SORT_TITLE", sort_label(key, reverse))
	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(*[InlineKeyboardButton(f"{'✅ ' if option == key else ''}{sort_label(option, reverse if option == key else False, short=True)}",
									callback_data=build_call("sortSet", filter_key, LIST_SORTS[option][0]))
				for option in available_sorts()])
	markup.row(InlineKeyboardButton(get_text("BUTTON_SORT_REVERSE"), callback_data=build_call("sortSet", filter_key, "x")))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("list", filter_key, 0)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return text, markup


def set_sort(code):
	"""Applies a button of the sort menu: an order, or turning the current one around"""
	key, reverse = current_sort()
	if code == "x":
		bot_settings.set("list_sort_reverse", not reverse)
		return
	chosen = LIST_SORT_BY_CODE.get(code)
	if chosen in available_sorts() and chosen != key:
		bot_settings.set("list_sort", chosen)
		bot_settings.set("list_sort_reverse", False)


def build_list(filter_key, page, refreshing=False, order=None):
	"""order is {torrent id: position} shared by the paints of one live list:
	the first one fills it and the rest keep the torrents in those places (new
	ones go last), so a list sorted by speed or activity does not reshuffle
	every two seconds under the finger about to tap one"""
	torrents = sort_torrents(get_filtered_torrents(filter_key))
	if order is not None:
		if order:
			torrents.sort(key=lambda t: order.get(t.id, len(order)))
		else:
			order.update({t.id: position for position, t in enumerate(torrents)})
	total = len(torrents)
	pages = max(1, math.ceil(total / TORRENTS_PER_PAGE))
	page = max(0, min(int(page), pages - 1))
	label = get_filter_label(filter_key)

	text = get_text("LIST_TITLE", label, total, page + 1, pages)
	if total > 0:
		text += "\n" + get_text("LIST_SIZE", sizeof_fmt(sum(t.total_size or 0 for t in torrents)))
	if total == 0:
		text += f"\n\n{get_text('LIST_EMPTY')}"
	text += f"\n\n{live_footer(refreshing)}"

	markup = InlineKeyboardMarkup(row_width=1)
	start = page * TORRENTS_PER_PAGE
	for torrent in torrents[start:start + TORRENTS_PER_PAGE]:
		emoji = STATUS_EMOJI.get(torrent.status, "")
		progress = "" if torrent.is_finished else f" {torrent.progress:.0f}%"
		markup.add(InlineKeyboardButton(
			f"{emoji}{progress} {truncate(torrent.name)}",
			callback_data=build_call("info", torrent.id, filter_key, page)))

	if pages > 1:
		markup.row(*pagination_row(page, pages, "list", filter_key))

	# What acts on every torrent of the list waits in a menu of its own: it is
	# seldom used and would otherwise take three rows of every list
	tools = []
	if total > 1:
		tools.append(InlineKeyboardButton(get_text("BUTTON_SORT", sort_label(*current_sort(), short=True)), callback_data=build_call("sort", filter_key)))
	if mass_menu_rows(filter_key, total):
		tools.append(InlineKeyboardButton(get_text("BUTTON_MASS_MENU"), callback_data=build_call("massMenu", filter_key, page)))
	if tools:
		markup.row(*tools)

	markup.row(*live_buttons(refreshing, build_call("list", filter_key, page),
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("dashboard")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	))
	return text, markup


def mass_menu_rows(filter_key, total):
	"""Buttons of the actions on every torrent of a list, empty when there is none"""
	rows = []
	if total > 0:
		rows.append([
			InlineKeyboardButton(get_text("BUTTON_MASS_RESUME"), callback_data=build_call("mass", "resume", filter_key)),
			InlineKeyboardButton(get_text("BUTTON_MASS_PAUSE"), callback_data=build_call("mass", "pause", filter_key)),
		])
		rows.append([InlineKeyboardButton(get_text("BUTTON_MASS_MOVE"), callback_data=build_call("mass", "move", filter_key))])
		if client.supports_categories:
			# Where the folders go by category, the category is what is changed, not the folder
			rows[-1].append(InlineKeyboardButton(get_text("BUTTON_MASS_CATEGORY"), callback_data=build_call("mass", "cat", filter_key)))
	# On a tracker filter the limits of the tracker take the place of the ratio
	# of all: they cover the torrents there are now and the ones that arrive
	tracker = list_tracker(filter_key) if tracker_limits_supported() else None
	if total > 0 and (client.supports_ratio_limit or client.supports_torrent_speed_limit) and not tracker:
		# "ratio" is the name the limits of all had when they were only the ratio:
		# kept, so the buttons already in the chats still work
		rows.append([InlineKeyboardButton(get_text("BUTTON_MASS_LIMITS"), callback_data=build_call("mass", "ratio", filter_key))])
	if tracker:
		rows.append([InlineKeyboardButton(get_text("BUTTON_TRACKER_LIMITS_OF", "📌" if tracker_limits.get_rule(tracker) else "➕"),
										callback_data=build_call("tlRule", tracker_limits.tracker_key(tracker), filter_key))])
	if total > 0:
		rows.append([InlineKeyboardButton(get_text("BUTTON_MASS_DELETE"), callback_data=build_call("mass", "delete", filter_key))])
	return rows


def build_mass_menu(filter_key, page):
	total = len(get_filtered_torrents(filter_key))
	text = get_text("MASS_MENU_TITLE", get_filter_label(filter_key), total)
	markup = InlineKeyboardMarkup()
	for row in mass_menu_rows(filter_key, total):
		markup.row(*row)
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("list", filter_key, page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return text, markup


def render_list(chat_id, message_id, filter_key, page, thread_id=None):
	live = True
	order = {}
	try:
		text, markup = build_list(filter_key, page, refreshing=True, order=order)
	except ExpiredContext:
		text = get_text("SEARCH_EXPIRED")
		markup = back_close_markup()
		live = False
	except TorrentClientError as e:
		text = client_error_text(e)
		markup = back_close_markup()
		live = False
	if message_id:
		edit_message(chat_id, message_id, text, markup)
	else:
		sent = send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)
		message_id = sent.message_id if sent else None
	if live and message_id:
		start_live_view(chat_id, message_id, thread_id, lambda refreshing: build_list(filter_key, page, refreshing, order),
						build_call("list", filter_key, page))


def back_close_markup(back_call=None):
	markup = InlineKeyboardMarkup()
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=back_call or build_call("dashboard")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return markup


def close_markup():
	markup = InlineKeyboardMarkup()
	markup.row(InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")))
	return markup


# ---------------------------------------------------------------------------
# TORRENT DETAIL
# ---------------------------------------------------------------------------

def build_detail(torrent_id, filter_key, page, refreshing=False):
	torrent = client.get_torrent(torrent_id)
	if torrent is None:
		return get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page))

	emoji = STATUS_EMOJI.get(torrent.status, "")
	status_label = get_text(STATUS_TEXT_KEY.get(torrent.status, "STATUS_ALL"))
	lines = [f"<b>{html.escape(torrent.name)}</b>", ""]
	status_text = f"{get_text('INFO_STATUS')}: {emoji} {status_label}"
	idle = torrent.status in (TorrentStatus.DOWNLOADING, TorrentStatus.SEEDING) and not torrent.download_rate and not torrent.upload_rate
	if idle and torrent.last_activity:
		status_text += f" · {get_text('INFO_IDLE', format_eta(max(int(time.time() - torrent.last_activity), 0)))}"
	lines.append(status_text)
	progress_text = f"{get_text('INFO_PROGRESS')}: {torrent.progress:.1f}%"
	if torrent.status == TorrentStatus.DOWNLOADING and torrent.availability >= 0:
		progress_text += f" · {get_text('INFO_AVAILABILITY', f'{round(torrent.availability, 2):g}')}"
	lines.append(progress_text)
	lines.append(f"{get_text('INFO_SIZE')}: {sizeof_fmt(torrent.total_size)}")
	down_cap = f" · {get_text('INFO_SPEED_LIMIT', speed_limit_text(torrent.download_limit))}" if torrent.download_limit > 0 else ""
	up_cap = f" · {get_text('INFO_SPEED_LIMIT', speed_limit_text(torrent.upload_limit))}" if torrent.upload_limit > 0 else ""
	lines.append(f"{get_text('INFO_DOWNLOADED')}: {sizeof_fmt(torrent.downloaded)} (🔽 {sizeof_fmt(torrent.download_rate)}/s{down_cap})")
	lines.append(f"{get_text('INFO_UPLOADED')}: {sizeof_fmt(torrent.uploaded)} (🔼 {sizeof_fmt(torrent.upload_rate)}/s{up_cap})")
	ratio_text = f"{get_text('INFO_RATIO')}: {torrent.ratio}"
	if torrent.ratio_limit >= 0:
		ratio_text += f" {get_text('INFO_RATIO_LIMIT', f'{torrent.ratio_limit:g}')}"
	lines.append(ratio_text)
	if torrent.status == TorrentStatus.DOWNLOADING:
		lines.append(f"{get_text('INFO_ETA')}: {format_eta(torrent.eta)}")
	# While seeding, the eta is the time left until the seeding limit stops it
	seeding_left = torrent.eta if torrent.status == TorrentStatus.SEEDING else -1
	if torrent.seeding_time > 0 or seeding_left >= 0:
		seeding_text = f"{get_text('INFO_SEEDING_TIME')}: {format_eta(torrent.seeding_time)}"
		if torrent.seeding_time_limit > 0:
			seeding_text += f" {get_text('INFO_SEEDING_TIME_LIMIT', format_eta(torrent.seeding_time_limit))}"
		if seeding_left >= 0:
			seeding_text += f" {get_text('INFO_SEEDING_LEFT', format_eta(seeding_left))}"
		lines.append(seeding_text)
	if torrent.swarm_seeders >= 0 and torrent.swarm_leechers >= 0:
		# The swarm can never be smaller than what we are connected to
		peers_detail = get_text("INFO_PEERS_DETAIL_SWARM",
			torrent.seeders, max(torrent.swarm_seeders, torrent.seeders),
			torrent.leechers, max(torrent.swarm_leechers, torrent.leechers))
	else:
		peers_detail = get_text("INFO_PEERS_DETAIL", torrent.seeders, torrent.leechers)
	lines.append(f"{get_text('INFO_PEERS')}: {torrent.peers} {peers_detail}")
	if torrent.trackers:
		tracker_text = html.escape(torrent.trackers[0])
		if len(torrent.trackers) > 1:
			tracker_text += f" {get_text('INFO_TRACKER_AND_MORE', len(torrent.trackers) - 1)}"
		if torrent.private:
			tracker_text += f" · {get_text('INFO_PRIVATE')}"
		# Transmission also turns it into the error of the torrent, shown below
		if torrent.tracker_error and torrent.tracker_error != torrent.error_message:
			tracker_text += f" · ⚠️ <code>{html.escape(torrent.tracker_error)}</code>"
		lines.append(f"{get_text('INFO_TRACKER')}: {tracker_text}")
	if client.supports_categories:
		category_text = html.escape(torrent.category) if torrent.category else get_text("CATEGORY_NONE")
		lines.append(f"{get_text('INFO_CATEGORY')}: {category_text}")
	lines.append(f"{get_text('INFO_DIR')}: <code>{html.escape(torrent.download_dir)}</code>")
	if client.supports_categories and torrent.auto_managed:
		lines.append(get_text("INFO_AUTO_MANAGED"))
	elif client.supports_categories and category_save_path(torrent.category) not in ("", torrent.download_dir.rstrip("/")):
		lines.append(get_text("INFO_CATEGORY_IGNORED"))
	if torrent.added_date:
		added = datetime.fromtimestamp(torrent.added_date).strftime("%Y-%m-%d %H:%M")
		added_text = f"{get_text('INFO_ADDED')}: {added}"
		if torrent.completed_date:
			completed = datetime.fromtimestamp(torrent.completed_date).strftime("%Y-%m-%d %H:%M")
			added_text += f" · {get_text('INFO_COMPLETED', completed)}"
		lines.append(added_text)
	if torrent.error_message:
		lines.append(f"{get_text('INFO_ERROR')}: <code>{html.escape(torrent.error_message)}</code>")
	show_files = has_browsable_files(torrent)
	if show_files:
		lines.append("")
		lines.append(f"<b>{get_text('INFO_FILES')} ({len(torrent.files)}):</b>")
		for path, size, _ in torrent.files[:10]:
			skipped = " ⏭️" if torrent.file_priorities.get(path) == FILE_SKIP else ""
			lines.append(f"• <code>{html.escape(path)}</code> ({sizeof_fmt(size)}){skipped}")
		if len(torrent.files) > 10:
			lines.append(get_text("INFO_AND_MORE_FILES", len(torrent.files) - 10))
	lines.append("")
	lines.append(live_footer(refreshing))

	markup = InlineKeyboardMarkup()
	if torrent.status == TorrentStatus.PAUSED:
		toggle = InlineKeyboardButton(get_text("BUTTON_RESUME"), callback_data=build_call("resume", torrent.id, filter_key, page))
	else:
		toggle = InlineKeyboardButton(get_text("BUTTON_PAUSE"), callback_data=build_call("pause", torrent.id, filter_key, page))
	first_row = [toggle]
	if client.supports_verify:
		first_row.append(InlineKeyboardButton(get_text("BUTTON_VERIFY"), callback_data=build_call("verify", torrent.id, filter_key, page)))
	# Only an active torrent announces, and only if it has a tracker to ask
	if client.supports_reannounce and torrent.trackers and torrent.status in (TorrentStatus.DOWNLOADING, TorrentStatus.SEEDING):
		first_row.append(InlineKeyboardButton(get_text("BUTTON_REANNOUNCE"), callback_data=build_call("reannounce", torrent.id, filter_key, page)))
	markup.row(*first_row)
	action_row = []
	if client.supports_rename:
		action_row.append(InlineKeyboardButton(get_text(rename_key(torrent, "BUTTON_RENAME")), callback_data=build_call("rename", torrent.id, filter_key, page)))
	# An auto managed torrent takes its directory from its category, and moving
	# it by hand is what drops it out of automatic management: the category is
	# the only control offered for it, with the move as an informed way out
	if not (client.supports_categories and torrent.auto_managed):
		action_row.append(InlineKeyboardButton(get_text("BUTTON_MOVE"), callback_data=build_call("move", torrent.id, filter_key, page)))
	if action_row:
		markup.row(*action_row)
	limits_row = []
	if client.supports_categories:
		cat_ctx = new_nav_context(torrent.id, filter_key, page)
		limits_row.append(InlineKeyboardButton(get_text("BUTTON_CATEGORY"), callback_data=build_call("catMenu", cat_ctx)))
	if client.supports_ratio_limit or client.supports_torrent_speed_limit:
		ratio_ctx = new_nav_context(torrent.id, filter_key, page)
		limits_row.append(InlineKeyboardButton(get_text("BUTTON_RATIO_LIMIT"), callback_data=build_call("ratioMenu", ratio_ctx)))
	if limits_row:
		markup.row(*limits_row)
	# The file screen is where folders and files are renamed one by one and
	# where the files to download are chosen
	if show_files and (client.supports_rename or client.file_priority_levels):
		files_ctx = new_nav_context(torrent.id, filter_key, page)
		folders = torrent_folders(torrent)
		root_ref = folder_ref(folders, folders.index(torrent_root(torrent)))
		markup.row(InlineKeyboardButton(get_text("BUTTON_FILES"), callback_data=build_call("files", files_ctx, root_ref, 0)))
	markup.row(InlineKeyboardButton(get_text("BUTTON_DELETE"), callback_data=build_call("delete", torrent.id, filter_key, page)))
	markup.row(*live_buttons(refreshing, build_call("info", torrent.id, filter_key, page),
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("list", filter_key, page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	))
	return "\n".join(lines), markup


def render_detail(chat_id, message_id, torrent_id, filter_key, page, thread_id=None):
	try:
		text, markup = build_detail(torrent_id, filter_key, page, refreshing=True)
	except TorrentClientError as e:
		text = client_error_text(e)
		markup = back_close_markup(build_call("list", filter_key, page))
		edit_message(chat_id, message_id, text, markup)
		return
	edit_message(chat_id, message_id, text, markup)
	start_live_view(chat_id, message_id, thread_id, lambda refreshing: build_detail(torrent_id, filter_key, page, refreshing),
					build_call("info", torrent_id, filter_key, page))


# ---------------------------------------------------------------------------
# TORRENT FILES
# ---------------------------------------------------------------------------

def file_basename(path):
	return path.rsplit("/", 1)[-1]


def file_folder(path):
	return path.rsplit("/", 1)[0] if "/" in path else ""


def file_extension(path):
	name = file_basename(path)
	return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_video_file(path):
	return file_extension(path) in VALID_EXTENSIONS


def is_subtitle_file(path):
	return file_extension(path) in SUBTITLE_EXTENSIONS


def torrent_is_folder(torrent):
	"""True when the torrent keeps its files inside a folder. Renaming such a
	torrent only renames that folder, never the files inside"""
	return any("/" in path for path, _, _ in torrent.files)


def has_browsable_files(torrent):
	"""Single file torrents are renamed through the torrent itself, so there is
	nothing to browse in their file list"""
	return bool(torrent.files) and (len(torrent.files) > 1 or torrent_is_folder(torrent))


def rename_key(torrent, key):
	"""Picks the folder wording of a rename string when the torrent is a folder"""
	return f"{key}_FOLDER" if torrent_is_folder(torrent) else key


def torrent_root(torrent):
	"""Folder holding the whole torrent, or an empty string when its files sit
	at the top level. Renaming it is renaming the torrent"""
	return content_root(path for path, _, _ in torrent.files)


def torrent_folders(torrent):
	"""Every folder of the torrent sorted by path, with the top level ("")
	first. The browsing screens point at a folder by its index here because a
	path does not fit in callback_data. Renaming a folder never moves the index
	of the folder holding it, nor of a folder whose contents were renamed: they
	only change paths sorting after them"""
	folders = set()
	for path, _, _ in torrent.files:
		folder = file_folder(path)
		while folder:
			folders.add(folder)
			folder = file_folder(folder)
	return [""] + sorted(folders, key=str.lower)


def is_inside(path, folder):
	return not folder or path.startswith(folder + "/")


def folder_key(folder):
	"""Short hash of a folder path, carried with its index by the buttons"""
	return hashlib.sha1(folder.encode("utf-8")).hexdigest()[:4]


def folder_ref(folders, index):
	"""How a button points at folders[index]: "index.key". A rename elsewhere
	in the torrent can move a folder in the sorted list, and the index alone
	would then name another one"""
	return f"{index}.{folder_key(folders[index])}"


def get_torrent_folder(torrent, folder_index, strict=False):
	"""Folder a button points at, or the torrent folder when the index is out
	of range or points above it (the top level only holds the torrent folder).
	A button that carries the folder's key follows it if it moved, and when it
	is gone, strict (a button that acts on the folder) gives None instead"""
	root = torrent_root(torrent)
	folders = torrent_folders(torrent)
	index_text, _, key = str(folder_index).partition(".")
	try:
		folder = folders[int(index_text)]
	except (IndexError, ValueError):
		folder = None
	if key:
		if folder is None or folder_key(folder) != key:
			folder = next((f for f in folders if folder_key(f) == key), None)
		if folder is None:
			return None if strict else root
	elif folder is None:
		return root
	return folder if is_inside(folder, root) or folder == root else root


def folder_entries(torrent, folder):
	"""(subfolder paths, file indexes) directly inside folder"""
	subfolders = [f for f in torrent_folders(torrent)[1:] if file_folder(f) == folder]
	files = [i for i, (path, _, _) in enumerate(torrent.files) if file_folder(path) == folder]
	return subfolders, files


def single_video(torrent):
	"""Whether the torrent holds one video, its sample clips aside"""
	return sum(1 for path, _, _ in torrent.files if is_video_file(path) and not is_sample_file(path)) == 1


def suggest_for_file(torrent, path, one_video=None, root=None):
	"""Suggestion for one file of the torrent, using the torrent name and the
	folder holding it (a season folder) as context. Returns None when nothing
	can be suggested, and always for a sample clip, which would otherwise take
	the name of the real video. one_video and root, when given, spare the scan
	of every file that a plan over thousands of files repeated for each one"""
	if not is_video_file(path) or is_sample_file(path):
		return None
	if one_video is None:
		one_video = single_video(torrent)
	if root is None:
		root = torrent_root(torrent)
	folder = file_folder(path)
	folder_name = file_basename(folder) if folder and folder != root else None
	suggested = parse_file_name(file_basename(path), torrent.name, one_video, folder_name)
	if not suggested or suggested == file_basename(path):
		return None
	return suggested


def suggest_for_folder(torrent, folder):
	"""Suggestion for a folder of the torrent. The torrent folder takes the
	suggestion of the torrent, exactly like the rename button of the torrent.
	Returns None when nothing can be suggested"""
	if not folder:
		return None
	if folder == torrent_root(torrent):
		suggested = parse_name(torrent.name)
		return suggested if suggested and suggested not in (torrent.name, folder) else None
	suggested = parse_folder_name(file_basename(folder), torrent.name)
	if not suggested or suggested == file_basename(folder):
		return None
	return suggested


def subtitles_by_folder(torrent):
	"""{folder: subtitle paths in it}"""
	subtitles = {}
	for path, _, _ in torrent.files:
		if is_subtitle_file(path):
			subtitles.setdefault(file_folder(path), []).append(path)
	return subtitles


def subtitle_renames(torrent, video_path, new_name, subtitles=None):
	"""Renames needed so the subtitles of video_path keep matching it.
	subtitles is subtitles_by_folder(torrent), when already worked out"""
	renames = []
	folder = file_folder(video_path)
	video_file = file_basename(video_path)
	videos = [path for path, _, _ in torrent.files if file_folder(path) == folder and is_video_file(path)]
	candidates = (subtitles if subtitles is not None else subtitles_by_folder(torrent)).get(folder, [])
	for path in candidates:
		if path == video_path:
			continue
		companion = companion_subtitle_name(file_basename(path), video_file, new_name)
		if companion and companion != file_basename(path) and subtitle_owner(path, videos) == video_path:
			renames.append((path, companion))
	return renames


def subtitle_owner(subtitle_path, videos):
	"""The video a subtitle goes with when several of its folder would take
	it: the one with the longest name ('Alien Resurrection.es.srt' is the
	one of 'Alien Resurrection.mkv', not of 'Alien.mkv'), the first one on a
	tie ('Ep.mkv' and 'Ep.mp4'), so a subtitle is only ever renamed once"""
	subtitle = file_basename(subtitle_path)
	owners = [video for video in videos if companion_subtitle_name(subtitle, file_basename(video), "x") is not None]
	return max(owners, key=lambda video: len(file_basename(video).rpartition(".")[0]), default=None)


def build_rename_plan(torrent, only_path=None, within=""):
	"""List of (old_path, new_name) for the videos of the torrent inside the
	folder within (or just only_path) plus their subtitles. Also returns the
	names that collide"""
	plan = []
	taken = {file_basename(p) for p, _, _ in torrent.files}
	collisions = []
	one_video = single_video(torrent)
	root = torrent_root(torrent)
	subtitles = subtitles_by_folder(torrent)
	for path, _, _ in torrent.files:
		if only_path and path != only_path:
			continue
		if not is_inside(path, within):
			continue
		suggested = suggest_for_file(torrent, path, one_video, root)
		if not suggested:
			continue
		taken.discard(file_basename(path))
		if suggested in taken:
			collisions.append(suggested)
			taken.add(file_basename(path))
			continue
		taken.add(suggested)
		plan.append((path, suggested))
		plan.extend(subtitle_renames(torrent, path, suggested, subtitles))
	return plan, collisions


def build_folder_plan(torrent, within=""):
	"""List of (old_path, new_name) for the folders below within, never within
	itself nor the torrent folder (that one is renamed with the torrent).
	Deepest first: a folder rename changes the path of everything under it, so
	going upwards every rename still finds the path it was planned with. Also
	returns the names that collide with something next to them"""
	root = torrent_root(torrent)
	folders = torrent_folders(torrent)[1:]
	taken = {}  # parent folder -> names in use inside it
	for path in folders + [p for p, _, _ in torrent.files]:
		taken.setdefault(file_folder(path), set()).add(file_basename(path))
	plan = []
	collisions = []
	for folder in folders:
		if folder in (root, within) or not is_inside(folder, within):
			continue
		suggested = suggest_for_folder(torrent, folder)
		if not suggested:
			continue
		siblings = taken[file_folder(folder)]
		siblings.discard(file_basename(folder))
		if suggested in siblings:
			collisions.append(suggested)
			siblings.add(file_basename(folder))
			continue
		siblings.add(suggested)
		plan.append((folder, suggested))
	plan.sort(key=lambda item: item[0].count("/"), reverse=True)
	return plan, collisions


def build_full_plan(torrent, within=""):
	"""Files and folders below within that have a suggestion: (file plan,
	folder plan, number of names skipped because they collide)"""
	files, file_collisions = build_rename_plan(torrent, within=within)
	folders, folder_collisions = build_folder_plan(torrent, within)
	return files, folders, len(file_collisions) + len(folder_collisions)


def file_rename_ok_text(new_name, done):
	"""Result of renaming one file: done counts the file itself plus the
	subtitles renamed along with it"""
	subtitles = max(done - 1, 0)
	if subtitles:
		return get_text("FILE_RENAME_OK_SUBTITLES", html.escape(new_name), subtitles)
	return get_text("FILE_RENAME_OK", html.escape(new_name))


def apply_rename_plan(torrent_id, plan, rename=None):
	"""Applies the renames one by one with rename (files by default).
	Returns (done, errors)"""
	rename = rename or client.rename_file
	done = 0
	errors = []
	for old_path, new_name in plan:
		try:
			rename(torrent_id, old_path, new_name)
			done += 1
		except TorrentClientError as e:
			errors.append(str(e))
	return done, errors


def apply_full_plan(torrent_id, files, folders):
	"""Files first and then the folders, deepest first: every rename uses a
	path none of the renames before it has changed.
	Returns (files done, folders done, errors)"""
	files_done, errors = apply_rename_plan(torrent_id, files)
	folders_done, folder_errors = apply_rename_plan(torrent_id, folders, client.rename_folder)
	return files_done, folders_done, errors + folder_errors


def rename_count_text(files, folders):
	"""'3 folder(s) and 40 file(s)', or just the files when no folder is involved"""
	if folders:
		return get_text("RENAME_COUNT_FOLDERS", folders, files)
	return get_text("RENAME_COUNT_FILES", files)


def rename_result_text(files_done, folders_done, errors):
	if errors:
		return get_text("FILES_RENAME_PARTIAL", files_done + folders_done, len(errors), html.escape(errors[0]))
	return get_text("FILES_RENAME_OK", rename_count_text(files_done, folders_done))


# Whether a file is downloaded and how eagerly are two separate things on
# screen, though the manager keeps both in its priority: leaving a file out is
# a switch, and the priority is only offered for the files downloaded.
# Codes of the buttons in callback_data: "d" downloads what was left out
# (with the normal priority) and "s" leaves it out
FILE_PRIORITY_CODES = {FILE_SKIP: "s", FILE_LOW: "l", FILE_NORMAL: "n", FILE_HIGH: "h"}
FILE_PRIORITY_BY_CODE = {code: priority for priority, code in FILE_PRIORITY_CODES.items()}
FILE_WANT_CODE = "d"
# Mark of a file in the file list, none for the normal ones
FILE_PRIORITY_MARKS = {FILE_SKIP: "⏭️ ", FILE_LOW: "🔻 ", FILE_HIGH: "🔺 "}


def file_priority(torrent, path):
	return torrent.file_priorities.get(path, FILE_NORMAL)


def priority_label(priority):
	return get_text(f"PRIORITY_{priority.upper()}")


def download_priorities():
	"""Priorities a downloaded file can have, the ones offered as such"""
	return [level for level in client.file_priority_levels if level != FILE_SKIP]


def can_skip_files():
	return FILE_SKIP in client.file_priority_levels


def files_inside(torrent, folder):
	"""Paths of every file under folder, at any depth"""
	return [path for path, _, _ in torrent.files if is_inside(path, folder)]


def priority_row(current, set_call):
	"""One button per priority of a downloaded file, the current one marked.
	set_call receives the code of the priority as its last argument. Empty
	when the manager has a single one: there would be nothing to choose"""
	levels = download_priorities()
	if len(levels) < 2:
		return []
	return [InlineKeyboardButton(f"{'✅ ' if level == current else ''}{priority_label(level)}",
								callback_data=f"{set_call}|{FILE_PRIORITY_CODES[level]}") for level in levels]


def wanted_files_line(torrent, folder=""):
	"""How much of the content is downloaded, only when some file is left out"""
	paths = files_inside(torrent, folder)
	inside = set(paths)
	wanted = [(path, size) for path, size, _ in torrent.files if path in inside and file_priority(torrent, path) != FILE_SKIP]
	if len(wanted) == len(paths):
		return None
	return get_text("FILES_WANTED", len(wanted), len(paths), sizeof_fmt(sum(size for _, size in wanted)))


def file_download_lines(priority):
	"""Whether a file is downloaded and, when it is, with which priority"""
	if priority == FILE_SKIP:
		return [get_text("FILE_SKIPPED")]
	lines = [get_text("FILE_DOWNLOADED")]
	if len(download_priorities()) > 1:
		lines.append(get_text("FILE_PRIORITY", priority_label(priority)))
	return lines


def file_download_rows(priority, set_call):
	"""The switch to download the file or leave it out, and the priority
	while it is downloaded"""
	rows = []
	if priority == FILE_SKIP:
		rows.append([InlineKeyboardButton(get_text("BUTTON_FILE_WANT"), callback_data=f"{set_call}|{FILE_WANT_CODE}")])
		return rows
	row = priority_row(priority, set_call)
	if row:
		rows.append(row)
	if can_skip_files():
		rows.append([InlineKeyboardButton(get_text("BUTTON_FILE_SKIP"), callback_data=f"{set_call}|{FILE_PRIORITY_CODES[FILE_SKIP]}")])
	return rows


def folder_download_change(torrent, folder, code):
	"""(paths, priority) a button of the folder screen changes. Leaving out and
	downloading only touch the files that need it, and a priority is only
	given to the files downloaded: the ones left out stay so"""
	paths = files_inside(torrent, folder)
	skipped = [path for path in paths if file_priority(torrent, path) == FILE_SKIP]
	wanted = [path for path in paths if file_priority(torrent, path) != FILE_SKIP]
	if code == FILE_WANT_CODE:
		return skipped, FILE_NORMAL
	priority = FILE_PRIORITY_BY_CODE.get(code)
	if priority == FILE_SKIP and can_skip_files():
		return wanted, FILE_SKIP
	if priority in download_priorities():
		return [path for path in wanted if file_priority(torrent, path) != priority], priority
	return [], None


def build_folder_priority(torrent, folder, ctx_id, folder_index, file_page):
	"""Which files under the folder are downloaded, and how eagerly"""
	paths = files_inside(torrent, folder)
	wanted = [path for path in paths if file_priority(torrent, path) != FILE_SKIP]
	name = file_basename(folder) if folder else torrent.name
	lines = [get_text("FOLDER_PRIORITY_TITLE", html.escape(name), len(paths)), ""]
	lines.append(get_text("FOLDER_WANTED_COUNT", len(wanted), len(paths) - len(wanted)))
	levels = {file_priority(torrent, path) for path in wanted}
	if wanted and len(download_priorities()) > 1:
		counts = [(level, sum(1 for path in wanted if file_priority(torrent, path) == level)) for level in download_priorities()]
		lines.append(get_text("FOLDER_PRIORITY_NOW", " · ".join(
			priority_label(level) if len(levels) == 1 else f"{priority_label(level)}: {count}" for level, count in counts if count)))
		lines.append("")
		lines.append(get_text("FOLDER_PRIORITY_HINT"))
	if len(wanted) < len(paths) and client.skipped_files_in_size:
		lines.append("")
		lines.append(get_text("FOLDER_SKIPPED_IN_SIZE"))
	set_call = build_call("folderPrio", ctx_id, folder_index, file_page)
	markup = InlineKeyboardMarkup()
	switches = []
	if len(wanted) < len(paths):
		switches.append(InlineKeyboardButton(get_text("BUTTON_FOLDER_WANT_ALL"), callback_data=f"{set_call}|{FILE_WANT_CODE}"))
	if wanted and can_skip_files():
		switches.append(InlineKeyboardButton(get_text("BUTTON_FOLDER_SKIP_ALL"), callback_data=f"{set_call}|{FILE_PRIORITY_CODES[FILE_SKIP]}"))
	if switches:
		markup.row(*switches)
	if wanted:
		row = priority_row(levels.pop() if len(levels) == 1 else None, set_call)
		if row:
			markup.row(*row)
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("files", ctx_id, folder_index, file_page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def build_files(ctx_id, torrent_id, filter_key, page, folder_index, file_page, refreshing=False):
	torrent = client.get_torrent(torrent_id)
	if torrent is None:
		return get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page))

	folders = torrent_folders(torrent)
	folder = get_torrent_folder(torrent, folder_index)
	folder_index = folder_ref(folders, folders.index(folder))
	subfolders, files = folder_entries(torrent, folder)
	entries = [("folder", folders.index(f)) for f in subfolders] + [("file", i) for i in files]
	total = len(entries)
	pages = max(1, math.ceil(total / FILES_PER_PAGE))
	file_page = max(0, min(int(file_page), pages - 1))
	root = torrent_root(torrent)
	location = "/" + folder[len(root):].lstrip("/") if root else "/" + folder
	text = get_text("FILES_TITLE", html.escape(torrent.name), html.escape(location),
					len(subfolders), len(files), file_page + 1, pages)
	wanted_line = wanted_files_line(torrent, folder) if client.file_priority_levels else None
	if wanted_line:
		text += f"\n{wanted_line}"

	markup = InlineKeyboardMarkup(row_width=1)
	start = file_page * FILES_PER_PAGE
	for kind, index in entries[start:start + FILES_PER_PAGE]:
		if kind == "folder":
			markup.add(InlineKeyboardButton(
				f"📁 {truncate(file_basename(folders[index]))}",
				callback_data=build_call("files", ctx_id, folder_ref(folders, index), 0)))
		else:
			path = torrent.files[index][0]
			icon = "🎬" if is_video_file(path) else ("💬" if is_subtitle_file(path) else "📄")
			mark = FILE_PRIORITY_MARKS.get(file_priority(torrent, path), "") if client.file_priority_levels else ""
			# Like the torrent list: the share only shows while there is some
			# left, and not on a file left out, which is not going anywhere
			_, size, completed = torrent.files[index]
			share = file_progress(size, completed)
			skipped = client.file_priority_levels and file_priority(torrent, path) == FILE_SKIP
			progress = f" {share:.0f}%" if share < 100 and not skipped else ""
			markup.add(InlineKeyboardButton(
				f"{mark}{icon}{progress} {truncate(file_basename(path))}",
				callback_data=file_call("file", ctx_id, folder_index, file_page, index, path)))

	if pages > 1:
		markup.row(*pagination_row(file_page, pages, "files", ctx_id, folder_index))

	if folder != root:
		parent = file_folder(folder)
		markup.row(InlineKeyboardButton(get_text("BUTTON_FOLDER_UP", truncate(file_basename(parent) or "/")),
										callback_data=build_call("files", ctx_id, folder_ref(folders, folders.index(parent)), 0)))
	# What acts on the folder being browsed shares a row
	folder_row = []
	if client.file_priority_levels:
		folder_row.append(InlineKeyboardButton(get_text("BUTTON_FOLDER_PRIORITY"), callback_data=build_call("folderPrio", ctx_id, folder_index, file_page)))
	if folder and client.supports_rename:
		folder_row.append(InlineKeyboardButton(get_text("BUTTON_FOLDER_RENAME"), callback_data=build_call("folder", ctx_id, folder_index, file_page)))
	if folder_row:
		markup.row(*folder_row)
	files_plan, folders_plan, _ = build_full_plan(torrent, within=folder) if client.supports_rename else ([], [], [])
	if files_plan or folders_plan:
		markup.row(InlineKeyboardButton(get_text("BUTTON_FILES_RENAME_ALL"), callback_data=build_call("filesAll", ctx_id, folder_index, file_page)))
	text += f"\n\n{live_footer(refreshing)}"
	markup.row(*live_buttons(refreshing, build_call("files", ctx_id, folder_index, file_page),
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("info", torrent_id, filter_key, page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	))
	return text, markup


def render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, folder_index, file_page, thread_id=None):
	"""The file list repaints itself for a while, like the torrent screen: it
	shows how far each file has got"""
	try:
		text, markup = build_files(ctx_id, torrent_id, filter_key, page, folder_index, file_page, refreshing=True)
	except TorrentClientError as e:
		edit_message(chat_id, message_id, client_error_text(e),
					back_close_markup(build_call("info", torrent_id, filter_key, page)))
		return
	edit_message(chat_id, message_id, text, markup)
	start_live_view(chat_id, message_id, thread_id,
		lambda refreshing: build_files(ctx_id, torrent_id, filter_key, page, folder_index, file_page, refreshing),
		build_call("files", ctx_id, folder_index, file_page))


def file_key(path):
	"""Short hash of a file path. The buttons of a file carry it next to the
	file's position, which a rename can hand over to another file"""
	return hashlib.sha1(path.encode("utf-8")).hexdigest()[:6]


def file_call(command, ctx_id, folder_index, file_page, file_index, path, *extra):
	return build_call(command, ctx_id, folder_index, file_page, file_index, file_key(path), *extra)


def get_torrent_file(torrent, file_index):
	"""Returns (path, size) for the file index or None when out of range"""
	try:
		path, size, _ = torrent.files[int(file_index)]
	except (IndexError, TypeError, ValueError):
		return None
	return path, size


def resolve_file(torrent, file_index, key=None):
	"""(index, path, size) of the file a button points at, or None when it is
	no longer in the torrent. The position alone is not trusted: renaming a
	file reorders the list, and the same position then names another file,
	which the skip button would have left out. key is None on buttons from
	before the hash, which only carried the position"""
	if torrent is None:
		return None
	entry = get_torrent_file(torrent, file_index)
	if key is None:
		return (int(file_index),) + entry if entry else None
	if entry and file_key(entry[0]) == key:
		return (int(file_index),) + entry
	for index, (path, size, _) in enumerate(torrent.files):
		if file_key(path) == key:
			return index, path, size
	return None


def renamed_file_call(ctx_id, folder_index, file_page, torrent_id, old_path, new_name):
	"""Where the back button of a file rename goes: the file under its new
	name, or the file list when it cannot be found"""
	folder = file_folder(old_path)
	new_path = f"{folder}/{new_name}" if folder else new_name
	try:
		torrent = client.get_torrent(torrent_id)
	except TorrentClientError:
		torrent = None
	for index, (path, _, _) in enumerate(torrent.files if torrent else []):
		if path in (new_path, old_path):
			return file_call("file", ctx_id, folder_index, file_page, index, path)
	return build_call("files", ctx_id, folder_index, file_page)


def file_progress(size, completed):
	"""Share of a file already on disk, 0-100"""
	if size <= 0:
		return 100.0
	return min(100.0, max(0.0, completed / size * 100))


def file_completed(torrent, path):
	return next((completed for p, _, completed in torrent.files if p == path), 0) or 0


def build_file_detail(torrent, path, size, ctx_id, folder_index, file_page, file_index, refreshing=False):
	lines = [f"<b>{html.escape(file_basename(path))}</b>", ""]
	folder = file_folder(path)
	if folder:
		lines.append(f"{get_text('INFO_DIR')}: <code>{html.escape(folder)}</code>")
	lines.append(f"{get_text('INFO_SIZE')}: {sizeof_fmt(size)}")
	completed = file_completed(torrent, path)
	lines.append(get_text("FILE_PROGRESS", f"{file_progress(size, completed):.1f}", sizeof_fmt(completed), sizeof_fmt(size)))

	markup = InlineKeyboardMarkup(row_width=1)
	if client.file_priority_levels:
		priority = file_priority(torrent, path)
		lines.extend(file_download_lines(priority))
		for row in file_download_rows(priority, file_call("filePrio", ctx_id, folder_index, file_page, file_index, path)):
			markup.row(*row)
	suggested = suggest_for_file(torrent, path) if client.supports_rename else None
	if suggested:
		lines.append("")
		lines.append(get_text("FILE_SUGGEST", html.escape(suggested)))
		subtitles = subtitle_renames(torrent, path, suggested)
		if subtitles:
			lines.append(get_text("FILE_SUBTITLES", len(subtitles)))
		markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_AUTO"), callback_data=file_call("fileAuto", ctx_id, folder_index, file_page, file_index, path)))
	elif is_video_file(path) and client.supports_rename:
		# One line: the screen is also where the download of the file is chosen
		lines.append("")
		lines.append(get_text("FILE_NO_SUGGESTION_SHORT"))
	if client.supports_rename:
		markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_MANUAL"), callback_data=file_call("fileManual", ctx_id, folder_index, file_page, file_index, path)))
	lines.append("")
	lines.append(live_footer(refreshing))
	markup.row(*live_buttons(refreshing, file_call("file", ctx_id, folder_index, file_page, file_index, path),
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("files", ctx_id, folder_index, file_page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	))
	return "\n".join(lines), markup


def build_file_screen(torrent_id, ctx_id, folder_index, file_page, file_index, refreshing=False, key=None):
	"""The file screen read again from the manager. It follows the file if
	another rename moved it in the list, and a file that is gone (the torrent
	removed, or the file renamed meanwhile) ends the repainting"""
	torrent = client.get_torrent(torrent_id)
	back = back_close_markup(build_call("files", ctx_id, folder_index, file_page))
	if torrent is None:
		raise ExpiredContext((get_text("TORRENT_NOT_FOUND"), back_close_markup()))
	resolved = resolve_file(torrent, file_index, key)
	if resolved is None:
		raise ExpiredContext((get_text("FILE_NOT_FOUND"), back))
	index, path, size = resolved
	return build_file_detail(torrent, path, size, ctx_id, folder_index, file_page, index, refreshing)


def render_file_detail(chat_id, message_id, thread_id, torrent, resolved, ctx_id, folder_index, file_page):
	"""Paints the file screen of resolved (index, path, size) and keeps it
	repainting itself for a while"""
	index, path, size = resolved
	text, markup = build_file_detail(torrent, path, size, ctx_id, folder_index, file_page, index, refreshing=True)
	edit_message(chat_id, message_id, text, markup)
	key = file_key(path)
	start_live_view(chat_id, message_id, thread_id,
		lambda refreshing: build_file_screen(torrent.id, ctx_id, folder_index, file_page, index, refreshing, key=key),
		file_call("file", ctx_id, folder_index, file_page, index, path))


def build_folder_detail(torrent, folder, ctx_id, folder_index, file_page):
	"""Rename screen of the folder being browsed: only the folder itself, its
	contents have their own button in the files screen"""
	lines = [get_text("FOLDER_RENAME_TITLE", html.escape(file_basename(folder)))]
	markup = InlineKeyboardMarkup(row_width=1)
	suggested = suggest_for_folder(torrent, folder)
	if suggested:
		lines.append("")
		lines.append(get_text("FILE_SUGGEST", html.escape(suggested)))
		markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_AUTO"), callback_data=build_call("folderAuto", ctx_id, folder_index, file_page)))
	else:
		lines.append("")
		lines.append(get_text("FOLDER_NO_SUGGESTION"))
	markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_MANUAL"), callback_data=build_call("folderManual", ctx_id, folder_index, file_page)))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("files", ctx_id, folder_index, file_page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def folder_rename_back_call(torrent, ctx_id, folder):
	"""Where to go once folder is renamed: the folder holding it, whose index
	the rename does not move. The torrent folder has nothing above it to show,
	and its index does not move either: every other folder is inside it"""
	folders = torrent_folders(torrent)
	target = folder if folder == torrent_root(torrent) else file_folder(folder)
	return build_call("files", ctx_id, folder_ref(folders, folders.index(target)), 0)


def rename_folder_to(torrent, folder, new_name):
	"""Renames a folder of the torrent: the torrent folder through the torrent.
	Returns the error text to show, or None when it was renamed"""
	if folder == torrent_root(torrent):
		if name_already_exists(new_name, exclude_id=torrent.id):
			return get_text("RENAME_DUPLICATE", html.escape(new_name))
		client.rename_torrent(torrent.id, new_name)
		return None
	siblings = {file_basename(p) for p in torrent_folders(torrent)[1:] + [p for p, _, _ in torrent.files]
				if file_folder(p) == file_folder(folder) and p != folder}
	if new_name in siblings:
		return get_text("FOLDER_RENAME_DUPLICATE", html.escape(new_name))
	client.rename_folder(torrent.id, folder, new_name)
	return None


def build_torrent_rename(torrent):
	"""Everything the rename button of a torrent renames: the torrent itself
	and, when it is a folder, every subfolder and file inside it too (a
	complete series with a folder per season). Returns (suggested torrent name
	or None, file plan, folder plan, names skipped because they collide)"""
	suggested = parse_name(torrent.name)
	if not suggested or suggested == torrent.name:
		suggested = None
	if not torrent_is_folder(torrent):
		return suggested, [], [], 0
	files_plan, folders_plan, skipped = build_full_plan(torrent)
	return suggested, files_plan, folders_plan, skipped


def torrent_rename_text(torrent, suggested, files_plan, folders_plan, skipped):
	if not files_plan and not folders_plan:
		return get_text(rename_key(torrent, "RENAME_SUGGEST"), html.escape(torrent.name), html.escape(suggested))
	lines = [get_text("RENAME_FULL_TITLE", html.escape(torrent.name)), ""]
	if suggested:
		lines.append(get_text("FILE_SUGGEST", html.escape(suggested)))
	else:
		lines.append(get_text("RENAME_FULL_KEEP_NAME"))
	lines.append("")
	lines.append(get_text("RENAME_FULL_CONTENTS", rename_count_text(len(files_plan), len(folders_plan))))
	lines.append(build_plan_preview(files_plan, folders_plan))
	if skipped:
		lines.append("")
		lines.append(get_text("FILES_RENAME_SKIPPED", skipped))
	return "\n".join(lines)


def apply_torrent_rename(torrent, suggested, files_plan, folders_plan):
	"""Renames the contents first and the torrent last, so every planned path
	is still there when its turn comes. Returns the text with the outcome"""
	lines = []
	if files_plan or folders_plan:
		lines.append(rename_result_text(*apply_full_plan(torrent.id, files_plan, folders_plan)))
	if suggested:
		if name_already_exists(suggested, exclude_id=torrent.id):
			lines.append(get_text("RENAME_DUPLICATE", html.escape(suggested)))
		else:
			client.rename_torrent(torrent.id, suggested)
			lines.insert(0, get_text(rename_key(torrent, "RENAME_OK"), html.escape(suggested)))
	return "\n".join(lines)


def build_plan_preview(plan, folders=()):
	"""Lines old -> new of a rename plan, the folders first and by path"""
	entries = [("📁 ", old, new) for old, new in sorted(folders, key=lambda f: f[0].lower())]
	entries += [("", old, new) for old, new in plan]
	shown = entries[:MAX_PLAN_PREVIEW_LINES]
	lines = [f"• {icon}<code>{html.escape(file_basename(old))}</code> → <code>{html.escape(new)}</code>" for icon, old, new in shown]
	if len(entries) > len(shown):
		lines.append(get_text("INFO_AND_MORE_FILES", len(entries) - len(shown)))
	return "\n".join(lines)


# ---------------------------------------------------------------------------
# TRACKER FILTER
# ---------------------------------------------------------------------------

def build_trackers_menu():
	counts = Counter()
	for torrent in client.get_torrents():
		for tracker in torrent.trackers:
			counts[tracker] += 1
	text = get_text("TRACKERS_TITLE", len(counts))
	if not counts:
		text += f"\n\n{get_text('TRACKERS_EMPTY')}"
	markup = InlineKeyboardMarkup(row_width=1)
	for tracker, count in counts.most_common(MAX_TRACKER_BUTTONS):
		ctx_id = new_tracker_context(tracker)
		markup.add(InlineKeyboardButton(
			f"🌐 {truncate(tracker)} ({count})",
			callback_data=build_call("list", f"t{ctx_id}", 0)))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("dashboard")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return text, markup


_categories_cache = {"value": [], "ts": 0.0}


def get_known_categories():
	"""Categories defined in the torrent manager, or an empty list when it has
	none or does not support them. A single screen asks for them more than once
	and they change about never, so the answer is held for a few seconds"""
	if not client.supports_categories:
		return []
	with _contexts_lock:
		if time.time() - _categories_cache["ts"] < CATEGORIES_CACHE_TTL:
			return list(_categories_cache["value"])
	try:
		categories = client.get_categories()
	except TorrentClientError as e:
		warning(f"Cannot get categories: {e}")
		return []
	with _contexts_lock:
		_categories_cache["value"] = list(categories)
		_categories_cache["ts"] = time.time()
	return categories


def category_save_path(category):
	"""The folder a category points at, or an empty string when it has none"""
	if not category:
		return ""
	for name, save_path in get_known_categories():
		if name == category:
			return (save_path or "").rstrip("/")
	return ""


def build_categories_menu(page=0):
	categories = get_known_categories()
	counts = Counter()
	for torrent in client.get_torrents():
		counts[torrent.category] += 1
	text = get_text("CATEGORIES_TITLE", len(categories))
	if not categories:
		text += f"\n\n{get_text('CATEGORIES_EMPTY')}"
	markup = InlineKeyboardMarkup(row_width=1)
	# A library organized by category easily has more of them than fit in one
	# keyboard, and a truncated list makes the rest unreachable for good
	pages = max(1, math.ceil(len(categories) / CATEGORIES_PER_PAGE))
	page = max(0, min(int(page), pages - 1))
	start = page * CATEGORIES_PER_PAGE
	for name, save_path in categories[start:start + CATEGORIES_PER_PAGE]:
		label = f"🏷️ {truncate(name)} ({counts.get(name, 0)})"
		markup.add(InlineKeyboardButton(label, callback_data=build_call("list", f"c{new_category_context(name)}", 0)))
	if pages > 1:
		markup.row(*pagination_row(page, pages, "categories"))
	# Torrents with no category are invisible in the list above and are exactly
	# the ones a user organizing by category needs to find
	if counts.get("", 0):
		markup.add(InlineKeyboardButton(
			f"🚫 {get_text('CATEGORY_NONE')} ({counts['']})",
			callback_data=build_call("list", f"c{new_category_context('')}", 0)))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("dashboard")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return text, markup


def build_category_markup(cat_call, cancel_call, include_none=True, extra_rows=None, page_parts=None, page=0, lead_rows=None):
	"""Keyboard with one button per category defined in the manager, paginated
	the same way the directories are. Category names are free text, so they
	travel as short ids like directories do.
	page_parts is the callback prefix that receives the page as last arg.
	lead_rows go above the categories, extra_rows below them"""
	markup = InlineKeyboardMarkup(row_width=1)
	for row in lead_rows or []:
		markup.add(row)
	categories = get_known_categories()
	pages = max(1, math.ceil(len(categories) / CATEGORIES_PER_PAGE))
	page = max(0, min(int(page), pages - 1))
	start = page * CATEGORIES_PER_PAGE
	for name, _save_path in categories[start:start + CATEGORIES_PER_PAGE]:
		markup.add(InlineKeyboardButton(f"🏷️ {truncate(name)}", callback_data=cat_call(get_cat_id(name))))
	if pages > 1 and page_parts:
		markup.row(*pagination_row(page, pages, *page_parts))
	if include_none:
		markup.add(InlineKeyboardButton(get_text("BUTTON_CATEGORY_NONE"), callback_data=cat_call(get_cat_id(""))))
	for row in extra_rows or []:
		markup.add(row)
	markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=cancel_call))
	return markup


def build_torrent_category_screen(torrent, filter_key, page, cat_page=0):
	"""The screen that changes the category of a torrent. On an auto managed
	torrent this is what decides its directory, so the manual move is offered
	here instead, spelling out that it gives up the automatic management"""
	lines = [get_text("CATEGORY_ASK", html.escape(torrent.name))]
	current = html.escape(torrent.category) if torrent.category else get_text("CATEGORY_NONE")
	lines.append(get_text("CATEGORY_CURRENT", current))
	if not get_known_categories():
		lines.append("")
		lines.append(get_text("CATEGORIES_EMPTY"))
	if torrent.auto_managed:
		lines.append("")
		lines.append(get_text("CATEGORY_MOVES_WARNING"))
	nav_ctx = new_nav_context(torrent.id, filter_key, page)
	extra = []
	if torrent.auto_managed:
		extra.append(InlineKeyboardButton(
			get_text("BUTTON_MOVE_MANUAL"),
			callback_data=build_call("move", torrent.id, filter_key, page)))
	elif torrent.category:
		# Only with a category: without one the manager would drag the torrent
		# to the default download folder, which nobody asked for
		lines.append("")
		lines.append(get_text("CATEGORY_AUTO_MANAGE_HINT", html.escape(category_save_path(torrent.category) or "?")))
		extra.append(InlineKeyboardButton(
			get_text("BUTTON_AUTO_MANAGE"),
			callback_data=build_call("autoManage", nav_ctx)))
	markup = build_category_markup(
		cat_call=lambda cat_id: build_call("catSet", nav_ctx, cat_id),
		cancel_call=build_call("info", torrent.id, filter_key, page),
		extra_rows=extra,
		page_parts=("catMenu", nav_ctx),
		page=cat_page,
	)
	return "\n".join(lines), markup


# ---------------------------------------------------------------------------
# RATIO LIMIT
# ---------------------------------------------------------------------------

RATIO_PRESETS = (0.5, 1, 2)
MAX_RATIO_LIMIT = 9998  # The highest qBittorrent accepts


def ratio_limit_label(torrent):
	"""What decides when the torrent stops seeding, in words"""
	limit = f"{torrent.ratio_limit:g}" if torrent.ratio_limit >= 0 else get_text("NO_LIMIT")
	if torrent.ratio_limit_mode == RATIO_MODE_GLOBAL:
		return get_text("RATIO_GLOBAL_VALUE", limit)
	return limit


def parse_ratio(text):
	"""Ratio typed by the user (a comma works as decimal separator too), or
	None when it is not a valid limit"""
	try:
		value = float(text.strip().replace(",", "."))
	except ValueError:
		return None
	if not 0 <= value <= MAX_RATIO_LIMIT or value != value:
		return None
	return round(value, 2)


def ratio_option_rows(limit, selected, set_call, ask_call, with_global=False):
	"""The choice of ratio limit shared by a torrent and the settings: limit is
	the one in force and selected the option it came from ("g" the manager's,
	"n" none, "v" a value, "" unknown)"""
	def option(text, value, chosen):
		return InlineKeyboardButton(("✅ " if chosen else "") + text, callback_data=set_call(value))

	first = [option(get_text("BUTTON_RATIO_NONE"), "n", selected == "n")]
	if with_global:
		first.insert(0, option(get_text("BUTTON_RATIO_GLOBAL"), "g", selected == "g"))
	custom = selected == "v" and limit not in RATIO_PRESETS
	custom_text = ("✅ " if custom else "") + get_text("BUTTON_RATIO_CUSTOM") + (f" ({limit:g})" if custom else "")
	return [
		first,
		[option(f"{preset:g}", f"{preset:g}", selected == "v" and limit == preset) for preset in RATIO_PRESETS],
		[InlineKeyboardButton(custom_text, callback_data=ask_call)],
	]


def speed_limit_text(kbps):
	return f"{kbps} KB/s" if kbps > 0 else get_text("NO_LIMIT")


def build_ratio_screen(torrent, filter_key, page):
	"""The limits of a single torrent: until which ratio it keeps seeding and
	how fast it may download and upload, whatever the manager supports"""
	lines = [get_text("LIMITS_TITLE", html.escape(torrent.name)), ""]
	nav_ctx = new_nav_context(torrent.id, filter_key, page)
	markup = InlineKeyboardMarkup()
	if client.supports_ratio_limit:
		lines.append(f"{get_text('INFO_RATIO')}: {torrent.ratio}")
		lines.append(get_text("RATIO_LIMIT_NOW", ratio_limit_label(torrent)))
	if client.supports_torrent_speed_limit:
		lines.append(get_text("LIMITS_DOWN", speed_limit_text(torrent.download_limit)))
		lines.append(get_text("LIMITS_UP", speed_limit_text(torrent.upload_limit)))
	if client.supports_ratio_limit:
		lines += ["", get_text("RATIO_HINT")]
		selected = {RATIO_MODE_GLOBAL: "g", RATIO_MODE_NONE: "n", RATIO_MODE_TORRENT: "v"}.get(torrent.ratio_limit_mode, "")
		for row in ratio_option_rows(torrent.ratio_limit, selected, lambda value: build_call("ratioSet", nav_ctx, value),
									build_call("ratioAsk", nav_ctx), with_global=True):
			markup.row(*row)
	if client.supports_torrent_speed_limit:
		markup.row(
			InlineKeyboardButton(get_text("BUTTON_TORRENT_DOWN_LIMIT"), callback_data=build_call("tSpeedAsk", nav_ctx, "down")),
			InlineKeyboardButton(get_text("BUTTON_TORRENT_UP_LIMIT"), callback_data=build_call("tSpeedAsk", nav_ctx, "up")),
		)
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("info", torrent.id, filter_key, page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	# Told by the rule it got, not by its trackers: the list of a manager and
	# the full torrent may name them differently (Deluge, qBittorrent)
	rule_tracker = tracker_limits.applied_tracker(torrent.id) if tracker_limits_supported() else None
	if rule_tracker:
		lines += ["", get_text("LIMITS_FROM_TRACKER", html.escape(rule_tracker))]
	return "\n".join(lines), markup


# ---------------------------------------------------------------------------
# LIMITS PER TRACKER
# ---------------------------------------------------------------------------
# A rule is applied to the torrents a tracker already has when it is changed
# (only the value that changed), and by the monitor to the ones that arrive
# (see tracker_limits for why each torrent gets it only once)

def tracker_limits_supported():
	return client.supports_ratio_limit or client.supports_torrent_speed_limit


def tracker_rule_ratio_label(ratio):
	if ratio is None:
		return get_text("BUTTON_RATIO_GLOBAL")
	return f"{ratio:g}" if ratio >= 0 else get_text("NO_LIMIT")


def tracker_rule_summary(rule):
	"""The values of a rule in a few characters, for its button"""
	parts = []
	if client.supports_ratio_limit and rule["ratio"] is not None:
		parts.append(f"⚖️ {rule['ratio']:g}" if rule["ratio"] >= 0 else "⚖️ ♾️")
	if client.supports_torrent_speed_limit and rule["down"] > 0:
		parts.append(f"🔽 {rule['down']}")
	if client.supports_torrent_speed_limit and rule["up"] > 0:
		parts.append(f"🔼 {rule['up']}")
	return " · ".join(parts) if parts else get_text("TRACKER_LIMITS_NONE_SET")


def apply_rule_fields(torrent_ids, rule, fields):
	if not torrent_ids:
		return
	if "ratio" in fields and client.supports_ratio_limit:
		client.set_ratio_limit(torrent_ids, rule["ratio"])
	for direction in ("down", "up"):
		if direction in fields and client.supports_torrent_speed_limit:
			client.set_torrent_speed_limit(torrent_ids, direction, rule[direction])


def tracker_rule_torrent_ids(tracker, torrents):
	"""Ids of the torrents that follow the rule of the tracker (or would, for a
	tracker that has none yet): the ones whose main ruled tracker it is"""
	current = tracker_limits.rules()
	current.setdefault(tracker, dict(tracker_limits.EMPTY_RULE))
	return [str(t.id) for t in torrents if tracker_limits.rule_tracker(t.trackers, current) == tracker]


def known_trackers():
	trackers = []
	for torrent in client.get_torrents():
		for tracker in torrent.trackers:
			if tracker not in trackers:
				trackers.append(tracker)
	return trackers


def tracker_from_key(key):
	"""The tracker of a button, among the ones with a rule and the ones in use"""
	tracker = tracker_limits.tracker_by_key(key)
	if tracker is None:
		tracker = tracker_limits.tracker_by_key(key, known_trackers())
	return tracker


def set_tracker_rule(tracker, field, value):
	"""Changes one value of the rule and applies it to the torrents the tracker
	already has. Returns how many got it"""
	rule = tracker_limits.set_field(tracker, field, value)
	try:
		torrents = client.get_torrents()
		ids = tracker_rule_torrent_ids(tracker, torrents)
		apply_rule_fields(ids, rule, [field])
	except TorrentClientError:
		# The rule is saved but its torrents may not have the new value, and
		# they are marked as having the rule already, so the monitor would
		# never give it to them. The change itself is left for the monitor to
		# finish, that field only: the rest of the rule, and the limits set by
		# hand on some of them, stay as they are
		with _rule_retries_lock:
			_rule_retries.setdefault(tracker, set()).add(field)
		raise
	tracker_limits.mark_applied(ids, tracker)
	# A fresh and whole list of the manager: the moment to forget torrents that are gone
	tracker_limits.prune(t.id for t in torrents)
	return len(ids)


def delete_tracker_rule(tracker, restore):
	"""Removes the rule; with restore, its torrents go back to the manager's
	ratio and no speed limits, for the values the rule had set. Returns how
	many were restored"""
	rule = tracker_limits.get_rule(tracker)
	restored = 0
	if restore and rule:
		fields = tracker_limits.non_default_fields(rule)
		ids = tracker_rule_torrent_ids(tracker, client.get_torrents())
		apply_rule_fields(ids, tracker_limits.EMPTY_RULE, fields)
		restored = len(ids) if fields else 0
	tracker_limits.delete_rule(tracker)
	return restored


# Rule changes the manager failed to apply: tracker -> fields. The monitor
# applies them on its next poll, as the change would have
_rule_retries = {}
_rule_retries_lock = threading.Lock()


def keep_hand_limit(torrent_id, fields):
	"""A limit set by hand on one torrent is its own. A torrent the rule of its
	tracker had not reached yet (it arrived after the last poll) gets the rest
	of the rule now and is marked as having it: the monitor would otherwise put
	the rule over the hand-set fields, and marking it without the rest would
	leave it without the rule's other limits for good"""
	if not tracker_limits_supported():
		return
	try:
		torrent = client.get_torrent(torrent_id)
		current = tracker_limits.rules()
		tracker = tracker_limits.rule_tracker(torrent.trackers, current) if torrent else None
		if not tracker or tracker_limits.applied_tracker(torrent_id) == tracker:
			return
		others = [f for f in tracker_limits.non_default_fields(current[tracker]) if f not in fields]
		apply_rule_fields([str(torrent_id)], current[tracker], others)
	except TorrentClientError as e:
		warning(f"Cannot give {torrent_id} the rest of the limits of its tracker: {e}")
		return
	tracker_limits.mark_applied([torrent_id], tracker)


def retry_failed_rule_changes(torrents):
	"""The rule changes that failed, applied as they would have been: the
	field changed, to every torrent that follows the rule"""
	# Copies: a change failing meanwhile adds its field to the set kept here,
	# and must still be there after this round
	with _rule_retries_lock:
		retries = {tracker: frozenset(fields) for tracker, fields in _rule_retries.items()}
	for tracker, fields in retries.items():
		rule = tracker_limits.get_rule(tracker)
		try:
			if rule:
				apply_rule_fields(tracker_rule_torrent_ids(tracker, torrents), rule, sorted(fields))
		except TorrentClientError as e:
			warning(f"Cannot apply the changed limits of the tracker {tracker} yet: {e}")
			continue
		with _rule_retries_lock:
			left = _rule_retries.get(tracker, set()) - fields
			if left:
				_rule_retries[tracker] = left
			else:
				_rule_retries.pop(tracker, None)


def apply_pending_tracker_limits(torrents):
	"""Gives the rule of their tracker to the torrents that did not get it yet:
	new ones, the ones added while the bot was down, and the ones whose tracker
	the manager did not know yet (a magnet before its first announce)"""
	retry_failed_rule_changes(torrents)
	for tracker, ids in tracker_limits.pending(torrents):
		rule = tracker_limits.get_rule(tracker)
		fields = tracker_limits.non_default_fields(rule)
		try:
			apply_rule_fields(ids, rule, fields)
		except PermanentTorrentError as e:
			warning(f"Cannot apply the limits of the tracker {tracker}, giving up on {len(ids)} torrents: {e}")
		except TorrentClientError as e:
			# Tried again on the next poll
			warning(f"Cannot apply the limits of the tracker {tracker} to {len(ids)} torrents: {e}")
			continue
		else:
			if fields:
				debug(f"Limits of the tracker {tracker} applied to {len(ids)} torrents")
				count_usage("auto_tracker_limits")
		tracker_limits.mark_applied(ids, tracker)


def tracker_rule_back(back_filter):
	"""Where the back button of a rule goes: the list of the tracker filter it
	was opened from, or the screen of the rules"""
	return build_call("list", back_filter, 0) if back_filter else settings_call("tlimits")


def build_tracker_rule_screen(tracker, back_filter="", done=None):
	key = tracker_limits.tracker_key(tracker)
	extra = (back_filter,) if back_filter else ()
	rule = tracker_limits.get_rule(tracker)
	shown = rule or tracker_limits.EMPTY_RULE
	count = len(tracker_rule_torrent_ids(tracker, client.get_torrents()))
	lines = []
	if done:
		lines += [done, ""]
	lines += [get_text("TRACKER_LIMITS_RULE_TITLE", html.escape(tracker)), get_text("TRACKER_LIMITS_COUNT", count), ""]
	if client.supports_ratio_limit:
		lines.append(get_text("TRACKER_LIMITS_RATIO", tracker_rule_ratio_label(shown["ratio"])))
	if client.supports_torrent_speed_limit:
		lines.append(get_text("LIMITS_DOWN", speed_limit_text(shown["down"])))
		lines.append(get_text("LIMITS_UP", speed_limit_text(shown["up"])))
	lines += ["", get_text("TRACKER_LIMITS_RULE_HELP")]
	markup = InlineKeyboardMarkup()
	if client.supports_ratio_limit:
		ratio = shown["ratio"]
		selected = "g" if ratio is None else ("n" if ratio < 0 else "v")
		for row in ratio_option_rows(ratio if ratio is not None else -1, selected,
									lambda value: build_call("tlRatio", key, value, *extra),
									build_call("tlRatioAsk", key, *extra), with_global=True):
			markup.row(*row)
	if client.supports_torrent_speed_limit:
		markup.row(
			InlineKeyboardButton(get_text("BUTTON_TORRENT_DOWN_LIMIT"), callback_data=build_call("tlSpeedAsk", key, "down", *extra)),
			InlineKeyboardButton(get_text("BUTTON_TORRENT_UP_LIMIT"), callback_data=build_call("tlSpeedAsk", key, "up", *extra)),
		)
	if rule is not None:
		markup.row(InlineKeyboardButton(get_text("BUTTON_TRACKER_LIMITS_DELETE"), callback_data=build_call("tlDel", key, *extra)))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=tracker_rule_back(back_filter)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def build_tracker_rule_delete(tracker, back_filter=""):
	key = tracker_limits.tracker_key(tracker)
	extra = (back_filter,) if back_filter else ()
	count = len(tracker_rule_torrent_ids(tracker, client.get_torrents()))
	text = get_text("TRACKER_LIMITS_DELETE_ASK", html.escape(tracker), count)
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("BUTTON_TRACKER_LIMITS_DELETE_RESTORE"), callback_data=build_call("tlDelOk", key, "1", *extra)))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TRACKER_LIMITS_DELETE_KEEP"), callback_data=build_call("tlDelOk", key, "0", *extra)))
	markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=build_call("tlRule", key, *extra)))
	return text, markup


def build_tracker_rule_pick():
	"""The trackers in use that have no rule yet, most used first"""
	current = tracker_limits.rules()
	counts = Counter()
	for torrent in client.get_torrents():
		for tracker in torrent.trackers:
			if tracker not in current:
				counts[tracker] += 1
	text = get_text("TRACKER_LIMITS_PICK")
	if not counts:
		text += f"\n\n{get_text('TRACKERS_EMPTY')}"
	markup = InlineKeyboardMarkup(row_width=1)
	for tracker, count in counts.most_common(MAX_TRACKER_BUTTONS):
		markup.add(InlineKeyboardButton(f"🌐 {truncate(tracker)} ({count})",
										callback_data=build_call("tlRule", tracker_limits.tracker_key(tracker))))
	return text, settings_nav(markup, back=settings_call("tlimits"))


def build_settings_tracker_limits(page=0):
	"""The rules, a page at a time: Telegram refuses a keyboard of more than
	100 buttons, and every rule is one"""
	current = tracker_limits.rules()
	trackers = sorted(current)
	pages = max(1, math.ceil(len(trackers) / TRACKER_RULES_PER_PAGE))
	page = max(0, min(int(page), pages - 1))
	lines = [get_text("SETTINGS_TRACKER_LIMITS_TITLE"), "", get_text("SETTINGS_TRACKER_LIMITS_HELP")]
	if not current:
		lines += ["", get_text("TRACKER_LIMITS_EMPTY")]
	markup = InlineKeyboardMarkup(row_width=1)
	start = page * TRACKER_RULES_PER_PAGE
	for tracker in trackers[start:start + TRACKER_RULES_PER_PAGE]:
		markup.add(InlineKeyboardButton(f"🌐 {truncate(tracker, 28)} · {tracker_rule_summary(current[tracker])}",
										callback_data=build_call("tlRule", tracker_limits.tracker_key(tracker))))
	if pages > 1:
		markup.row(*pagination_row(page, pages, "tlPage"))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TRACKER_LIMITS_ADD"), callback_data=build_call("tlAdd")))
	return "\n".join(lines), settings_nav(markup, back=settings_call("limits"))


def send_tracker_rule_screen(chat_id, thread_id, tracker, back_filter, done):
	"""After a typed value: back on the rule, as when a preset is pressed"""
	text, markup = build_tracker_rule_screen(tracker, back_filter, done=done)
	send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)


# ---------------------------------------------------------------------------
# DIRECTORIES
# ---------------------------------------------------------------------------

def get_known_dirs():
	"""Known directories: favorite dirs first, then the ones in use by the
	client (most used first) and the auto download dir"""
	dirs = []
	for favorite in bot_settings.get("favorite_dirs") or []:
		favorite = (favorite or "").strip().rstrip("/")
		if favorite and favorite not in dirs:
			dirs.append(favorite)
	try:
		in_use = client.get_download_dirs()
	except TorrentClientError as e:
		warning(f"Cannot get download dirs: {e}")
		in_use = []
	extras = list(in_use) + [bot_settings.get("auto_download_dir")]
	for extra in extras:
		extra = (extra or "").strip().rstrip("/")
		if extra and extra not in dirs:
			dirs.append(extra)
	return dirs


def build_dir_markup(dir_call, write_call, cancel_call, page_parts=None, page=0, suggested=()):
	"""Keyboard with one button per known dir (paginated) + write path + cancel.
	dir_call receives the dir_id and must return the callback_data.
	page_parts is the callback prefix that receives the page as last arg.
	suggested are the folders of the series (see series_dirs): they go first,
	so they are on the first page whatever the number of favorites"""
	markup = InlineKeyboardMarkup(row_width=1)
	dirs = list(suggested) + [d for d in get_known_dirs() if d not in suggested]
	pages = max(1, math.ceil(len(dirs) / MAX_DIR_BUTTONS))
	page = max(0, min(int(page), pages - 1))
	start = page * MAX_DIR_BUTTONS
	for directory in dirs[start:start + MAX_DIR_BUTTONS]:
		icon = "✨" if directory in suggested else "📂"
		markup.add(InlineKeyboardButton(f"{icon} {truncate_dir(directory)}", callback_data=dir_call(get_dir_id(directory))))
	if pages > 1 and page_parts:
		markup.row(*pagination_row(page, pages, *page_parts))
	markup.add(InlineKeyboardButton(get_text("BUTTON_WRITE_DIR"), callback_data=write_call))
	markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=cancel_call))
	return markup


# ---------------------------------------------------------------------------
# SERIES FOLDERS
# ---------------------------------------------------------------------------
# A new episode is offered the folder where the rest of its series already is,
# found through the other torrents of the manager (see name_parser)

SERIES_SUGGESTIONS_MAX = 2  # Folders offered at most: more would only be noise

# Texts that open the block about the series folder at the end of an add
# message: the buttons under it rebuild the message without that block
SERIES_BLOCK_KEYS = ("SERIES_SUGGEST_ONE", "SERIES_SUGGEST_MANY", "SERIES_PLACED", "SERIES_PLACED_NEW", "SERIES_MOVING")


def series_candidates(name, exclude_id=None, current_dir=None):
	"""Folders the series of name already lives in, best first, as (dir, named).
	Empty when name is not a series, the manager has nothing of it, or they
	could not be looked for (see series_lookup to tell that apart)"""
	return series_lookup(name, exclude_id, current_dir) or []


def series_lookup(name, exclude_id=None, current_dir=None):
	"""series_candidates, but None when the folders could not be looked for
	(the manager did not answer, the parser failed): "nowhere" and "could
	not tell" are not the same for a series about to get a new folder"""
	if series_identity(name) is None:
		return []
	try:
		torrents = client.get_torrents()
	except TorrentClientError as e:
		warning(f"Cannot look for the series folder of {name}: {e}")
		return None
	others = []
	for torrent in torrents:
		if str(torrent.id) == str(exclude_id) or not torrent.download_dir:
			continue
		others.append((torrent.name, torrent.download_dir, added_timestamp(torrent.added_date)))
	try:
		return series_dir_candidates(name, others, known_dirs=bot_settings.get("favorite_dirs") or [],
									current_dir=current_dir, generic_dirs=with_and_without_slash(generic_dirs()))
	except Exception as e:
		# A folder name the parser chokes on must not take the add, move or
		# series screens down with it: nothing is suggested instead
		warning(f"Cannot work out the series folder of {name}: {e}")
		return None


def generic_dirs(strict=False):
	"""Where anything lands before being sorted: the automatic download folder
	and the default one of the manager. strict: None when the manager does not
	tell its default one, instead of leaving it out"""
	generic = [bot_settings.get("auto_download_dir")]
	try:
		generic.append(client.get_default_download_dir())
	except TorrentClientError:
		if strict:
			return None
	return [_dir_key(d) for d in generic if d]


def _dir_key(path):
	"""A folder as it is compared: without its trailing separator, which a
	Windows Deluge writes as a backslash. "/" stays "/" """
	path = (path or "").strip()
	return path.rstrip("/\\") or path[:1]


def same_dir(a, b):
	"""Whether two folders are the same one. Download Station tells its
	folders without the leading slash (DESCARGAS/SERIES) and takes them
	either way, so a folder typed as /DESCARGAS/SERIES is that one too"""
	return _dir_key(a).lstrip("/") == _dir_key(b).lstrip("/")


def with_and_without_slash(dirs):
	"""Each folder of dirs both with and without its leading slash, for the
	comparisons done in name_parser, which take a folder as written"""
	variants = []
	for directory in dirs:
		bare = _dir_key(directory).lstrip("/")
		for variant in (bare, "/" + bare):
			if bare and variant not in variants:
				variants.append(variant)
	return variants


def added_timestamp(added_date):
	"""When a torrent was added as a unix number, 0 when unknown. Every
	manager reports a unix number; a datetime is taken too"""
	if not added_date:
		return 0
	try:
		return added_date.timestamp() if hasattr(added_date, "timestamp") else float(added_date)
	except (OverflowError, OSError, TypeError, ValueError):
		return 0


def series_dirs(name, exclude_id=None, current_dir=None):
	"""The folders worth offering for name, see series_candidates"""
	return [d for d, _named in series_candidates(name, exclude_id, current_dir)][:SERIES_SUGGESTIONS_MAX]


def series_hint(name, suggested):
	"""The line that explains the ✨ buttons of a folder picker"""
	identity = series_identity(name)
	if not suggested or identity is None:
		return ""
	return get_text("SERIES_DIR_HINT", html.escape(identity["title"]))


def auto_move_series_enabled(external=False):
	"""The series go straight to their folder only when the bot adds without
	asking: when it asks, the folder is offered first and that is enough"""
	if not bot_settings.get("auto_download") or not bot_settings.get("auto_move_series"):
		return False
	return bool(bot_settings.get("auto_move_series_external")) if external else True


# Characters no folder name can carry on some of the systems behind a manager
# (Windows Deluge, SMB shares of a NAS), dropped from the name of a new one
_FOLDER_UNSAFE = re.compile(r'[\\/:*?"<>|]+')
_SEPARATORS = "/\\"  # Of a path, a Windows Deluge using backslashes


def new_series_folder(name):
	"""The folder a series nobody has yet would get: one named after it
	("Title (Year)", the way the series folders are recognised) inside the
	folder for new series. None without that folder or a usable title"""
	identity = series_identity(name)
	if not (bot_settings.get("new_series_dir") or "").strip() or identity is None:
		return None
	title = " ".join(_FOLDER_UNSAFE.sub(" ", identity["title"] or "").split()).strip(". ")
	if not title:
		return None
	if identity["year"]:
		title = f"{title} ({identity['year']})"
	return new_series_folder_in(bot_settings.get("new_series_dir"), title)


def new_series_folder_in(base, title):
	"""base/title, whatever separator base ends with ("/" stays the root)"""
	base = _dir_key(base).rstrip(_SEPARATORS)
	# A Windows folder (D:\\Series) gets its own separator
	separator = "\\" if "\\" in base and "/" not in base else "/"
	return base + separator + title


# New series folders given out lately, {folder: when}: an episode of the same
# series added right after (two sent at once) does not see the first one in
# the manager yet, and would be told as a new series too
_new_series_given = {}
_new_series_lock = threading.Lock()
NEW_SERIES_MEMORY = 600  # Seconds a new series folder counts as given out


def claim_new_series_folder(target):
	"""True the first time target is given out as the folder of a new series,
	False when it already was a moment ago"""
	now = time.time()
	with _new_series_lock:
		for folder in [f for f, ts in _new_series_given.items() if now - ts > NEW_SERIES_MEMORY]:
			del _new_series_given[folder]
		if target in _new_series_given:
			return False
		_new_series_given[target] = now
		return True


def release_new_series_folder(folder):
	"""A new series folder given out that ended up with nothing in it (the
	add failed, or was undone): the next episode is new again"""
	with _new_series_lock:
		for given in [f for f in _new_series_given if same_dir(f, folder)]:
			del _new_series_given[given]


def has_video(files):
	"""Whether a list of file paths holds a video. None (not known yet, a
	magnet without its metadata) counts as yes: the name is all there is"""
	return files is None or any(is_video_file(path) for path in files)


def series_target(name, exclude_id=None, current_dir=None, files=None):
	"""Where a series goes without asking, as (folder, new): the only folder
	named after it, or, when it is nowhere at all, a new one in the folder for
	new series (new=True) when it is in one of the generic_dirs and has a
	video (a soundtrack named 'Show S01' is no episode). (None, False) when it
	stays where it is: not a series, already there, or a doubt the user is
	asked about instead. (None, None) when it could not be told: the manager
	or the parser failed while looking for the series"""
	# Looked for without current_dir: with it, a torrent already in the folder
	# of its series finds no folder either, and would look like a new series
	candidates = series_lookup(name, exclude_id=exclude_id)
	if candidates is None:
		# Not knowing where the series is never makes it a new one
		return None, None
	current = _dir_key(current_dir)
	if candidates:
		# What series_candidates does with current_dir
		if same_dir(candidates[0][0], current):
			return None, False
		return series_auto_dir([c for c in candidates if not same_dir(c[0], current)]), False
	# A new series only leaves the folders where everything lands before being
	# sorted: anywhere else (its own folder, one another app keeps...) someone
	# chose to put it there
	target = new_series_folder(name)
	if target is None or not has_video(files):
		return None, False
	generic = generic_dirs(strict=True)
	if generic is None:
		return None, None
	if current and not any(same_dir(current, d) for d in generic):
		return None, False
	return target, claim_new_series_folder(target)


def series_auto_target(pending, current_dir=None):
	"""Where a torrent about to be added goes without asking, as (folder,
	new), see series_target: only with the automatic move on"""
	if not auto_move_series_enabled():
		return None, False
	return series_target(pending_series_name(pending), current_dir=current_dir, files=torrent_data_files(pending.get("data")))


def series_suggestion(torrent, current_dir):
	"""The block that offers the series folder to a torrent that was added
	somewhere else, with its buttons: (text, markup), or (None, None) when
	there is nothing to offer. An auto managed torrent takes its folder from
	its category, and moving it by hand would drop it out of that"""
	if torrent.auto_managed:
		return None, None
	identity = series_identity(torrent.name)
	if identity is None:
		return None, None
	dirs = series_dirs(torrent.name, exclude_id=torrent.id, current_dir=current_dir)
	if not dirs:
		return None, None
	title = html.escape(identity["title"])
	if len(dirs) == 1:
		text = get_text("SERIES_SUGGEST_ONE", html.escape(dirs[0]), title)
	else:
		text = get_text("SERIES_SUGGEST_MANY", title)
	markup = InlineKeyboardMarkup(row_width=1)
	for directory in dirs:
		markup.add(InlineKeyboardButton(get_text("BUTTON_SERIES_MOVE", truncate_dir(directory)),
										callback_data=build_call("serMove", torrent.id, get_dir_id(directory))))
	markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=build_call("serDrop")))
	count_usage("auto_series_suggestion")
	return text, markup


def series_placed(torrent_id, title, previous_dir, new=False):
	"""The block that tells a torrent went straight to the folder of its
	series (a new one for a series that had none), with the button that sends
	it where it would have gone instead and the one that leaves it there and
	takes both buttons away"""
	text = get_text("SERIES_PLACED_NEW" if new else "SERIES_PLACED", html.escape(title))
	markup = None
	if previous_dir:
		markup = InlineKeyboardMarkup()
		markup.row(InlineKeyboardButton(get_text("BUTTON_SERIES_UNDO"), callback_data=build_call("serUndo", torrent_id, get_dir_id(previous_dir))),
				InlineKeyboardButton(get_text("BUTTON_SERIES_KEEP"), callback_data=build_call("serKeep")))
	return text, markup


def without_series_block(message_html):
	"""The add message without its block about the series folder (the text
	of the buttons being pressed), read back from what Telegram shows"""
	message_html = message_html or ""
	cut = -1
	for key in SERIES_BLOCK_KEYS:
		prefix = get_text(key).split("$", 1)[0]
		if prefix:
			cut = max(cut, message_html.rfind(f"\n\n{prefix}"))
	return message_html[:cut] if cut >= 0 else message_html


_DIR_LINE = re.compile(r"📂 <code>[^<]*</code>")


def with_dir_line(message_html, new_dir):
	"""The add message with its 📂 line pointing at new_dir: the message
	always tells where the torrent is, never how it got there"""
	return _DIR_LINE.sub(lambda _match: f"📂 <code>{html.escape(new_dir, quote=False)}</code>", message_html, count=1)


def place_series_from_message(chat_id, message_id, message_html, torrent_id, dir_id):
	"""A button under an add message sends the torrent to dir_id: the message
	itself is updated, showing the new folder in its 📂 line"""
	base = without_series_block(message_html)

	def wrap(text):
		return f"{base}\n\n{text}" if base else text

	new_dir = get_dir_by_id(dir_id)
	if new_dir is None:
		edit_message(chat_id, message_id, wrap(get_text("SERIES_EXPIRED")))
		return
	torrent = client.get_torrent(torrent_id)
	if torrent is None:
		edit_message(chat_id, message_id, wrap(get_text("TORRENT_NOT_FOUND")))
		return
	if torrent.download_dir:
		# Undone: the next episode finds that folder empty, a new series again
		release_new_series_folder(torrent.download_dir)
	progress_text = wrap(get_text("SERIES_MOVING", html.escape(new_dir)))
	success_text = with_dir_line(base, new_dir) if base else get_text("SERIES_MOVED", html.escape(new_dir))
	deliver_move_order(chat_id, [torrent_id], new_dir, progress_text, success_text, message_id=message_id, wrap=wrap)


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

def auto_dir_label():
	"""How the automatic download directory reads, or that the one of the
	torrent manager is used when there is none set"""
	auto_dir = bot_settings.get("auto_download_dir")
	return f"<code>{html.escape(auto_dir)}</code>" if auto_dir else get_text("AUTO_DIR_CLIENT_DEFAULT")


def auto_category():
	"""The configured automatic category, or an empty string when there is none.
	A category deleted in the manager since it was picked is not usable, and
	silently adding torrents to a category that no longer exists is worse than
	falling back to the directory"""
	category = bot_settings.get("auto_category") or ""
	if not category or not client.supports_categories:
		return ""
	if category not in [name for name, _ in get_known_categories()]:
		warning(f"The automatic category '{category}' no longer exists in the torrent manager")
		return ""
	return category


def auto_category_label():
	category = bot_settings.get("auto_category")
	return f"<b>{html.escape(category)}</b>" if category else get_text("AUTO_CATEGORY_NONE")


def auto_rename_label():
	"""What the automatic rename touches, so the two options under it read as
	what they are: independent scopes that add up"""
	if not bot_settings.get("auto_rename"):
		return get_text("DISABLED")
	scope = get_text("AUTO_RENAME_SCOPE_FILES" if bot_settings.get("auto_rename_files") else "AUTO_RENAME_SCOPE_TORRENT")
	source = get_text("AUTO_RENAME_SOURCE_ALL" if bot_settings.get("auto_rename_external") else "AUTO_RENAME_SOURCE_BOT")
	return get_text("SETTINGS_AUTO_RENAME_ON", scope, source)


def auto_move_series_label():
	"""Whether the series go straight to their folder, and which torrents"""
	if not auto_move_series_enabled():
		return get_text("DISABLED")
	source = get_text("AUTO_RENAME_SOURCE_ALL" if auto_move_series_enabled(external=True) else "AUTO_RENAME_SOURCE_BOT")
	return get_text("SETTINGS_AUTO_MOVE_SERIES_ON", source)


def _named_after(torrent_name, directory):
	"""Whether directory is named after the series of torrent_name (or is a
	season folder inside one)"""
	try:
		found = series_dir_candidates(torrent_name, [(torrent_name, directory, 0)])
	except Exception:
		return False
	return bool(found and found[0][1])


def _parent_dir(path):
	path = _dir_key(path)
	cut = max(path.rfind("/"), path.rfind("\\"))
	return path[:cut] if cut > 0 else ""


def series_of_folder(directory):
	"""The title of the series directory is the folder of (named after it, or
	a season folder inside one), going by the series in the manager. None for
	a folder of no series, or when the manager cannot be read"""
	try:
		torrents = client.get_torrents()
	except TorrentClientError:
		return None
	directory = _dir_key(directory)
	for torrent in torrents:
		identity = series_identity(torrent.name)
		if identity is not None and _named_after(torrent.name, directory):
			return identity["title"]
	return None


def series_parent_dirs():
	"""The folders that hold the folders of the series in the manager (where
	the user keeps their series), the most used first: what the folder for new
	series most likely is"""
	try:
		torrents = client.get_torrents()
	except TorrentClientError:
		return []
	counts = {}
	for torrent in torrents:
		directory = _dir_key(torrent.download_dir)
		if not directory or series_identity(torrent.name) is None or not _named_after(torrent.name, directory):
			continue
		parent = _parent_dir(directory)
		# A season folder: the series folder is the one above it
		if parent and _named_after(torrent.name, parent):
			parent = _parent_dir(parent)
		if parent:
			counts[parent] = counts.get(parent, 0) + 1
	return sorted(counts, key=lambda d: (-counts[d], d))[:SERIES_SUGGESTIONS_MAX]


def new_series_dir_problem(directory):
	"""Why directory looks like a wrong folder for new series, as the text
	that warns about it, or None. The folder of one series would get every new
	series inside it; the download folder is where they are now"""
	example = html.escape(new_series_folder_in(directory, "The Bear"))
	title = series_of_folder(directory)
	if title:
		return get_text("NEW_SERIES_DIR_LOOKS_SERIES", html.escape(directory), html.escape(title), example)
	if any(same_dir(directory, d) for d in generic_dirs()):
		return get_text("NEW_SERIES_DIR_LOOKS_GENERIC", html.escape(directory), example)
	return None


def new_series_dir_warning(directory, problem):
	"""The screen that stops a doubtful folder for new series from being
	saved, with the way to pick another one or to use it anyway"""
	dir_id = get_dir_id(directory)
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("BUTTON_NEW_SERIES_DIR_OTHER"), callback_data=build_call("newSerMenu", 0)))
	markup.add(InlineKeyboardButton(get_text("BUTTON_NEW_SERIES_DIR_FORCE"), callback_data=build_call("newSerForce", dir_id)))
	markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=settings_call("add")))
	return f"⚠️ {problem}", markup


def new_series_dir_label():
	"""Where a series with no folder yet goes"""
	base = bot_settings.get("new_series_dir")
	if not base:
		return get_text("NEW_SERIES_DIR_NONE")
	return get_text("NEW_SERIES_DIR_SET", html.escape(base))


# The settings are a main screen with one row per section and a screen per
# section, all painted on the same message: every screen ends with a way
# back to the one it came from and a way out

# Screen each toggle lives in, so pressing it repaints that same screen
SETTING_SCREENS = {
	"notify_completed": "notify", "notify_errors": "notify", "notify_external_added": "notify",
	"low_space_warning": "notify", "notify_ratio_reached": "notify",
	"auto_download": "add", "auto_move_series": "add", "auto_move_series_external": "add",
	"auto_rename": "rename", "auto_rename_files": "rename", "auto_rename_external": "rename",
	"auto_replace": "rename", "auto_remove_ratio": "limits", "notify_auto_removed": "notify",
	"auto_mover": "mover",
}


def auto_remove_ratio_enabled():
	return client.supports_ratio_limit and bool(bot_settings.get("auto_remove_ratio"))


def available_notices():
	"""The notices this manager can give. Where the ratio limit stops a
	torrent, there is one notice or the other: with the automatic removal on
	the torrent is gone, so the notice offering to keep it seeding gives way
	to the one telling it was removed"""
	notices = ["notify_completed", "notify_errors", "notify_external_added", "low_space_warning"]
	if client.supports_ratio_limit:
		notices.append("notify_auto_removed" if auto_remove_ratio_enabled() else "notify_ratio_reached")
	return notices


def settings_call(screen="main"):
	return build_call("settings", screen)


def settings_nav(markup, back=None):
	"""The last row of a settings screen: one step back, and out"""
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=back or settings_call()),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return markup


def toggle_button(setting_key, text_key, prefix=""):
	state = "✅" if bot_settings.get(setting_key) else "❌"
	return InlineKeyboardButton(f"{prefix}{state} {get_text(text_key)}", callback_data=build_call("toggleSetting", setting_key))


def limit_text(value, enabled):
	return f"{value} KB/s" if enabled else get_text("NO_LIMIT")


def speed_label(settings):
	"""How the speed reads in one line of the main screen"""
	if client.supports_alt_speed and settings["alt_speed_enabled"]:
		return get_text("SPEED_VALUE_ALT")
	if not settings["speed_limit_down_enabled"] and not settings["speed_limit_up_enabled"]:
		return get_text("NO_LIMIT")
	return get_text("SPEED_VALUE_LIMITS", limit_text(settings["speed_limit_down"], settings["speed_limit_down_enabled"]),
					limit_text(settings["speed_limit_up"], settings["speed_limit_up_enabled"]))


def on_off(value):
	return "✅" if value else "❌"


def build_settings_main():
	settings = client.get_settings()
	markup = InlineKeyboardMarkup(row_width=1)

	def row(text, call):
		markup.add(InlineKeyboardButton(text, callback_data=call))

	row(get_text("SETTINGS_ROW_LIMITS"), settings_call("limits"))
	notices = available_notices()
	enabled_notices = sum(1 for key in notices if bot_settings.get(key))
	row(get_text("SETTINGS_ROW_NOTIFY", enabled_notices, len(notices)), settings_call("notify"))
	add_mode = "ADD_MODE_AUTO" if bot_settings.get("auto_download") else "ADD_MODE_ASK"
	row(get_text("SETTINGS_ROW_ADD", get_text(add_mode)), settings_call("add"))
	if client.supports_rename:
		row(get_text("SETTINGS_ROW_RENAME", on_off(auto_rename_enabled())), settings_call("rename"))
	row(get_text("SETTINGS_ROW_FAV_DIRS", len(bot_settings.get("favorite_dirs") or [])), build_call("favDirsMenu"))
	row(get_text("SETTINGS_ROW_MOVER", on_off(auto_mover_enabled())), settings_call("mover"))
	row(get_text("SETTINGS_ROW_TELEMETRY", on_off(telemetry_on())), build_call("telemetryMenu"))
	settings_nav(markup, back=build_call("dashboard"))
	lines = [get_text("SETTINGS_TITLE", settings["version"]), "",
			get_text("SETTINGS_DEFAULT_DIR", html.escape(settings["download_dir"]))]
	return "\n".join(lines), markup


def build_settings_limits():
	"""The limits of the manager (speed and ratio) and the ones per tracker"""
	settings = client.get_settings()
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("SETTINGS_ROW_SPEED", speed_label(settings)), callback_data=settings_call("speed")))
	if client.supports_ratio_limit:
		markup.add(InlineKeyboardButton(get_text("SETTINGS_ROW_RATIO", global_ratio_label(client.get_global_ratio_limit())),
										callback_data=settings_call("ratio")))
	if tracker_limits_supported():
		markup.add(InlineKeyboardButton(get_text("SETTINGS_ROW_TRACKER_LIMITS", len(tracker_limits.rules())),
										callback_data=settings_call("tlimits")))
	lines = [get_text("SETTINGS_LIMITS_TITLE"), "", get_text("SETTINGS_LIMITS_HELP")]
	if client.supports_ratio_limit:
		markup.add(toggle_button("auto_remove_ratio", "BUTTON_SETTING_AUTO_REMOVE_RATIO"))
		lines += ["", get_text("SETTINGS_AUTO_REMOVE_RATIO_ON" if auto_remove_ratio_enabled() else "SETTINGS_AUTO_REMOVE_RATIO_OFF")]
	return "\n".join(lines), settings_nav(markup)


def build_settings_speed():
	settings = client.get_settings()
	lines = [get_text("SETTINGS_SPEED_TITLE"), "", get_text("SETTINGS_SPEED_HELP"), ""]
	if client.supports_alt_speed:
		lines.append(get_text("SETTINGS_ALT_SPEED", get_text("ENABLED") if settings["alt_speed_enabled"] else get_text("DISABLED"),
							settings["alt_speed_down"], settings["alt_speed_up"]))
	lines.append(get_text("SETTINGS_DOWN_LIMIT", limit_text(settings["speed_limit_down"], settings["speed_limit_down_enabled"])))
	lines.append(get_text("SETTINGS_UP_LIMIT", limit_text(settings["speed_limit_up"], settings["speed_limit_up_enabled"])))
	markup = InlineKeyboardMarkup(row_width=1)
	if client.supports_alt_speed:
		markup.add(InlineKeyboardButton(get_text("BUTTON_TOGGLE_ALT_SPEED"), callback_data=build_call("toggleAltSpeed")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TOGGLE_DOWN_LIMIT"), callback_data=build_call("toggleDownLimit")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TOGGLE_UP_LIMIT"), callback_data=build_call("toggleUpLimit")))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_SET_DOWN_LIMIT"), callback_data=build_call("setDownLimit")),
		InlineKeyboardButton(get_text("BUTTON_SET_UP_LIMIT"), callback_data=build_call("setUpLimit")),
	)
	return "\n".join(lines), settings_nav(markup, back=settings_call("limits"))


def global_ratio_label(limit):
	return f"{limit:g}" if limit >= 0 else get_text("NO_LIMIT")


def build_settings_ratio():
	limit = client.get_global_ratio_limit()
	lines = [get_text("SETTINGS_RATIO_TITLE"), "", get_text("SETTINGS_RATIO_HELP"), "",
			get_text("RATIO_LIMIT_NOW", global_ratio_label(limit))]
	if client.ratio_limit_copied_on_add:
		lines += ["", get_text("SETTINGS_RATIO_ONLY_NEW")]
	markup = InlineKeyboardMarkup()
	for row in ratio_option_rows(limit, "n" if limit < 0 else "v", lambda value: build_call("globalRatioSet", value),
								build_call("globalRatioAsk")):
		markup.row(*row)
	return "\n".join(lines), settings_nav(markup, back=settings_call("limits"))


def build_settings_notify():
	lines = [get_text("SETTINGS_NOTIFY_TITLE"), "", get_text("SETTINGS_NOTIFY_HELP")]
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(toggle_button("notify_completed", "BUTTON_SETTING_NOTIFY_COMPLETED"))
	markup.add(toggle_button("notify_errors", "BUTTON_SETTING_NOTIFY_ERRORS"))
	markup.add(toggle_button("notify_external_added", "BUTTON_SETTING_NOTIFY_EXTERNAL"))
	markup.add(toggle_button("low_space_warning", "BUTTON_SETTING_LOW_SPACE"))
	if "notify_ratio_reached" in available_notices():
		markup.add(toggle_button("notify_ratio_reached", "BUTTON_SETTING_NOTIFY_RATIO"))
	if "notify_auto_removed" in available_notices():
		markup.add(toggle_button("notify_auto_removed", "BUTTON_SETTING_NOTIFY_AUTO_REMOVED"))
	return "\n".join(lines), settings_nav(markup)


def build_settings_add():
	lines = [get_text("SETTINGS_ADD_TITLE"), "", get_text("SETTINGS_ADD_HELP"), ""]
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(toggle_button("auto_download", "BUTTON_SETTING_AUTO_DOWNLOAD"))
	if bot_settings.get("auto_download"):
		lines.append(get_text("SETTINGS_AUTO_DIR", auto_dir_label()))
		markup.add(InlineKeyboardButton(f"↳ {get_text('BUTTON_SETTING_AUTO_DIR')}", callback_data=build_call("autoDirMenu", 0)))
		if client.supports_categories:
			lines.append(get_text("SETTINGS_AUTO_CATEGORY", auto_category_label()))
			markup.add(InlineKeyboardButton(f"↳ {get_text('BUTTON_SETTING_AUTO_CATEGORY')}", callback_data=build_call("autoCatMenu")))
		lines.append(get_text("SETTINGS_AUTO_MOVE_SERIES", auto_move_series_label()))
		lines.append(get_text("SETTINGS_AUTO_MOVE_SERIES_HELP"))
		markup.add(toggle_button("auto_move_series", "BUTTON_SETTING_AUTO_MOVE_SERIES", prefix="↳ "))
		if bot_settings.get("auto_move_series"):
			markup.add(toggle_button("auto_move_series_external", "BUTTON_SETTING_AUTO_MOVE_SERIES_EXTERNAL", prefix="↳ "))
			lines.append(get_text("SETTINGS_NEW_SERIES_DIR", new_series_dir_label()))
			markup.add(InlineKeyboardButton(f"↳ {get_text('BUTTON_SETTING_NEW_SERIES_DIR')}", callback_data=build_call("newSerMenu", 0)))
		else:
			# What turning it on brings, the folder for new series included,
			# which is only shown once it is on
			lines.append(get_text("SETTINGS_AUTO_MOVE_SERIES_HINT"))
	else:
		lines.append(get_text("SETTINGS_ADD_ASKING"))
	return "\n".join(lines), settings_nav(markup)


def build_settings_rename():
	lines = [get_text("SETTINGS_RENAME_TITLE"), "", get_text("SETTINGS_RENAME_HELP"), "",
			get_text("SETTINGS_AUTO_RENAME", auto_rename_label())]
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(toggle_button("auto_rename", "BUTTON_SETTING_AUTO_RENAME"))
	if bot_settings.get("auto_rename"):
		markup.add(toggle_button("auto_rename_files", "BUTTON_SETTING_AUTO_RENAME_FILES", prefix="↳ "))
		markup.add(toggle_button("auto_rename_external", "BUTTON_SETTING_AUTO_RENAME_EXTERNAL", prefix="↳ "))
		markup.add(toggle_button("auto_replace", "BUTTON_SETTING_AUTO_REPLACE", prefix="↳ "))
		lines.append(get_text("SETTINGS_AUTO_REPLACE", get_text("REPLACE_MODE_AUTO" if bot_settings.get("auto_replace") else "REPLACE_MODE_ASK")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_SETTING_TEMPLATES"), callback_data=build_call("tplMenu")))
	return "\n".join(lines), settings_nav(markup)


# ---------------------------------------------------------------------------
# AUTOMATIC MOVER
# ---------------------------------------------------------------------------
# One rule, see auto_mover.py: when the source folder runs low on space, the
# torrents finished longest ago go to the same place under the target folder.
# The free space is the one the manager tells: the bot does not see the disks.
# A move is only told as done once the torrents are seen in their new folder:
# qBittorrent and Deluge take the order and move later, and say nothing when
# they could not

# The fields of the rule that are typed: field -> (setting, text of the question)
MOVER_FIELDS = {
	"source": ("auto_mover_source", "MOVER_ASK_SOURCE"),
	"target": ("auto_mover_target", "MOVER_ASK_TARGET"),
	"min": ("auto_mover_min_free_gb", "MOVER_ASK_MIN_FREE"),
	"wanted": ("auto_mover_wanted_free_gb", "MOVER_ASK_WANTED_FREE"),
	"days": ("auto_mover_min_days", "MOVER_ASK_DAYS"),
}

# What a pass found that the user has to know and the monitor tells only once
MOVER_QUIET_SECONDS = 120  # After a move, how long the errors of its torrents are still the mover's to tell
MOVER_MAX_CHOICES = 6  # Folders offered with a button for the source or the target
# What may follow a typed number, and what it is worth in GB or in days
MOVER_SIZE_UNITS = {"": 1, "g": 1, "gb": 1, "gib": 1, "gigas": 1, "t": 1024, "tb": 1024, "tib": 1024, "teras": 1024,
					"m": 1 / 1024, "mb": 1 / 1024, "mib": 1 / 1024, "megas": 1 / 1024}
MOVER_DAY_UNITS = {unit: 1 for unit in ("", "d", "dia", "dias", "día", "días", "day", "days")}
MOVER_PROBLEMS = ("none", "target_full", "same_disk", "unknown")
MOVER_UNKNOWN_PASSES = 3  # Passes in a row without the free space of the source before it is told

_mover_quiet = {}  # id -> until when the monitor leaves the errors of a torrent to the notice of the mover
_auto_mover_job = [None]  # {"count": torrents being moved} while a move of the mover is going on
_auto_mover_unknown = [0]  # Passes in a row in which the manager did not tell the free space of the source
_auto_mover_told = [None]  # The problem the monitor told last, so it is not told every five minutes
_auto_mover_lock = threading.Lock()
_auto_mover_run_lock = threading.Lock()  # One pass at a time, the monitor's or the one asked by hand


class MoverPlan:
	"""What a pass of the mover found, see auto_mover_plan"""

	def __init__(self, outcome, rule, free=-1, units=(), skipped=None, target_free=-1):
		self.outcome = outcome
		self.rule = rule
		self.free = free
		self.units = list(units)
		self.skipped = skipped or {}
		self.target_free = target_free
		self.torrents = [torrent for unit in self.units for torrent in unit.torrents]
		self.size = sum(unit.size for unit in self.units)


def mover_number(key):
	"""A margin of the rule, the default one when the file holds nonsense"""
	value = bot_settings.get(key)
	if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
		return bot_settings.DEFAULTS[key]
	return value


def auto_mover_rule():
	"""The rule as the settings hold it, with the margins in bytes and seconds"""
	return {
		"source": bot_settings.get("auto_mover_source") or "",
		"target": bot_settings.get("auto_mover_target") or "",
		"min_free": mover_number("auto_mover_min_free_gb") * auto_mover.GB,
		"wanted_free": mover_number("auto_mover_wanted_free_gb") * auto_mover.GB,
		"min_seconds": mover_number("auto_mover_min_days") * auto_mover.DAY,
	}


def auto_mover_configured():
	rule = auto_mover_rule()
	return bool(rule["source"] and rule["target"]) and auto_mover.paths_error(rule["source"], rule["target"]) is None


def auto_mover_enabled():
	return bool(bot_settings.get("auto_mover")) and auto_mover_configured()


def mover_free(path):
	"""Free bytes the manager tells for a folder, -1 when it does not"""
	free = client.get_free_space(path)
	return free if isinstance(free, (int, float)) and not isinstance(free, bool) and free >= 0 else -1


def mover_target_free(path):
	"""(free bytes, whether the folder is there) of the target. A manager only
	tells the space of a folder that exists, and the target, or the subfolder
	of it a torrent goes to, may still have to be made: its space is then the
	one of the closest folder above it that is there"""
	free = mover_free(path)
	if free >= 0:
		return free, True
	parent = auto_mover.normalize_dir(path)
	while parent.strip("/"):
		# Up to the root itself: a folder at the top that is not there is the
		# commonest slip, a path typed wrong
		parent = parent.rsplit("/", 1)[0] if "/" in parent.strip("/") else ("/" if parent.startswith("/") else "")
		if not parent:
			break
		free = mover_free(parent)
		if free >= 0:
			return free, False
	return -1, True


def auto_mover_plan(force=False):
	"""What the mover would do right now, a MoverPlan whose outcome is:
	"busy": a move it ordered is still going on.
	"unknown": the manager does not tell the free space of the source.
	"enough": there is no need to move anything.
	"wait": the manager is checking or moving torrents of the source, by hand
	or from before a restart of the bot: the space they free is not there yet.
	"none": space is needed, but no torrent may be moved (skipped tells why).
	"same_disk": the target tells the same free space as the source, so they
	are the same disk and moving would free nothing.
	"target_full": what would be moved does not fit in the target.
	"move": units holds what to move.
	force is the pass asked by hand, which does not wait for the space to drop
	under the minimum: it moves whenever the wanted free space is not there"""
	rule = auto_mover_rule()
	with _auto_mover_lock:
		if _auto_mover_job[0]:
			return MoverPlan("busy", rule)
	free = mover_free(rule["source"])
	if free < 0:
		return MoverPlan("unknown", rule)
	if free >= (rule["wanted_free"] if force else rule["min_free"]):
		return MoverPlan("enough", rule, free)
	torrents = client.get_torrents()
	if any(torrent.status == TorrentStatus.CHECKING and auto_mover.is_under(torrent.download_dir, rule["source"]) for torrent in torrents):
		return MoverPlan("wait", rule, free)
	units, skipped = auto_mover.candidates(torrents, rule["source"], rule["min_seconds"], time.time())
	picked = auto_mover.pick(units, free, rule["wanted_free"])
	if not picked:
		return MoverPlan("none", rule, free, skipped=skipped)
	plan = MoverPlan("move", rule, free, picked, skipped, mover_target_free(rule["target"])[0])
	if plan.target_free >= 0:
		# The source is read again: a download going on changes it between the
		# two readings, and one disk told twice would then look like two
		again = mover_free(rule["source"])
		if again >= 0 and min(free, again) <= plan.target_free <= max(free, again):
			plan.outcome = "same_disk"
		elif plan.size > plan.target_free:
			plan.outcome = "target_full"
	return plan


def mover_count(count):
	return get_text("MOVER_TORRENTS_ONE") if count == 1 else get_text("MOVER_TORRENTS", count)


def mover_days():
	days = mover_number("auto_mover_min_days")
	return get_text("MOVER_DAYS_ONE") if days == 1 else get_text("MOVER_DAYS_MANY", f"{days:g}")


def mover_listing(torrents, rule=None, reasons=None):
	"""One line per torrent, the first AUTO_MOVER_MAX_LISTED of them, the ones
	sharing their data on one line. With the rule, under the folder each one
	is in and the one it goes to; with reasons, {id: text}, the ones not moved for the same reason
	together, followed by it"""
	entries = {}  # (folder, name) -> [torrent, how many, reason]
	for torrent in torrents:
		entry = entries.setdefault((torrent.download_dir, torrent.name), [torrent, 0, (reasons or {}).get(str(torrent.id))])
		entry[1] += 1
	ordered = list(entries.values())
	if reasons:
		# The ones that failed for the same reason together, and the reason once
		distinct = list(dict.fromkeys(reason for _torrent, _count, reason in ordered))
		ordered.sort(key=lambda entry: distinct.index(entry[2]))
	if rule:
		# Each folder once, in the order they first show up, with all of its
		# torrents under it: they come by age, which mixes the folders
		folders = list(dict.fromkeys(entry[0].download_dir for entry in ordered))
		ordered.sort(key=lambda entry: folders.index(entry[0].download_dir))
	shown = ordered[:AUTO_MOVER_MAX_LISTED]
	lines, folder = [], None
	for index, (torrent, count, reason) in enumerate(shown):
		if rule and torrent.download_dir != folder:
			# From where to where: the torrents are still in the first one
			folder = torrent.download_dir
			target = auto_mover.target_dir(folder, rule["source"], rule["target"])
			lines.append(f"📁 <code>{html.escape(auto_mover.normalize_dir(folder))}</code> → <code>{html.escape(target or '')}</code>")
		line = f"• {html.escape(truncate(torrent.name, 60))} ({sizeof_fmt(torrent.total_size)})"
		if count > 1:
			line += f" ×{count}"
		lines.append(line)
		if reason and (index + 1 == len(shown) or shown[index + 1][2] != reason):
			lines.append(f"   <i>{html.escape(truncate(reason, 300))}</i>")
	if len(entries) > len(shown):
		lines.append(get_text("MOVER_AND_MORE", len(entries) - len(shown)))
	return "\n".join(lines)


def mover_tells(torrent_id):
	"""Whether an error of the torrent is the mover's to tell: it is moving
	it, or just did, and its notice names the ones that failed with why. The
	monitor telling each of them again would be the same news twice"""
	with _auto_mover_lock:
		until = _mover_quiet.get(str(torrent_id))
		if until is not None and until < time.time():
			del _mover_quiet[str(torrent_id)]
			until = None
		return until is not None


def mover_failure_reason(torrent_id):
	"""Why the manager says a torrent was not moved, or that it does not say"""
	try:
		torrent = client.get_torrent(torrent_id)
	except TorrentClientError:
		torrent = None
	message = (torrent.error_message if torrent else "") or ""
	# qBittorrent only tells "Error"
	return message if len(message) > 8 else get_text("MOVER_REASON_SILENT")


def mover_watch(rule, torrents, failed, stopped):
	"""Waits until the manager is done with the torrents whose move order it
	took. Returns the ones seen out of the source and adds to failed, {id:
	reason}, the ones it left there: in error, not being moved any more, or
	not there after AUTO_MOVER_TIMEOUT. One removed meanwhile is neither.
	stopped gets the ones the manager left stopped in error for not moving them"""
	pending = {str(torrent.id): torrent for torrent in torrents}
	moved = []
	started = time.time()
	while pending:
		try:
			listed = {str(torrent.id): torrent for torrent in client.get_torrents()}
		except TorrentClientError:
			listed = None  # Transmission does not answer while it moves
		elapsed = time.time() - started
		if listed is not None:
			for torrent_id, torrent in list(pending.items()):
				now = listed.get(torrent_id)
				if now is None:
					del pending[torrent_id]
				elif now.status == TorrentStatus.CHECKING:
					continue  # How qBittorrent and Deluge tell a torrent being moved
				elif not auto_mover.is_under(now.download_dir, rule["source"]):
					moved.append(torrent)
					del pending[torrent_id]
				elif auto_mover.in_error(now) or elapsed >= AUTO_MOVER_GRACE_SECONDS:
					failed[torrent_id] = mover_failure_reason(torrent_id)
					if auto_mover.in_error(now):
						stopped.append(torrent)
					del pending[torrent_id]
		if pending and elapsed >= AUTO_MOVER_TIMEOUT:
			for torrent_id in pending:
				failed[torrent_id] = get_text("MOVER_REASON_TIMEOUT")
			break
		if pending:
			time.sleep(AUTO_MOVER_WATCH_SECONDS)
	return moved


def mover_restart(torrents):
	"""Puts back as they were the torrents a failed move left stopped in
	error (Transmission, Deluge): their data is intact where it was, and left
	like that they would neither seed nor be moved once the problem is solved"""
	if not torrents:
		return
	try:
		client.resume_torrents([torrent.id for torrent in torrents])
		paused = [torrent.id for torrent in torrents if torrent.status == TorrentStatus.PAUSED]
		if paused:
			client.pause_torrents(paused)
	except TorrentClientError as e:
		warning(f"Automatic mover: cannot restart the torrents that were not moved: {e}")


def mover_occupied(source):
	"""The bytes the torrents of the source have on disk, the ones sharing
	their data counted once. None when the manager does not answer"""
	try:
		torrents = client.get_torrents()
	except TorrentClientError:
		return None
	sizes = {}
	for torrent in torrents:
		if torrent.download_dir and auto_mover.is_under(torrent.download_dir, source):
			size = (torrent.total_size or 0) * min(max(torrent.progress or 0, 0), 100) / 100
			key = (torrent.download_dir, torrent.name)
			sizes[key] = max(sizes.get(key, 0), size)
	return sum(sizes.values())


def mover_job(plan, sent, chat_id, thread_id):
	"""The move of a plan, in its own thread: orders it, waits for the manager
	to be done and tells how it went on the notice that announced it. After a
	failure, or a move that freed nothing, the mover is turned off: the next
	pass would only run into the same"""
	rule = plan.rule
	source, target = html.escape(rule["source"]), html.escape(rule["target"])
	ordered, failed = [], {}
	occupied = mover_occupied(rule["source"])
	for target_dir, group in auto_mover.group_by_target(plan.units, rule["source"], rule["target"]):
		ids = [torrent.id for torrent in group]
		order = register_move_order(ids)
		reason = run_move_order(lambda: client.move_torrents(ids, target_dir), ids, order, target_dir, plain=True)
		if reason is None:
			ordered += group
		elif reason != get_text("MOVE_SUPERSEDED"):
			# Superseded: sent somewhere else by hand meanwhile, which is no failure
			failed.update({str(torrent.id): reason for torrent in group})
	stopped = []
	moved = mover_watch(rule, ordered, failed, stopped)
	mover_restart(stopped)
	blocks = [get_text("SETTINGS_MOVER_TITLE")]
	if moved:
		blocks.append(f"{get_text('MOVER_DONE', mover_count(len(moved)), mover_size(moved), source, target)}\n{mover_listing(moved, rule)}")
	not_moved = [torrent for torrent in plan.torrents if str(torrent.id) in failed]
	if not_moved:
		blocks.append(f"{get_text('MOVER_FAILED', mover_count(len(not_moved)))}\n{mover_listing(not_moved, reasons=failed)}")
	if not moved and not not_moved:
		blocks.append(get_text("MOVER_GONE"))
	turned_off = bool(not_moved) and bool(bot_settings.get("auto_mover"))
	if turned_off:
		bot_settings.set("auto_mover", False)
		blocks.append(get_text("MOVER_TURNED_OFF"))
	done = "\n\n".join(blocks)
	# Good news takes the place of the notice. Bad news is a message of its
	# own: Telegram tells nobody of an edit, and this one says the mover is off
	if not sent or not_moved or not edit_message(chat_id, sent.message_id, done):
		if sent:
			delete_message(chat_id, sent.message_id)
		send_message(chat_id, done, thread_id=thread_id)
	bot_settings.set("auto_mover_notice", None)
	# The space the move freed is only trusted a while later: qBittorrent
	# takes about half a minute to tell it
	if not moved:
		return
	with _auto_mover_lock:
		if _auto_mover_job[0]:
			_auto_mover_job[0]["settling"] = True  # Told as moved: what is left is the wait
	time.sleep(AUTO_MOVER_SETTLE_SECONDS)
	if not not_moved and bot_settings.get("auto_mover"):
		free = mover_free(rule["source"])
		occupied_now = mover_occupied(rule["source"])
		if free < 0 or occupied is None or occupied_now is None:
			return
		# Free space plus what the torrents of the source take stays the same
		# when what left the source freed its room, whatever was downloaded
		# or added meanwhile. It drops by what was moved when it freed nothing
		# Nearly all of it, so that something else writing to the disk
		# meanwhile is not taken for a move that freed nothing
		lost = (plan.free + occupied) - (free + occupied_now)
		if lost > mover_size_bytes(moved) * 0.9:
			bot_settings.set("auto_mover", False)
			send_message(chat_id, f"{get_text('SETTINGS_MOVER_TITLE')}\n\n{get_text('MOVER_NO_GAIN', sizeof_fmt(plan.free), sizeof_fmt(free))}", thread_id=thread_id)


def mover_size(torrents):
	return sizeof_fmt(mover_size_bytes(torrents))


def mover_size_bytes(torrents):
	"""What the torrents take on disk, the ones sharing their data counted once"""
	seen, total = set(), 0
	for torrent in torrents:
		key = (torrent.download_dir, torrent.name)
		if key not in seen:
			seen.add(key)
			total += torrent.total_size or 0
	return total


def run_auto_mover(force=False, chat_id=None, thread_id=None):
	"""One pass of the mover: when there is something to move, tells it and
	moves it in the background. Returns the MoverPlan of the pass"""
	with _auto_mover_run_lock:
		plan = auto_mover_plan(force)
		if plan.outcome != "move":
			return plan
		with _auto_mover_lock:
			_auto_mover_job[0] = {"count": len(plan.torrents)}
			_mover_quiet.update({str(torrent.id): float("inf") for torrent in plan.torrents})
	count_usage("auto_mover")
	rule = plan.rule
	debug(f"Automatic mover: {sizeof_fmt(plan.free)} free in {rule['source']}, moving {len(plan.torrents)} torrents to {rule['target']}")
	text = "\n\n".join([
		get_text("SETTINGS_MOVER_TITLE"),
		f"{get_text('MOVER_MOVING', mover_count(len(plan.torrents)), sizeof_fmt(plan.size), html.escape(rule['source']), html.escape(rule['target']), sizeof_fmt(plan.free), sizeof_fmt(plan.free + plan.size))}"
		f"\n{mover_listing(plan.torrents, rule)}",
		*mover_short_text(plan),
		get_text("MOVER_MOVING_WAIT")])
	sent = notify(text, chat_id=chat_id, thread_id=thread_id)
	if chat_id is None:
		chat_id, thread_id = (TELEGRAM_GROUP if TELEGRAM_GROUP else ADMIN_IDS[0]), TELEGRAM_THREAD
	if sent:
		# Remembered across a restart, see mover_close_lost_notice
		bot_settings.set("auto_mover_notice", [chat_id, sent.message_id, thread_id])

	def worker():
		try:
			mover_job(plan, sent, chat_id, thread_id)
		except Exception as e:
			error(f"Automatic mover: {e}")
			mover_close_lost_notice()
		finally:
			with _auto_mover_lock:
				_auto_mover_job[0] = None
				# The monitor may still be about to see what the move left
				until = time.time() + MOVER_QUIET_SECONDS
				_mover_quiet.update({str(torrent.id): until for torrent in plan.torrents})

	threading.Thread(target=worker, daemon=True).start()
	return plan


def mover_close_lost_notice():
	"""The notice of a move nobody is following any more, because the bot was
	restarted or failed meanwhile, would say for ever that it will change:
	it is replaced by one telling that how it ended is not known"""
	notice = bot_settings.get("auto_mover_notice")
	if not isinstance(notice, list) or len(notice) != 3:
		return
	bot_settings.set("auto_mover_notice", None)
	chat_id, message_id, thread_id = notice
	delete_message(chat_id, message_id)
	send_message(chat_id, f"{get_text('SETTINGS_MOVER_TITLE')}\n\n{get_text('MOVER_LOST')}", thread_id=thread_id)


def auto_mover_tick():
	"""The pass of the monitor. Silent unless it moves something, or finds a
	problem the user has to solve, which is told once and not on every pass"""
	if not auto_mover_enabled():
		_auto_mover_told[0] = None
		return
	plan = run_auto_mover()
	if plan.outcome == "unknown":
		# A manager that is busy or down does not tell it either: only after
		# a few passes in which it does answer is the folder the problem
		_auto_mover_unknown[0] += 1
		if _auto_mover_unknown[0] < MOVER_UNKNOWN_PASSES or _auto_mover_told[0] == "unknown":
			return
		try:
			client.get_torrents()
		except TorrentClientError:
			return
	else:
		_auto_mover_unknown[0] = 0
	if plan.outcome in MOVER_PROBLEMS:
		if _auto_mover_told[0] != plan.outcome:
			_auto_mover_told[0] = plan.outcome
			notify(f"{get_text('SETTINGS_MOVER_TITLE')}\n\n{mover_outcome_text(plan)}")
	elif plan.outcome in ("enough", "move"):
		_auto_mover_told[0] = None


def mover_skipped_lines(plan):
	"""Why the torrents of the source that are not moved stay, one line per reason"""
	texts = {
		auto_mover.SKIP_RECENT: lambda count: get_text("MOVER_SKIP_RECENT", mover_count(count), mover_days()),
		auto_mover.SKIP_UNFINISHED: lambda count: get_text("MOVER_SKIP_UNFINISHED", mover_count(count)),
		auto_mover.SKIP_BUSY: lambda count: get_text("MOVER_SKIP_BUSY", mover_count(count)),
		auto_mover.SKIP_MANAGED: lambda count: get_text("MOVER_SKIP_MANAGED", mover_count(count)),
		auto_mover.SKIP_UNDATED: lambda count: get_text("MOVER_SKIP_UNDATED", mover_count(count)),
	}
	return [texts[reason](plan.skipped[reason]) for reason in auto_mover.SKIP_REASONS if plan.skipped.get(reason)]


def mover_short_text(plan):
	"""[what to tell] when moving everything that can be moved does not get
	to the free space wanted: that it will not, and what holds the rest back.
	Empty when it gets there"""
	rule = plan.rule
	if plan.free + plan.size >= rule["wanted_free"]:
		return []
	wanted, source = sizeof_fmt(rule["wanted_free"]), html.escape(rule["source"])
	skipped = mover_skipped_lines(plan)
	if not skipped:
		return [get_text("MOVER_SHORT_ALL", wanted, source)]
	return ["\n".join([get_text("MOVER_SHORT_REST", wanted)] + skipped)]


def mover_busy_text(job):
	"""Why nothing is moved right now: a move is going on, or one just ended
	and the manager is given a while to tell the space it freed"""
	if job.get("settling"):
		return get_text("MOVER_SETTLING")
	return get_text("MOVER_BUSY", mover_count(job.get("count", 0)))


def mover_outcome_text(plan):
	"""What a pass would do, or why it does nothing"""
	rule = plan.rule
	source = html.escape(rule["source"])
	if plan.outcome == "busy":
		with _auto_mover_lock:
			job = _auto_mover_job[0] or {}
		return mover_busy_text(job)
	if plan.outcome == "unknown":
		return mover_unknown_text()
	if plan.outcome == "wait":
		return get_text("MOVER_WAIT", source)
	if plan.outcome == "enough":
		return get_text("MOVER_ENOUGH", sizeof_fmt(plan.free), source, sizeof_fmt(rule["wanted_free"]))
	if plan.outcome == "none":
		lines = [get_text("MOVER_NONE", sizeof_fmt(plan.free), source)] + (mover_skipped_lines(plan) or [get_text("MOVER_NONE_EMPTY")])
		return "\n".join(lines)
	if plan.outcome in ("same_disk", "target_full"):
		# Nothing will be moved: what it would be is told without the room it would leave
		due = get_text("MOVER_DUE", sizeof_fmt(plan.free), source, mover_count(len(plan.torrents)), sizeof_fmt(plan.size))
		why = (get_text("MOVER_SAME_DISK") if plan.outcome == "same_disk"
			else get_text("MOVER_TARGET_FULL", html.escape(rule["target"]), sizeof_fmt(plan.target_free)))
		return f"{due}\n{mover_listing(plan.torrents, rule)}\n\n{why}"
	would = get_text("MOVER_WOULD_MOVE", sizeof_fmt(plan.free), source, mover_count(len(plan.torrents)), sizeof_fmt(plan.size), sizeof_fmt(plan.free + plan.size))
	return "\n\n".join([f"{would}\n{mover_listing(plan.torrents, rule)}"] + mover_short_text(plan))


def mover_unknown_text():
	"""The manager does not tell the free space of the source. qBittorrent only
	knows the one of its default folder, which is then named"""
	text = get_text("MOVER_FREE_UNKNOWN")
	if TORRENT_CLIENT != "qbittorrent":
		# The others tell it of any folder that is there
		return f"{text} {get_text('MOVER_FREE_UNKNOWN_MISSING', html.escape(auto_mover_rule()['source']))}"
	try:
		default_dir = client.get_default_download_dir()
	except TorrentClientError:
		default_dir = ""
	if default_dir:
		text += " " + get_text("MOVER_FREE_UNKNOWN_HINT", html.escape(default_dir))
	return text


def mover_dir_line(text_key, directory, free):
	if not directory:
		return get_text(text_key, get_text("MOVER_NOT_SET"))
	line = get_text(text_key, f"<code>{html.escape(directory)}</code>")
	return f"{line} · {get_text('MOVER_FREE', sizeof_fmt(free))}" if free >= 0 else line


def build_settings_mover():
	rule = auto_mover_rule()
	configured = auto_mover_configured()
	with _auto_mover_lock:
		job = _auto_mover_job[0]
	# Not asked while the mover moves: Transmission answers nothing meanwhile
	asked = not job or job.get("settling")
	free = mover_free(rule["source"]) if rule["source"] and asked else -1
	target_free, target_there = mover_target_free(rule["target"]) if rule["target"] and asked else (-1, True)
	enabled = auto_mover_enabled()
	lines = [get_text("SETTINGS_MOVER_TITLE"), "", get_text("SETTINGS_MOVER_HELP"), "",
			mover_dir_line("MOVER_SOURCE", rule["source"], free),
			# Not the room of a folder that is not there: it is the one of another
			mover_dir_line("MOVER_TARGET", rule["target"], target_free if target_there else -1),
			get_text("MOVER_MIN_FREE", sizeof_fmt(rule["min_free"])),
			get_text("MOVER_WANTED_FREE", sizeof_fmt(rule["wanted_free"])),
			get_text("MOVER_DAYS", mover_days()) if rule["min_seconds"] else get_text("MOVER_DAYS_ANY"), ""]
	if not configured:
		lines.append(get_text("MOVER_NEEDS_DIRS"))
	elif job:
		lines.append(mover_busy_text(job))
	elif free < 0:
		lines.append(mover_unknown_text())
	elif 0 <= target_free == free:
		lines.append(get_text("MOVER_SAME_DISK"))
	else:
		if not target_there:
			lines.append(get_text("MOVER_TARGET_MISSING", html.escape(rule["target"])))
		elif target_free < 0:
			lines.append(get_text("MOVER_TARGET_UNKNOWN"))
		# A stop that not even an empty source would reach: everything would go, every time
		occupied = mover_occupied(rule["source"])
		if occupied is not None and free + occupied < rule["wanted_free"]:
			lines.append(get_text("MOVER_WANTED_TOO_HIGH", sizeof_fmt(rule["wanted_free"]), sizeof_fmt(free + occupied)))
		lines.append(get_text("MOVER_STATUS_OFF" if not enabled else "MOVER_STATUS_DUE" if free < rule["min_free"] else "MOVER_STATUS_OK"))
	markup = InlineKeyboardMarkup(row_width=1)
	# No switch to turn on what could not work, but always one to turn it off
	if enabled or (configured and (free >= 0 or not asked)):
		markup.add(toggle_button("auto_mover", "BUTTON_SETTING_AUTO_MOVER"))

	def ask(field, text_key):
		return InlineKeyboardButton(get_text(text_key), callback_data=build_call("moverAsk", field))

	markup.row(ask("source", "BUTTON_MOVER_SOURCE"), ask("target", "BUTTON_MOVER_TARGET"))
	markup.row(ask("min", "BUTTON_MOVER_MIN_FREE"), ask("wanted", "BUTTON_MOVER_WANTED_FREE"))
	markup.add(ask("days", "BUTTON_MOVER_DAYS"))
	if configured:
		markup.add(InlineKeyboardButton(get_text("BUTTON_MOVER_PREVIEW"), callback_data=build_call("moverPreview")))
	return "\n".join(lines), settings_nav(markup)


def build_mover_preview():
	"""What the pass asked by hand would move right now, and the button to do it"""
	plan = auto_mover_plan(force=True)
	lines = [get_text("MOVER_PREVIEW_TITLE"), "", mover_outcome_text(plan)]
	if plan.outcome == "move" and plan.free >= plan.rule["min_free"]:
		lines += ["", get_text("MOVER_NOT_YET", sizeof_fmt(plan.rule["min_free"]))]
	if plan.outcome == "move" and not mover_target_free(plan.rule["target"])[1]:
		lines += ["", get_text("MOVER_TARGET_MISSING", html.escape(plan.rule["target"]))]
	markup = InlineKeyboardMarkup(row_width=1)
	if plan.outcome == "move":
		markup.add(InlineKeyboardButton(get_text("BUTTON_MOVER_RUN"), callback_data=build_call("moverRun")))
	return "\n".join(lines), settings_nav(markup, back=settings_call("mover"))


def mover_ask_text(field):
	"""The question of a typed field"""
	return get_text(MOVER_FIELDS[field][1])


def mover_folder_choices(field):
	"""[(folder, [subfolders in use])] to offer for the source or the target:
	the folders that hold the ones in use, without those that cannot go with
	the other folder of the rule"""
	try:
		dirs = client.get_download_dirs()
	except TorrentClientError:
		return []
	other = bot_settings.get("auto_mover_target" if field == "source" else "auto_mover_source") or ""
	choices = []
	roots = auto_mover.root_folders(dirs)
	for root in roots:
		if auto_mover.paths_error(root, other):
			continue
		inside = [auto_mover.normalize_dir(d).strip("/")[len(root.strip("/")):].strip("/") for d in dirs if auto_mover.is_under(d, root)]
		choices.append((root, [sub for sub in inside if sub]))
	# A folder the manager names but is not there (its default one, never
	# used) is not offered, where the manager tells which ones are
	try:
		there = {root: mover_free(root) >= 0 for root in roots}
	except TorrentClientError:
		there = {}
	if any(there.values()):
		choices = [choice for choice in choices if there[choice[0]]]
	# The folder the manager downloads to, as it is, when the one above it
	# was left out: qBittorrent only tells the free space of that one
	try:
		default_dir = auto_mover.normalize_dir(client.get_default_download_dir() or "")
		offered = [root for root, _inside in choices]
		if (default_dir.strip("/") and not any(auto_mover.is_under(default_dir, root) for root in offered)
				and not auto_mover.paths_error(default_dir, other) and mover_free(default_dir) >= 0):
			choices.insert(0, (default_dir, []))
	except TorrentClientError:
		pass
	return choices[:MOVER_MAX_CHOICES]


def build_mover_folder(field):
	"""The folders to pick the source or the target from, with what each one
	holds: the rule is about the folder above, not about one of its
	subfolders. None when there is none to offer, and the folder is typed"""
	choices = mover_folder_choices(field)
	if not choices:
		return None
	current = bot_settings.get(MOVER_FIELDS[field][0]) or ""
	lines = [get_text("MOVER_PICK_SOURCE" if field == "source" else "MOVER_PICK_TARGET"), ""]
	markup = InlineKeyboardMarkup()
	for root, inside in choices:
		line = f"📁 <code>{html.escape(root)}</code>"
		if inside:
			shown = ", ".join(html.escape(truncate(sub, 30)) for sub in inside[:3]) + ("…" if len(inside) > 3 else "")
			line += f"\n   {get_text('MOVER_PICK_INSIDE', shown)}"
		lines.append(line)
		label = f"{'✅' if root == current else '📁'} {truncate(root, 40)}"
		markup.add(InlineKeyboardButton(label, callback_data=build_call("moverSet", field, get_dir_id(root))))
	markup.add(InlineKeyboardButton(get_text("BUTTON_WRITE_DIR"), callback_data=build_call("moverType", field)))
	return "\n".join(lines), settings_nav(markup, back=settings_call("mover"))


def mover_path(text):
	"""A typed folder the way the manager writes its own: with the leading
	slash, or without it where the manager has none (Download Station)"""
	directory = auto_mover.normalize_dir(text).strip("/")
	try:
		default_dir = client.get_default_download_dir() or ""
	except TorrentClientError:
		default_dir = ""
	return directory if default_dir and not default_dir.startswith("/") else f"/{directory}"


def set_mover_field(field, text):
	"""Stores a typed field of the rule. Returns what to tell about it: why
	it cannot be, or what else changed with it. None once stored as it came"""
	key = MOVER_FIELDS[field][0]
	if field in ("source", "target"):
		directory = mover_path(text)
		other = bot_settings.get("auto_mover_target" if field == "source" else "auto_mover_source") or ""
		if auto_mover.paths_error(directory, other):
			return get_text("MOVER_ERROR_NESTED")
		bot_settings.set(key, directory)
		_auto_mover_told[0] = None  # Another rule: what was told of the old one may be so again
		return None
	# "50", "50 GB", "0,5", "1 TB": the number, in the unit that comes with it.
	# Another unit is not guessed: 500 MB taken for 500 GB would move it all
	match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([a-zñáí]*)", text.strip().lower())
	units = MOVER_DAY_UNITS if field == "days" else MOVER_SIZE_UNITS
	if not match or match.group(2) not in units:
		return get_text("MOVER_ERROR_DAYS" if field == "days" else "MOVER_ERROR_NUMBER")
	number = round(float(match.group(1).replace(",", ".")) * units[match.group(2)], 3)
	number = int(number) if number == int(number) else number
	if field != "days" and number <= 0:
		return get_text("MOVER_ERROR_ZERO")
	bot_settings.set(key, number)
	_auto_mover_told[0] = None
	# The start stays under the stop: the other one follows, and it is told,
	# instead of refusing a value until the other is changed first
	if field == "min" and number >= mover_number("auto_mover_wanted_free_gb"):
		bot_settings.set("auto_mover_wanted_free_gb", number * 2)
		return get_text("MOVER_ADJUSTED_WANTED", sizeof_fmt(number * 2 * auto_mover.GB))
	if field == "wanted" and number <= mover_number("auto_mover_min_free_gb"):
		bot_settings.set("auto_mover_min_free_gb", round(number / 2, 3))
		return get_text("MOVER_ADJUSTED_MIN", sizeof_fmt(round(number / 2, 3) * auto_mover.GB))
	return None


SETTINGS_SCREENS = {
	"main": build_settings_main,
	"limits": build_settings_limits,
	"speed": build_settings_speed,
	"ratio": build_settings_ratio,
	"notify": build_settings_notify,
	"add": build_settings_add,
	"rename": build_settings_rename,
	"tlimits": build_settings_tracker_limits,
	"mover": build_settings_mover,
}


def build_settings(screen="main"):
	"""(text, markup) of a settings screen; an unknown one is the main screen.
	The rename screen does not exist for a manager that cannot rename"""
	if ((screen == "rename" and not client.supports_rename) or (screen == "ratio" and not client.supports_ratio_limit)
			or (screen == "tlimits" and not tracker_limits_supported())):
		screen = "main"
	return SETTINGS_SCREENS.get(screen, build_settings_main)()


def render_settings(chat_id, message_id, screen="main"):
	try:
		text, markup = build_settings(screen)
	except TorrentClientError as e:
		text = client_error_text(e)
		markup = back_close_markup()
	edit_message(chat_id, message_id, text, markup)


def send_settings_menu(chat_id, thread_id=None, prefix=None, screen="main"):
	"""Sends a settings screen as a new message, optionally preceded by a confirmation"""
	try:
		text, markup = build_settings(screen)
	except TorrentClientError as e:
		text = client_error_text(e)
		markup = back_close_markup()
	if prefix:
		text = f"{prefix}\n\n{text}"
	send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)


# ---------------------------------------------------------------------------
# ANONYMOUS STATISTICS
# ---------------------------------------------------------------------------
# Once a day, a snapshot of the settings and how many times each command and
# button was used, sent to telemetry.dgongut.com. Numbers, yes/no and values
# from closed lists only: never a torrent, a directory, a category or an id.
# What may be sent is declared in projects/torrent-controller-bot.yaml of the
# telemetry repo, whose server drops anything else, and is listed on
# https://stats.dgongut.com/torrent-controller-bot/privacy
#
# On by default, with no notice asking first. Nothing is counted or sent unless
# CONFIG_PATH is a volume, TELEMETRY=false is not set and the setting is on.
# Without a volume the install id would be new on every recreate, so one
# installation would count as many, and turning it off would not survive the
# next update.

TELEMETRY_PROJECT = "torrent-controller-bot"
TELEMETRY_PREVIEW_USAGE_KEYS = 25  # Shown instead of the whole list when it would not fit a message
_config_persistent = None


def config_is_persistent():
	"""Whether CONFIG_PATH survives a container recreate. os.path.ismount
	compares device ids, which is enough for the volumes Docker creates, but
	/proc/mounts is read too. Worked out once: it cannot change while running"""
	global _config_persistent
	if _config_persistent is None:
		path = os.path.abspath(_config_module.CONFIG_PATH)
		persistent = False
		try:
			persistent = os.path.ismount(path)
		except OSError:
			pass
		if not persistent:
			try:
				with open("/proc/mounts", "r", encoding="utf-8") as mounts:
					persistent = any(len(fields) > 1 and fields[1] == path for fields in (line.split() for line in mounts))
			except OSError:
				pass
		_config_persistent = persistent
	return _config_persistent


def telemetry_forced_off():
	"""Why the statistics are off whatever the setting says: "TELEMETRY" when
	the variable turned them off, "volume" when the settings would not survive
	a recreate, or None when nothing overrides the setting"""
	if telemetry.disabled_by_environment():
		return "TELEMETRY"
	if not config_is_persistent():
		return "volume"
	return None


def telemetry_on():
	return telemetry_forced_off() is None and bool(bot_settings.get("telemetry"))


def collect_telemetry_metrics():
	"""The snapshot sent with each ping, on the telemetry thread. Every key has
	to be declared in the manifest, or the server drops it. What needs the
	torrent manager is left out, rather than sent as zero, when it does not answer"""
	client_name = (TORRENT_CLIENT or "transmission").lower()
	metrics = {
		"client": client_name,
		"language": LANGUAGE.lower(),
		"admins": len([admin for admin in ADMIN_IDS if admin]),
		"telegram_group": bool(TELEGRAM_GROUP),
		"torrents_per_page": TORRENTS_PER_PAGE,
		"notify_completed": bool(bot_settings.get("notify_completed")),
		"notify_errors": bool(bot_settings.get("notify_errors")),
		"notify_external_added": bool(bot_settings.get("notify_external_added")),
		"notify_ratio_reached": client.supports_ratio_limit and bool(bot_settings.get("notify_ratio_reached")),
		"auto_remove_ratio": auto_remove_ratio_enabled(),
		"notify_auto_removed": auto_remove_ratio_enabled() and bool(bot_settings.get("notify_auto_removed")),
		"auto_download": bool(bot_settings.get("auto_download")),
		"auto_download_dir": bool(bot_settings.get("auto_download_dir")),
		"auto_category": bool(bot_settings.get("auto_category")),
		"auto_rename": auto_rename_enabled(),
		"auto_rename_files": auto_rename_enabled() and bool(bot_settings.get("auto_rename_files")),
		"auto_rename_external": auto_rename_enabled() and bool(bot_settings.get("auto_rename_external")),
		"auto_replace": auto_replace_enabled(),
		"auto_move_series": auto_move_series_enabled(),
		"auto_move_series_external": auto_move_series_enabled(external=True),
		"new_series_dir": auto_move_series_enabled() and bool(bot_settings.get("new_series_dir")),
		"low_space_warning": bool(bot_settings.get("low_space_warning")),
		"auto_mover": auto_mover_enabled(),
		"favorite_dirs": len(bot_settings.get("favorite_dirs") or []),
		"custom_templates": sum(1 for key in ("template_movie", "template_series", "template_season") if bot_settings.get(key)),
		"tracker_limits": len(tracker_limits.rules()) if tracker_limits_supported() else 0,
	}
	try:
		match = re.search(r"(\d+)\.", client.test_connection())
		if match:
			metrics[f"{client_name}_major"] = int(match.group(1))
	except Exception as e:
		debug(f"Telemetry could not read the torrent manager version: {e}")
	try:
		metrics["torrents"] = len(client.get_torrents())
	except Exception as e:
		debug(f"Telemetry could not count the torrents: {e}")
	return metrics


telemetry_client = telemetry.Telemetry(
	project=TELEMETRY_PROJECT,
	version=VERSION,
	state_path=os.path.join(_config_module.CONFIG_PATH, "telemetry.json"),
	metrics=collect_telemetry_metrics,
	enabled=telemetry_on,
	endpoint=TELEMETRY_ENDPOINT,
	log=debug,
	debug=TELEMETRY_DEBUG,
)


def count_usage(key):
	"""Counts one use of key for the next ping. Never raises into the caller"""
	try:
		telemetry_client.count(key)
	except Exception as e:
		debug(f"Telemetry could not count {key}: {e}")


def build_telemetry_settings():
	"""What the statistics are, the switch and what they send. When something
	outside the menu turned them off there is no switch, only the reason,
	because pressing it would change nothing"""
	forced = telemetry_forced_off()
	lines = [get_text("TELEMETRY_TITLE"), "", get_text("TELEMETRY_HELP")]
	markup = InlineKeyboardMarkup(row_width=1)
	if forced == "volume":
		lines += ["", get_text("TELEMETRY_FORCED_VOLUME", html.escape(_config_module.CONFIG_PATH))]
	elif forced:
		lines += ["", get_text("TELEMETRY_FORCED_ENV", forced)]
	else:
		state = "✅" if bot_settings.get("telemetry") else "❌"
		markup.add(InlineKeyboardButton(f"{state} {get_text('BUTTON_TELEMETRY_TOGGLE')}", callback_data=build_call("toggleTelemetry")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TELEMETRY_SHOW"), callback_data=build_call("telemetryShow")))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("settings")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def build_telemetry_preview():
	"""Exactly what the next ping would carry. The usage counters are cut to
	the most used ones when the whole list would not fit in a message"""
	payload = telemetry_client.preview()
	usage = payload.get("usage") or {}
	hidden = 0
	if len(usage) > TELEMETRY_PREVIEW_USAGE_KEYS:
		ranked = sorted(usage.items(), key=lambda item: (-item[1], item[0]))
		payload["usage"] = dict(ranked[:TELEMETRY_PREVIEW_USAGE_KEYS])
		hidden = len(usage) - TELEMETRY_PREVIEW_USAGE_KEYS
	body = html.escape(json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True))
	text = f"{get_text('TELEMETRY_PREVIEW')}\n\n<pre>{body}</pre>"
	if hidden:
		text += f"\n{get_text('TELEMETRY_PREVIEW_TRUNCATED', hidden)}"
	return text, close_markup()


def build_favorite_dirs_menu():
	favorites = bot_settings.get("favorite_dirs") or []
	lines = [get_text("FAV_DIRS_TITLE"), ""]
	if favorites:
		lines.append(get_text("FAV_DIRS_HINT"))
	else:
		lines.append(get_text("FAV_DIRS_EMPTY"))
	markup = InlineKeyboardMarkup(row_width=1)
	for directory in favorites:
		label = directory if len(directory) <= MAX_NAME_LENGTH_IN_BUTTON else "…" + directory[-MAX_NAME_LENGTH_IN_BUTTON:]
		markup.add(InlineKeyboardButton(f"🗑 {label}", callback_data=build_call("favDirDel", get_dir_id(directory))))
	markup.add(InlineKeyboardButton(get_text("BUTTON_FAV_DIR_ADD"), callback_data=build_call("favDirAdd")))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("settings")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def render_favorite_dirs_menu(chat_id, message_id):
	text, markup = build_favorite_dirs_menu()
	edit_message(chat_id, message_id, text, markup)


# ---------------------------------------------------------------------------
# RENAME TEMPLATES
# ---------------------------------------------------------------------------

# Example names contain every detectable field so any template can be previewed
TEMPLATE_EXAMPLE_MOVIE = "Minions.and.Monsters.2026.1080p.HDR.Castellano.WEB-DL.AAC.2.0.H.264-HDZ.mkv"
TEMPLATE_EXAMPLE_SERIES = "Breaking.Bad.2008.S01E03.720p.HDR.Castellano.HDTV.DD+5.1.x264-NTb.mkv"
TEMPLATE_EXAMPLE_SEASON = "Breaking.Bad.2008.S02.1080p.HDR.Castellano.WEB-DL.DD+5.1.H.264-NTb.mkv"


def render_templates_menu(chat_id, message_id):
	template_movie = bot_settings.get("template_movie") or DEFAULT_MOVIE_TEMPLATE
	template_series = bot_settings.get("template_series") or DEFAULT_SERIES_TEMPLATE
	template_season = bot_settings.get("template_season") or DEFAULT_SEASON_PACK_TEMPLATE
	lines = [
		get_text("TPL_TITLE"),
		"",
		get_text("TPL_CURRENT_MOVIE", html.escape(template_movie)),
		get_text("TPL_PREVIEW", html.escape(parse_name(TEMPLATE_EXAMPLE_MOVIE) or "-")),
		"",
		get_text("TPL_CURRENT_SERIES", html.escape(template_series)),
		get_text("TPL_PREVIEW", html.escape(parse_name(TEMPLATE_EXAMPLE_SERIES) or "-")),
		"",
		get_text("TPL_CURRENT_SEASON", html.escape(template_season)),
		get_text("TPL_PREVIEW", html.escape(parse_name(TEMPLATE_EXAMPLE_SEASON) or "-")),
		"",
		get_text("TPL_FIELDS_HELP"),
	]
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(InlineKeyboardButton(get_text("BUTTON_TPL_EDIT_MOVIE"), callback_data=build_call("tplEdit", "movie")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TPL_EDIT_SERIES"), callback_data=build_call("tplEdit", "series")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TPL_EDIT_SEASON"), callback_data=build_call("tplEdit", "season")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TPL_RESET"), callback_data=build_call("tplReset")))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=settings_call("rename")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	edit_message(chat_id, message_id, "\n".join(lines), markup)


# ---------------------------------------------------------------------------
# ADD TORRENT
# ---------------------------------------------------------------------------

def pending_series_name(pending):
	"""The name that tells which series a pending torrent belongs to: the one
	inside a .torrent file, since the file itself is often just a number"""
	return torrent_data_name(pending.get("data")) or pending.get("name") or ""


def ask_download_dir(chat_id, pending_id, name, thread_id=None, message_id=None, dir_page=0):
	pending = get_pending_torrent(pending_id) or {}
	series_name = pending_series_name(pending) or name
	suggested = series_dirs(series_name)
	markup = build_dir_markup(
		dir_call=lambda dir_id: build_call("addTo", pending_id, dir_id),
		write_call=build_call("addNewDir", pending_id),
		cancel_call=build_call("cancelAdd", pending_id),
		page_parts=("addDirPage", pending_id),
		page=dir_page,
		suggested=suggested,
	)
	text = get_text("ADD_ASK_DIR", html.escape(name))
	hint = series_hint(series_name, suggested)
	if hint:
		text = f"{text}\n\n{hint}"
	if message_id:
		edit_message(chat_id, message_id, text, markup)
	else:
		send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)


def ask_add_category(chat_id, pending_id, name, thread_id=None, message_id=None, cat_page=0):
	"""On a manager with categories the category is what decides the directory,
	so it is asked first; picking a folder by hand is still one button away,
	and the folder of the series, when there is one, is offered before anything"""
	pending = get_pending_torrent(pending_id) or {}
	series_name = pending_series_name(pending) or name
	suggested = series_dirs(series_name)
	markup = build_category_markup(
		cat_call=lambda cat_id: build_call("addCat", pending_id, cat_id),
		cancel_call=build_call("cancelAdd", pending_id),
		include_none=False,  # "no category" here is just "let me pick the folder"
		extra_rows=[InlineKeyboardButton(get_text("BUTTON_ADD_CHOOSE_DIR"),
					callback_data=build_call("addDirPage", pending_id, 0))],
		page_parts=("addCatPage", pending_id),
		page=cat_page,
		lead_rows=[InlineKeyboardButton(f"✨ {truncate_dir(directory)}", callback_data=build_call("addTo", pending_id, get_dir_id(directory)))
				for directory in suggested],
	)
	text = get_text("ADD_ASK_CATEGORY", html.escape(name))
	hint = series_hint(series_name, suggested)
	if hint:
		text = f"{text}\n\n{hint}"
	if message_id:
		edit_message(chat_id, message_id, text, markup)
	else:
		send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)


def name_already_exists(name, exclude_id=None):
	"""True when another torrent already has exactly that name"""
	try:
		for t in client.get_torrents():
			if t.name == name and str(t.id) != str(exclude_id):
				return True
	except TorrentClientError:
		pass
	return False


# Held from checking that a name is free until the torrent has it
_rename_lock = threading.Lock()


def auto_rename_enabled():
	"""Auto-rename needs both the setting and a client able to rename"""
	return client.supports_rename and bool(bot_settings.get("auto_rename"))


def auto_rename_torrent(torrent):
	"""Renames the torrent to its suggested name. Returns (new name, duplicate):
	new name is None when there is no suggestion or the rename is skipped, and
	duplicate is the suggested name when it was skipped because another
	torrent already has it, so the user can be told why"""
	suggested = parse_name(torrent.name)
	if not suggested or suggested == torrent.name:
		return None, None
	# The check and the rename go together: two renames at once (two magnets
	# getting their metadata in the same poll) would both find the name free
	with _rename_lock:
		if name_already_exists(suggested, exclude_id=torrent.id):
			warning(f"Auto-rename skipped for {torrent.name}: a torrent named '{suggested}' already exists")
			return None, suggested
		client.rename_torrent(torrent.id, suggested)
	count_usage("auto_rename")
	return suggested, None


def auto_rename_torrent_files(torrent):
	"""Renames the subfolders and files inside a folder torrent to their
	suggested names. It has to run before the torrent is renamed: the paths are
	planned from this snapshot, and a torrent manager that renames in the
	background (qBittorrent) would still be reporting a mix of old and new
	paths right after renaming the torrent folder.
	Returns the preview of what was renamed, or None when nothing was done"""
	if not torrent_is_folder(torrent):
		return None
	torrent_id = torrent.id
	files_plan, folders_plan, skipped = build_full_plan(torrent)
	if skipped:
		warning(f"Auto-rename skipped {skipped} name(s) of {torrent.name}: the suggested name is already in use")
	if not files_plan and not folders_plan:
		return None
	files_done, folders_done, errors = apply_full_plan(torrent_id, files_plan, folders_plan)
	for message in errors:
		warning(f"Auto-rename failed for an item of {torrent.name}: {message}")
	if not files_done and not folders_done:
		return None
	return get_text("ADD_AUTO_RENAMED_FILES", rename_count_text(files_done, folders_done), build_plan_preview(files_plan, folders_plan))


def deferred_auto_rename(torrent_id, original_name, chat_id=None, thread_id=None):
	"""A magnet has no metadata when it is added: the client only knows the
	name hinted in the link and rejects renaming until the real one arrives.
	Waits in background for the metadata and renames then"""
	for _ in range(AUTO_RENAME_WAIT_ATTEMPTS):
		time.sleep(AUTO_RENAME_WAIT_DELAY)
		try:
			torrent = client.get_torrent(torrent_id)
		except TorrentClientError as e:
			warning(f"Auto-rename failed for {original_name}: {e}")
			return
		if torrent is None:
			return
		if not torrent.files:
			continue
		try:
			files_renamed = auto_rename_torrent_files(torrent) if bot_settings.get("auto_rename_files") else None
			renamed, duplicate = auto_rename_torrent(torrent)
		except TorrentClientError as e:
			warning(f"Auto-rename failed for {torrent.name}: {e}")
			return
		if renamed:
			notify(get_text("NOTIFY_AUTO_RENAMED", html.escape(original_name), html.escape(renamed)), chat_id, thread_id)
		if duplicate:
			hint = replace_hint()
			text = get_text("NOTIFY_AUTO_RENAME_DUPLICATE", html.escape(original_name), html.escape(duplicate))
			notify(f"{text}\n{hint}" if hint else text, chat_id, thread_id)
		if files_renamed:
			notify(f"{get_text('NOTIFY_AUTO_RENAMED_FILES', html.escape(renamed or original_name))}\n{files_renamed}", chat_id, thread_id)
		return
	warning(f"Auto-rename gave up for {original_name}: the metadata never arrived")


def auto_rename_lines(torrent):
	"""Auto-renames a torrent whose metadata is already known and returns the
	lines that tell what was done"""
	lines = []
	try:
		# The contents go first, see auto_rename_torrent_files
		files_renamed = auto_rename_torrent_files(torrent) if bot_settings.get("auto_rename_files") else None
		renamed, duplicate = auto_rename_torrent(torrent)
		if renamed:
			lines.append(get_text("ADD_AUTO_RENAMED", html.escape(renamed)))
		if duplicate:
			lines.append(get_text("ADD_AUTO_RENAME_DUPLICATE", html.escape(duplicate)))
			hint = replace_hint()
			if hint:
				lines.append(hint)
		if files_renamed:
			lines.append(files_renamed)
	except TorrentClientError as e:
		warning(f"Auto-rename failed for {torrent.name}: {e}")
	return lines


# ---------------------------------------------------------------------------
# REPLACE: a finished download takes the name of the torrent it supersedes
# ---------------------------------------------------------------------------
# The automatic rename skips a torrent whose name is already taken, which is
# what happens when a better copy of something you already have arrives. Once
# it finishes, the old one can go (with its data) and the new one take its name

def auto_replace_enabled():
	return auto_rename_enabled() and bool(bot_settings.get("auto_replace"))


def replace_hint():
	"""What will happen to a torrent whose rename was skipped for a duplicate,
	or None when nothing will: the offer travels in the completion notice"""
	if auto_replace_enabled():
		return get_text("REPLACE_HINT_AUTO")
	if bot_settings.get("notify_completed"):
		return get_text("REPLACE_HINT_ASK")
	return None


def torrent_paths(torrent):
	"""Where the files of the torrent are on disk"""
	base = (torrent.download_dir or "").rstrip("/\\")
	return {f"{base}/{path}" for path, _, _ in torrent.files}


def replace_targets(torrent):
	"""What torrent would replace: (its suggested name, the other torrents that
	already have that name). A torrent sharing any file with it never is one,
	since deleting its data would delete this download too, and neither is one
	whose files are still unknown. Returns (None, []) when there is nothing"""
	if not client.supports_rename:
		return None, []
	suggested = parse_name(torrent.name)
	if not suggested or suggested == torrent.name:
		return None, []
	try:
		holders = [t for t in client.get_torrents() if t.name == suggested and str(t.id) != str(torrent.id)]
		if not holders:
			return None, []
		# Managers list torrents without their files
		holders = [client.get_torrent(t.id) for t in holders]
		if not torrent.files:
			torrent = client.get_torrent(torrent.id)
	except TorrentClientError as e:
		warning(f"Cannot look for what {torrent.name} would replace: {e}")
		return None, []
	if torrent is None or not torrent.files:
		return None, []
	# A torrent that leaves files out is not a whole copy of anything: done
	# with a few episodes of a pack, it would delete the complete one before it
	if any(priority == FILE_SKIP for priority in torrent.file_priorities.values()):
		return None, []
	own = torrent_paths(torrent)
	videos = video_count(torrent)
	# Nor does it replace one with more videos than it brings, whatever the
	# names say: that is another release that only looks the same to the parser
	previous = [t for t in holders if t is not None and t.files and not torrent_paths(t) & own and video_count(t) <= videos]
	return (suggested, previous) if previous else (None, [])


def video_count(torrent):
	"""Videos the torrent downloads, its sample clips and the files left out
	not counted"""
	return sum(1 for path, _, _ in torrent.files
			if is_video_file(path) and not is_sample_file(path) and torrent.file_priorities.get(path) != FILE_SKIP)


def wait_until_removed(torrent_ids):
	"""Waits a while for the manager to drop the torrents. Only a wait: the
	data is already being deleted, so a manager that does not answer here
	must not stop what comes after (the notice, the rename)"""
	for _ in range(REPLACE_WAIT_ATTEMPTS):
		try:
			if all(client.get_torrent(torrent_id) is None for torrent_id in torrent_ids):
				return
		except TorrentClientError as e:
			debug(f"Still waiting for the removal of {', '.join(torrent_ids)}: {e}")
		time.sleep(REPLACE_WAIT_DELAY)


def replace_previous(torrent_id, only=None):
	"""Deletes, with their data, the torrents that have the name torrent_id
	should take, and renames it to that name (its subfolders and files too
	when the automatic rename includes them). The manager deletes in the
	background and the old files can still be on disk when the rename runs,
	so the rename is retried for a while. only, when given, are the ids a
	confirmation listed: nothing else is deleted, whatever has turned up since.
	Returns the text telling what was done"""
	try:
		torrent = client.get_torrent(torrent_id)
		suggested, previous = replace_targets(torrent) if torrent else (None, [])
		if only is not None:
			previous = [t for t in previous if str(t.id) in only]
		if not previous:
			return get_text("REPLACE_NOTHING")
		# The name is the one the new torrent takes, so where and how big tell them apart
		if len(previous) == 1:
			removed = get_text("REPLACED_OLD_ONE", sizeof_fmt(previous[0].total_size), html.escape(previous[0].download_dir or ""))
		else:
			removed = get_text("REPLACED_OLD_MANY", len(previous))
		client.remove_torrents([t.id for t in previous], delete_data=True)
	except TorrentClientError as e:
		warning(f"Cannot replace the torrents before {torrent_id}: {e}")
		return get_text("REPLACE_FAILED", html.escape(str(e)))
	debug(f"Replaced {', '.join(t.name for t in previous)} with {torrent.name}")
	wait_until_removed([t.id for t in previous])
	lines = [get_text("REPLACED", html.escape(suggested), removed)]
	try:
		# The contents go first, see auto_rename_torrent_files
		if auto_rename_enabled() and bot_settings.get("auto_rename_files"):
			files_renamed = auto_rename_torrent_files(client.get_torrent(torrent_id) or torrent)
			if files_renamed:
				lines.append(files_renamed)
	except TorrentClientError as e:
		warning(f"Cannot rename the contents of {torrent.name} after replacing: {e}")
	for attempt in range(REPLACE_WAIT_ATTEMPTS):
		try:
			client.rename_torrent(torrent_id, suggested)
			break
		except TorrentClientError as e:
			if attempt == REPLACE_WAIT_ATTEMPTS - 1:
				warning(f"Cannot rename {torrent.name} to {suggested} after replacing: {e}")
				lines[0] = get_text("REPLACED_NOT_RENAMED", html.escape(suggested), removed, html.escape(str(e)))
				return "\n".join(lines)
			time.sleep(REPLACE_WAIT_DELAY)
	return "\n".join(lines)


def show_replace(chat_id, message_id, torrent_id, confirm):
	"""The replace confirmation listing what would be deleted (confirm), or the
	completion notice with its offer again. The confirm button carries exactly
	the torrents listed: what turns up afterwards is never deleted with them"""
	torrent = client.get_torrent(torrent_id)
	if torrent is None:
		edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), close_markup())
		return
	suggested, previous = replace_targets(torrent)
	completed = get_text("NOTIFY_COMPLETED", html.escape(torrent.name))
	if not previous:
		edit_message(chat_id, message_id, f"{completed}\n\n{get_text('REPLACE_NOTHING')}")
	elif not confirm:
		edit_message(chat_id, message_id, *replace_offer(torrent, suggested))
	else:
		items = "\n".join(get_text("REPLACE_ITEM", html.escape(t.name), html.escape(t.download_dir or ""), sizeof_fmt(t.total_size))
						for t in previous)
		replace_ctx = new_action_context("r", torrent_id=str(torrent.id), previous=[str(t.id) for t in previous])
		markup = InlineKeyboardMarkup(row_width=1)
		markup.add(InlineKeyboardButton(get_text("BUTTON_REPLACE_CONFIRM"), callback_data=build_call("replaceOk", replace_ctx)))
		markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=build_call("replaceNo", torrent.id)))
		edit_message(chat_id, message_id, get_text("REPLACE_CONFIRM", items, html.escape(torrent.name), html.escape(suggested)), markup)


def replace_offer(torrent, suggested):
	"""The completion notice of a torrent that can replace another, with the
	buttons that do it or leave both"""
	text = f"{get_text('NOTIFY_COMPLETED', html.escape(torrent.name))}\n{get_text('REPLACE_OFFER', html.escape(suggested))}"
	markup = InlineKeyboardMarkup()
	markup.row(InlineKeyboardButton(get_text("BUTTON_REPLACE"), callback_data=build_call("replace", torrent.id)),
			InlineKeyboardButton(get_text("BUTTON_REPLACE_SKIP"), callback_data=build_call("replaceSkip", torrent.id)))
	return text, markup


def handle_completed(torrent):
	"""Tells that a download finished and, when it can replace a torrent that
	has the name it should have, replaces it (automatic setting) or offers to"""
	notify_on = bot_settings.get("notify_completed")
	if notify_on:
		count_usage("auto_notify_completed")
	suggested, previous = replace_targets(torrent) if notify_on or auto_replace_enabled() else (None, [])
	text = get_text("NOTIFY_COMPLETED", html.escape(torrent.name))
	if previous and auto_replace_enabled():
		count_usage("auto_replace")
		# Told even with the notice off: data was deleted
		notify(f"{text}\n\n{replace_previous(torrent.id)}")
	elif previous:
		offer_text, markup = replace_offer(torrent, suggested)
		notify(offer_text, reply_markup=markup)
	elif notify_on:
		notify(text)


def seeding_limit_reached(torrent):
	"""The limit of the manager that stopped a torrent that was seeding, as
	(kind, text telling it), or None when it reached none: someone paused it.
	Ratio, and seeding time where the manager has it (only qBittorrent)"""
	if torrent.ratio_limit >= 0 and torrent.ratio + 0.005 >= torrent.ratio_limit:
		return "ratio", get_text("LIMIT_REACHED_RATIO", f"{torrent.ratio_limit:g}")
	if torrent.seeding_time_limit > 0 and torrent.seeding_time >= torrent.seeding_time_limit:
		return "seeding time", get_text("LIMIT_REACHED_TIME", format_eta(torrent.seeding_time_limit))
	return None


def handle_seeding_stopped(torrent_id):
	"""A torrent went from seeding to stopped: when it was a limit of the
	manager that stopped it (and not someone pausing it), removes it from the
	manager (automatic removal) or, for the ratio limit, tells so with a way to
	keep it seeding. The light listing does not carry the limits, so they are
	read here"""
	torrent = client.get_torrent(torrent_id)
	reached = seeding_limit_reached(torrent) if torrent else None
	if reached is None:
		return
	if auto_remove_ratio_enabled():
		remove_at_limit(torrent, *reached)
		return
	if reached[0] != "ratio" or not bot_settings.get("notify_ratio_reached"):
		return
	count_usage("auto_notify_ratio")
	markup = InlineKeyboardMarkup()
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_KEEP_SEEDING"), callback_data=build_call("keepSeeding", torrent.id)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	notify(get_text("NOTIFY_RATIO_REACHED", html.escape(torrent.name), f"{torrent.ratio_limit:g}"), reply_markup=markup)


def remove_at_limit(torrent, kind, reason):
	"""Removes from the manager a torrent one of its limits stopped. Only the
	torrent: its files stay where they are. The monitor sees the stop once, so
	a removal that fails is told even with the notice off: nothing will try
	again and the torrent is still there"""
	name = html.escape(torrent.name)
	try:
		client.remove_torrents([torrent.id], delete_data=False)
	except TorrentClientError as e:
		warning(f"Cannot remove {torrent.name}, which reached its {kind} limit: {e}")
		notify(get_text("NOTIFY_AUTO_REMOVE_FAILED", name, reason, html.escape(str(e))))
		return
	count_usage("auto_remove_ratio")
	debug(f"Removed {torrent.name}, which reached its {kind} limit")
	if bot_settings.get("notify_auto_removed"):
		notify(get_text("NOTIFY_AUTO_REMOVED", name, reason, html.escape(torrent.download_dir or "")))


# Torrents added through the bot that the monitor has not seen yet: id -> ts.
# The lock is held for the whole add, so the monitor can never classify as
# external a torrent the manager already lists but the bot has not registered
_bot_adds_lock = threading.Lock()
_bot_added_ids = {}
BOT_ADDED_TTL = 3600  # Seconds an unseen id is kept (torrent deleted before the next poll)

# Adds that failed but may still land: a manager that is busy can take the
# torrent after the bot gave up waiting for its answer. Info hash -> what the
# add was for, so the monitor finishes it as the bot's own add instead of
# telling it as an external one
_late_adds = {}


def add_torrent_from_bot(late_add=None, **kwargs):
	"""Adds the torrent and registers it as the bot's. When the add fails and
	late_add is given (the context to finish it with), the torrent is watched
	for in case the manager takes it anyway"""
	with _bot_adds_lock:
		now = time.time()
		for registry in (_bot_added_ids, _late_adds):
			for key, entry in list(registry.items()):
				if now - (entry["ts"] if isinstance(entry, dict) else entry) > BOT_ADDED_TTL:
					del registry[key]
		try:
			torrent = client.add_torrent(**kwargs)
		except PermanentTorrentError:
			raise  # Refused for good (or there already): nothing can land late
		except TorrentClientError:
			if late_add is not None:
				# Every id it can get: a v2 torrent is known by two
				for info_hash in expected_torrent_ids(kwargs.get("magnet"), kwargs.get("torrent_data")):
					_late_adds[info_hash] = dict(late_add, ts=now)
			raise
		_bot_added_ids[str(torrent.id)] = now
	return torrent


def pop_late_add(torrent_id):
	"""The context of a failed add that turned out to land as torrent_id, or
	None. Must be called holding _bot_adds_lock"""
	return _late_adds.pop(str(torrent_id).lower(), None)


def perform_add_torrent(pending, download_dir, chat_id=None, thread_id=None, category=None, automatic=False):
	"""Adds the torrent and returns the result (text, markup): add + optional
	auto-rename + optional low space warning, and when it was added without
	asking (automatic), the folder of its series. With the automatic move on
	and no doubt about that folder, the torrent goes straight there instead of
	to download_dir; otherwise it is offered under the message"""
	return add_with_context(pending, add_context(pending, download_dir, chat_id, thread_id, category, automatic))


def add_context(pending, download_dir, chat_id=None, thread_id=None, category=None, automatic=False):
	"""What an add is for: where it goes and where it is told. It is worked
	out once, so an add that is retried later goes where it was going to go"""
	series_title = None
	previous_dir = None
	series_new = False
	series_unknown = False
	if automatic and not category:
		series_dir, series_new = series_auto_target(pending, current_dir=download_dir)
		# None: the manager did not tell where the series is. An add retried
		# later works it out again then, instead of going without it
		series_unknown = series_new is None
		series_new = bool(series_new)
		if series_dir:
			series_title = series_identity(pending_series_name(pending))["title"]
			previous_dir = download_dir
			download_dir = series_dir
	return {"chat_id": chat_id, "thread_id": thread_id, "download_dir": download_dir, "category": category,
			"automatic": automatic, "series_title": series_title, "previous_dir": previous_dir, "series_new": series_new,
			"series_unknown": series_unknown}


def add_with_context(pending, context):
	"""Adds the torrent for what add_context worked out: (text, markup). A
	new series folder the manager refuses (Download Station cannot create it:
	no File Station permission, a share that does not exist...) does not lose
	the add: it goes where it was going to go, telling why"""
	try:
		torrent = add_torrent_from_bot(late_add=context, magnet=pending["magnet"], torrent_data=pending["data"],
								download_dir=context["download_dir"], category=context["category"] or None)
	except DuplicateTorrentError:
		if context.get("series_new"):
			release_new_series_folder(context["download_dir"])
		raise
	except TorrentClientError as e:
		if not (context.get("series_new") and context.get("previous_dir")) or add_failure_is_temporary(e):
			raise
		folder = context["download_dir"]
		release_new_series_folder(folder)
		warning(f"Cannot add {pending.get('name')} to the new series folder {folder}, adding it to {context['previous_dir']}: {e}")
		with _bot_adds_lock:
			# It failed for good: nothing of that add can land late
			for info_hash in expected_torrent_ids(pending["magnet"], pending["data"]):
				_late_adds.pop(info_hash, None)
		context = dict(context, download_dir=context["previous_dir"], series_title=None, previous_dir=None, series_new=False)
		torrent = add_torrent_from_bot(late_add=context, magnet=pending["magnet"], torrent_data=pending["data"],
								download_dir=context["download_dir"], category=context["category"] or None)
		return added_message(torrent, **context, note=get_text("SERIES_NEW_DIR_FAILED", html.escape(folder), html.escape(str(e))))
	return added_message(torrent, **context)


def added_message(torrent, chat_id=None, thread_id=None, download_dir=None, category=None, automatic=False,
				series_title=None, previous_dir=None, series_new=False, series_unknown=False, note=None):
	"""The message that tells a torrent the bot added: (text, markup). note is
	a line told right under the first one"""
	if series_title:
		count_usage("auto_new_series" if series_new else "auto_move_series")
	# With a category the directory is not known until the manager has resolved
	# it, so the one it reports back is the only accurate one
	download_dir = torrent.download_dir or download_dir or ""
	lines = [get_text("ADD_OK", html.escape(torrent.name), html.escape(download_dir))]
	if note:
		lines.append(note)
	# A magnet has no size until its metadata arrives
	if torrent.total_size and torrent.total_size > 0:
		lines.append(get_text("ADD_OK_SIZE", sizeof_fmt(torrent.total_size)))
	if category:
		lines.append(get_text("ADD_OK_CATEGORY", html.escape(category)))
	if auto_rename_enabled():
		if torrent.files:
			lines.extend(auto_rename_lines(torrent))
		else:
			lines.append(get_text("ADD_AUTO_RENAME_PENDING"))
			threading.Thread(target=deferred_auto_rename, args=(torrent.id, torrent.name, chat_id, thread_id), daemon=True).start()
	if bot_settings.get("low_space_warning"):
		space_warning = build_low_space_warning(torrent.total_size, download_dir)
		if space_warning:
			lines.append(space_warning)
	block, markup = None, None
	if series_title:
		block, markup = series_placed(torrent.id, series_title, previous_dir, series_new)
	elif automatic:
		block, markup = series_suggestion(torrent, download_dir)
	text = "\n".join(lines)
	if block:
		text = f"{text}\n\n{block}"
	return text, markup


def build_low_space_warning(total_size, download_dir):
	"""Returns the warning text if the torrent does not fit in download_dir, else None"""
	if not total_size or total_size <= 0:
		return None
	try:
		free = client.get_free_space(download_dir)
	except TorrentClientError:
		return None
	if free is None or free < 0 or total_size <= free:
		return None
	return get_text("LOW_SPACE_WARNING", sizeof_fmt(total_size), sizeof_fmt(free))


def handle_late_add(torrent_id, late_add):
	"""A torrent whose add failed showed up in the manager after all: it is
	told as the bot's add it was, with its rename and its series folder. When
	it was waiting in the queue, it leaves it, and the message that said it
	was queued is the one that tells it was added"""
	try:
		torrent = client.get_torrent(torrent_id)
	except TorrentClientError as e:
		warning(f"Cannot read the late added torrent {torrent_id}: {e}")
		return
	if torrent is None:
		return
	queued = queued_adds.find(torrent_id)
	if queued is not None:
		# None: the queue worker got to it first and tells it itself
		item = queued_adds.take(queued["id"])
		if item is not None:
			count_usage("late_add")
			text, markup = added_message(torrent, **item["context"])
			finish_queued_add(item, text, markup)
		return
	if not late_add:
		return
	context = {key: value for key, value in late_add.items() if key != "ts"}
	text, markup = added_message(torrent, **context)
	count_usage("late_add")
	notify(f"{get_text('ADD_LATE')}\n\n{text}", context["chat_id"], context["thread_id"], reply_markup=markup)


def add_error_text(error):
	"""What a failed add says. A torrent that was already in the manager is
	named, so it can be found, instead of looking like a broken file"""
	if isinstance(error, DuplicateTorrentError):
		existing = None
		if error.torrent_id:
			try:
				existing = client.get_torrent(error.torrent_id)
			except TorrentClientError:
				pass
		return get_text("ADD_DUPLICATE", html.escape(existing.name if existing else str(error.torrent_id or "?")))
	if isinstance(error, RestartRequiredError):
		return client_error_text(error)
	return get_text("ADD_ERROR", html.escape(str(error)))


# ---------------------------------------------------------------------------
# ADD QUEUE
# ---------------------------------------------------------------------------
# An add the manager did not answer (it is busy moving terabytes, restarting,
# down...) is not lost: it waits in CONFIG_PATH/add_queue, which survives a
# restart, and is sent again, oldest first, until the manager takes it. The
# message that says it is queued becomes the one that tells how it ended.
# What the manager refused is not queued: sending it again would not help.

queued_adds = AddQueue(os.path.join(_config_module.CONFIG_PATH, "add_queue"))
ADD_QUEUE_IDLE_SECONDS = 60  # Seconds between looks at the queue when nothing woke the worker

# What a manager that is not answering looks like in the error of an add
TEMPORARY_ADD_ERROR = re.compile(
	r"time ?out|timed out|connect|refused|unreachable|reset by peer|broken pipe|aborted|temporar"
	r"|unavailable|max retries|name resolution|bad gateway|\b50[234]\b", re.IGNORECASE)


def add_failure_is_temporary(error):
	"""Whether an add failed because the manager did not answer, so trying
	again later can work. Anything else (a broken file, a refusal...) is
	final. When the error does not say, the manager is asked something
	simple: if that fails too, it is the manager that is not answering"""
	if isinstance(error, PermanentTorrentError):
		return False
	if TEMPORARY_ADD_ERROR.search(str(error)):
		return True
	try:
		client.test_connection()
	except TorrentClientError:
		return True
	except Exception:
		return True
	return False


def add_queued_text(item):
	text = get_text("ADD_QUEUED", html.escape(item["name"] or "?"), ADD_QUEUE_MAX_AGE // 3600)
	waiting = len(queued_adds)
	if waiting > 1:
		text = f"{text}\n\n{get_text('ADD_QUEUE_COUNT', waiting)}"
	return text


def add_queued_markup(item):
	markup = InlineKeyboardMarkup()
	markup.add(InlineKeyboardButton(get_text("BUTTON_ADD_QUEUE_CANCEL"), callback_data=build_call("addQCancel", item["id"])))
	return markup


def add_or_queue(pending, download_dir, chat_id=None, thread_id=None, category=None, automatic=False,
				message_id=None, manager_down=False):
	"""Adds the torrent, or queues it when the manager is not answering.
	Returns (text, markup, item): item is the queued add, None when it was
	added or failed for good. The message that shows the text is the one
	that tells how a queued add ends: message_id when it is already known,
	otherwise the caller gives it with queued_adds.set_message.
	When the manager has just failed to answer (manager_down, or the queue is
	waiting after a failure) the add is queued without trying: a try would
	only keep the conversation waiting for another timeout, and add to the
	load of the manager. What needs the manager to be worked out (the folder
	of its series) is then worked out when it is sent"""
	if manager_down or queued_adds.wait_time() > 0:
		context = {"chat_id": chat_id, "thread_id": thread_id, "download_dir": download_dir, "category": category,
				"automatic": automatic, "series_title": None, "previous_dir": None}
		return queue_add(pending, context, time.time(), message_id, "the manager has just failed to answer", resolve=True)
	context = add_context(pending, download_dir, chat_id, thread_id, category, automatic)
	started = time.time()
	try:
		text, markup = add_with_context(pending, context)
	except TorrentClientError as e:
		if not add_failure_is_temporary(e):
			return add_error_text(e), None, None
		# Where its series is could not be told either: it is looked for again
		# when the add is sent, instead of going without it
		return queue_add(pending, context, started, message_id, e, failure=e, resolve=bool(context.get("series_unknown")))
	if len(queued_adds):
		queued_adds.succeeded()  # The manager answers again: the queue goes now
	return text, markup, None


def queue_add(pending, context, started, message_id, reason, failure=None, resolve=False):
	"""Puts an add in the queue: (text, markup, item) for add_or_queue"""
	try:
		item = queued_adds.put(pending.get("name"), pending.get("magnet"), pending.get("data"),
							expected_torrent_ids(pending.get("magnet"), pending.get("data")),
							context, started=started, resolve=resolve)
	except OSError as write_error:
		error(f"Cannot queue {pending.get('name')}: {write_error}")
		if failure is not None:
			return add_error_text(failure), None, None
		return get_text("ADD_ERROR", html.escape(str(write_error))), None, None
	warning(f"{pending.get('name')} waits in the add queue: {reason}")
	count_usage("add_queued")
	if message_id is not None:
		queued_adds.set_message(item["id"], message_id)
	return add_queued_text(item), add_queued_markup(item), item


def finish_queued_add(item, text, markup=None):
	"""Tells how a queued add ended, on the message that said it was queued
	when it can still be changed, in a new one otherwise"""
	context = item["context"]
	if item.get("message_id") and context.get("chat_id") is not None:
		if edit_message(context["chat_id"], item["message_id"], text, markup) is not None:
			return
	notify(text, context.get("chat_id"), context.get("thread_id"), reply_markup=markup)


def queued_duplicate(item, duplicate):
	"""What a retry the manager answers with "already there" means. Often the
	try that did not get an answer had in fact added it: it is the add, told
	as such. A torrent that was there before the first try is a duplicate"""
	torrent = None
	if duplicate.torrent_id:
		try:
			torrent = client.get_torrent(duplicate.torrent_id)
		except TorrentClientError:
			pass
	with _bot_adds_lock:
		ours = False
		for torrent_id in set(item["ids"]) | ({str(duplicate.torrent_id).lower()} if duplicate.torrent_id else set()):
			ours = pop_late_add(torrent_id) is not None or ours
	started = item.get("started") or item["created"]
	if torrent is not None and (ours or added_timestamp(torrent.added_date) >= started - 5):
		# The bot's own add: the monitor must not tell it again as external
		with _bot_adds_lock:
			_bot_added_ids[str(torrent.id)] = time.time()
		return added_message(torrent, **item["context"])
	return add_error_text(duplicate), None


def run_queued_add(item):
	"""One try at a queued add. False when the manager still does not answer"""
	name = html.escape(item["name"] or "?")
	# From here on it cannot be cancelled: what the user is told has to be true
	if not queued_adds.begin(item["id"]):
		return True
	try:
		context = item["context"]
		data = queued_adds.data(item["id"]) if item["has_data"] else None
		if item["has_data"] and data is None:
			queued_adds.end(item["id"])
			if queued_adds.take(item["id"]) is not None:
				error(f"The queued .torrent of {item['name']} is gone")
				finish_queued_add(item, get_text("ADD_QUEUE_FILE_GONE", name))
			return True
		pending = {"name": item["name"], "magnet": item["magnet"], "data": data}
		if item.get("resolve"):
			# Queued without trying: what needed the manager is worked out now
			context = add_context(pending, context["download_dir"], context["chat_id"], context["thread_id"],
								context["category"], context["automatic"])
			item = dict(item, context=context)
		try:
			text, markup = add_with_context(pending, context)
		except DuplicateTorrentError as e:
			if queued_adds.get(item["id"]) is None:
				return True  # Seen landing by the monitor, which told it
			text, markup = queued_duplicate(item, e)
		except TorrentClientError as e:
			if add_failure_is_temporary(e):
				delay = queued_adds.failed()
				warning(f"The manager still does not answer, next try of the add queue in {delay} s: {e}")
				return False
			# The message said which torrent was waiting: so does its error
			text, markup = get_text("ADD_QUEUE_FAILED", name, html.escape(str(e))), None
	finally:
		queued_adds.end(item["id"])
	queued_adds.succeeded()
	if queued_adds.take(item["id"]) is not None:
		count_usage("add_queue_done")
		finish_queued_add(item, text, markup)
	return True


def process_add_queue():
	"""Sends what is queued, oldest first, while the manager takes it. An add
	that has waited too long is given up and told"""
	for item in queued_adds.items():
		if time.time() - item["created"] > ADD_QUEUE_MAX_AGE:
			if queued_adds.take(item["id"]) is not None:
				warning(f"Giving up on the queued add of {item['name']}: the manager did not take it in time")
				count_usage("add_queue_gave_up")
				finish_queued_add(item, get_text("ADD_QUEUE_GAVE_UP", html.escape(item["name"] or "?"), ADD_QUEUE_MAX_AGE // 3600))
			continue
		if queued_adds.wait_time() > 0:
			return
		if not run_queued_add(item):
			return


def add_queue_worker():
	while True:
		try:
			process_add_queue()
		except Exception as e:
			warning(f"Add queue: {e}")
		queued_adds.wait(queued_adds.wait_time() or ADD_QUEUE_IDLE_SECONDS)


def do_add_torrent(chat_id, message_id, pending_id, download_dir, thread_id=None, category=None):
	pending = pop_pending_torrent(pending_id)
	if pending is None:
		edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
		return
	with _contexts_lock:
		_adding.add(pending_id)
	try:
		# The folder buttons (and cancel) go away while the manager takes it
		edit_message(chat_id, message_id, "⏳")
		text, markup, _item = add_or_queue(pending, download_dir, chat_id, thread_id, category, message_id=message_id)
		edit_message(chat_id, message_id, text, markup)
	finally:
		with _contexts_lock:
			_adding.discard(pending_id)


def start_add_flow(chat_id, name, magnet=None, data=None, thread_id=None):
	"""Entry point when receiving a torrent: asks for the category or the download
	dir, or adds it directly when auto download is enabled"""
	if bot_settings.get("auto_download"):
		category = auto_category()
		download_dir = bot_settings.get("auto_download_dir")
		manager_down = False
		if not category and not download_dir and not queued_adds.wait_time():
			try:
				download_dir = client.get_default_download_dir()
			except TorrentClientError as e:
				# Not worth losing the add over: without a folder the manager
				# puts it in its own default one all the same. A manager that
				# does not answer this would not answer the add either
				warning(f"Cannot get the default download dir, adding without one: {e}")
				download_dir = None
				manager_down = add_failure_is_temporary(e)
		text, markup, item = add_or_queue({"name": name, "magnet": magnet, "data": data},
								download_dir, chat_id, thread_id, category, automatic=True, manager_down=manager_down)
		sent = send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)
		if item is not None and sent:
			queued_adds.set_message(item["id"], sent.message_id)
		return
	pending_id = new_pending_torrent(name, magnet=magnet, data=data)
	if get_known_categories():
		ask_add_category(chat_id, pending_id, name, thread_id=thread_id)
	else:
		ask_download_dir(chat_id, pending_id, name, thread_id=thread_id)


def extract_magnet_name(magnet):
	match = re.search(r"dn=([^&]+)", magnet)
	if match:
		try:
			from urllib.parse import unquote_plus
			return unquote_plus(match.group(1))
		except Exception:
			pass
	return "magnet"


class MagnetRedirect(Exception):
	"""A torrent link that leads to a magnet (indexers that only have the
	magnet answer their download link with a redirect to it)"""

	def __init__(self, magnet):
		super().__init__(magnet)
		self.magnet = magnet


def download_torrent_from_url(url):
	"""Downloads a .torrent from a URL with size/time limits and validates
	the content is bencoded torrent metadata. Returns (data, name), raises
	MagnetRedirect when the link leads to a magnet, or ValueError if the
	content is not a torrent. It never takes longer than URL_DOWNLOAD_DEADLINE:
	the read timeout alone lets a server that sends a byte now and then keep a
	handler (there are only two) busy for hours"""
	outcome = {}
	cancelled = threading.Event()

	def worker():
		try:
			outcome["value"] = _download_torrent(url, cancelled, outcome)
		except BaseException as e:
			outcome["error"] = e

	thread = threading.Thread(target=worker, daemon=True)
	thread.start()
	thread.join(URL_DOWNLOAD_DEADLINE)
	if thread.is_alive():
		cancelled.set()  # The worker stops at its next chunk...
		_abort_response(outcome.get("response"))  # ...or now, if it is waiting on the server
		raise ValueError(f"the download took more than {URL_DOWNLOAD_DEADLINE} s")
	if "error" in outcome:
		raise outcome["error"]
	return outcome["value"]


def _abort_response(response):
	"""Cuts the connection of a download from another thread: closing alone
	does not wake a read blocked on the socket, shutting it down does"""
	if response is None:
		return
	try:
		sock = getattr(getattr(response.raw, "_connection", None), "sock", None)
		if sock is not None:
			sock.shutdown(socket.SHUT_RDWR)
	except Exception:
		pass
	try:
		response.close()
	except Exception:
		pass


def _download_torrent(url, cancelled, holder=None):
	headers = {"User-Agent": f"torrent-controller-bot/{VERSION}"}
	# Redirects are followed by hand: requests would follow one to a magnet
	# and fail with InvalidSchema, a ValueError, told as "not a torrent"
	for _ in range(URL_MAX_REDIRECTS + 1):
		response = requests.get(url, stream=True, timeout=URL_DOWNLOAD_TIMEOUT, headers=headers, allow_redirects=False)
		if holder is not None:
			holder["response"] = response
		if not response.is_redirect:
			break
		location = (response.headers.get("Location") or "").strip()
		response.close()
		if location.lower().startswith("magnet:"):
			raise MagnetRedirect(location)
		url = urljoin(url, location)
	else:
		raise ValueError("too many redirects")
	data = b""
	try:
		response.raise_for_status()
		length = response.headers.get("Content-Length")
		if length and int(length) > URL_DOWNLOAD_MAX_BYTES:
			raise ValueError("too big")
		for chunk in response.iter_content(chunk_size=16 * 1024):
			if cancelled.is_set():
				raise ValueError("cancelled")
			data += chunk
			if len(data) > URL_DOWNLOAD_MAX_BYTES:
				raise ValueError("too big")
			# A torrent file is a bencoded dict: abort early if it cannot be one
			if not data.startswith(b"d"):
				raise ValueError("not a torrent")
	finally:
		response.close()
	if b"4:info" not in data or not data.endswith(b"e"):
		raise ValueError("not a torrent")
	return data, torrent_data_name(data) or "torrent"


def torrent_data_name(data):
	"""The name inside the metadata of a .torrent file, or None"""
	if not data:
		return None
	match = re.search(rb"4:name(\d+):", data)
	if not match:
		return None
	start = match.end()
	return data[start:start + int(match.group(1))].decode("utf-8", errors="replace") or None


def torrent_data_files(data):
	"""The paths of the files inside a .torrent file, or None without one (a
	magnet) or when it cannot be read"""
	if not data:
		return None
	try:
		start = data.index(b"4:info") + len(b"4:info")
		info = _bdecode(data, start)[0]
		if b"files" in info:
			return ["/".join(part.decode("utf-8", "replace") for part in entry[b"path"]) for entry in info[b"files"]]
		return [info[b"name"].decode("utf-8", "replace")]
	except (ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError):
		return None


def _bdecode(data, start):
	"""(value, end) of the bencoded value at start"""
	kind = data[start:start + 1]
	if kind == b"i":
		end = data.index(b"e", start)
		return int(data[start + 1:end]), end + 1
	if kind in (b"l", b"d"):
		items, position = [], start + 1
		while data[position:position + 1] != b"e":
			if position >= len(data):
				raise ValueError("unterminated")
			value, position = _bdecode(data, position)
			items.append(value)
		if kind == b"l":
			return items, position + 1
		return dict(zip(items[::2], items[1::2])), position + 1
	end = _bencode_end(data, start)
	return data[data.index(b":", start) + 1:end], end


def _bencode_end(data, start):
	"""Where the bencoded value that starts at start ends"""
	kind = data[start:start + 1]
	if kind == b"i":
		return data.index(b"e", start) + 1
	if kind in (b"l", b"d"):
		position = start + 1
		while data[position:position + 1] != b"e":
			following = _bencode_end(data, position)
			if following <= position:
				raise ValueError(f"no progress at {position}")
			position = following
		return position + 1
	colon = data.index(b":", start)
	length = int(data[start:colon])
	# A negative length would point backwards and the caller would loop on
	# the same value forever (while holding the add lock)
	if length < 0:
		raise ValueError(f"negative string length at {start}")
	return colon + 1 + length


def torrent_data_hash(data):
	"""The info hash of a .torrent file (SHA-1 of its info dictionary), or None"""
	if not data or data[:1] != b"d":
		return None
	try:
		position = 1
		while data[position:position + 1] != b"e":
			key_end = _bencode_end(data, position)
			key = data[data.index(b":", position) + 1:key_end]
			value_end = _bencode_end(data, key_end)
			if key == b"info":
				return hashlib.sha1(data[key_end:value_end]).hexdigest()
			if value_end <= position:
				return None  # Malformed: it would never move on
			position = value_end
	except (ValueError, IndexError, RecursionError):
		return None
	return None


def magnet_hash(magnet):
	"""The info hash of a magnet link, in hex, or None. It comes in hex or in base32"""
	match = re.search(r"xt=urn:btih:([0-9A-Za-z]+)", magnet or "")
	if not match:
		return None
	value = match.group(1)
	if len(value) == 40 and re.fullmatch(r"[0-9A-Fa-f]+", value):
		return value.lower()
	if len(value) == 32:
		try:
			return base64.b32decode(value.upper()).hex()
		except ValueError:
			return None
	return None


def expected_info_hash(magnet=None, torrent_data=None):
	"""The info hash a torrent will have once added, or None when it cannot be known"""
	return magnet_hash(magnet) if magnet else torrent_data_hash(torrent_data)


# ---------------------------------------------------------------------------
# GLOBAL SPEED LIMITS
# ---------------------------------------------------------------------------

def saved_speed_limit(direction):
	return (bot_settings.get("saved_speed_limits") or {}).get(direction) or 0


def remember_speed_limit(direction, kbps):
	if kbps and kbps > 0:
		saved = dict(bot_settings.get("saved_speed_limits") or {})
		saved[direction] = int(kbps)
		bot_settings.set("saved_speed_limits", saved)


def turn_speed_limit_off(direction, settings=None):
	"""Off, keeping the limit it had: most managers forget it (qBittorrent
	stores no limit as 0) or keep it only until the bot restarts"""
	settings = settings or client.get_settings()
	if settings.get(f"speed_limit_{direction}_enabled"):
		remember_speed_limit(direction, settings.get(f"speed_limit_{direction}"))
	client.set_speed_limit(direction, None, enabled=False)


# ---------------------------------------------------------------------------
# MASS ACTIONS
# ---------------------------------------------------------------------------

def render_mass_confirm(chat_id, message_id, action, filter_key, dir_page=0):
	try:
		torrents = get_filtered_torrents(filter_key)
	except ExpiredContext:
		edit_message(chat_id, message_id, get_text("MASS_EXPIRED"), back_close_markup())
		return
	except TorrentClientError as e:
		edit_message(chat_id, message_id, client_error_text(e), back_close_markup())
		return

	count = len(torrents)
	back_call = build_call("list", filter_key, 0)
	if count == 0:
		edit_message(chat_id, message_id, get_text("LIST_EMPTY"), back_close_markup(back_call))
		return

	# The buttons carry what was counted here, not the filter: a filter is
	# read again when pressed, and by then it can hold other torrents (the
	# ones downloading now, a search a new torrent matches...)
	mass_ctx = new_action_context("m", filter_key=filter_key, ids=[str(t.id) for t in torrents])
	markup = InlineKeyboardMarkup(row_width=1)
	if action == "resume":
		text = get_text("MASS_CONFIRM_RESUME", count)
		markup.add(InlineKeyboardButton(get_text("BUTTON_CONFIRM"), callback_data=build_call("confirmMass", "resume", mass_ctx, "-")))
	elif action == "pause":
		text = get_text("MASS_CONFIRM_PAUSE", count)
		markup.add(InlineKeyboardButton(get_text("BUTTON_CONFIRM"), callback_data=build_call("confirmMass", "pause", mass_ctx, "-")))
	elif action == "delete":
		text = get_text("MASS_CONFIRM_DELETE", count)
		markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_KEEP_DATA"), callback_data=build_call("confirmMass", "delete", mass_ctx, "0")))
		markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_WITH_DATA"), callback_data=build_call("confirmMass", "delete", mass_ctx, "1")))
	elif action == "ratio":
		text, markup = build_mass_limits(mass_ctx, filter_key, count)
		edit_message(chat_id, message_id, text, markup)
		return
	elif action == "move":
		text = get_text("MASS_MOVE_ASK_DIR", count)
		markup = build_dir_markup(
			dir_call=lambda dir_id: build_call("massMoveDir", mass_ctx, dir_id),
			write_call=build_call("massMoveNew", mass_ctx),
			cancel_call=back_call,
			page_parts=("mass", "move", filter_key),
			page=dir_page,
		)
	elif action == "cat" and client.supports_categories:
		# What changing it does depends on each torrent: the manager moves the
		# ones it manages to the folder of the category, and only labels the rest
		managed = sum(1 for torrent in torrents if torrent.auto_managed)
		effect = (get_text("MASS_CATEGORY_STAYS") if not managed else get_text("MASS_CATEGORY_MOVES_ALL") if managed == count
				else get_text("MASS_CATEGORY_MOVES_SOME", managed))
		text = f"{get_text('MASS_CATEGORY_ASK', count)}\n\n{effect}"
		markup = build_category_markup(
			cat_call=lambda cat_id: build_call("massCat", mass_ctx, cat_id),
			cancel_call=back_call,
			page_parts=("mass", "cat", filter_key),
			page=dir_page,
		)
	else:
		return
	if action not in ("move", "cat"):
		markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=back_call))
	edit_message(chat_id, message_id, text, markup)


def mass_targets(mass_ctx):
	"""(filter key, ids) a mass confirmation counted, keeping only the torrents
	still in the manager, or None when the confirmation is not known (expired,
	the bot restarted, or a button from before the confirmations kept them)"""
	ctx = get_action_context(mass_ctx, "m")
	if ctx is None:
		return None
	present = {str(t.id) for t in client.get_torrents()}
	return ctx["filter_key"], [torrent_id for torrent_id in ctx["ids"] if torrent_id in present]


def resume_old_mass_button(chat_id, message_id, action, key):
	"""A mass button that does not carry a known confirmation: an expired one
	says so, and one from an older version (it carries the filter itself) shows
	the confirmation again, counted now, instead of acting on whatever the
	filter holds at this moment"""
	if key.startswith("m"):
		edit_message(chat_id, message_id, get_text("MASS_EXPIRED"), back_close_markup())
	else:
		render_mass_confirm(chat_id, message_id, action, key)


def build_mass_limits(mass_ctx, filter_key, count, done=None):
	"""The limits of every torrent a confirmation counted: nothing is marked,
	since each one may have its own, and every choice stays on the screen"""
	lines = [done, ""] if done else []
	lines.append(get_text("MASS_LIMITS_TITLE", count))
	markup = InlineKeyboardMarkup()
	if client.supports_ratio_limit:
		lines += ["", get_text("MASS_RATIO_HINT")]
		for row in ratio_option_rows(-1, "", lambda value: build_call("confirmMass", "ratio", mass_ctx, value),
									build_call("massRatioAsk", mass_ctx), with_global=True):
			markup.row(*row)
	if client.supports_torrent_speed_limit:
		markup.row(
			InlineKeyboardButton(get_text("BUTTON_TORRENT_DOWN_LIMIT"), callback_data=build_call("massSpeedAsk", mass_ctx, "down")),
			InlineKeyboardButton(get_text("BUTTON_TORRENT_UP_LIMIT"), callback_data=build_call("massSpeedAsk", mass_ctx, "up")),
		)
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("list", filter_key, 0)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def mass_ratio_label(limit):
	if limit is None:
		return get_text("BUTTON_RATIO_GLOBAL")
	return f"{limit:g}" if limit >= 0 else get_text("NO_LIMIT")


def do_mass_action(chat_id, message_id, action, mass_ctx, extra=None):
	"""Runs a confirmed mass action on the torrents its confirmation counted
	that are still there, never on what the filter holds now"""
	try:
		targets = mass_targets(mass_ctx)
	except TorrentClientError as e:
		edit_message(chat_id, message_id, client_error_text(e), back_close_markup())
		return
	if targets is None:
		resume_old_mass_button(chat_id, message_id, "ratio" if action == "speed" else action, mass_ctx)
		return
	filter_key, ids = targets
	count = len(ids)
	back_call = build_call("list", filter_key, 0)
	if not ids:
		# Never an empty id list to the manager: some read it as every torrent
		edit_message(chat_id, message_id, get_text("MASS_GONE"), back_close_markup(back_call))
		return
	try:
		if action == "resume":
			client.resume_torrents(ids)
			text = get_text("MASS_DONE_RESUME", count)
		elif action == "pause":
			client.pause_torrents(ids)
			text = get_text("MASS_DONE_PAUSE", count)
		elif action == "ratio":
			limit = None if extra == "g" else (-1 if extra == "n" else parse_ratio(extra or ""))
			if limit is None and extra != "g":
				return
			client.set_ratio_limit(ids, limit)
			text, markup = build_mass_limits(mass_ctx, filter_key, count, done=get_text("MASS_DONE_RATIO", count, mass_ratio_label(limit)))
			edit_message(chat_id, message_id, text, markup)
			return
		elif action == "speed":
			direction, _, kbps = (extra or "").partition(":")
			if direction not in ("down", "up") or not kbps.isdigit():
				return
			client.set_torrent_speed_limit(ids, direction, int(kbps))
			done = get_text("MASS_DONE_DOWN" if direction == "down" else "MASS_DONE_UP", count, speed_limit_text(int(kbps)))
			text, markup = build_mass_limits(mass_ctx, filter_key, count, done=done)
			edit_message(chat_id, message_id, text, markup)
			return
		elif action == "delete":
			client.remove_torrents(ids, delete_data=extra == "1")
			text = get_text("MASS_DONE_DELETE", count)
			back_call = build_call("dashboard")
		elif action == "move":
			progress_text = get_text("MASS_MOVING", count, html.escape(extra))
			success_text = get_text("MASS_DONE_MOVE", count, html.escape(extra))
			deliver_move_order(chat_id, ids, extra, progress_text, success_text, message_id=message_id, reply_markup=back_close_markup(back_call))
			return
		else:
			return
	except TorrentClientError as e:
		text = client_error_text(e)
	edit_message(chat_id, message_id, text, back_close_markup(back_call))


# ---------------------------------------------------------------------------
# PENDING TEXT INPUTS
# ---------------------------------------------------------------------------

def set_pending_input(chat_id, user_id, action, thread_id=None, **extra):
	with _contexts_lock:
		pending_inputs[(chat_id, user_id)] = {"action": action, "ts": time.time(), "thread": normalize_thread(thread_id), **extra}


def pop_pending_input(chat_id, user_id, thread_id=None, any_thread=False):
	"""The question the bot asked this user in this chat and is waiting an
	answer for, taken out. None when there is none, when it is older than
	INPUT_TTL (an answer that never came is not waited for forever: the next
	text could be anything), or when it was asked in another topic (the
	question stays for that one). any_thread takes it wherever it was asked"""
	with _contexts_lock:
		pending = pending_inputs.get((chat_id, user_id))
		if pending is None:
			return None
		if time.time() - pending.get("ts", 0) > INPUT_TTL:
			del pending_inputs[(chat_id, user_id)]
			return None
		if not any_thread and pending.get("thread") != normalize_thread(thread_id):
			return None
		return pending_inputs.pop((chat_id, user_id))


def back_to_add_dir(chat_id, pending, message_id=None):
	"""A typed folder for a torrent waiting to be added that never came: the
	question took the place of the folder choice, so that choice comes back,
	otherwise nothing would lead to the torrent any more and it would never be
	added. False when the question was another one, or the torrent is gone"""
	if not pending or pending.get("action") != "addDir":
		return False
	pending_id = pending["pending_id"]
	pending_torrent = get_pending_torrent(pending_id)
	if pending_torrent is None:
		return False
	ask_download_dir(chat_id, pending_id, pending_torrent.get("name") or "", thread_id=pending.get("thread"),
					message_id=message_id)
	return True


def drop_pending_input(chat_id, user_id):
	"""The user did something else (a button, a command, a file): the question
	is abandoned, and its prompt goes with it"""
	pending = pop_pending_input(chat_id, user_id, any_thread=True)
	if pending and pending.get("prompt_message_id"):
		delete_message(chat_id, pending["prompt_message_id"])
	back_to_add_dir(chat_id, pending)
	return pending


def send_limits_screen(chat_id, thread_id, pending, done, back_markup):
	"""After a typed limit: back on the limits screen, as when a preset is
	pressed, with the confirmation on top and the value read back from the manager"""
	torrent = client.get_torrent(pending["torrent_id"])
	if torrent is None:
		send_message(chat_id, done, reply_markup=back_markup, thread_id=thread_id)
		return
	text, markup = build_ratio_screen(torrent, pending["filter_key"], pending["page"])
	send_message(chat_id, f"{done}\n\n{text}", reply_markup=markup, thread_id=thread_id)


def handle_pending_input(message, pending):
	chat_id = message.chat.id
	thread_id = message_thread(message)
	text = message.text.strip()
	action = pending["action"]

	prompt_message_id = pending.get("prompt_message_id")
	if prompt_message_id:
		delete_message(chat_id, prompt_message_id)
	delete_message(chat_id, message.message_id)

	# The paginated message is still on screen, so a "back" button here would duplicate it
	back_markup = close_markup() if action == "gotoPage" else back_close_markup(pending.get("back_call"))

	# '/cancel@Bot' is how a group sends the command
	if not text or text.lower() in ("cancel", "cancelar") or re.fullmatch(r'/cancel(?:@\w+)?', text, re.IGNORECASE):
		if back_to_add_dir(chat_id, pending):
			return
		# Told in passing, and straight back to the screen the question took
		# the place of: there is nothing else to do after cancelling
		send_notice(chat_id, get_text("INPUT_CANCELLED"), thread_id=thread_id)
		if pending.get("replaced"):
			screen = send_message(chat_id, "⏳", thread_id=thread_id)
			if screen:
				dispatch_callback(chat_id, screen.message_id, message.from_user.id,
								pending.get("back_call") or build_call("dashboard"), thread_id=thread_id)
		return

	if action == "search":
		ctx_id = new_search_context(text)
		render_list(chat_id, None, f"q{ctx_id}", 0, thread_id=thread_id)
	elif action == "gotoPage":
		pages = pending["pages"]
		try:
			number = int(text)
		except ValueError:
			number = 0
		if number < 1 or number > pages:
			send_message(chat_id, get_text("GOTO_PAGE_INVALID", pages), reply_markup=back_markup, thread_id=thread_id)
			return
		# In the topic of the message: the screen it repaints is live there
		dispatch_callback(chat_id, pending["target_message_id"], message.from_user.id, f"{pending['base']}|{number - 1}",
						thread_id=thread_id)
	elif action == "rename":
		if "/" in text:
			# A torrent name is its folder or its file: a slash would move it
			torrent = client.get_torrent(pending["torrent_id"])
			key = "FOLDER_RENAME_INVALID" if torrent and torrent_is_folder(torrent) else "FILE_RENAME_INVALID"
			send_message(chat_id, get_text(key), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			# The check and the rename go together, see auto_rename_torrent
			with _rename_lock:
				duplicate = name_already_exists(text, exclude_id=pending["torrent_id"])
				if not duplicate:
					torrent = client.get_torrent(pending["torrent_id"])
					client.rename_torrent(pending["torrent_id"], text)
			if duplicate:
				send_message(chat_id, get_text("RENAME_DUPLICATE", html.escape(text)), reply_markup=back_markup, thread_id=thread_id)
				return
			key = rename_key(torrent, "RENAME_OK") if torrent else "RENAME_OK"
			send_message(chat_id, get_text(key, html.escape(text)), reply_markup=back_markup, thread_id=thread_id)
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action == "renameFile":
		torrent_id = pending["torrent_id"]
		file_path = pending["file_path"]
		if "/" in text:
			send_message(chat_id, get_text("FILE_RENAME_INVALID"), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			torrent = client.get_torrent(torrent_id)
			plan = [(file_path, text)]
			if torrent and is_video_file(file_path):
				plan.extend(subtitle_renames(torrent, file_path, text))
			done, errors = apply_rename_plan(torrent_id, plan)
			if pending.get("file_nav"):
				# Back to the file under its new name, wherever the rename put it in the list
				back_markup = back_close_markup(renamed_file_call(*pending["file_nav"], torrent_id, file_path, text))
			if errors:
				send_message(chat_id, get_text("FILES_RENAME_PARTIAL", done, len(errors), html.escape(errors[0])), reply_markup=back_markup, thread_id=thread_id)
			else:
				send_message(chat_id, file_rename_ok_text(text, done), reply_markup=back_markup, thread_id=thread_id)
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action == "renameFolder":
		if "/" in text:
			send_message(chat_id, get_text("FOLDER_RENAME_INVALID"), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			torrent = client.get_torrent(pending["torrent_id"])
			if torrent is None:
				send_message(chat_id, get_text("TORRENT_NOT_FOUND"), reply_markup=back_markup, thread_id=thread_id)
				return
			failure = rename_folder_to(torrent, pending["folder_path"], text)
			send_message(chat_id, failure or get_text("RENAME_OK_FOLDER", html.escape(text)), reply_markup=back_markup, thread_id=thread_id)
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action == "move":
		name = html.escape(pending.get("name", ""))
		dest = html.escape(text)
		progress_text = get_text("MOVING", name, dest)
		success_text = get_text("MOVE_OK", name, dest)
		deliver_move_order(chat_id, [pending["torrent_id"]], text, progress_text, success_text, thread_id=thread_id, reply_markup=back_markup)
	elif action == "addDir":
		pending_id = pending["pending_id"]
		pending_torrent = pop_pending_torrent(pending_id)
		if pending_torrent is None:
			send_message(chat_id, get_text("ADD_EXPIRED"), reply_markup=back_markup, thread_id=thread_id)
			return
		result, markup, item = add_or_queue(pending_torrent, text, chat_id, thread_id)
		sent = send_message(chat_id, result, reply_markup=markup if item is not None else back_markup, thread_id=thread_id)
		if item is not None and sent:
			queued_adds.set_message(item["id"], sent.message_id)
	elif action == "autoDir":
		bot_settings.set("auto_download_dir", text.rstrip("/") or "/")
		send_settings_menu(chat_id, thread_id=thread_id, prefix=get_text("SETTINGS_UPDATED"), screen="add")
	elif action == "newSerDir":
		new_dir = text.rstrip("/") or "/"
		problem = new_series_dir_problem(new_dir)
		if problem:
			warning_text, warning_markup = new_series_dir_warning(new_dir, problem)
			send_message(chat_id, warning_text, reply_markup=warning_markup, thread_id=thread_id)
			return
		bot_settings.set("new_series_dir", new_dir)
		send_settings_menu(chat_id, thread_id=thread_id, prefix=get_text("SETTINGS_UPDATED"), screen="add")
	elif action == "favDir":
		directory = text.rstrip("/") or "/"
		favorites = bot_settings.get("favorite_dirs") or []
		if directory not in favorites:
			favorites.append(directory)
			bot_settings.set("favorite_dirs", favorites)
		fav_text, fav_markup = build_favorite_dirs_menu()
		send_message(chat_id, f"{get_text('SETTINGS_UPDATED')}\n\n{fav_text}", reply_markup=fav_markup, thread_id=thread_id)
	elif action == "moverField":
		# Back on the rule either way: a refused answer is told on top of it,
		# with the buttons to try again right there
		problem = set_mover_field(pending["field"], text)
		send_settings_menu(chat_id, thread_id=thread_id, prefix=problem or get_text("SETTINGS_UPDATED"), screen="mover")
	elif action == "template":
		kind = pending["kind"]
		try:
			validate_template(text)
		except TemplateError as e:
			if e.code == "unknown_field":
				error_text = get_text("TPL_ERROR_UNKNOWN_FIELD", html.escape(e.detail))
			elif e.code == "padding_not_allowed":
				error_text = get_text("TPL_ERROR_PADDING_NOT_ALLOWED", html.escape(e.detail))
			elif e.code == "invalid_char":
				error_text = get_text("TPL_ERROR_INVALID_CHAR", html.escape(e.detail))
			elif e.code == "padding_too_wide":
				error_text = get_text("TPL_ERROR_PADDING_TOO_WIDE", html.escape(e.detail))
			else:
				error_text = get_text("TPL_ERROR_INVALID")
			send_message(chat_id, f"{error_text}\n\n{get_text('TPL_FIELDS_HELP')}", reply_markup=back_markup, thread_id=thread_id)
			return
		bot_settings.set(f"template_{kind}", text)
		example = {"movie": TEMPLATE_EXAMPLE_MOVIE, "series": TEMPLATE_EXAMPLE_SERIES, "season": TEMPLATE_EXAMPLE_SEASON}[kind]
		preview = parse_name(example) or "-"
		send_message(chat_id, get_text("TPL_SAVED", html.escape(text), html.escape(example), html.escape(preview)), reply_markup=back_markup, thread_id=thread_id)
	elif action == "massRatio":
		limit = parse_ratio(text)
		if limit is None:
			send_message(chat_id, get_text("RATIO_INVALID", MAX_RATIO_LIMIT), reply_markup=back_markup, thread_id=thread_id)
			return
		sent = send_message(chat_id, "⏳", thread_id=thread_id)
		if sent:
			do_mass_action(chat_id, sent.message_id, "ratio", pending["mass_ctx"], f"{limit:g}")
	elif action == "massSpeed":
		try:
			kbps = int(text)
			if kbps < 0:
				raise ValueError()
		except ValueError:
			send_message(chat_id, get_text("INVALID_NUMBER"), reply_markup=back_markup, thread_id=thread_id)
			return
		sent = send_message(chat_id, "⏳", thread_id=thread_id)
		if sent:
			do_mass_action(chat_id, sent.message_id, "speed", pending["mass_ctx"], f"{pending['direction']}:{kbps}")
	elif action == "massMove":
		do_mass_move_to(chat_id, pending["mass_ctx"], text, thread_id=thread_id)
	elif action == "ratioLimit":
		limit = parse_ratio(text)
		if limit is None:
			send_message(chat_id, get_text("RATIO_INVALID", MAX_RATIO_LIMIT), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			client.set_ratio_limit([pending["torrent_id"]], limit)
			keep_hand_limit(pending["torrent_id"], ["ratio"])
			send_limits_screen(chat_id, thread_id, pending, get_text("RATIO_SET_OK", html.escape(pending.get("name", "")), f"{limit:g}"), back_markup)
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action == "torrentSpeed":
		try:
			kbps = int(text)
			if kbps < 0:
				raise ValueError()
		except ValueError:
			send_message(chat_id, get_text("INVALID_NUMBER"), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			client.set_torrent_speed_limit([pending["torrent_id"]], pending["direction"], kbps)
			keep_hand_limit(pending["torrent_id"], [pending["direction"]])
			send_limits_screen(chat_id, thread_id, pending, get_text("LIMIT_SET_OK"), back_markup)
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action == "trackerRatio":
		limit = parse_ratio(text)
		if limit is None:
			send_message(chat_id, get_text("RATIO_INVALID", MAX_RATIO_LIMIT), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			count = set_tracker_rule(pending["tracker"], "ratio", limit)
			send_tracker_rule_screen(chat_id, thread_id, pending["tracker"], pending["back_filter"], get_text("TRACKER_LIMITS_APPLIED", count))
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action == "trackerSpeed":
		try:
			kbps = int(text)
			if kbps < 0:
				raise ValueError()
		except ValueError:
			send_message(chat_id, get_text("INVALID_NUMBER"), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			count = set_tracker_rule(pending["tracker"], pending["direction"], kbps)
			send_tracker_rule_screen(chat_id, thread_id, pending["tracker"], pending["back_filter"], get_text("TRACKER_LIMITS_APPLIED", count))
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action == "globalRatio":
		limit = parse_ratio(text)
		if limit is None:
			send_message(chat_id, get_text("RATIO_INVALID", MAX_RATIO_LIMIT), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			client.set_global_ratio_limit(limit)
			send_settings_menu(chat_id, thread_id=thread_id, prefix=get_text("SETTINGS_UPDATED"), screen="ratio")
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)
	elif action in ("downLimit", "upLimit"):
		try:
			kbps = int(text)
			if kbps < 0:
				raise ValueError()
		except ValueError:
			send_message(chat_id, get_text("INVALID_NUMBER"), reply_markup=back_markup, thread_id=thread_id)
			return
		direction = "down" if action == "downLimit" else "up"
		try:
			if kbps == 0:
				turn_speed_limit_off(direction)
			else:
				client.set_speed_limit(direction, kbps, enabled=True)
				remember_speed_limit(direction, kbps)
			send_settings_menu(chat_id, thread_id=thread_id, prefix=get_text("SETTINGS_UPDATED"), screen="speed")
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)


# The last move order given for each torrent: id -> order number. A retry of
# an older order gives up once a newer one exists
_move_orders = {}
_move_orders_lock = threading.Lock()
_move_order_counter = [0]


def register_move_order(ids):
	with _move_orders_lock:
		_move_order_counter[0] += 1
		for torrent_id in ids:
			_move_orders[str(torrent_id)] = _move_order_counter[0]
		return _move_order_counter[0]


def move_order_superseded(ids, order):
	with _move_orders_lock:
		return any(_move_orders.get(str(torrent_id), order) != order for torrent_id in ids)


def run_move_order(deliver, ids, order, target, on_retrying=None, plain=False):
	"""Delivers a move order, blocking until it is delivered or given up: the
	RPC blocks until the physical move is finished, and a daemon that does not
	answer (e.g. busy relocating large amounts of data) is asked again until
	it does. deliver must be safe to call again. Returns None once delivered,
	or the text telling why it was not: a whole message, or with plain only
	what the manager said, for a caller that words its own"""
	def told(key, reason):
		return reason if plain else get_text(key, html.escape(reason))

	try:
		deliver()
		return None
	except PermanentTorrentError as e:
		# The manager will not do it: retrying only delays the bad news
		warning(f"Move order refused: {e}")
		return told("MOVE_REFUSED", str(e))
	except TorrentClientError as e:
		err = str(e)
	except Exception as e:
		error(f"Unexpected error delivering move order: {e}")
		return told("MOVE_GIVE_UP", str(e))
	warning(f"Move order not delivered, will retry in background: {err}")
	if on_retrying:
		on_retrying()
	for _ in range(MOVE_RETRY_ATTEMPTS):
		time.sleep(MOVE_RETRY_DELAY)
		if move_order_superseded(ids, order):
			# A newer order for these torrents went out meanwhile: retrying
			# this one would send them back where they are no longer wanted
			debug(f"Move order to {target} superseded, not retried")
			return get_text("MOVE_SUPERSEDED")
		try:
			deliver()
			debug(f"Move order delivered after retrying: {len(ids)} torrents -> {target}")
			return None
		except PermanentTorrentError as e:
			# A retry can learn what the first try could not (Download Station
			# telling that File Station failed half way): told, not retried
			warning(f"Move order refused on a retry: {e}")
			return told("MOVE_REFUSED", str(e))
		except TorrentClientError as e:
			err = str(e)
		except Exception as e:
			error(f"Unexpected error delivering move order on a retry: {e}")
			return told("MOVE_GIVE_UP", str(e))
	error(f"Move order gave up after {MOVE_RETRY_ATTEMPTS} attempts: {err}")
	return told("MOVE_GIVE_UP", err)


def deliver_move_order(chat_id, ids, new_dir, progress_text, success_text, message_id=None, thread_id=None, reply_markup=None, apply=None, target=None, wrap=None, failure_markup="same"):
	"""Show a progress message and deliver the move order in a background thread
	(see run_move_order). The message is updated with the final result.
	apply overrides what is delivered, for the operations that also relocate the
	data and block the same way (assigning a category to an auto managed
	torrent); it must be safe to call again on a retry.
	wrap turns the texts that tell about a retry or a failure into the whole
	message, for a message that has more to say than the move itself."""
	# What a failure is left with: by default the same buttons as a success
	failure_markup = reply_markup if failure_markup == "same" else failure_markup
	deliver = apply or (lambda: client.move_torrents(ids, new_dir))
	wrap = wrap or (lambda text: text)
	target = target or new_dir  # What the logs call the destination
	order = register_move_order(ids)
	if message_id is not None:
		edit_message(chat_id, message_id, progress_text)
	else:
		msg = send_message(chat_id, progress_text, thread_id=thread_id)
		message_id = msg.message_id if msg else None

	def finish(text, markup=reply_markup):
		if message_id is not None:
			edit_message(chat_id, message_id, text, markup)
		else:
			send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)

	def retrying():
		if message_id is not None:
			edit_message(chat_id, message_id, wrap(get_text("MOVE_RETRYING")))

	def worker():
		failure = run_move_order(deliver, ids, order, target, on_retrying=retrying)
		if failure is None:
			finish(success_text)
		else:
			finish(wrap(failure), failure_markup)

	threading.Thread(target=worker, daemon=True).start()


def do_mass_move_to(chat_id, mass_ctx, new_dir, thread_id=None, message_id=None):
	"""Moves the torrents the "move all" screen counted that are still there"""
	def answer(text, markup):
		if message_id:
			edit_message(chat_id, message_id, text, markup)
		else:
			send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)

	try:
		targets = mass_targets(mass_ctx)
	except TorrentClientError as e:
		answer(client_error_text(e), back_close_markup())
		return
	if targets is None:
		answer(get_text("MASS_EXPIRED"), back_close_markup())
		return
	filter_key, ids = targets
	back_markup = back_close_markup(build_call("list", filter_key, 0))
	if not ids:
		answer(get_text("MASS_GONE"), back_markup)
		return
	progress_text = get_text("MASS_MOVING", len(ids), html.escape(new_dir))
	success_text = get_text("MASS_DONE_MOVE", len(ids), html.escape(new_dir))
	deliver_move_order(chat_id, ids, new_dir, progress_text, success_text, message_id=message_id, thread_id=thread_id, reply_markup=back_markup)


def do_mass_category(chat_id, mass_ctx, category, thread_id=None, message_id=None):
	"""Gives a category (none, with "") to the torrents the "category of all"
	screen counted that are still there. The manager moves the data of the
	ones it manages, which blocks like a move: delivered and retried as one"""
	try:
		targets = mass_targets(mass_ctx)
	except TorrentClientError as e:
		edit_message(chat_id, message_id, client_error_text(e), back_close_markup())
		return
	if targets is None:
		edit_message(chat_id, message_id, get_text("MASS_EXPIRED"), back_close_markup())
		return
	filter_key, ids = targets
	back_markup = back_close_markup(build_call("list", filter_key, 0))
	if not ids:
		edit_message(chat_id, message_id, get_text("MASS_GONE"), back_markup)
		return
	label = html.escape(category)
	deliver_move_order(
		chat_id, ids, "",
		get_text("MASS_CATEGORY_CHANGING", len(ids), label) if category else get_text("MASS_CATEGORY_CLEARING", len(ids)),
		get_text("MASS_CATEGORY_DONE", len(ids), label) if category else get_text("MASS_CATEGORY_CLEARED", len(ids)),
		message_id=message_id, thread_id=thread_id, reply_markup=back_markup,
		apply=lambda: client.set_category(ids, category),
		target=f"category '{category}'" if category else "no category")


# ---------------------------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------------------------

_bot_username = None


def bot_username():
	"""Username of this bot, asked to Telegram once. None while it cannot be
	known, and then every command is taken as addressed to this bot"""
	global _bot_username
	if _bot_username is None:
		try:
			_bot_username = bot.get_me().username.lower()
		except Exception as e:
			warning(f"Cannot get the bot username: {describe_error(e)}")
	return _bot_username


def is_for_another_bot(message):
	"""True for /command@OtherBot. telebot drops the @mention when matching
	commands, so in a group with several bots every one of them would answer"""
	if not message.text or not message.text.startswith("/"):
		return False
	command = message.text.split(maxsplit=1)[0]
	if "@" not in command:
		return False
	mentioned = command.split("@", 1)[1].lower()
	own = bot_username()
	return own is not None and mentioned != own


def for_me(message):
	return not is_for_another_bot(message)


def check_auth(message):
	"""For commands and files: the user is allowed, or is told not to be. An
	allowed one moved on from any question the bot was still waiting for"""
	if not is_authorized(message.from_user.id, message.chat.id):
		warning(f"Unauthorized access attempt: user {message.from_user.id} in chat {message.chat.id}")
		send_message(message.chat.id, get_text("USER_NOT_ALLOWED", message.from_user.id), thread_id=message_thread(message))
		return False
	drop_pending_input(message.chat.id, message.from_user.id)
	return True


@bot.message_handler(func=for_me, commands=["start"])
def command_start(message):
	if not check_auth(message):
		return
	count_usage("cmd_start")
	delete_message(message.chat.id, message.message_id)
	show_dashboard(message.chat.id, thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["help"])
def command_help(message):
	if not check_auth(message):
		return
	count_usage("cmd_help")
	delete_message(message.chat.id, message.message_id)
	send_message(message.chat.id, get_text("START_MESSAGE"), thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["list"])
def command_list(message):
	if not check_auth(message):
		return
	count_usage("cmd_list")
	delete_message(message.chat.id, message.message_id)
	render_list(message.chat.id, None, FILTER_ALL, 0, thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["find"])
def command_find(message):
	if not check_auth(message):
		return
	count_usage("cmd_find")
	delete_message(message.chat.id, message.message_id)
	parts = message.text.split(maxsplit=1)
	if len(parts) > 1 and parts[1].strip():
		ctx_id = new_search_context(parts[1].strip())
		render_list(message.chat.id, None, f"q{ctx_id}", 0, thread_id=message_thread(message))
	else:
		ask_for_input(message.chat.id, message.from_user.id, "search", get_text("SEARCH_ASK"), thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["add"])
def command_add(message):
	if not check_auth(message):
		return
	count_usage("cmd_add")
	delete_message(message.chat.id, message.message_id)
	send_message(message.chat.id, get_text("ADD_USAGE"), thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["settings"])
def command_settings(message):
	if not check_auth(message):
		return
	count_usage("cmd_settings")
	delete_message(message.chat.id, message.message_id)
	try:
		text, markup = build_settings()
	except TorrentClientError as e:
		text = client_error_text(e)
		markup = back_close_markup()
	send_message(message.chat.id, text, reply_markup=markup, thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["version"])
def command_version(message):
	if not check_auth(message):
		return
	count_usage("cmd_version")
	delete_message(message.chat.id, message.message_id)
	try:
		connected_to = html.escape(client.test_connection())
	except TorrentClientError as e:
		# The error can carry the HTML page of a proxy, which Telegram would refuse
		connected_to = f"❌ {html.escape(str(e))}"
	send_message(message.chat.id, get_text("VERSION_TEXT", VERSION, connected_to), thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["donate"])
def command_donate(message):
	if not check_auth(message):
		return
	count_usage("cmd_donate")
	delete_message(message.chat.id, message.message_id)
	send_message(message.chat.id, get_text("DONATE"), thread_id=message_thread(message))


@bot.message_handler(func=for_me, commands=["donors"])
def command_donors(message):
	if not check_auth(message):
		return
	count_usage("cmd_donors")
	delete_message(message.chat.id, message.message_id)
	donors = get_donors_online()
	if donors:
		text = get_text("DONORS_LIST", "\n".join(f"· {html.escape(d)}" for d in donors))
	else:
		text = get_text("ERROR_GETTING_DONORS")
	send_message(message.chat.id, text, thread_id=message_thread(message))


def get_donors_online():
	"""Sorted list of donor names, empty when the list cannot be retrieved"""
	try:
		response = requests.get(DONORS_URL, timeout=URL_DOWNLOAD_TIMEOUT, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
	except Exception as e:
		error(f"Error getting donors: {e}")
		return []
	if response.status_code != 200:
		error(f"Error getting donors: error code [{response.status_code}]")
		return []
	try:
		data = response.json()
	except ValueError:
		error(f"Error getting donors: data is not a json [{response.text}]")
		return []
	if not isinstance(data, list):
		error(f"Error getting donors: data is not a list [{data}]")
		return []
	return sorted(str(d) for d in data)


def current_dirs_text(dirs):
	dirs = sorted(set(d.rstrip("/") or "/" for d in dirs if d))
	if not dirs:
		return ""
	if len(dirs) == 1:
		return get_text("CURRENT_DIR", html.escape(dirs[0]))
	shown = "\n".join(f"• <code>{html.escape(d)}</code>" for d in dirs[:5])
	if len(dirs) > 5:
		shown += "\n" + get_text("INFO_AND_MORE_FILES", len(dirs) - 5)
	return get_text("CURRENT_DIRS", shown)


def ask_for_input(chat_id, user_id, action, prompt, thread_id=None, message_id=None, **extra):
	if message_id:
		delete_message(chat_id, message_id)
	text = f"{prompt}\n\n{get_text('INPUT_CANCEL_HINT')}"
	sent = send_message(chat_id, text, reply_markup=ForceReply(), thread_id=thread_id)
	prompt_message_id = sent.message_id if sent else None
	# replaced: the question took the place of a screen, which comes back if it is cancelled
	set_pending_input(chat_id, user_id, action, thread_id=thread_id, prompt_message_id=prompt_message_id,
					replaced=bool(message_id), **extra)


def download_telegram_file(file_id):
	"""Downloads a file sent to the bot. Telegram's servers sometimes stop
	answering for a while, so a network failure is retried: both requests only
	read, repeating them is harmless. An error answered by Telegram itself
	(file too big, expired...) is final and raised straight away.
	bot.download_file is not used because it sets no timeout: a stalled
	download would hang the handler forever"""
	for attempt in range(1, TELEGRAM_DOWNLOAD_ATTEMPTS + 1):
		try:
			file_info = bot.get_file(file_id)
			response = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}",
									timeout=(telebot.apihelper.CONNECT_TIMEOUT, telebot.apihelper.READ_TIMEOUT))
			if response.status_code != 200:
				raise telebot.apihelper.ApiHTTPException("Download file", response)
			return response.content
		except requests.exceptions.RequestException as e:
			if attempt == TELEGRAM_DOWNLOAD_ATTEMPTS:
				raise
			warning(f"Cannot download file from Telegram (attempt {attempt}/{TELEGRAM_DOWNLOAD_ATTEMPTS}), retrying: {describe_error(e)}")
			time.sleep(TELEGRAM_DOWNLOAD_RETRY_DELAY)


@bot.message_handler(content_types=["document"])
def handle_document(message):
	document = message.document
	is_torrent = bool(document.file_name) and document.file_name.lower().endswith(".torrent")
	# In a group the bot sees every file anyone posts: only an admin's .torrent
	# is for it, and the rest gets no answer, as with the links posted there
	if getattr(message.chat, "type", None) != "private" and (
			not is_torrent or not is_authorized(message.from_user.id, message.chat.id)):
		return
	if not check_auth(message):
		return
	if not is_torrent:
		send_message(message.chat.id, get_text("ADD_INVALID_FILE"), thread_id=message_thread(message))
		return
	if document.file_size and document.file_size > 20 * 1024 * 1024:
		send_message(message.chat.id, get_text("ADD_INVALID_FILE"), thread_id=message_thread(message))
		return
	try:
		data = download_telegram_file(document.file_id)
	except Exception as e:
		send_message(message.chat.id, get_text("ADD_ERROR", html.escape(describe_error(e))), thread_id=message_thread(message))
		return
	name = document.file_name[:-len(".torrent")]
	count_usage("add_file")
	start_add_flow(message.chat.id, name, data=data, thread_id=message_thread(message))


@bot.message_handler(func=lambda message: True)
def handle_text(message):
	if not message.text:
		return
	# Before the pending input: a command for another bot must not consume it
	if is_for_another_bot(message):
		return
	if not is_authorized(message.from_user.id, message.chat.id):
		return

	text = message.text.strip()
	if is_link(text):
		# A link pasted while a question waits is a torrent to add: taken as
		# the answer it was a move to a folder called "magnet:?xt=..." or a
		# torrent renamed to it, and the torrent was never added
		drop_pending_input(message.chat.id, message.from_user.id)
	else:
		pending = pop_pending_input(message.chat.id, message.from_user.id, thread_id=message_thread(message))
		if pending:
			handle_pending_input(message, pending)
			return

	if text.lower().startswith("magnet:"):
		add_magnet_from_message(message, text)
	elif text.lower().startswith(("http://", "https://")) and " " not in text:
		add_url_from_message(message, text)
	else:
		# Links inside a longer message or hidden behind text ("Hola! Esto es un enlace")
		for url in message_links(message):
			if url.lower().startswith("magnet:"):
				add_magnet_from_message(message, url)
			elif url.lower().startswith(("http://", "https://")):
				add_url_from_message(message, url)


def is_link(text):
	"""A whole message that is a magnet or a single web link"""
	lowered = (text or "").strip().lower()
	return lowered.startswith("magnet:") or (lowered.startswith(("http://", "https://")) and " " not in lowered)


def message_links(message):
	"""The URLs of the link entities of a message, without duplicates. A
	text_link carries its URL; a url entity is a slice of the text, whose
	offsets Telegram counts in UTF-16 code units"""
	links = []
	encoded = message.text.encode("utf-16-le")
	for entity in message.entities or []:
		if entity.type == "text_link":
			url = entity.url
		elif entity.type == "url":
			url = encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le", errors="ignore")
		else:
			continue
		if url and url not in links:
			links.append(url)
	return links


def add_magnet_from_message(message, magnet):
	name = extract_magnet_name(magnet)
	count_usage("add_magnet")
	start_add_flow(message.chat.id, name, magnet=magnet, thread_id=message_thread(message))


def add_url_from_message(message, url):
	# In groups, ignore invalid links silently (the bot may live with other
	# bots and people pasting links); only reply in private chats
	is_private = message.chat.type == "private"
	try:
		data, name = download_torrent_from_url(url)
	except MagnetRedirect as redirect:
		add_magnet_from_message(message, redirect.magnet)
		return
	except ValueError:
		debug(f"URL is not a torrent, ignored: {url}")
		if is_private:
			send_message(message.chat.id, get_text("ADD_URL_NOT_TORRENT"), thread_id=message_thread(message))
		return
	except Exception as e:
		debug(f"Cannot download URL {url}: {e}")
		if is_private:
			send_message(message.chat.id, get_text("ADD_URL_ERROR", html.escape(str(e))), thread_id=message_thread(message))
		return
	count_usage("add_url")
	start_add_flow(message.chat.id, name, data=data, thread_id=message_thread(message))


# ---------------------------------------------------------------------------
# CALLBACK HANDLER
# ---------------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
	if not is_authorized(call.from_user.id, call.message.chat.id):
		bot.answer_callback_query(call.id, get_text("USER_NOT_ALLOWED", call.from_user.id).replace("<code>", "").replace("</code>", ""))
		return

	chat_id = call.message.chat.id
	message_id = call.message.message_id
	command = call.data.split("|")[0]

	tooltip = None
	if command == "cancelInput":
		tooltip = get_text("INPUT_CANCELLED")
	elif command == "cancelAdd":
		tooltip = get_text("ADD_CANCELLED")
	elif command == "addQCancel" and queued_adds.is_busy(call.data.split("|")[1] if "|" in call.data else ""):
		tooltip = get_text("ADD_QUEUE_BUSY")
	elif command == "reannounce":
		tooltip = get_text("REANNOUNCED")
	try:
		bot.answer_callback_query(call.id, tooltip)
	except Exception:
		pass

	if command != "noop":
		count_usage(f"btn_{command}")
	thread_id = message_thread(call.message)
	# A button means the user moved on from any question still waiting
	if command != "cancelInput":
		drop_pending_input(chat_id, call.from_user.id)
	# Only a button that repaints this message stops it repainting itself: the
	# dead arrows of a list and the page prompt leave it as it was, and a
	# stopped screen would be left saying it updates itself, with no button
	if command not in ("noop", "goto"):
		stop_live_view(chat_id, message_id, thread_id)
	dispatch_callback(chat_id, message_id, call.from_user.id, call.data, thread_id=thread_id, message_html=getattr(call.message, "html_text", None))


def dispatch_callback(chat_id, message_id, user_id, data, thread_id=None, message_html=None):
	"""Runs the action encoded in a callback_data string, editing message_id.
	thread_id is the topic the button was pressed in: the screens that answer by
	sending a new message instead of editing must stay in that same topic.
	message_html is the message the button is under, for the buttons that
	update a message they did not build (the ones under an add message)"""
	parts = data.split("|")
	command = parts[0]
	args = parts[1:]

	try:
		if command == "noop":
			pass

		elif command == "cerrar":
			# Telegram does not let a message older than 48 hours be deleted:
			# it is left without buttons instead of the tap doing nothing
			if not delete_message(chat_id, message_id):
				remove_markup(chat_id, message_id)

		elif command == "dashboard" or command == "refreshDashboard":
			show_dashboard(chat_id, thread_id=thread_id, message_id=message_id)

		elif command == "list":
			filter_key, page = args[0], args[1]
			render_list(chat_id, message_id, filter_key, page, thread_id=thread_id)

		elif command == "massMenu":
			text, markup = build_mass_menu(args[0], args[1] if len(args) > 1 else 0)
			edit_message(chat_id, message_id, text, markup)

		elif command == "sort":
			text, markup = build_sort_menu(args[0])
			edit_message(chat_id, message_id, text, markup)

		elif command == "sortSet":
			# The menu stays, with the mark moved, so the order can be turned
			# around right away. Its back button opens the list on the first
			# page: the one being looked at shows other torrents now
			set_sort(args[1])
			text, markup = build_sort_menu(args[0])
			edit_message(chat_id, message_id, text, markup)

		elif command == "info":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			render_detail(chat_id, message_id, torrent_id, filter_key, page, thread_id=thread_id)

		elif command in ("pause", "resume", "verify", "reannounce"):
			torrent_id, filter_key, page = args[0], args[1], args[2]
			# A button left in the chat can outlive its torrent (or carry an id
			# the client no longer honors), and that is not a connection error
			if client.get_torrent(torrent_id) is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
				return
			if command == "pause":
				client.pause_torrents([torrent_id])
			elif command == "resume":
				client.resume_torrents([torrent_id])
			elif command == "reannounce":
				client.reannounce_torrents([torrent_id])
			else:
				client.verify_torrent(torrent_id)
			time.sleep(0.5)
			render_detail(chat_id, message_id, torrent_id, filter_key, page, thread_id=thread_id)

		elif command == "delete":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				# The torrent id does not always fit in callback_data alongside the
				# filter (a tracker filter is longer), so the confirmation travels
				# through a nav context like the files and move screens do
				nav_ctx = new_nav_context(torrent_id, filter_key, page)
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_KEEP_DATA"), callback_data=build_call("confirmDelete", nav_ctx, "0")))
				markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_WITH_DATA"), callback_data=build_call("confirmDelete", nav_ctx, "1")))
				markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=build_call("info", torrent_id, filter_key, page)))
				edit_message(chat_id, message_id, get_text("DELETE_CONFIRM", html.escape(torrent.name)), markup)

		elif command == "confirmDelete":
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("DELETE_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			with_data = args[1]
			torrent = client.get_torrent(torrent_id)
			name = torrent.name if torrent else torrent_id
			client.remove_torrents([torrent_id], delete_data=with_data == "1")
			edit_message(chat_id, message_id, get_text("DELETED", html.escape(name)), back_close_markup(build_call("list", filter_key, page)))

		elif command in ("replace", "replaceNo"):
			# The button under a completion notice, and the way back to it
			show_replace(chat_id, message_id, args[0], confirm=command == "replace")

		elif command == "replaceSkip":
			# Both stay: the notice is left as a plain completion, with no buttons.
			# A torrent gone since then still has its name in the notice
			torrent = client.get_torrent(args[0])
			if torrent is not None:
				edit_message(chat_id, message_id, get_text("NOTIFY_COMPLETED", html.escape(torrent.name)))
			elif message_html:
				edit_message(chat_id, message_id, "\n".join(message_html.split("\n")[:2]))

		elif command == "replaceOk":
			ctx = get_action_context(args[0], "r")
			if ctx is None:
				# Expired, or a button from before the confirmation kept what it
				# listed (it carries the torrent id): asked again, listed now
				if args[0].startswith("r") and len(args[0]) == 7:
					edit_message(chat_id, message_id, get_text("REPLACE_EXPIRED"), close_markup())
				else:
					show_replace(chat_id, message_id, args[0], confirm=True)
				return
			torrent = client.get_torrent(ctx["torrent_id"])
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), close_markup())
				return
			completed = get_text("NOTIFY_COMPLETED", html.escape(torrent.name))
			edit_message(chat_id, message_id, f"{completed}\n\n{get_text('REPLACING')}")
			edit_message(chat_id, message_id, f"{completed}\n\n{replace_previous(torrent.id, only=set(ctx['previous']))}")

		elif command == "rename":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				suggested, files_plan, folders_plan, skipped = build_torrent_rename(torrent)
				if suggested or files_plan or folders_plan:
					# Same as the delete confirmation: these commands are long and the
					# torrent id plus a tracker filter would not fit in callback_data
					nav_ctx = new_nav_context(torrent_id, filter_key, page)
					auto_key = "BUTTON_RENAME_AUTO_ALL" if files_plan or folders_plan else "BUTTON_RENAME_AUTO"
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text(auto_key), callback_data=build_call("renameAuto", nav_ctx)))
					markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_MANUAL"), callback_data=build_call("renameManual", nav_ctx)))
					markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=build_call("info", torrent_id, filter_key, page)))
					edit_message(chat_id, message_id, torrent_rename_text(torrent, suggested, files_plan, folders_plan, skipped), markup)
				else:
					ask_for_input(chat_id, user_id, "rename", get_text(rename_key(torrent, "RENAME_ASK"), html.escape(torrent.name)),
								thread_id=thread_id, message_id=message_id, torrent_id=torrent_id, back_call=build_call("info", torrent_id, filter_key, page))

		elif command == "renameAuto":
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("RENAME_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				suggested, files_plan, folders_plan, _ = build_torrent_rename(torrent)
				info_call = build_call("info", torrent_id, filter_key, page)
				if not suggested and not files_plan and not folders_plan:
					edit_message(chat_id, message_id, get_text("RENAME_NO_SUGGESTION"), back_close_markup(info_call))
				elif suggested and not files_plan and not folders_plan and name_already_exists(suggested, exclude_id=torrent_id):
					edit_message(chat_id, message_id, get_text("RENAME_DUPLICATE", html.escape(suggested)), back_close_markup(info_call))
				else:
					edit_message(chat_id, message_id, apply_torrent_rename(torrent, suggested, files_plan, folders_plan), back_close_markup(info_call))

		elif command == "renameManual":
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("RENAME_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				ask_for_input(chat_id, user_id, "rename", get_text(rename_key(torrent, "RENAME_ASK"), html.escape(torrent.name)),
							thread_id=thread_id, message_id=message_id, torrent_id=torrent_id, back_call=build_call("info", torrent_id, filter_key, page))

		elif command in ("files", "file", "fileAuto", "fileManual", "filesAll", "filesAllOk", "folder", "folderAuto", "folderManual",
				"filePrio", "folderPrio"):
			ctx = get_nav_context(args[0])
			if ctx is None or len(args) < 3:
				edit_message(chat_id, message_id, get_text("FILES_CONTEXT_EXPIRED"), back_close_markup(build_call("dashboard")))
				return
			torrent_id, filter_key, page = ctx
			ctx_id = args[0]
			folder_index = args[1]
			file_page = args[2]
			back_call = build_call("files", ctx_id, folder_index, file_page)

			if command == "files":
				render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, folder_index, file_page, thread_id=thread_id)

			elif command == "filePrio":
				# filePrio|ctx|folder|page|index|key|code (older: without the key)
				file_index = args[3] if len(args) > 3 else None
				key, code = (args[4], args[5]) if len(args) > 5 else (None, args[4] if len(args) > 4 else None)
				torrent = client.get_torrent(torrent_id)
				resolved = resolve_file(torrent, file_index, key)
				priority = FILE_NORMAL if code == FILE_WANT_CODE else FILE_PRIORITY_BY_CODE.get(code)
				if resolved is None or priority not in client.file_priority_levels:
					render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, folder_index, file_page, thread_id=thread_id)
					return
				client.set_file_priority(torrent_id, [resolved[1]], priority)
				# Read back from the manager, so the mark shows what really applies
				torrent = client.get_torrent(torrent_id) or torrent
				resolved = resolve_file(torrent, resolved[0], file_key(resolved[1])) or resolved
				render_file_detail(chat_id, message_id, thread_id, torrent, resolved, ctx_id, folder_index, file_page)

			elif command == "folderPrio":
				torrent = client.get_torrent(torrent_id)
				if torrent is None:
					edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
					return
				folder = get_torrent_folder(torrent, folder_index, strict=len(args) > 3)
				if folder is None:
					# The folder is gone (renamed meanwhile): nothing is left out
					render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, folder_index, file_page, thread_id=thread_id)
					return
				if len(args) > 3:
					paths, priority = folder_download_change(torrent, folder, args[3])
					if paths:
						client.set_file_priority(torrent_id, paths, priority)
						# Read back from the manager: the screen stays, showing what really applies
						torrent = client.get_torrent(torrent_id) or torrent
				text, markup = build_folder_priority(torrent, folder, ctx_id, folder_index, file_page)
				edit_message(chat_id, message_id, text, markup)

			elif command in ("file", "fileAuto", "fileManual"):
				file_index = args[3] if len(args) > 3 else None
				torrent = client.get_torrent(torrent_id)
				resolved = resolve_file(torrent, file_index, args[4] if len(args) > 4 else None)
				if resolved is None:
					render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, folder_index, file_page, thread_id=thread_id)
					return
				index, path, _size = resolved
				entry = (path, _size)
				file_back_call = file_call("file", ctx_id, folder_index, file_page, index, path)
				if command == "file":
					render_file_detail(chat_id, message_id, thread_id, torrent, resolved, ctx_id, folder_index, file_page)
				elif command == "fileManual":
					ask_for_input(chat_id, user_id, "renameFile", get_text("FILE_RENAME_ASK", html.escape(file_basename(path))),
								thread_id=thread_id, message_id=message_id, torrent_id=torrent_id, file_path=path, back_call=file_back_call,
								file_nav=[ctx_id, folder_index, file_page])
				else:
					plan, collisions = build_rename_plan(torrent, only_path=entry[0])
					if collisions:
						edit_message(chat_id, message_id, get_text("FILE_RENAME_DUPLICATE", html.escape(collisions[0])), back_close_markup(file_back_call))
					elif not plan:
						edit_message(chat_id, message_id, get_text("FILE_NO_SUGGESTION"), back_close_markup(file_back_call))
					else:
						done, errors = apply_rename_plan(torrent_id, plan)
						# Back to the file under its new name, wherever the rename put it in the list
						file_back_call = renamed_file_call(ctx_id, folder_index, file_page, torrent_id, path, plan[0][1])
						if errors:
							edit_message(chat_id, message_id, get_text("FILES_RENAME_PARTIAL", done, len(errors), html.escape(errors[0])), back_close_markup(file_back_call))
						else:
							edit_message(chat_id, message_id, file_rename_ok_text(plan[0][1], done), back_close_markup(file_back_call))

			elif command in ("folder", "folderAuto", "folderManual"):
				torrent = client.get_torrent(torrent_id)
				if torrent is None:
					edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
					return
				folder = get_torrent_folder(torrent, folder_index, strict=True)
				if not folder:
					render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, folder_index, file_page, thread_id=thread_id)
					return
				if command == "folder":
					text, markup = build_folder_detail(torrent, folder, ctx_id, folder_index, file_page)
					edit_message(chat_id, message_id, text, markup)
				elif command == "folderManual":
					ask_for_input(chat_id, user_id, "renameFolder", get_text("FOLDER_RENAME_ASK", html.escape(file_basename(folder))),
								thread_id=thread_id, message_id=message_id, torrent_id=torrent_id, folder_path=folder,
								back_call=folder_rename_back_call(torrent, ctx_id, folder))
				else:
					suggested = suggest_for_folder(torrent, folder)
					if not suggested:
						edit_message(chat_id, message_id, get_text("FOLDER_NO_SUGGESTION"), back_close_markup(back_call))
					else:
						failure = rename_folder_to(torrent, folder, suggested)
						if failure:
							edit_message(chat_id, message_id, failure, back_close_markup(back_call))
						else:
							edit_message(chat_id, message_id, get_text("RENAME_OK_FOLDER", html.escape(suggested)),
										back_close_markup(folder_rename_back_call(torrent, ctx_id, folder)))

			else:
				torrent = client.get_torrent(torrent_id)
				if torrent is None:
					edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
				else:
					folder = get_torrent_folder(torrent, folder_index, strict=True)
					if folder is None:
						render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, folder_index, file_page, thread_id=thread_id)
						return
					files_plan, folders_plan, skipped = build_full_plan(torrent, within=folder)
					if not files_plan and not folders_plan:
						edit_message(chat_id, message_id, get_text("FILES_NO_SUGGESTIONS"), back_close_markup(back_call))
					elif command == "filesAll":
						text = get_text("FILES_RENAME_CONFIRM", rename_count_text(len(files_plan), len(folders_plan)),
										build_plan_preview(files_plan, folders_plan))
						if skipped:
							text += f"\n\n{get_text('FILES_RENAME_SKIPPED', skipped)}"
						markup = InlineKeyboardMarkup(row_width=1)
						markup.add(InlineKeyboardButton(get_text("BUTTON_CONFIRM"), callback_data=build_call("filesAllOk", ctx_id, folder_index, file_page)))
						markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=back_call))
						edit_message(chat_id, message_id, text, markup)
					else:
						files_done, folders_done, errors = apply_full_plan(torrent_id, files_plan, folders_plan)
						edit_message(chat_id, message_id, rename_result_text(files_done, folders_done, errors), back_close_markup(back_call))

		elif command == "move":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			dir_page = int(args[3]) if len(args) > 3 else 0
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				nav_ctx = new_nav_context(torrent_id, filter_key, page)
				suggested = series_dirs(torrent.name, exclude_id=torrent.id, current_dir=torrent.download_dir)
				markup = build_dir_markup(
					dir_call=lambda dir_id: build_call("moveToDir", nav_ctx, dir_id),
					write_call=build_call("moveNewDir", nav_ctx),
					cancel_call=build_call("info", torrent_id, filter_key, page),
					page_parts=("move", torrent_id, filter_key, page),
					page=dir_page,
					suggested=suggested,
				)
				text = get_text("MOVE_ASK_DIR", html.escape(torrent.name))
				hint = series_hint(torrent.name, suggested)
				if hint:
					text = f"{text}\n\n{hint}"
				edit_message(chat_id, message_id, text, markup)

		elif command in ("moveToDir", "moveNewDir"):
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			back_call = build_call("info", torrent_id, filter_key, page)
			torrent = client.get_torrent(torrent_id)
			if command == "moveToDir":
				new_dir = get_dir_by_id(args[1])
				name = torrent.name if torrent else torrent_id
				if new_dir is None:
					edit_message(chat_id, message_id, get_text("ADD_EXPIRED"), back_close_markup(back_call))
				else:
					progress_text = get_text("MOVING", html.escape(name), html.escape(new_dir))
					success_text = get_text("MOVE_OK", html.escape(name), html.escape(new_dir))
					deliver_move_order(chat_id, [torrent_id], new_dir, progress_text, success_text, thread_id=thread_id, message_id=message_id, reply_markup=back_close_markup(back_call))
			else:
				name = torrent.name if torrent else ""
				prompt = get_text("MOVE_NEW_DIR_ASK")
				if torrent and torrent.download_dir:
					prompt = f"{current_dirs_text([torrent.download_dir])}\n{prompt}"
				if name:
					prompt = f"{get_text('MOVE_ASK_DIR', html.escape(name))}\n\n{prompt}"
				ask_for_input(chat_id, user_id, "move", prompt, thread_id=thread_id, message_id=message_id, torrent_id=torrent_id, name=name, back_call=back_call)

		elif command in ("serMove", "serUndo"):
			place_series_from_message(chat_id, message_id, message_html, args[0], args[1])

		elif command == "serDrop":
			if message_html:
				edit_message(chat_id, message_id, without_series_block(message_html))
			else:
				remove_markup(chat_id, message_id)

		elif command == "serKeep":
			remove_markup(chat_id, message_id)

		elif command == "search":
			ask_for_input(chat_id, user_id, "search", get_text("SEARCH_ASK"), thread_id=thread_id, message_id=message_id)

		elif command == "trackers":
			text, markup = build_trackers_menu()
			edit_message(chat_id, message_id, text, markup)

		elif command == "categories":
			text, markup = build_categories_menu(int(args[0]) if args else 0)
			edit_message(chat_id, message_id, text, markup)

		elif command == "catMenu":
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("SEARCH_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			cat_page = int(args[1]) if len(args) > 1 else 0
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				text, markup = build_torrent_category_screen(torrent, filter_key, page, cat_page)
				edit_message(chat_id, message_id, text, markup)

		elif command in ("ratioMenu", "ratioSet", "ratioAsk", "tSpeedAsk"):
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("SEARCH_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			elif command == "ratioMenu":
				text, markup = build_ratio_screen(torrent, filter_key, page)
				edit_message(chat_id, message_id, text, markup)
			elif command == "tSpeedAsk":
				direction = "up" if args[1:] == ["up"] else "down"
				prompt = "LIMITS_ASK_UP" if direction == "up" else "LIMITS_ASK_DOWN"
				ask_for_input(chat_id, user_id, "torrentSpeed", get_text(prompt, html.escape(torrent.name)),
							thread_id=thread_id, message_id=message_id, torrent_id=torrent_id, name=torrent.name, direction=direction,
							filter_key=filter_key, page=page, back_call=build_call("ratioMenu", args[0]))
			elif command == "ratioAsk":
				ask_for_input(chat_id, user_id, "ratioLimit", get_text("RATIO_ASK_VALUE", html.escape(torrent.name)),
							thread_id=thread_id, message_id=message_id, torrent_id=torrent_id, name=torrent.name,
							filter_key=filter_key, page=page, back_call=build_call("ratioMenu", args[0]))
			else:
				value = args[1]
				limit = None if value == "g" else (-1 if value == "n" else parse_ratio(value))
				if limit is not None or value == "g":
					client.set_ratio_limit([torrent_id], limit)
					keep_hand_limit(torrent_id, ["ratio"])
				# Stays on the screen with the mark on what was just chosen, read
				# back from the manager so it shows what really applies
				torrent = client.get_torrent(torrent_id) or torrent
				text, markup = build_ratio_screen(torrent, filter_key, page)
				edit_message(chat_id, message_id, text, markup)

		elif command == "catSet":
			ctx = get_nav_context(args[0])
			category = get_cat_by_id(args[1])
			if ctx is None or category is None:
				edit_message(chat_id, message_id, get_text("SEARCH_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			elif torrent.auto_managed:
				# Relocates the data: same blocking call and same progress and
				# retry treatment as a move, or the screen would just hang
				back_call = build_call("info", torrent_id, filter_key, page)
				label = html.escape(category) if category else get_text("CATEGORY_NONE")
				deliver_move_order(
					chat_id, [torrent_id], "",
					get_text("CATEGORY_CHANGING", html.escape(torrent.name), label),
					get_text("CATEGORY_DONE", html.escape(torrent.name), label),
					message_id=message_id, thread_id=thread_id,
					reply_markup=back_close_markup(back_call),
					apply=lambda: client.set_category([torrent_id], category),
					target=f"category '{category}'" if category else "no category")
			else:
				client.set_category([torrent_id], category)
				time.sleep(0.5)
				render_detail(chat_id, message_id, torrent_id, filter_key, page, thread_id=thread_id)

		elif command == "addTo":
			pending_id, dir_id = args[0], args[1]
			download_dir = get_dir_by_id(dir_id)
			if download_dir is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
			else:
				do_add_torrent(chat_id, message_id, pending_id, download_dir, thread_id=thread_id)

		elif command == "autoManage":
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("SEARCH_EXPIRED"), back_close_markup())
				return
			torrent_id, filter_key, page = ctx
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				# Handing the location over relocates the data to the category
				# folder, so it blocks exactly like a move does
				back_call = build_call("info", torrent_id, filter_key, page)
				destination = category_save_path(torrent.category) or "?"
				deliver_move_order(
					chat_id, [torrent_id], "",
					get_text("AUTO_MANAGE_CHANGING", html.escape(torrent.name), html.escape(destination)),
					get_text("AUTO_MANAGE_DONE", html.escape(torrent.name), html.escape(destination)),
					message_id=message_id, thread_id=thread_id,
					reply_markup=back_close_markup(back_call),
					apply=lambda: client.set_auto_managed([torrent_id], True),
					target=f"automatic management -> {destination}")

		elif command == "addCatPage":
			pending_id, cat_page = args[0], int(args[1])
			pending = get_pending_torrent(pending_id)
			if pending is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
			else:
				ask_add_category(chat_id, pending_id, pending["name"], message_id=message_id, cat_page=cat_page)

		elif command == "addCat":
			pending_id, cat_id = args[0], args[1]
			category = get_cat_by_id(cat_id)
			if category is None or get_pending_torrent(pending_id) is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
			else:
				do_add_torrent(chat_id, message_id, pending_id, None, thread_id=thread_id, category=category)

		elif command == "addNewDir":
			pending_id = args[0]
			pending_torrent = get_pending_torrent(pending_id)
			if pending_torrent is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
			else:
				prompt = get_text("MOVE_NEW_DIR_ASK")
				name = pending_torrent.get("name")
				if name:
					prompt = f"{get_text('ADD_ASK_DIR', html.escape(name))}\n\n{prompt}"
				ask_for_input(chat_id, user_id, "addDir", prompt, thread_id=thread_id, message_id=message_id, pending_id=pending_id,
							back_call=build_call("dashboard"))

		elif command == "addQCancel":
			item = queued_adds.cancel(args[0])
			if isinstance(item, dict):
				# A try that timed out may still land: it is not the bot's add
				# to tell any more
				with _bot_adds_lock:
					for torrent_id in item["ids"]:
						pop_late_add(torrent_id)
				edit_message(chat_id, message_id, get_text("ADD_QUEUE_CANCELLED", html.escape(item["name"] or "?")))
			# "busy": being sent right now, its result is about to land on this
			# message. None: it ended already, and this message tells how

		elif command == "cancelAdd":
			# Gone already means the folder was chosen and the add is under way:
			# its result is about to land on this message, and a dashboard
			# opened here would paint itself over it
			with _contexts_lock:
				under_way = args[0] in _adding
			if not under_way:
				# Cancelled, or expired (a restart forgets them): either way the
				# prompt is done with, and the dashboard takes its place
				pop_pending_torrent(args[0])
				show_dashboard(chat_id, thread_id=thread_id, message_id=message_id)

		elif command == "addDirPage":
			pending_id, dir_page = args[0], int(args[1])
			pending = get_pending_torrent(pending_id)
			if pending is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
			else:
				ask_download_dir(chat_id, pending_id, pending["name"], thread_id=thread_id, message_id=message_id, dir_page=dir_page)

		elif command == "mass":
			action, filter_key = args[0], args[1]
			dir_page = int(args[2]) if len(args) > 2 else 0
			render_mass_confirm(chat_id, message_id, action, filter_key, dir_page=dir_page)

		elif command == "confirmMass":
			action, mass_ctx, extra = args[0], args[1], args[2]
			do_mass_action(chat_id, message_id, action, mass_ctx, extra)

		elif command in ("massSpeedAsk", "massRatioAsk"):
			ctx = get_action_context(args[0], "m")
			if ctx is None:
				resume_old_mass_button(chat_id, message_id, "ratio", args[0])
				return
			back_call = build_call("mass", "ratio", ctx["filter_key"])
			if command == "massSpeedAsk":
				direction = "up" if args[1:2] == ["up"] else "down"
				ask_for_input(chat_id, user_id, "massSpeed", get_text("MASS_LIMITS_ASK_UP" if direction == "up" else "MASS_LIMITS_ASK_DOWN"),
							thread_id=thread_id, message_id=message_id, mass_ctx=args[0], direction=direction, back_call=back_call)
			else:
				ask_for_input(chat_id, user_id, "massRatio", get_text("MASS_RATIO_ASK_VALUE"), thread_id=thread_id, message_id=message_id,
							mass_ctx=args[0], back_call=back_call)

		elif command == "keepSeeding":
			torrent_id = args[0]
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), close_markup())
				return
			client.set_ratio_limit([torrent_id], -1)
			keep_hand_limit(torrent_id, ["ratio"])
			client.resume_torrents([torrent_id])
			base = message_html or get_text("NOTIFY_RATIO_REACHED", html.escape(torrent.name), "")
			edit_message(chat_id, message_id, f"{base}\n\n{get_text('KEEP_SEEDING_DONE')}", close_markup())

		elif command == "massMoveDir":
			mass_ctx, dir_id = args[0], args[1]
			if get_action_context(mass_ctx, "m") is None:
				resume_old_mass_button(chat_id, message_id, "move", mass_ctx)
				return
			new_dir = get_dir_by_id(dir_id)
			if new_dir is None:
				edit_message(chat_id, message_id, get_text("MASS_EXPIRED"), back_close_markup())
			else:
				do_mass_move_to(chat_id, mass_ctx, new_dir, thread_id=thread_id, message_id=message_id)

		elif command == "massCat":
			mass_ctx = args[0]
			category = get_cat_by_id(args[1])
			if get_action_context(mass_ctx, "m") is None or category is None:
				edit_message(chat_id, message_id, get_text("MASS_EXPIRED"), back_close_markup())
			else:
				do_mass_category(chat_id, mass_ctx, category, thread_id=thread_id, message_id=message_id)

		elif command == "massMoveNew":
			mass_ctx = args[0]
			ctx = get_action_context(mass_ctx, "m")
			if ctx is None:
				resume_old_mass_button(chat_id, message_id, "move", mass_ctx)
				return
			prompt = get_text("MOVE_NEW_DIR_ASK")
			try:
				counted = set(ctx["ids"])
				dirs_text = current_dirs_text([t.download_dir for t in client.get_torrents() if str(t.id) in counted])
				if dirs_text:
					prompt = f"{dirs_text}\n{prompt}"
			except TorrentClientError:
				pass
			ask_for_input(chat_id, user_id, "massMove", prompt, thread_id=thread_id, message_id=message_id, mass_ctx=mass_ctx,
						back_call=build_call("list", ctx["filter_key"], 0))

		elif command == "settings":
			render_settings(chat_id, message_id, args[0] if args else "main")

		elif command == "tlPage":
			text, markup = build_settings_tracker_limits(args[0] if args else 0)
			edit_message(chat_id, message_id, text, markup)

		elif command == "tlAdd":
			text, markup = build_tracker_rule_pick()
			edit_message(chat_id, message_id, text, markup)

		elif command in ("tlRule", "tlRatio", "tlRatioAsk", "tlSpeedAsk", "tlDel", "tlDelOk"):
			tracker = tracker_from_key(args[0]) if args else None
			if tracker is None:
				edit_message(chat_id, message_id, get_text("TRACKER_LIMITS_GONE"), back_close_markup(settings_call("tlimits")))
				return
			# The tracker filter the rule was opened from, the last argument of every button
			back_filter = {"tlRule": args[1:2], "tlRatio": args[2:3], "tlRatioAsk": args[1:2], "tlSpeedAsk": args[2:3],
						"tlDel": args[1:2], "tlDelOk": args[2:3]}[command]
			back_filter = back_filter[0] if back_filter else ""
			rule_call = build_call("tlRule", args[0], *([back_filter] if back_filter else []))
			done = None
			if command == "tlRatioAsk":
				ask_for_input(chat_id, user_id, "trackerRatio", get_text("TRACKER_LIMITS_ASK_RATIO", html.escape(tracker)),
							thread_id=thread_id, message_id=message_id, tracker=tracker, back_filter=back_filter, back_call=rule_call)
				return
			if command == "tlSpeedAsk":
				direction = "up" if args[1:2] == ["up"] else "down"
				prompt = "TRACKER_LIMITS_ASK_UP" if direction == "up" else "TRACKER_LIMITS_ASK_DOWN"
				ask_for_input(chat_id, user_id, "trackerSpeed", get_text(prompt, html.escape(tracker)), thread_id=thread_id,
							message_id=message_id, tracker=tracker, direction=direction, back_filter=back_filter, back_call=rule_call)
				return
			if command == "tlDel":
				text, markup = build_tracker_rule_delete(tracker, back_filter)
				edit_message(chat_id, message_id, text, markup)
				return
			if command == "tlDelOk":
				restored = delete_tracker_rule(tracker, restore=args[1:2] == ["1"])
				text = get_text("TRACKER_LIMITS_DELETED", html.escape(tracker))
				if restored:
					text += "\n" + get_text("TRACKER_LIMITS_RESTORED", restored)
				edit_message(chat_id, message_id, text, back_close_markup(tracker_rule_back(back_filter)))
				return
			if command == "tlRatio":
				value = args[1] if len(args) > 1 else ""
				limit = None if value == "g" else (-1 if value == "n" else parse_ratio(value))
				if limit is not None or value == "g":
					count = set_tracker_rule(tracker, "ratio", limit)
					done = get_text("TRACKER_LIMITS_APPLIED", count)
			text, markup = build_tracker_rule_screen(tracker, back_filter, done=done)
			edit_message(chat_id, message_id, text, markup)

		elif command == "toggleAltSpeed":
			settings = client.get_settings()
			client.set_alt_speed(not settings["alt_speed_enabled"])
			render_settings(chat_id, message_id, "speed")

		elif command in ("toggleDownLimit", "toggleUpLimit"):
			direction = "down" if command == "toggleDownLimit" else "up"
			settings = client.get_settings()
			if settings[f"speed_limit_{direction}_enabled"]:
				turn_speed_limit_off(direction, settings)
			else:
				kbps = settings[f"speed_limit_{direction}"] or saved_speed_limit(direction)
				if not kbps:
					# No limit known to bring back: asked, as when setting one
					ask_for_input(chat_id, user_id, f"{direction}Limit",
								get_text("SETTINGS_ASK_DOWN_LIMIT" if direction == "down" else "SETTINGS_ASK_UP_LIMIT"),
								thread_id=thread_id, message_id=message_id, back_call=settings_call("speed"))
					return
				client.set_speed_limit(direction, kbps, enabled=True)
			render_settings(chat_id, message_id, "speed")

		elif command == "globalRatioSet":
			value = args[0] if args else ""
			limit = -1 if value == "n" else parse_ratio(value)
			if limit is not None:
				client.set_global_ratio_limit(limit)
			render_settings(chat_id, message_id, "ratio")

		elif command == "globalRatioAsk":
			ask_for_input(chat_id, user_id, "globalRatio", get_text("SETTINGS_ASK_RATIO"), thread_id=thread_id, message_id=message_id,
						back_call=settings_call("ratio"))

		elif command == "setDownLimit":
			ask_for_input(chat_id, user_id, "downLimit", get_text("SETTINGS_ASK_DOWN_LIMIT"), thread_id=thread_id, message_id=message_id,
						back_call=settings_call("speed"))

		elif command == "setUpLimit":
			ask_for_input(chat_id, user_id, "upLimit", get_text("SETTINGS_ASK_UP_LIMIT"), thread_id=thread_id, message_id=message_id,
						back_call=settings_call("speed"))

		elif command == "telemetryMenu":
			text, markup = build_telemetry_settings()
			edit_message(chat_id, message_id, text, markup)

		elif command == "toggleTelemetry":
			if not bot_settings.toggle("telemetry"):
				# The id goes too, so turning them back on later starts as a new
				# installation instead of stitching the two periods together
				telemetry_client.forget()
			text, markup = build_telemetry_settings()
			edit_message(chat_id, message_id, text, markup)

		elif command == "telemetryShow":
			# A message of its own, so the screen it was pressed on stays as it was
			text, markup = build_telemetry_preview()
			send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)

		elif command == "toggleSetting":
			enabled = bot_settings.toggle(args[0])
			if args[0] == "auto_rename" and not enabled:
				# Both depend on the torrent rename: never leave them active but hidden
				bot_settings.set("auto_rename_files", False)
				bot_settings.set("auto_rename_external", False)
				bot_settings.set("auto_replace", False)
			if args[0] == "auto_move_series" and not enabled:
				bot_settings.set("auto_move_series_external", False)
			render_settings(chat_id, message_id, SETTING_SCREENS.get(args[0], "main"))

		elif command == "autoDirMenu":
			dir_page = int(args[0]) if args else 0
			markup = build_dir_markup(
				dir_call=lambda dir_id: build_call("autoDirSet", dir_id),
				write_call=build_call("autoDirNew"),
				cancel_call=settings_call("add"),
				page_parts=("autoDirMenu",),
				page=dir_page,
			)
			markup.keyboard.insert(0, [InlineKeyboardButton(get_text("BUTTON_AUTO_DIR_DEFAULT"), callback_data=build_call("autoDirDefault"))])
			text = f"{get_text('AUTO_DIR_ASK')}\n\n{get_text('AUTO_DIR_CURRENT', auto_dir_label())}"
			edit_message(chat_id, message_id, text, markup)

		elif command == "autoDirSet":
			new_dir = get_dir_by_id(args[0])
			if new_dir is not None:
				bot_settings.set("auto_download_dir", new_dir)
			render_settings(chat_id, message_id, "add")

		elif command == "autoDirDefault":
			bot_settings.set("auto_download_dir", "")
			render_settings(chat_id, message_id, "add")

		elif command == "autoDirNew":
			ask_for_input(chat_id, user_id, "autoDir", get_text("MOVE_NEW_DIR_ASK"), thread_id=thread_id, message_id=message_id,
						back_call=settings_call("add"))

		elif command == "newSerMenu":
			markup = build_dir_markup(
				dir_call=lambda dir_id: build_call("newSerSet", dir_id),
				write_call=build_call("newSerNew"),
				cancel_call=settings_call("add"),
				page_parts=("newSerMenu",),
				page=int(args[0]) if args else 0,
				suggested=series_parent_dirs(),
			)
			markup.keyboard.insert(0, [InlineKeyboardButton(get_text("BUTTON_NEW_SERIES_DIR_NONE"), callback_data=build_call("newSerNone"))])
			text = f"{get_text('NEW_SERIES_DIR_ASK')}\n\n{get_text('NEW_SERIES_DIR_CURRENT', new_series_dir_label())}"
			if any(button.text.startswith("✨") for row in markup.keyboard for button in row):
				text += f"\n{get_text('NEW_SERIES_DIR_SUGGESTED')}"
			edit_message(chat_id, message_id, text, markup)

		elif command in ("newSerSet", "newSerForce"):
			new_dir = get_dir_by_id(args[0])
			if new_dir is None:
				render_settings(chat_id, message_id, "add")
			elif command == "newSerSet" and new_series_dir_problem(new_dir):
				text, markup = new_series_dir_warning(new_dir, new_series_dir_problem(new_dir))
				edit_message(chat_id, message_id, text, markup)
			else:
				bot_settings.set("new_series_dir", new_dir.rstrip("/") or "/")
				render_settings(chat_id, message_id, "add")

		elif command == "newSerNone":
			bot_settings.set("new_series_dir", "")
			render_settings(chat_id, message_id, "add")

		elif command == "newSerNew":
			ask_for_input(chat_id, user_id, "newSerDir", get_text("MOVE_NEW_DIR_ASK"), thread_id=thread_id, message_id=message_id,
						back_call=settings_call("add"))

		elif command == "autoCatMenu":
			markup = build_category_markup(
				cat_call=lambda cat_id: build_call("autoCatSet", cat_id),
				cancel_call=settings_call("add"),
				include_none=False,
				page_parts=("autoCatMenu",),
				page=int(args[0]) if args else 0,
			)
			markup.keyboard.insert(0, [InlineKeyboardButton(get_text("BUTTON_AUTO_CATEGORY_NONE"), callback_data=build_call("autoCatSet", get_cat_id("")))])
			text = f"{get_text('AUTO_CATEGORY_ASK')}\n\n{get_text('AUTO_CATEGORY_CURRENT', auto_category_label())}"
			edit_message(chat_id, message_id, text, markup)

		elif command == "autoCatSet":
			category = get_cat_by_id(args[0])
			if category is not None:
				bot_settings.set("auto_category", category)
			render_settings(chat_id, message_id, "add")

		elif command == "favDirsMenu":
			render_favorite_dirs_menu(chat_id, message_id)

		elif command == "favDirAdd":
			ask_for_input(chat_id, user_id, "favDir", get_text("MOVE_NEW_DIR_ASK"), thread_id=thread_id, message_id=message_id,
						back_call=build_call("favDirsMenu"))

		elif command == "favDirDel":
			directory = get_dir_by_id(args[0])
			favorites = bot_settings.get("favorite_dirs") or []
			if directory in favorites:
				favorites.remove(directory)
				bot_settings.set("favorite_dirs", favorites)
			render_favorite_dirs_menu(chat_id, message_id)

		elif command == "moverAsk" and args and args[0] in ("source", "target") and build_mover_folder(args[0]):
			text, markup = build_mover_folder(args[0])
			edit_message(chat_id, message_id, text, markup)

		elif command == "moverSet":
			directory = get_dir_by_id(args[1])
			problem = get_text("SEARCH_EXPIRED") if directory is None else set_mover_field(args[0], directory)
			text, markup = build_settings("mover")
			edit_message(chat_id, message_id, f"{problem or get_text('SETTINGS_UPDATED')}\n\n{text}", markup)

		elif command in ("moverAsk", "moverType"):
			field = args[0] if args and args[0] in MOVER_FIELDS else "source"
			ask_for_input(chat_id, user_id, "moverField", mover_ask_text(field), thread_id=thread_id,
						message_id=message_id, field=field, back_call=settings_call("mover"))

		elif command == "moverPreview":
			text, markup = build_mover_preview()
			edit_message(chat_id, message_id, text, markup)

		elif command == "moverRun":
			# The notice of the move is a message of its own, which the move
			# updates when it is over: this screen has nothing left to offer,
			# and painting another one would mean asking a manager that may
			# not answer while it moves
			plan = run_auto_mover(force=True, chat_id=chat_id, thread_id=thread_id)
			if plan.outcome == "move":
				if not delete_message(chat_id, message_id):
					remove_markup(chat_id, message_id)
			else:
				edit_message(chat_id, message_id, mover_outcome_text(plan), back_close_markup(settings_call("mover")))

		elif command == "tplMenu":
			render_templates_menu(chat_id, message_id)

		elif command == "tplEdit":
			kind = args[0]
			current = bot_settings.get(f"template_{kind}") or {
				"movie": DEFAULT_MOVIE_TEMPLATE, "series": DEFAULT_SERIES_TEMPLATE, "season": DEFAULT_SEASON_PACK_TEMPLATE}[kind]
			example = {"movie": TEMPLATE_EXAMPLE_MOVIE, "series": TEMPLATE_EXAMPLE_SERIES, "season": TEMPLATE_EXAMPLE_SEASON}[kind]
			current_block = get_text("TPL_ASK_CURRENT", html.escape(current), html.escape(parse_name(example) or "-"))
			prompt_key = {"movie": "TPL_ASK_MOVIE", "series": "TPL_ASK_SERIES", "season": "TPL_ASK_SEASON"}[kind]
			prompt = get_text(prompt_key, f"{current_block}\n\n{get_text('TPL_FIELDS_HELP')}")
			ask_for_input(chat_id, user_id, "template", prompt, thread_id=thread_id, message_id=message_id, kind=kind,
						back_call=build_call("tplMenu"))

		elif command == "tplReset":
			bot_settings.set("template_movie", "")
			bot_settings.set("template_series", "")
			bot_settings.set("template_season", "")
			render_templates_menu(chat_id, message_id)

		elif command == "goto":
			base = get_page_context(args[0])
			page, pages = int(args[1]), int(args[2])
			if base is None:
				edit_message(chat_id, message_id, get_text("SEARCH_EXPIRED"), back_close_markup())
			else:
				ask_for_input(chat_id, user_id, "gotoPage", get_text("GOTO_PAGE_ASK", pages), thread_id=thread_id,
							back_call=f"{base}|{page}", target_message_id=message_id, base=base, pages=pages)

		elif command == "cancelInput":
			pending_input = pop_pending_input(chat_id, user_id, any_thread=True)
			back_call = (pending_input or {}).get("back_call")
			if back_to_add_dir(chat_id, pending_input, message_id=message_id):
				pass
			elif back_call:
				# The tap already told it was cancelled
				dispatch_callback(chat_id, message_id, user_id, back_call, thread_id=thread_id)
			else:
				show_dashboard(chat_id, thread_id=thread_id, message_id=message_id)

		else:
			debug(f"Unknown callback: {data}")

	except TorrentClientError as e:
		edit_message(chat_id, message_id, client_error_text(e), back_close_markup())
	except ExpiredContext:
		edit_message(chat_id, message_id, get_text("SEARCH_EXPIRED"), back_close_markup())
	except Exception as e:
		error(f"Error handling callback {data}: {e}")
		edit_message(chat_id, message_id, get_text("ERROR_GENERIC", html.escape(str(e))), back_close_markup())


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def send_startup_message():
	try:
		torrent_count = len(client.get_torrents())
		text = get_text("STARTUP_MESSAGE", VERSION, html.escape(client_version), torrent_count)
	except TorrentClientError as e:
		text = get_text("STARTUP_MESSAGE_ERROR", VERSION, html.escape(str(e)))
	notify(text)


def handle_external_torrent(torrent_id):
	"""A torrent the bot did not add (another app, the manager's own UI...).
	Tells about it and auto-renames it, depending on the settings"""
	try:
		torrent = client.get_torrent(torrent_id)
	except TorrentClientError as e:
		warning(f"Cannot read the externally added torrent {torrent_id}: {e}")
		return
	if torrent is None:
		return
	lines = [get_text("NOTIFY_EXTERNAL_ADDED", html.escape(torrent.name), html.escape(torrent.download_dir or ""))]
	if torrent.total_size and torrent.total_size > 0:
		lines.append(get_text("ADD_OK_SIZE", sizeof_fmt(torrent.total_size)))
	block, markup, target, new = None, None, None, False
	if (bot_settings.get("notify_external_added") or auto_move_series_enabled(external=True)) and not torrent.auto_managed:
		target, new = external_series_target(torrent)
		if target is None and bot_settings.get("notify_external_added"):
			block, markup = series_suggestion(torrent, torrent.download_dir or "")
	renamed = []
	if auto_rename_enabled() and bot_settings.get("auto_rename_external"):
		if torrent.files:
			renamed = auto_rename_lines(torrent)
		else:
			# Its own notifications tell the outcome once the metadata arrives
			lines.append(get_text("ADD_AUTO_RENAME_PENDING"))
			threading.Thread(target=deferred_auto_rename, args=(torrent.id, torrent.name), daemon=True).start()
	# With the add notification off, a rename or a move is still worth telling:
	# the torrent changed without anyone in the chat asking for it
	if bot_settings.get("notify_external_added") or renamed or target:
		count_usage("auto_notify_external")
		text = "\n".join(lines + renamed)
		if target:
			move_external_to_series(torrent, text, target, new)
			return
		if block:
			text = f"{text}\n\n{block}"
		notify(text, reply_markup=markup)


def external_series_target(torrent):
	"""The folder of its series a torrent the bot did not add goes to with
	the automatic move on, as (folder, new), see series_target"""
	current_dir = torrent.download_dir or ""
	if not (auto_move_series_enabled(external=True) and current_dir):
		return None, False
	files = [path for path, _size, _done in torrent.files] if torrent.files else None
	return series_target(torrent.name, exclude_id=torrent.id, current_dir=current_dir, files=files)


def move_external_to_series(torrent, text, target, new=False):
	"""Moves a torrent the bot did not add to the folder of its series. It is
	already in the manager, so this really moves its data, which can take
	long: the notice goes out at once saying where it is going, and the move
	is delivered (and retried) in the background like any other, which then
	updates the notice with where it ended up. Done inline, the monitor
	waited for the move, and a timeout was told as the torrent staying where
	it was while the manager went on moving it"""
	count_usage("auto_new_series" if new else "auto_move_series")
	chat_id = TELEGRAM_GROUP if TELEGRAM_GROUP else ADMIN_IDS[0]
	progress_text = f"{text}\n\n{get_text('SERIES_MOVING', html.escape(target))}"
	sent = notify(progress_text)
	placed_block, placed_markup = series_placed(torrent.id, series_identity(torrent.name)["title"], torrent.download_dir or "", new)
	deliver_move_order(chat_id, [torrent.id], target, progress_text, f"{with_dir_line(text, target)}\n\n{placed_block}",
					message_id=sent.message_id if sent else None, thread_id=TELEGRAM_THREAD, reply_markup=placed_markup,
					wrap=lambda failure: f"{text}\n\n{failure}", failure_markup=None)


def torrent_finished(torrent, prev):
	"""Whether the monitor counts the torrent as finished. While a torrent is
	checked (or queued to be), qBittorrent and Deluge give the progress of the
	check, so a finished one goes 100 -> 0 -> 100 and looked freshly completed
	after every recheck: it keeps what it was. None is unknown (first seen
	while checking: data that was already there, or a restart), and becoming
	finished from unknown is not a completion. A torrent with nothing left to
	download (every file left out) is not finished either: there was no
	download to announce"""
	if torrent.status in (TorrentStatus.CHECKING, TorrentStatus.QUEUED) and not torrent.is_finished:
		if prev:
			return prev["finished"]
		return None if torrent.status == TorrentStatus.CHECKING else False
	return bool(torrent.is_finished) and (torrent.total_size or 0) > 0


def torrent_error_text(torrent):
	return torrent.error_message or ""


def poll_torrents(states):
	"""One pass of the monitor. states holds the last state of every torrent
	seen since the bot started (None on the first poll, which only builds the
	baseline). Torrents that drop out of the list are remembered too, so a
	manager that is restarting and lists them bit by bit never makes them look
	new or freshly completed. Returns the updated states"""
	baseline = states is None
	states = {} if baseline else states
	torrents = client.get_torrents()
	new_ids = [str(t.id) for t in torrents if str(t.id) not in states]
	external = set()
	late = {}
	if new_ids and not baseline:
		watch = (bot_settings.get("notify_external_added") or (auto_rename_enabled() and bot_settings.get("auto_rename_external"))
				or auto_move_series_enabled(external=True))
		# Waits for any add in progress: the bot registers what it added
		with _bot_adds_lock:
			for torrent_id in new_ids:
				if _bot_added_ids.pop(torrent_id, None) is not None:
					continue
				late_add = pop_late_add(torrent_id)
				if late_add is None and queued_adds.find(torrent_id) is not None:
					late_add = {}  # Its queued add has what it was for
				if late_add is not None:
					late[torrent_id] = late_add
				elif watch:
					external.add(torrent_id)
	for torrent in torrents:
		torrent_id = str(torrent.id)
		prev = states.get(torrent_id)
		state = {"finished": torrent_finished(torrent, prev), "error": torrent_error_text(torrent), "status": torrent.status}
		if not baseline:
			if torrent_id in late:
				try:
					handle_late_add(torrent.id, late[torrent_id])
				except Exception as e:
					warning(f"Cannot handle the late added torrent {torrent.name}: {e}")
			if torrent_id in external:
				# Inline, so it is told before a completion seen in this same poll.
				# A failure must not stop it from being recorded as seen, or the
				# next poll would handle it again
				try:
					handle_external_torrent(torrent.id)
				except Exception as e:
					warning(f"Cannot handle the externally added torrent {torrent.name}: {e}")
			# A torrent already finished the first time it is seen is a
			# download that completed between two polls, not a leftover:
			# the baseline built on the first poll is what covers restarts
			if state["finished"] is True and (prev is None or prev["finished"] is False):
				try:
					handle_completed(torrent)
				except Exception as e:
					warning(f"Cannot handle the completion of {torrent.name}: {e}")
			if (prev and prev.get("status") == TorrentStatus.SEEDING and state["status"] == TorrentStatus.PAUSED
					and client.supports_ratio_limit and (bot_settings.get("notify_ratio_reached") or auto_remove_ratio_enabled())):
				try:
					handle_seeding_stopped(torrent.id)
				except Exception as e:
					warning(f"Cannot handle {torrent.name} stopping seeding: {e}")
			if (state["error"] and (prev is None or state["error"] != prev["error"]) and bot_settings.get("notify_errors")
					and not mover_tells(torrent_id)):
				count_usage("auto_notify_error")
				notify(get_text("NOTIFY_TORRENT_ERROR", html.escape(torrent.name), html.escape(state["error"])))
		states[torrent_id] = state
	if tracker_limits_supported():
		try:
			apply_pending_tracker_limits(torrents)
		except Exception as e:
			warning(f"Cannot apply the limits per tracker: {e}")
	return states


def torrent_monitor():
	"""Background loop that detects finished/errored/externally added torrents
	and notifies. The first poll only builds the baseline, so restarting the
	bot never re-notifies torrents that were already there"""
	states = None
	# The first check of the mover comes one interval after the start, once
	# the manager had time to be up and listing all its torrents
	last_mover = time.monotonic()
	while True:
		try:
			states = poll_torrents(states)
		except Exception as e:
			warning(f"Torrent monitor: {e}")
		if time.monotonic() - last_mover >= AUTO_MOVER_INTERVAL_SECONDS:
			last_mover = time.monotonic()
			try:
				auto_mover_tick()
			except Exception as e:
				warning(f"Automatic mover: {e}")
		time.sleep(MONITOR_INTERVAL_SECONDS)


def track_polling_heartbeat():
	"""Touches HEARTBEAT_PATH every time Telegram answers a poll. infinity_polling
	swallows every error and retries forever, so a bot that serves nobody (no
	network, or a 409 because another instance uses the same token) keeps the
	process alive; the Docker HEALTHCHECK looks at how old this file is instead"""
	get_updates = bot.get_updates
	def get_updates_with_heartbeat(*args, **kwargs):
		updates = get_updates(*args, **kwargs)
		try:
			with open(HEARTBEAT_PATH, "a"):
				os.utime(HEARTBEAT_PATH)
		except OSError as e:
			warning(f"Cannot update heartbeat file {HEARTBEAT_PATH}: {e}")
		return updates
	bot.get_updates = get_updates_with_heartbeat


if __name__ == "__main__":
	debug(f"torrent-controller-bot {VERSION} started. Connected to {client_version}")
	send_startup_message()
	mover_close_lost_notice()
	threading.Thread(target=torrent_monitor, daemon=True).start()
	threading.Thread(target=add_queue_worker, daemon=True).start()
	if TELEMETRY_DEBUG:
		warning(f"TELEMETRY_DEBUG is on: statistics go to {TELEMETRY_ENDPOINT} a minute after every start")
	telemetry_client.start()
	try:
		bot.set_my_commands([
			telebot.types.BotCommand("/start", get_text("MENU_START")),
			telebot.types.BotCommand("/list", get_text("MENU_LIST")),
			telebot.types.BotCommand("/find", get_text("MENU_FIND")),
			telebot.types.BotCommand("/add", get_text("MENU_ADD")),
			telebot.types.BotCommand("/settings", get_text("MENU_SETTINGS")),
			telebot.types.BotCommand("/version", get_text("MENU_VERSION")),
			telebot.types.BotCommand("/help", get_text("MENU_HELP")),
			telebot.types.BotCommand("/donate", get_text("MENU_DONATE")),
			telebot.types.BotCommand("/donors", get_text("MENU_DONORS")),
		])
	except Exception as e:
		warning(f"Cannot set bot commands: {e}")
	track_polling_heartbeat()
	bot.infinity_polling(timeout=60)
