"""
Persistent bot settings stored in CONFIG_PATH/settings.json.
Unknown keys are ignored and missing keys fall back to DEFAULTS,
so upgrading the bot never breaks an existing settings file.
"""

import json
import os
import threading
from config import CONFIG_PATH
from logger import error, warning

SETTINGS_FILE = os.path.join(CONFIG_PATH, "settings.json")

DEFAULTS = {
	"notify_completed": True,
	"notify_errors": True,
	"auto_download": False,
	"auto_download_dir": "",  # "" = torrent client default dir
	"auto_rename": False,
	"auto_rename_files": False,  # Only used when auto_rename is enabled
	"low_space_warning": True,
	"favorite_dirs": [],  # Extra dirs offered as buttons when adding/moving torrents
	"template_movie": "",  # "" = built-in default template
	"template_series": "",
	"template_season": "",  # Season packs (series without episode)
}

_lock = threading.Lock()
_settings = None


def _load():
	global _settings
	settings = dict(DEFAULTS)
	try:
		with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
			stored = json.load(file)
		for key in DEFAULTS:
			if key in stored:
				settings[key] = stored[key]
	except FileNotFoundError:
		pass
	except Exception as e:
		warning(f"Cannot read {SETTINGS_FILE}, using defaults: {e}")
	_settings = settings


def _save():
	try:
		os.makedirs(CONFIG_PATH, exist_ok=True)
		tmp_file = SETTINGS_FILE + ".tmp"
		with open(tmp_file, "w", encoding="utf-8") as file:
			json.dump(_settings, file, indent=4)
		os.replace(tmp_file, SETTINGS_FILE)
	except Exception as e:
		error(f"Cannot write {SETTINGS_FILE}: {e}")


def get(key):
	with _lock:
		if _settings is None:
			_load()
		return _settings.get(key, DEFAULTS.get(key))


def set(key, value):
	with _lock:
		if _settings is None:
			_load()
		_settings[key] = value
		_save()


def toggle(key):
	"""Inverts a boolean setting and returns the new value. Unknown keys are ignored"""
	with _lock:
		if _settings is None:
			_load()
		if key not in DEFAULTS:
			warning(f"Unknown setting: {key}")
			return None
		_settings[key] = not _settings.get(key, DEFAULTS[key])
		_save()
		return _settings[key]
