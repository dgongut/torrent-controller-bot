"""Simple stdout logger used across the bot modules.

Nothing it writes carries the token of the bot. The token is part of the URL
of every request to Telegram, and a request that fails says its URL in the
error: logged as it comes, the log (which people paste when they ask for
help) gave control of the bot to whoever read it.
"""

import logging
import os
import re
import sys
import threading
import traceback
from datetime import datetime

# What a bot token looks like, for one that is not the configured one. In a
# URL its colon can come encoded
_TOKEN_SHAPE = re.compile(r"\d{6,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}")
MASK = "<token>"


def mask_token(text):
	"""The text without the token of the bot"""
	text = str(text)
	token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
	if token:
		text = text.replace(token, MASK).replace(token.replace(":", "%3A"), MASK).replace(token.replace(":", "%3a"), MASK)
	return _TOKEN_SHAPE.sub(MASK, text)


def _log(level, message):
	print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - {level}: {mask_token(message)}')


def debug(message):
	_log("DEBUG", message)


def error(message):
	_log("ERROR", message)


def warning(message):
	_log("WARNING", message)


class _MaskingFormatter(logging.Formatter):
	"""Formats like the formatter it replaces, traceback included, and masks"""

	def __init__(self, formatter):
		super().__init__()
		self._formatter = formatter or logging.Formatter()

	def format(self, record):
		return mask_token(self._formatter.format(record))


def mask_library_log(library_logger):
	"""What a library logs on its own (telebot, when polling fails) goes
	through the same mask"""
	for handler in library_logger.handlers:
		if not isinstance(handler.formatter, _MaskingFormatter):
			handler.setFormatter(_MaskingFormatter(handler.formatter))


def mask_tracebacks():
	"""An exception nobody caught is printed by Python with its message: the
	traceback is masked too, in the main thread and in the others"""
	def show(exc_type, exc_value, exc_traceback):
		sys.stderr.write(mask_token("".join(traceback.format_exception(exc_type, exc_value, exc_traceback))))

	def show_in_thread(args):
		if args.exc_type is SystemExit:
			return
		name = args.thread.name if args.thread is not None else "?"
		sys.stderr.write(f"Exception in thread {name}:\n")
		show(args.exc_type, args.exc_value, args.exc_traceback)

	sys.excepthook = show
	threading.excepthook = show_in_thread
