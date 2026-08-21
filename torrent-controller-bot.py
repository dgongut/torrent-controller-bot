import html
import json
import math
import re
import requests
import sys
import telebot
import threading
import time
import uuid
from collections import Counter
from config import *
from datetime import datetime
from telebot.types import ForceReply
from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup
from logger import debug, error, warning
from message_queue import MessageQueue
from name_parser import DEFAULT_MOVIE_TEMPLATE, DEFAULT_SERIES_TEMPLATE, DEFAULT_SEASON_PACK_TEMPLATE, SUBTITLE_EXTENSIONS, TemplateError, VALID_EXTENSIONS, companion_subtitle_name, suggest_file_name, suggest_name, validate_template
from torrent_clients import TorrentClientError, TorrentStatus, create_client
import bot_settings
import config as _config_module

VERSION = "1.2.4"

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
		for i, arg in enumerate(args, start=1):
			translated_text = translated_text.replace(f"${i}", str(arg))

	return translated_text


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


def parse_file_name(filename, parent_name, single_video):
	"""Same as parse_name but for a file inside a torrent, using the torrent
	name as context"""
	return suggest_file_name(
		filename,
		parent_name=parent_name,
		single_video=single_video,
		template_movie=bot_settings.get("template_movie") or None,
		template_series=bot_settings.get("template_series") or None,
		template_season=bot_settings.get("template_season") or None,
		season_prefix="T" if LANGUAGE.lower() == "es" else "S",
	)


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
	TELEGRAM_THREAD = int(TELEGRAM_THREAD)
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
	return "|".join([command] + [str(a) for a in args])


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


def send_message(chat_id, text, reply_markup=None, thread_id=None):
	kwargs = {"parse_mode": "HTML", "reply_markup": reply_markup, "disable_web_page_preview": True}
	if thread_id and thread_id != 1:
		kwargs["message_thread_id"] = thread_id
	return message_queue.enqueue_and_wait(bot.send_message, chat_id, text, **kwargs)


def edit_message(chat_id, message_id, text, reply_markup=None):
	try:
		return bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)
	except Exception as e:
		if "message is not modified" not in str(e):
			warning(f"Cannot edit message {message_id}: {e}")
		return None


def delete_message(chat_id, message_id):
	try:
		bot.delete_message(chat_id, message_id)
	except Exception:
		pass


def notify(text):
	"""Sends a bot-initiated notification to the configured destination:
	the group (and thread) if set, otherwise the first admin"""
	target = TELEGRAM_GROUP if TELEGRAM_GROUP else ADMIN_IDS[0]
	send_message(target, text, thread_id=TELEGRAM_THREAD)


# ---------------------------------------------------------------------------
# IN-MEMORY CONTEXTS
# ---------------------------------------------------------------------------

_contexts_lock = threading.Lock()

# Search contexts: short id -> {"query": str, "ts": float}
search_contexts = {}

# Tracker filter contexts: short id -> {"tracker": str, "ts": float}
tracker_contexts = {}

# Pending torrents waiting for a download dir: id -> {"magnet"/"data", "name", "ts"}
pending_torrents = {}

# Navigation contexts: short id -> {"torrent_id", "filter_key", "page", "ts"}
nav_contexts = {}

# Pagination contexts: short id -> {"base": callback prefix, "ts": float}
page_contexts = {}

# Pending text inputs: (chat_id, user_id) -> {"action": str, ...}
pending_inputs = {}

# Short ids for directories (stable during runtime)
_dir_by_id = {}
_dir_ids = {}

# Active dashboards: chat_id -> {"message_id": int, "generation": int}
dashboards = {}
_dashboards_lock = threading.Lock()


def get_dir_id(path):
	with _contexts_lock:
		if path not in _dir_ids:
			new_id = str(len(_dir_ids))
			_dir_ids[path] = new_id
			_dir_by_id[new_id] = path
		return _dir_ids[path]


def get_dir_by_id(dir_id):
	with _contexts_lock:
		return _dir_by_id.get(dir_id)


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
	pass


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
	lines.append("")
	if refreshing:
		lines.append(get_text("DASHBOARD_AUTOUPDATING"))
	else:
		lines.append(get_text("DASHBOARD_LAST_UPDATE", datetime.now().strftime("%H:%M:%S")))

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
	markup.add(InlineKeyboardButton(get_text("BUTTON_SETTINGS"), callback_data=build_call("settings")))
	bottom = []
	if not refreshing:
		bottom.append(InlineKeyboardButton(get_text("BUTTON_UPDATE"), callback_data=build_call("refreshDashboard")))
	bottom.append(InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")))
	markup.add(*bottom)

	return "\n".join(lines), markup


def _dashboard_refresher(chat_id, message_id, generation):
	iterations = max(1, DASHBOARD_REFRESH_DURATION // DASHBOARD_REFRESH_SECONDS)
	for _ in range(iterations):
		time.sleep(DASHBOARD_REFRESH_SECONDS)
		with _dashboards_lock:
			state = dashboards.get(chat_id)
			if not state or state["message_id"] != message_id or state["generation"] != generation:
				return
		try:
			text, markup = build_dashboard(refreshing=True)
			edit_message(chat_id, message_id, text, markup)
		except Exception as e:
			warning(f"Dashboard refresh failed: {e}")
	with _dashboards_lock:
		state = dashboards.get(chat_id)
		if not state or state["message_id"] != message_id or state["generation"] != generation:
			return
	try:
		text, markup = build_dashboard(refreshing=False)
		edit_message(chat_id, message_id, text, markup)
	except Exception as e:
		warning(f"Dashboard final refresh failed: {e}")


def show_dashboard(chat_id, message_id=None, thread_id=None):
	"""Sends (or edits) the dashboard and starts the auto-refresh cycle"""
	try:
		text, markup = build_dashboard(refreshing=True)
	except TorrentClientError as e:
		text = get_text("CONNECTION_ERROR", html.escape(str(e)))
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

	with _dashboards_lock:
		state = dashboards.get(chat_id, {"generation": 0})
		generation = state["generation"] + 1
		dashboards[chat_id] = {"message_id": message_id, "generation": generation}
	threading.Thread(target=_dashboard_refresher, args=(chat_id, message_id, generation), daemon=True).start()


def stop_dashboard(chat_id, message_id):
	with _dashboards_lock:
		state = dashboards.get(chat_id)
		if state and state["message_id"] == message_id:
			state["generation"] += 1


# ---------------------------------------------------------------------------
# TORRENT LIST
# ---------------------------------------------------------------------------

def build_list(filter_key, page):
	torrents = get_filtered_torrents(filter_key)
	total = len(torrents)
	pages = max(1, math.ceil(total / TORRENTS_PER_PAGE))
	page = max(0, min(int(page), pages - 1))
	label = get_filter_label(filter_key)

	text = get_text("LIST_TITLE", label, total, page + 1, pages)
	if total == 0:
		text += f"\n\n{get_text('LIST_EMPTY')}"

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

	if total > 0:
		markup.row(
			InlineKeyboardButton(get_text("BUTTON_MASS_RESUME"), callback_data=build_call("mass", "resume", filter_key)),
			InlineKeyboardButton(get_text("BUTTON_MASS_PAUSE"), callback_data=build_call("mass", "pause", filter_key)),
		)
		markup.row(
			InlineKeyboardButton(get_text("BUTTON_MASS_DELETE"), callback_data=build_call("mass", "delete", filter_key)),
			InlineKeyboardButton(get_text("BUTTON_MASS_MOVE"), callback_data=build_call("mass", "move", filter_key)),
		)

	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("dashboard")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return text, markup


def render_list(chat_id, message_id, filter_key, page, thread_id=None):
	try:
		text, markup = build_list(filter_key, page)
	except ExpiredContext:
		text = get_text("SEARCH_EXPIRED")
		markup = back_close_markup()
	except TorrentClientError as e:
		text = get_text("CONNECTION_ERROR", html.escape(str(e)))
		markup = back_close_markup()
	if message_id:
		edit_message(chat_id, message_id, text, markup)
	else:
		send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)


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

def build_detail(torrent_id, filter_key, page):
	torrent = client.get_torrent(torrent_id)
	if torrent is None:
		return get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page))

	emoji = STATUS_EMOJI.get(torrent.status, "")
	status_label = get_text(STATUS_TEXT_KEY.get(torrent.status, "STATUS_ALL"))
	lines = [f"<b>{html.escape(torrent.name)}</b>", ""]
	lines.append(f"{get_text('INFO_STATUS')}: {emoji} {status_label}")
	lines.append(f"{get_text('INFO_PROGRESS')}: {torrent.progress:.1f}%")
	lines.append(f"{get_text('INFO_SIZE')}: {sizeof_fmt(torrent.total_size)}")
	lines.append(f"{get_text('INFO_DOWNLOADED')}: {sizeof_fmt(torrent.downloaded)} (🔽 {sizeof_fmt(torrent.download_rate)}/s)")
	lines.append(f"{get_text('INFO_UPLOADED')}: {sizeof_fmt(torrent.uploaded)} (🔼 {sizeof_fmt(torrent.upload_rate)}/s)")
	lines.append(f"{get_text('INFO_RATIO')}: {torrent.ratio}")
	if torrent.status == TorrentStatus.DOWNLOADING:
		lines.append(f"{get_text('INFO_ETA')}: {format_eta(torrent.eta)}")
	lines.append(f"{get_text('INFO_PEERS')}: {torrent.peers} {get_text('INFO_PEERS_DETAIL', torrent.seeders, torrent.leechers)}")
	if torrent.trackers:
		tracker_text = html.escape(torrent.trackers[0])
		if len(torrent.trackers) > 1:
			tracker_text += f" {get_text('INFO_TRACKER_AND_MORE', len(torrent.trackers) - 1)}"
		lines.append(f"{get_text('INFO_TRACKER')}: {tracker_text}")
	lines.append(f"{get_text('INFO_DIR')}: <code>{html.escape(torrent.download_dir)}</code>")
	if torrent.added_date:
		added = datetime.fromtimestamp(torrent.added_date).strftime("%Y-%m-%d %H:%M")
		lines.append(f"{get_text('INFO_ADDED')}: {added}")
	if torrent.error_message:
		lines.append(f"{get_text('INFO_ERROR')}: <code>{html.escape(torrent.error_message)}</code>")
	show_files = has_browsable_files(torrent)
	if show_files:
		lines.append("")
		lines.append(f"<b>{get_text('INFO_FILES')} ({len(torrent.files)}):</b>")
		for path, size, _ in torrent.files[:10]:
			lines.append(f"• <code>{html.escape(path)}</code> ({sizeof_fmt(size)})")
		if len(torrent.files) > 10:
			lines.append(get_text("INFO_AND_MORE_FILES", len(torrent.files) - 10))

	markup = InlineKeyboardMarkup()
	if torrent.status == TorrentStatus.PAUSED:
		toggle = InlineKeyboardButton(get_text("BUTTON_RESUME"), callback_data=build_call("resume", torrent.id, filter_key, page))
	else:
		toggle = InlineKeyboardButton(get_text("BUTTON_PAUSE"), callback_data=build_call("pause", torrent.id, filter_key, page))
	markup.row(toggle, InlineKeyboardButton(get_text("BUTTON_VERIFY"), callback_data=build_call("verify", torrent.id, filter_key, page)))
	markup.row(
		InlineKeyboardButton(get_text(rename_key(torrent, "BUTTON_RENAME")), callback_data=build_call("rename", torrent.id, filter_key, page)),
		InlineKeyboardButton(get_text("BUTTON_MOVE"), callback_data=build_call("move", torrent.id, filter_key, page)),
	)
	if show_files:
		files_ctx = new_nav_context(torrent.id, filter_key, page)
		markup.row(InlineKeyboardButton(get_text("BUTTON_FILES"), callback_data=build_call("files", files_ctx, 0)))
	markup.row(InlineKeyboardButton(get_text("BUTTON_DELETE"), callback_data=build_call("delete", torrent.id, filter_key, page)))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("list", filter_key, page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def render_detail(chat_id, message_id, torrent_id, filter_key, page):
	try:
		text, markup = build_detail(torrent_id, filter_key, page)
	except TorrentClientError as e:
		text = get_text("CONNECTION_ERROR", html.escape(str(e)))
		markup = back_close_markup(build_call("list", filter_key, page))
	edit_message(chat_id, message_id, text, markup)


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


def suggest_for_file(torrent, path):
	"""Suggestion for one file of the torrent, using the torrent name as
	context. Returns None when nothing can be suggested"""
	if not is_video_file(path):
		return None
	single_video = len([f for f in torrent.files if is_video_file(f[0])]) == 1
	suggested = parse_file_name(file_basename(path), torrent.name, single_video)
	if not suggested or suggested == file_basename(path):
		return None
	return suggested


def subtitle_renames(torrent, video_path, new_name):
	"""Renames needed so the subtitles of video_path keep matching it"""
	renames = []
	folder = file_folder(video_path)
	video_file = file_basename(video_path)
	for path, _, _ in torrent.files:
		if path == video_path or file_folder(path) != folder or not is_subtitle_file(path):
			continue
		companion = companion_subtitle_name(file_basename(path), video_file, new_name)
		if companion and companion != file_basename(path):
			renames.append((path, companion))
	return renames


def build_rename_plan(torrent, only_path=None):
	"""List of (old_path, new_name) for the videos of the torrent (or just
	only_path) plus their subtitles. Also returns the names that collide"""
	plan = []
	taken = {file_basename(p) for p, _, _ in torrent.files}
	collisions = []
	for path, _, _ in torrent.files:
		if only_path and path != only_path:
			continue
		suggested = suggest_for_file(torrent, path)
		if not suggested:
			continue
		taken.discard(file_basename(path))
		if suggested in taken:
			collisions.append(suggested)
			taken.add(file_basename(path))
			continue
		taken.add(suggested)
		plan.append((path, suggested))
		plan.extend(subtitle_renames(torrent, path, suggested))
	return plan, collisions


def file_rename_ok_text(new_name, done):
	"""Result of renaming one file: done counts the file itself plus the
	subtitles renamed along with it"""
	subtitles = max(done - 1, 0)
	if subtitles:
		return get_text("FILE_RENAME_OK_SUBTITLES", html.escape(new_name), subtitles)
	return get_text("FILE_RENAME_OK", html.escape(new_name))


def apply_rename_plan(torrent_id, plan):
	"""Applies the renames one by one. Returns (done, errors)"""
	done = 0
	errors = []
	for old_path, new_name in plan:
		try:
			client.rename_file(torrent_id, old_path, new_name)
			done += 1
		except TorrentClientError as e:
			errors.append(str(e))
	return done, errors


def build_files(ctx_id, torrent_id, filter_key, page, file_page):
	torrent = client.get_torrent(torrent_id)
	if torrent is None:
		return get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page))

	files = torrent.files
	total = len(files)
	pages = max(1, math.ceil(total / FILES_PER_PAGE))
	file_page = max(0, min(int(file_page), pages - 1))
	text = get_text("FILES_TITLE", html.escape(torrent.name), total, file_page + 1, pages)

	markup = InlineKeyboardMarkup(row_width=1)
	start = file_page * FILES_PER_PAGE
	for index in range(start, min(start + FILES_PER_PAGE, total)):
		path = files[index][0]
		icon = "🎬" if is_video_file(path) else ("💬" if is_subtitle_file(path) else "📄")
		markup.add(InlineKeyboardButton(
			f"{icon} {truncate(file_basename(path))}",
			callback_data=build_call("file", ctx_id, file_page, index)))

	if pages > 1:
		markup.row(*pagination_row(file_page, pages, "files", ctx_id))

	plan, _ = build_rename_plan(torrent)
	if plan:
		markup.row(InlineKeyboardButton(get_text("BUTTON_FILES_RENAME_ALL"), callback_data=build_call("filesAll", ctx_id, file_page)))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("info", torrent_id, filter_key, page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return text, markup


def render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, file_page):
	try:
		text, markup = build_files(ctx_id, torrent_id, filter_key, page, file_page)
	except TorrentClientError as e:
		text = get_text("CONNECTION_ERROR", html.escape(str(e)))
		markup = back_close_markup(build_call("info", torrent_id, filter_key, page))
	edit_message(chat_id, message_id, text, markup)


def get_torrent_file(torrent, file_index):
	"""Returns (path, size) for the file index or None when out of range"""
	try:
		path, size, _ = torrent.files[int(file_index)]
	except (IndexError, ValueError):
		return None
	return path, size


def build_file_detail(torrent, path, size, ctx_id, file_page, file_index):
	lines = [f"<b>{html.escape(file_basename(path))}</b>", ""]
	folder = file_folder(path)
	if folder:
		lines.append(f"{get_text('INFO_DIR')}: <code>{html.escape(folder)}</code>")
	lines.append(f"{get_text('INFO_SIZE')}: {sizeof_fmt(size)}")

	markup = InlineKeyboardMarkup(row_width=1)
	suggested = suggest_for_file(torrent, path)
	if suggested:
		lines.append("")
		lines.append(get_text("FILE_SUGGEST", html.escape(suggested)))
		subtitles = subtitle_renames(torrent, path, suggested)
		if subtitles:
			lines.append(get_text("FILE_SUBTITLES", len(subtitles)))
		markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_AUTO"), callback_data=build_call("fileAuto", ctx_id, file_page, file_index)))
	elif is_video_file(path):
		lines.append("")
		lines.append(get_text("FILE_NO_SUGGESTION"))
	markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_MANUAL"), callback_data=build_call("fileManual", ctx_id, file_page, file_index)))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("files", ctx_id, file_page)),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def build_plan_preview(plan):
	shown = plan[:MAX_PLAN_PREVIEW_LINES]
	lines = [f"• <code>{html.escape(file_basename(old))}</code> → <code>{html.escape(new)}</code>" for old, new in shown]
	if len(plan) > len(shown):
		lines.append(get_text("INFO_AND_MORE_FILES", len(plan) - len(shown)))
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


def build_dir_markup(dir_call, write_call, cancel_call, page_parts=None, page=0):
	"""Keyboard with one button per known dir (paginated) + write path + cancel.
	dir_call receives the dir_id and must return the callback_data.
	page_parts is the callback prefix that receives the page as last arg"""
	markup = InlineKeyboardMarkup(row_width=1)
	dirs = get_known_dirs()
	pages = max(1, math.ceil(len(dirs) / MAX_DIR_BUTTONS))
	page = max(0, min(int(page), pages - 1))
	start = page * MAX_DIR_BUTTONS
	for directory in dirs[start:start + MAX_DIR_BUTTONS]:
		markup.add(InlineKeyboardButton(f"📂 {truncate_dir(directory)}", callback_data=dir_call(get_dir_id(directory))))
	if pages > 1 and page_parts:
		markup.row(*pagination_row(page, pages, *page_parts))
	markup.add(InlineKeyboardButton(get_text("BUTTON_WRITE_DIR"), callback_data=write_call))
	markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=cancel_call))
	return markup


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

def build_settings():
	settings = client.get_settings()

	def limit_text(value, enabled):
		return f"{value} KB/s" if enabled else get_text("NO_LIMIT")

	lines = [get_text("SETTINGS_TITLE", settings["version"]), ""]
	if client.supports_alt_speed:
		alt_state = get_text("ENABLED") if settings["alt_speed_enabled"] else get_text("DISABLED")
		lines.append(get_text("SETTINGS_ALT_SPEED", alt_state, settings["alt_speed_down"], settings["alt_speed_up"]))
	lines.append(get_text("SETTINGS_DOWN_LIMIT", limit_text(settings["speed_limit_down"], settings["speed_limit_down_enabled"])))
	lines.append(get_text("SETTINGS_UP_LIMIT", limit_text(settings["speed_limit_up"], settings["speed_limit_up_enabled"])))
	lines.append(get_text("SETTINGS_DEFAULT_DIR", html.escape(settings["download_dir"])))
	lines.append("")
	lines.append(get_text("SETTINGS_BOT_TITLE"))
	auto_dir = bot_settings.get("auto_download_dir")
	auto_dir_label = f"<code>{html.escape(auto_dir)}</code>" if auto_dir else get_text("AUTO_DIR_CLIENT_DEFAULT")
	lines.append(get_text("SETTINGS_AUTO_DIR", auto_dir_label))

	def toggle_button(setting_key, text_key, prefix=""):
		state = "✅" if bot_settings.get(setting_key) else "❌"
		return InlineKeyboardButton(f"{prefix}{state} {get_text(text_key)}", callback_data=build_call("toggleSetting", setting_key))

	markup = InlineKeyboardMarkup(row_width=1)
	if client.supports_alt_speed:
		markup.add(InlineKeyboardButton(get_text("BUTTON_TOGGLE_ALT_SPEED"), callback_data=build_call("toggleAltSpeed")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TOGGLE_DOWN_LIMIT"), callback_data=build_call("toggleDownLimit")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_TOGGLE_UP_LIMIT"), callback_data=build_call("toggleUpLimit")))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_SET_DOWN_LIMIT"), callback_data=build_call("setDownLimit")),
		InlineKeyboardButton(get_text("BUTTON_SET_UP_LIMIT"), callback_data=build_call("setUpLimit")),
	)
	markup.add(toggle_button("notify_completed", "BUTTON_SETTING_NOTIFY_COMPLETED"))
	markup.add(toggle_button("notify_errors", "BUTTON_SETTING_NOTIFY_ERRORS"))
	markup.add(toggle_button("auto_download", "BUTTON_SETTING_AUTO_DOWNLOAD"))
	markup.add(toggle_button("auto_rename", "BUTTON_SETTING_AUTO_RENAME"))
	if bot_settings.get("auto_rename"):
		markup.add(toggle_button("auto_rename_files", "BUTTON_SETTING_AUTO_RENAME_FILES", prefix="↳ "))
	markup.add(toggle_button("low_space_warning", "BUTTON_SETTING_LOW_SPACE"))
	markup.add(InlineKeyboardButton(get_text("BUTTON_SETTING_AUTO_DIR"), callback_data=build_call("autoDirMenu", 0)))
	markup.add(InlineKeyboardButton(get_text("BUTTON_SETTING_FAV_DIRS"), callback_data=build_call("favDirsMenu")))
	markup.add(InlineKeyboardButton(get_text("BUTTON_SETTING_TEMPLATES"), callback_data=build_call("tplMenu")))
	markup.row(
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("dashboard")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	return "\n".join(lines), markup


def render_settings(chat_id, message_id):
	try:
		text, markup = build_settings()
	except TorrentClientError as e:
		text = get_text("CONNECTION_ERROR", html.escape(str(e)))
		markup = back_close_markup()
	edit_message(chat_id, message_id, text, markup)


def send_settings_menu(chat_id, thread_id=None, prefix=None):
	"""Sends the settings menu as a new message, optionally preceded by a confirmation"""
	try:
		text, markup = build_settings()
	except TorrentClientError as e:
		text = get_text("CONNECTION_ERROR", html.escape(str(e)))
		markup = back_close_markup()
	if prefix:
		text = f"{prefix}\n\n{text}"
	send_message(chat_id, text, reply_markup=markup, thread_id=thread_id)


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
		InlineKeyboardButton(get_text("BUTTON_BACK"), callback_data=build_call("settings")),
		InlineKeyboardButton(get_text("BUTTON_CLOSE"), callback_data=build_call("cerrar")),
	)
	edit_message(chat_id, message_id, "\n".join(lines), markup)


# ---------------------------------------------------------------------------
# ADD TORRENT
# ---------------------------------------------------------------------------

def ask_download_dir(chat_id, pending_id, name, thread_id=None, message_id=None, dir_page=0):
	markup = build_dir_markup(
		dir_call=lambda dir_id: build_call("addTo", pending_id, dir_id),
		write_call=build_call("addNewDir", pending_id),
		cancel_call=build_call("cancelAdd", pending_id),
		page_parts=("addDirPage", pending_id),
		page=dir_page,
	)
	text = get_text("ADD_ASK_DIR", html.escape(name))
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


def auto_rename_torrent(torrent):
	"""Renames the torrent to its suggested name. Returns the new name, or None
	when there is no suggestion or the rename is skipped"""
	suggested = parse_name(torrent.name)
	if not suggested or suggested == torrent.name:
		return None
	if name_already_exists(suggested, exclude_id=torrent.id):
		warning(f"Auto-rename skipped for {torrent.name}: a torrent named '{suggested}' already exists")
		return None
	client.rename_torrent(torrent.id, suggested)
	return suggested


def auto_rename_torrent_files(torrent_id):
	"""Renames the files inside a folder torrent to their suggested names.
	Returns the preview of what was renamed, or None when nothing was done"""
	torrent = client.get_torrent(torrent_id)  # Reread: renaming the torrent changed the file paths
	if torrent is None or not torrent_is_folder(torrent):
		return None
	plan, collisions = build_rename_plan(torrent)
	if collisions:
		warning(f"Auto-rename skipped {len(collisions)} file(s) of {torrent.name}: the suggested name is already in use")
	if not plan:
		return None
	done, errors = apply_rename_plan(torrent_id, plan)
	for message in errors:
		warning(f"Auto-rename failed for a file of {torrent.name}: {message}")
	if not done:
		return None
	return get_text("ADD_AUTO_RENAMED_FILES", done, build_plan_preview(plan))


def deferred_auto_rename(torrent_id, original_name):
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
			renamed = auto_rename_torrent(torrent)
			files_renamed = auto_rename_torrent_files(torrent_id) if bot_settings.get("auto_rename_files") else None
		except TorrentClientError as e:
			warning(f"Auto-rename failed for {torrent.name}: {e}")
			return
		if renamed:
			notify(get_text("NOTIFY_AUTO_RENAMED", html.escape(original_name), html.escape(renamed)))
		if files_renamed:
			notify(f"{get_text('NOTIFY_AUTO_RENAMED_FILES', html.escape(renamed or original_name))}\n{files_renamed}")
		return
	warning(f"Auto-rename gave up for {original_name}: the metadata never arrived")


def perform_add_torrent(pending, download_dir):
	"""Adds the torrent and returns the result text
	(add + optional auto-rename + optional low space warning)"""
	torrent = client.add_torrent(magnet=pending["magnet"], torrent_data=pending["data"], download_dir=download_dir)
	lines = [get_text("ADD_OK", html.escape(torrent.name), html.escape(download_dir))]
	if bot_settings.get("auto_rename"):
		if torrent.files:
			try:
				renamed = auto_rename_torrent(torrent)
				if renamed:
					lines.append(get_text("ADD_AUTO_RENAMED", html.escape(renamed)))
				if bot_settings.get("auto_rename_files"):
					files_renamed = auto_rename_torrent_files(torrent.id)
					if files_renamed:
						lines.append(files_renamed)
			except TorrentClientError as e:
				warning(f"Auto-rename failed for {torrent.name}: {e}")
		else:
			lines.append(get_text("ADD_AUTO_RENAME_PENDING"))
			threading.Thread(target=deferred_auto_rename, args=(torrent.id, torrent.name), daemon=True).start()
	if bot_settings.get("low_space_warning"):
		space_warning = build_low_space_warning(torrent.total_size, download_dir)
		if space_warning:
			lines.append(space_warning)
	return "\n".join(lines)


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


def do_add_torrent(chat_id, message_id, pending_id, download_dir):
	pending = pop_pending_torrent(pending_id)
	if pending is None:
		edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
		return
	try:
		text = perform_add_torrent(pending, download_dir)
	except TorrentClientError as e:
		text = get_text("ADD_ERROR", html.escape(str(e)))
	edit_message(chat_id, message_id, text)


def start_add_flow(chat_id, name, magnet=None, data=None, thread_id=None):
	"""Entry point when receiving a torrent: asks for the download dir, or adds it
	directly to the automatic directory when auto download is enabled"""
	if bot_settings.get("auto_download"):
		download_dir = bot_settings.get("auto_download_dir")
		try:
			if not download_dir:
				download_dir = client.get_default_download_dir()
			text = perform_add_torrent({"name": name, "magnet": magnet, "data": data}, download_dir)
		except TorrentClientError as e:
			text = get_text("ADD_ERROR", html.escape(str(e)))
		send_message(chat_id, text, thread_id=thread_id)
		return
	pending_id = new_pending_torrent(name, magnet=magnet, data=data)
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


def download_torrent_from_url(url):
	"""Downloads a .torrent from a URL with size/time limits and validates
	the content is bencoded torrent metadata. Returns (data, name) or
	raises ValueError if the content is not a torrent"""
	data = b""
	response = requests.get(url, stream=True, timeout=URL_DOWNLOAD_TIMEOUT, headers={"User-Agent": f"torrent-controller-bot/{VERSION}"})
	try:
		response.raise_for_status()
		length = response.headers.get("Content-Length")
		if length and int(length) > URL_DOWNLOAD_MAX_BYTES:
			raise ValueError("too big")
		for chunk in response.iter_content(chunk_size=64 * 1024):
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
	name = "torrent"
	match = re.search(rb"4:name(\d+):", data)
	if match:
		start = match.end()
		name = data[start:start + int(match.group(1))].decode("utf-8", errors="replace")
	return data, name


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
		edit_message(chat_id, message_id, get_text("CONNECTION_ERROR", html.escape(str(e))), back_close_markup())
		return

	count = len(torrents)
	back_call = build_call("list", filter_key, 0)
	if count == 0:
		edit_message(chat_id, message_id, get_text("LIST_EMPTY"), back_close_markup(back_call))
		return

	markup = InlineKeyboardMarkup(row_width=1)
	if action == "resume":
		text = get_text("MASS_CONFIRM_RESUME", count)
		markup.add(InlineKeyboardButton(get_text("BUTTON_CONFIRM"), callback_data=build_call("confirmMass", "resume", filter_key, "-")))
	elif action == "pause":
		text = get_text("MASS_CONFIRM_PAUSE", count)
		markup.add(InlineKeyboardButton(get_text("BUTTON_CONFIRM"), callback_data=build_call("confirmMass", "pause", filter_key, "-")))
	elif action == "delete":
		text = get_text("MASS_CONFIRM_DELETE", count)
		markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_KEEP_DATA"), callback_data=build_call("confirmMass", "delete", filter_key, "0")))
		markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_WITH_DATA"), callback_data=build_call("confirmMass", "delete", filter_key, "1")))
	elif action == "move":
		text = get_text("MASS_MOVE_ASK_DIR", count)
		markup = build_dir_markup(
			dir_call=lambda dir_id: build_call("massMoveDir", filter_key, dir_id),
			write_call=build_call("massMoveNew", filter_key),
			cancel_call=back_call,
			page_parts=("mass", "move", filter_key),
			page=dir_page,
		)
	else:
		return
	if action != "move":
		markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=back_call))
	edit_message(chat_id, message_id, text, markup)


def do_mass_action(chat_id, message_id, action, filter_key, extra=None):
	try:
		torrents = get_filtered_torrents(filter_key)
	except ExpiredContext:
		edit_message(chat_id, message_id, get_text("MASS_EXPIRED"), back_close_markup())
		return
	except TorrentClientError as e:
		edit_message(chat_id, message_id, get_text("CONNECTION_ERROR", html.escape(str(e))), back_close_markup())
		return

	ids = [t.id for t in torrents]
	count = len(ids)
	back_call = build_call("list", filter_key, 0)
	try:
		if action == "resume":
			client.resume_torrents(ids)
			text = get_text("MASS_DONE_RESUME", count)
		elif action == "pause":
			client.pause_torrents(ids)
			text = get_text("MASS_DONE_PAUSE", count)
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
		text = get_text("ERROR_GENERIC", html.escape(str(e)))
	edit_message(chat_id, message_id, text, back_close_markup(back_call))


# ---------------------------------------------------------------------------
# PENDING TEXT INPUTS
# ---------------------------------------------------------------------------

def set_pending_input(chat_id, user_id, action, **extra):
	with _contexts_lock:
		pending_inputs[(chat_id, user_id)] = {"action": action, **extra}


def pop_pending_input(chat_id, user_id):
	with _contexts_lock:
		return pending_inputs.pop((chat_id, user_id), None)


def handle_pending_input(message, pending):
	chat_id = message.chat.id
	thread_id = message.message_thread_id
	text = message.text.strip()
	action = pending["action"]

	prompt_message_id = pending.get("prompt_message_id")
	if prompt_message_id:
		delete_message(chat_id, prompt_message_id)
	delete_message(chat_id, message.message_id)

	# The paginated message is still on screen, so a "back" button here would duplicate it
	back_markup = close_markup() if action == "gotoPage" else back_close_markup(pending.get("back_call"))

	if not text or text.lower() in ("/cancel", "cancel", "cancelar"):
		send_message(chat_id, get_text("INPUT_CANCELLED"), reply_markup=back_markup, thread_id=thread_id)
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
		dispatch_callback(chat_id, pending["target_message_id"], message.from_user.id, f"{pending['base']}|{number - 1}")
	elif action == "rename":
		if name_already_exists(text, exclude_id=pending["torrent_id"]):
			send_message(chat_id, get_text("RENAME_DUPLICATE", html.escape(text)), reply_markup=back_markup, thread_id=thread_id)
			return
		try:
			torrent = client.get_torrent(pending["torrent_id"])
			client.rename_torrent(pending["torrent_id"], text)
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
			if errors:
				send_message(chat_id, get_text("FILES_RENAME_PARTIAL", done, len(errors), html.escape(errors[0])), reply_markup=back_markup, thread_id=thread_id)
			else:
				send_message(chat_id, file_rename_ok_text(text, done), reply_markup=back_markup, thread_id=thread_id)
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
		try:
			result = perform_add_torrent(pending_torrent, text)
		except TorrentClientError as e:
			result = get_text("ADD_ERROR", html.escape(str(e)))
		send_message(chat_id, result, reply_markup=back_markup, thread_id=thread_id)
	elif action == "autoDir":
		bot_settings.set("auto_download_dir", text.rstrip("/") or "/")
		send_settings_menu(chat_id, thread_id=thread_id, prefix=get_text("SETTINGS_UPDATED"))
	elif action == "favDir":
		directory = text.rstrip("/") or "/"
		favorites = bot_settings.get("favorite_dirs") or []
		if directory not in favorites:
			favorites.append(directory)
			bot_settings.set("favorite_dirs", favorites)
		fav_text, fav_markup = build_favorite_dirs_menu()
		send_message(chat_id, f"{get_text('SETTINGS_UPDATED')}\n\n{fav_text}", reply_markup=fav_markup, thread_id=thread_id)
	elif action == "template":
		kind = pending["kind"]
		try:
			validate_template(text)
		except TemplateError as e:
			if e.code == "unknown_field":
				error_text = get_text("TPL_ERROR_UNKNOWN_FIELD", html.escape(e.detail))
			else:
				error_text = get_text("TPL_ERROR_INVALID")
			send_message(chat_id, f"{error_text}\n\n{get_text('TPL_FIELDS_HELP')}", reply_markup=back_markup, thread_id=thread_id)
			return
		bot_settings.set(f"template_{kind}", text)
		example = {"movie": TEMPLATE_EXAMPLE_MOVIE, "series": TEMPLATE_EXAMPLE_SERIES, "season": TEMPLATE_EXAMPLE_SEASON}[kind]
		preview = parse_name(example) or "-"
		send_message(chat_id, get_text("TPL_SAVED", html.escape(text), html.escape(example), html.escape(preview)), reply_markup=back_markup, thread_id=thread_id)
	elif action == "massMove":
		do_mass_move_to(chat_id, pending["filter_key"], text, thread_id=thread_id)
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
				client.set_speed_limit(direction, None, enabled=False)
			else:
				client.set_speed_limit(direction, kbps, enabled=True)
			send_settings_menu(chat_id, thread_id=thread_id, prefix=get_text("SETTINGS_UPDATED"))
		except TorrentClientError as e:
			send_message(chat_id, get_text("ERROR_GENERIC", html.escape(str(e))), reply_markup=back_markup, thread_id=thread_id)


def deliver_move_order(chat_id, ids, new_dir, progress_text, success_text, message_id=None, thread_id=None, reply_markup=None):
	"""Show a progress message and deliver the move order in a background thread
	(the RPC blocks until the physical move is finished). The message is updated
	with the final result. If the daemon does not answer (e.g. busy relocating
	large amounts of data), keep retrying until it is delivered."""
	if message_id is not None:
		edit_message(chat_id, message_id, progress_text)
	else:
		msg = send_message(chat_id, progress_text, thread_id=thread_id)
		message_id = msg.message_id if msg else None

	def finish(text):
		if message_id is not None:
			edit_message(chat_id, message_id, text, reply_markup)
		else:
			send_message(chat_id, text, reply_markup=reply_markup, thread_id=thread_id)

	def worker():
		try:
			client.move_torrents(ids, new_dir)
			finish(success_text)
			return
		except TorrentClientError as e:
			err = str(e)
		except Exception as e:
			error(f"Unexpected error delivering move order: {e}")
			finish(get_text("MOVE_GIVE_UP", html.escape(str(e))))
			return
		warning(f"Move order not delivered, will retry in background: {err}")
		if message_id is not None:
			edit_message(chat_id, message_id, get_text("MOVE_RETRYING"))
		for _ in range(MOVE_RETRY_ATTEMPTS):
			time.sleep(MOVE_RETRY_DELAY)
			try:
				client.move_torrents(ids, new_dir)
				debug(f"Move order delivered after retrying: {len(ids)} torrents -> {new_dir}")
				finish(success_text)
				return
			except TorrentClientError as e:
				err = str(e)
		error(f"Move order gave up after {MOVE_RETRY_ATTEMPTS} attempts: {err}")
		finish(get_text("MOVE_GIVE_UP", html.escape(err)))

	threading.Thread(target=worker, daemon=True).start()


def do_mass_move_to(chat_id, filter_key, new_dir, thread_id=None, message_id=None):
	back_markup = back_close_markup(build_call("list", filter_key, 0))
	try:
		torrents = get_filtered_torrents(filter_key)
	except (ExpiredContext, TorrentClientError) as e:
		text = get_text("MASS_EXPIRED") if isinstance(e, ExpiredContext) else get_text("CONNECTION_ERROR", html.escape(str(e)))
		if message_id:
			edit_message(chat_id, message_id, text, back_markup)
		else:
			send_message(chat_id, text, reply_markup=back_markup, thread_id=thread_id)
		return
	ids = [t.id for t in torrents]
	progress_text = get_text("MASS_MOVING", len(ids), html.escape(new_dir))
	success_text = get_text("MASS_DONE_MOVE", len(ids), html.escape(new_dir))
	deliver_move_order(chat_id, ids, new_dir, progress_text, success_text, message_id=message_id, thread_id=thread_id, reply_markup=back_markup)


# ---------------------------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------------------------

def check_auth(message):
	if not is_authorized(message.from_user.id, message.chat.id):
		warning(f"Unauthorized access attempt: user {message.from_user.id} in chat {message.chat.id}")
		send_message(message.chat.id, get_text("USER_NOT_ALLOWED", message.from_user.id), thread_id=message.message_thread_id)
		return False
	return True


@bot.message_handler(commands=["start"])
def command_start(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	show_dashboard(message.chat.id, thread_id=message.message_thread_id)


@bot.message_handler(commands=["help"])
def command_help(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	send_message(message.chat.id, get_text("START_MESSAGE"), thread_id=message.message_thread_id)


@bot.message_handler(commands=["list"])
def command_list(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	render_list(message.chat.id, None, FILTER_ALL, 0, thread_id=message.message_thread_id)


@bot.message_handler(commands=["find"])
def command_find(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	parts = message.text.split(maxsplit=1)
	if len(parts) > 1 and parts[1].strip():
		ctx_id = new_search_context(parts[1].strip())
		render_list(message.chat.id, None, f"q{ctx_id}", 0, thread_id=message.message_thread_id)
	else:
		ask_for_input(message.chat.id, message.from_user.id, "search", get_text("SEARCH_ASK"), thread_id=message.message_thread_id)


@bot.message_handler(commands=["add"])
def command_add(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	send_message(message.chat.id, get_text("ADD_USAGE"), thread_id=message.message_thread_id)


@bot.message_handler(commands=["settings"])
def command_settings(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	try:
		text, markup = build_settings()
	except TorrentClientError as e:
		text = get_text("CONNECTION_ERROR", html.escape(str(e)))
		markup = back_close_markup()
	send_message(message.chat.id, text, reply_markup=markup, thread_id=message.message_thread_id)


@bot.message_handler(commands=["version"])
def command_version(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	try:
		connected_to = client.test_connection()
	except TorrentClientError as e:
		connected_to = f"❌ {e}"
	send_message(message.chat.id, get_text("VERSION_TEXT", VERSION, connected_to), thread_id=message.message_thread_id)


@bot.message_handler(commands=["donate"])
def command_donate(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	send_message(message.chat.id, get_text("DONATE"), thread_id=message.message_thread_id)


@bot.message_handler(commands=["donors"])
def command_donors(message):
	if not check_auth(message):
		return
	delete_message(message.chat.id, message.message_id)
	donors = get_donors_online()
	if donors:
		text = get_text("DONORS_LIST", "\n".join(f"· {html.escape(d)}" for d in donors))
	else:
		text = get_text("ERROR_GETTING_DONORS")
	send_message(message.chat.id, text, thread_id=message.message_thread_id)


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
	set_pending_input(chat_id, user_id, action, prompt_message_id=prompt_message_id, **extra)


@bot.message_handler(content_types=["document"])
def handle_document(message):
	if not check_auth(message):
		return
	document = message.document
	if not document.file_name or not document.file_name.lower().endswith(".torrent"):
		send_message(message.chat.id, get_text("ADD_INVALID_FILE"), thread_id=message.message_thread_id)
		return
	if document.file_size and document.file_size > 20 * 1024 * 1024:
		send_message(message.chat.id, get_text("ADD_INVALID_FILE"), thread_id=message.message_thread_id)
		return
	try:
		file_info = bot.get_file(document.file_id)
		data = bot.download_file(file_info.file_path)
	except Exception as e:
		send_message(message.chat.id, get_text("ADD_ERROR", html.escape(str(e))), thread_id=message.message_thread_id)
		return
	name = document.file_name[:-len(".torrent")]
	start_add_flow(message.chat.id, name, data=data, thread_id=message.message_thread_id)


@bot.message_handler(func=lambda message: True)
def handle_text(message):
	if not message.text:
		return
	if not is_authorized(message.from_user.id, message.chat.id):
		return

	pending = pop_pending_input(message.chat.id, message.from_user.id)
	if pending:
		handle_pending_input(message, pending)
		return

	text = message.text.strip()
	if text.lower().startswith("magnet:"):
		name = extract_magnet_name(text)
		start_add_flow(message.chat.id, name, magnet=text, thread_id=message.message_thread_id)
	elif text.lower().startswith(("http://", "https://")) and " " not in text:
		# In groups, ignore invalid links silently (the bot may live with other
		# bots and people pasting links); only reply in private chats
		is_private = message.chat.type == "private"
		try:
			data, name = download_torrent_from_url(text)
		except ValueError:
			debug(f"URL is not a torrent, ignored: {text}")
			if is_private:
				send_message(message.chat.id, get_text("ADD_URL_NOT_TORRENT"), thread_id=message.message_thread_id)
			return
		except Exception as e:
			debug(f"Cannot download URL {text}: {e}")
			if is_private:
				send_message(message.chat.id, get_text("ADD_URL_ERROR", html.escape(str(e))), thread_id=message.message_thread_id)
			return
		start_add_flow(message.chat.id, name, data=data, thread_id=message.message_thread_id)


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
	try:
		bot.answer_callback_query(call.id, tooltip)
	except Exception:
		pass

	stop_dashboard(chat_id, message_id)
	dispatch_callback(chat_id, message_id, call.from_user.id, call.data)


def dispatch_callback(chat_id, message_id, user_id, data):
	"""Runs the action encoded in a callback_data string, editing message_id"""
	parts = data.split("|")
	command = parts[0]
	args = parts[1:]

	try:
		if command == "noop":
			pass

		elif command == "cerrar":
			delete_message(chat_id, message_id)

		elif command == "dashboard" or command == "refreshDashboard":
			show_dashboard(chat_id, message_id=message_id)

		elif command == "list":
			filter_key, page = args[0], args[1]
			render_list(chat_id, message_id, filter_key, page)

		elif command == "info":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			render_detail(chat_id, message_id, torrent_id, filter_key, page)

		elif command in ("pause", "resume", "verify"):
			torrent_id, filter_key, page = args[0], args[1], args[2]
			if command == "pause":
				client.pause_torrents([torrent_id])
			elif command == "resume":
				client.resume_torrents([torrent_id])
			else:
				client.verify_torrent(torrent_id)
			time.sleep(0.5)
			render_detail(chat_id, message_id, torrent_id, filter_key, page)

		elif command == "delete":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				markup = InlineKeyboardMarkup(row_width=1)
				markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_KEEP_DATA"), callback_data=build_call("confirmDelete", torrent_id, "0", filter_key, page)))
				markup.add(InlineKeyboardButton(get_text("BUTTON_DELETE_WITH_DATA"), callback_data=build_call("confirmDelete", torrent_id, "1", filter_key, page)))
				markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=build_call("info", torrent_id, filter_key, page)))
				edit_message(chat_id, message_id, get_text("DELETE_CONFIRM", html.escape(torrent.name)), markup)

		elif command == "confirmDelete":
			torrent_id, with_data, filter_key, page = args[0], args[1], args[2], args[3]
			torrent = client.get_torrent(torrent_id)
			name = torrent.name if torrent else torrent_id
			client.remove_torrents([torrent_id], delete_data=with_data == "1")
			edit_message(chat_id, message_id, get_text("DELETED", html.escape(name)), back_close_markup(build_call("list", filter_key, page)))

		elif command == "rename":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				suggested = parse_name(torrent.name)
				if suggested and suggested != torrent.name:
					markup = InlineKeyboardMarkup(row_width=1)
					markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_AUTO"), callback_data=build_call("renameAuto", torrent_id, filter_key, page)))
					markup.add(InlineKeyboardButton(get_text("BUTTON_RENAME_MANUAL"), callback_data=build_call("renameManual", torrent_id, filter_key, page)))
					markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=build_call("info", torrent_id, filter_key, page)))
					edit_message(chat_id, message_id, get_text(rename_key(torrent, "RENAME_SUGGEST"), html.escape(torrent.name), html.escape(suggested)), markup)
				else:
					ask_for_input(chat_id, user_id, "rename", get_text(rename_key(torrent, "RENAME_ASK"), html.escape(torrent.name)),
								message_id=message_id, torrent_id=torrent_id, back_call=build_call("info", torrent_id, filter_key, page))

		elif command == "renameAuto":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				suggested = parse_name(torrent.name)
				if not suggested or suggested == torrent.name:
					edit_message(chat_id, message_id, get_text("RENAME_NO_SUGGESTION"), back_close_markup(build_call("info", torrent_id, filter_key, page)))
				elif name_already_exists(suggested, exclude_id=torrent_id):
					edit_message(chat_id, message_id, get_text("RENAME_DUPLICATE", html.escape(suggested)), back_close_markup(build_call("info", torrent_id, filter_key, page)))
				else:
					client.rename_torrent(torrent_id, suggested)
					edit_message(chat_id, message_id, get_text(rename_key(torrent, "RENAME_OK"), html.escape(suggested)), back_close_markup(build_call("info", torrent_id, filter_key, page)))

		elif command == "renameManual":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				ask_for_input(chat_id, user_id, "rename", get_text(rename_key(torrent, "RENAME_ASK"), html.escape(torrent.name)),
							message_id=message_id, torrent_id=torrent_id, back_call=build_call("info", torrent_id, filter_key, page))

		elif command in ("files", "file", "fileAuto", "fileManual", "filesAll", "filesAllOk"):
			ctx = get_nav_context(args[0])
			if ctx is None:
				edit_message(chat_id, message_id, get_text("FILES_CONTEXT_EXPIRED"), back_close_markup(build_call("dashboard")))
				return
			torrent_id, filter_key, page = ctx
			ctx_id = args[0]
			file_page = args[1]
			back_call = build_call("files", ctx_id, file_page)

			if command == "files":
				render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, file_page)

			elif command in ("file", "fileAuto", "fileManual"):
				file_index = args[2]
				torrent = client.get_torrent(torrent_id)
				entry = get_torrent_file(torrent, file_index) if torrent else None
				if entry is None:
					render_files(chat_id, message_id, ctx_id, torrent_id, filter_key, page, file_page)
					return
				file_back_call = build_call("file", ctx_id, file_page, file_index)
				if command == "file":
					text, markup = build_file_detail(torrent, entry[0], entry[1], ctx_id, file_page, file_index)
					edit_message(chat_id, message_id, text, markup)
				elif command == "fileManual":
					ask_for_input(chat_id, user_id, "renameFile", get_text("FILE_RENAME_ASK", html.escape(file_basename(entry[0]))),
								message_id=message_id, torrent_id=torrent_id, file_path=entry[0], back_call=file_back_call)
				else:
					plan, collisions = build_rename_plan(torrent, only_path=entry[0])
					if collisions:
						edit_message(chat_id, message_id, get_text("FILE_RENAME_DUPLICATE", html.escape(collisions[0])), back_close_markup(file_back_call))
					elif not plan:
						edit_message(chat_id, message_id, get_text("FILE_NO_SUGGESTION"), back_close_markup(file_back_call))
					else:
						done, errors = apply_rename_plan(torrent_id, plan)
						if errors:
							edit_message(chat_id, message_id, get_text("FILES_RENAME_PARTIAL", done, len(errors), html.escape(errors[0])), back_close_markup(file_back_call))
						else:
							edit_message(chat_id, message_id, file_rename_ok_text(plan[0][1], done), back_close_markup(file_back_call))

			else:
				torrent = client.get_torrent(torrent_id)
				if torrent is None:
					edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
				else:
					plan, collisions = build_rename_plan(torrent)
					if not plan:
						edit_message(chat_id, message_id, get_text("FILES_NO_SUGGESTIONS"), back_close_markup(back_call))
					elif command == "filesAll":
						text = get_text("FILES_RENAME_CONFIRM", len(plan), build_plan_preview(plan))
						if collisions:
							text += f"\n\n{get_text('FILES_RENAME_SKIPPED', len(collisions))}"
						markup = InlineKeyboardMarkup(row_width=1)
						markup.add(InlineKeyboardButton(get_text("BUTTON_CONFIRM"), callback_data=build_call("filesAllOk", ctx_id, file_page)))
						markup.add(InlineKeyboardButton(get_text("BUTTON_CANCEL"), callback_data=back_call))
						edit_message(chat_id, message_id, text, markup)
					else:
						done, errors = apply_rename_plan(torrent_id, plan)
						if errors:
							edit_message(chat_id, message_id, get_text("FILES_RENAME_PARTIAL", done, len(errors), html.escape(errors[0])), back_close_markup(back_call))
						else:
							edit_message(chat_id, message_id, get_text("FILES_RENAME_OK", done), back_close_markup(back_call))

		elif command == "move":
			torrent_id, filter_key, page = args[0], args[1], args[2]
			dir_page = int(args[3]) if len(args) > 3 else 0
			torrent = client.get_torrent(torrent_id)
			if torrent is None:
				edit_message(chat_id, message_id, get_text("TORRENT_NOT_FOUND"), back_close_markup(build_call("list", filter_key, page)))
			else:
				nav_ctx = new_nav_context(torrent_id, filter_key, page)
				markup = build_dir_markup(
					dir_call=lambda dir_id: build_call("moveToDir", nav_ctx, dir_id),
					write_call=build_call("moveNewDir", nav_ctx),
					cancel_call=build_call("info", torrent_id, filter_key, page),
					page_parts=("move", torrent_id, filter_key, page),
					page=dir_page,
				)
				edit_message(chat_id, message_id, get_text("MOVE_ASK_DIR", html.escape(torrent.name)), markup)

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
					deliver_move_order(chat_id, [torrent_id], new_dir, progress_text, success_text, message_id=message_id, reply_markup=back_close_markup(back_call))
			else:
				name = torrent.name if torrent else ""
				prompt = get_text("MOVE_NEW_DIR_ASK")
				if torrent and torrent.download_dir:
					prompt = f"{current_dirs_text([torrent.download_dir])}\n{prompt}"
				if name:
					prompt = f"{get_text('MOVE_ASK_DIR', html.escape(name))}\n\n{prompt}"
				ask_for_input(chat_id, user_id, "move", prompt, message_id=message_id, torrent_id=torrent_id, name=name, back_call=back_call)

		elif command == "search":
			ask_for_input(chat_id, user_id, "search", get_text("SEARCH_ASK"), message_id=message_id)

		elif command == "trackers":
			text, markup = build_trackers_menu()
			edit_message(chat_id, message_id, text, markup)

		elif command == "addTo":
			pending_id, dir_id = args[0], args[1]
			download_dir = get_dir_by_id(dir_id)
			if download_dir is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
			else:
				do_add_torrent(chat_id, message_id, pending_id, download_dir)

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
				ask_for_input(chat_id, user_id, "addDir", prompt, message_id=message_id, pending_id=pending_id,
							back_call=build_call("dashboard"))

		elif command == "cancelAdd":
			pop_pending_torrent(args[0])
			show_dashboard(chat_id, message_id=message_id)

		elif command == "addDirPage":
			pending_id, dir_page = args[0], int(args[1])
			pending = get_pending_torrent(pending_id)
			if pending is None:
				edit_message(chat_id, message_id, get_text("ADD_EXPIRED"))
			else:
				ask_download_dir(chat_id, pending_id, pending["name"], message_id=message_id, dir_page=dir_page)

		elif command == "mass":
			action, filter_key = args[0], args[1]
			dir_page = int(args[2]) if len(args) > 2 else 0
			render_mass_confirm(chat_id, message_id, action, filter_key, dir_page=dir_page)

		elif command == "confirmMass":
			action, filter_key, extra = args[0], args[1], args[2]
			do_mass_action(chat_id, message_id, action, filter_key, extra)

		elif command == "massMoveDir":
			filter_key, dir_id = args[0], args[1]
			new_dir = get_dir_by_id(dir_id)
			if new_dir is None:
				edit_message(chat_id, message_id, get_text("MASS_EXPIRED"), back_close_markup())
			else:
				do_mass_move_to(chat_id, filter_key, new_dir, message_id=message_id)

		elif command == "massMoveNew":
			filter_key = args[0]
			prompt = get_text("MOVE_NEW_DIR_ASK")
			try:
				dirs = [t.download_dir for t in get_filtered_torrents(filter_key)]
				dirs_text = current_dirs_text(dirs)
				if dirs_text:
					prompt = f"{dirs_text}\n{prompt}"
			except (ExpiredContext, TorrentClientError):
				pass
			ask_for_input(chat_id, user_id, "massMove", prompt, message_id=message_id, filter_key=filter_key,
						back_call=build_call("list", filter_key, 0))

		elif command == "settings":
			render_settings(chat_id, message_id)

		elif command == "toggleAltSpeed":
			settings = client.get_settings()
			client.set_alt_speed(not settings["alt_speed_enabled"])
			render_settings(chat_id, message_id)

		elif command in ("toggleDownLimit", "toggleUpLimit"):
			settings = client.get_settings()
			if command == "toggleDownLimit":
				client.set_speed_limit("down", None, enabled=not settings["speed_limit_down_enabled"])
			else:
				client.set_speed_limit("up", None, enabled=not settings["speed_limit_up_enabled"])
			render_settings(chat_id, message_id)

		elif command == "setDownLimit":
			ask_for_input(chat_id, user_id, "downLimit", get_text("SETTINGS_ASK_DOWN_LIMIT"), message_id=message_id,
						back_call=build_call("settings"))

		elif command == "setUpLimit":
			ask_for_input(chat_id, user_id, "upLimit", get_text("SETTINGS_ASK_UP_LIMIT"), message_id=message_id,
						back_call=build_call("settings"))

		elif command == "toggleSetting":
			enabled = bot_settings.toggle(args[0])
			if args[0] == "auto_rename" and not enabled:
				# The file rename depends on the torrent rename: never leave it active but hidden
				bot_settings.set("auto_rename_files", False)
			render_settings(chat_id, message_id)

		elif command == "autoDirMenu":
			dir_page = int(args[0]) if args else 0
			markup = build_dir_markup(
				dir_call=lambda dir_id: build_call("autoDirSet", dir_id),
				write_call=build_call("autoDirNew"),
				cancel_call=build_call("settings"),
				page_parts=("autoDirMenu",),
				page=dir_page,
			)
			markup.keyboard.insert(0, [InlineKeyboardButton(get_text("BUTTON_AUTO_DIR_DEFAULT"), callback_data=build_call("autoDirDefault"))])
			edit_message(chat_id, message_id, get_text("AUTO_DIR_ASK"), markup)

		elif command == "autoDirSet":
			new_dir = get_dir_by_id(args[0])
			if new_dir is not None:
				bot_settings.set("auto_download_dir", new_dir)
			render_settings(chat_id, message_id)

		elif command == "autoDirDefault":
			bot_settings.set("auto_download_dir", "")
			render_settings(chat_id, message_id)

		elif command == "autoDirNew":
			ask_for_input(chat_id, user_id, "autoDir", get_text("MOVE_NEW_DIR_ASK"), message_id=message_id,
						back_call=build_call("settings"))

		elif command == "favDirsMenu":
			render_favorite_dirs_menu(chat_id, message_id)

		elif command == "favDirAdd":
			ask_for_input(chat_id, user_id, "favDir", get_text("MOVE_NEW_DIR_ASK"), message_id=message_id,
						back_call=build_call("favDirsMenu"))

		elif command == "favDirDel":
			directory = get_dir_by_id(args[0])
			favorites = bot_settings.get("favorite_dirs") or []
			if directory in favorites:
				favorites.remove(directory)
				bot_settings.set("favorite_dirs", favorites)
			render_favorite_dirs_menu(chat_id, message_id)

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
			ask_for_input(chat_id, user_id, "template", prompt, message_id=message_id, kind=kind,
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
				ask_for_input(chat_id, user_id, "gotoPage", get_text("GOTO_PAGE_ASK", pages),
							back_call=f"{base}|{page}", target_message_id=message_id, base=base, pages=pages)

		elif command == "cancelInput":
			pending_input = pop_pending_input(chat_id, user_id)
			back_call = (pending_input or {}).get("back_call")
			if back_call:
				edit_message(chat_id, message_id, get_text("INPUT_CANCELLED"), back_close_markup(back_call))
			else:
				show_dashboard(chat_id, message_id=message_id)

		else:
			debug(f"Unknown callback: {data}")

	except TorrentClientError as e:
		edit_message(chat_id, message_id, get_text("CONNECTION_ERROR", html.escape(str(e))), back_close_markup())
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
		text = get_text("STARTUP_MESSAGE", VERSION, client_version, torrent_count)
	except TorrentClientError as e:
		text = get_text("STARTUP_MESSAGE_ERROR", VERSION, html.escape(str(e)))
	notify(text)


def torrent_monitor():
	"""Background loop that detects finished/errored torrents and notifies.
	The first poll only builds the baseline, so restarting the bot never
	re-notifies torrents that were already finished or errored"""
	known = {}
	first_run = True
	while True:
		try:
			torrents = client.get_torrents()
			new_known = {}
			for torrent in torrents:
				state = {"finished": torrent.is_finished, "error": torrent.error_message or ""}
				prev = known.get(torrent.id)
				if not first_run:
					if state["finished"] and prev is not None and not prev["finished"] and bot_settings.get("notify_completed"):
						notify(get_text("NOTIFY_COMPLETED", html.escape(torrent.name)))
					if state["error"] and (prev is None or state["error"] != prev["error"]) and bot_settings.get("notify_errors"):
						notify(get_text("NOTIFY_TORRENT_ERROR", html.escape(torrent.name), html.escape(state["error"])))
				new_known[torrent.id] = state
			known = new_known
			first_run = False
		except Exception as e:
			warning(f"Torrent monitor: {e}")
		time.sleep(MONITOR_INTERVAL_SECONDS)


if __name__ == "__main__":
	debug(f"torrent-controller-bot {VERSION} started. Connected to {client_version}")
	send_startup_message()
	threading.Thread(target=torrent_monitor, daemon=True).start()
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
	bot.infinity_polling(timeout=60)
