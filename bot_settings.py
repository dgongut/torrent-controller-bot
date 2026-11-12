"""
Persistent bot settings stored in CONFIG_PATH/settings.json.
Unknown keys are ignored and missing keys fall back to DEFAULTS,
so upgrading the bot never breaks an existing settings file.
"""

import copy
import json
import os
import threading
from config import CONFIG_PATH
from logger import error, warning

SETTINGS_FILE = os.path.join(CONFIG_PATH, "settings.json")

DEFAULTS = {
	"notify_completed": True,
	"notify_errors": True,
	"notify_external_added": False,  # Torrents added outside the bot (other apps, the manager's UI...)
	"notify_ratio_reached": True,  # A torrent stopped seeding because it reached its ratio limit
	"auto_remove_ratio": False,  # A torrent that stops seeding at its ratio limit leaves the manager, its files stay
	"notify_auto_removed": True,  # Only used when auto_remove_ratio is enabled: tells each removal
	"auto_download": False,
	"auto_download_dir": "",  # "" = torrent client default dir
	"auto_category": "",  # "" = none; takes over the dir on clients with categories
	"auto_rename": False,
	"auto_rename_files": False,  # Only used when auto_rename is enabled
	"auto_rename_external": False,  # Only used when auto_rename is enabled
	"auto_replace": False,  # Only used when auto_rename is enabled: on completion, replaces the torrent holding its name
	"auto_move_series": False,  # Episodes added without asking go straight to the folder of their series
	"auto_move_series_external": False,  # Only used when auto_move_series is enabled
	"low_space_warning": True,
	# The automatic mover, see auto_mover.py: when the source folder runs low
	# on space, the torrents finished longest ago go to the same place under
	# the target folder. Only does anything with both folders set
	"auto_mover": False,
	"auto_mover_source": "",
	"auto_mover_target": "",
	"auto_mover_min_free_gb": 50,  # Under this much free space in the source, it moves
	"auto_mover_wanted_free_gb": 150,  # ...until the source has this much free
	"auto_mover_notice": None,  # [chat, message, topic] of the notice of the move going on, to close it after a restart
	"auto_mover_min_days": 7,  # Only torrents finished at least this many days ago
	"favorite_dirs": [],  # Extra dirs offered as buttons when adding/moving torrents
	"template_movie": "",  # "" = built-in default template
	"template_series": "",
	"template_season": "",  # Season packs (series without episode)
	"list_sort": "added",  # Order of the torrent lists, a key of LIST_SORTS in the bot
	"list_sort_reverse": False,  # The opposite of the natural order of list_sort
	"telemetry": True,  # Anonymous usage statistics, see the TELEMETRY section of the bot
	# Limits per tracker, {hostname: rule}, kept by tracker_limits. It has to be
	# here: _load only reads back the keys listed, so a missing one was dropped
	# on every restart and wiped from the file on the next save
	"tracker_limits": {},
	# The last global speed limit of each direction, {"down": kbps, "up": kbps},
	# so turning a limit off and on again brings it back. qBittorrent, Deluge
	# and Download Station keep no limit while it is off
	"saved_speed_limits": {},
}

_lock = threading.Lock()
_settings = None


def _load():
	global _settings
	# A deep copy: the defaults hold a list and a dict that must never be
	# changed through a setting that still has its default value
	settings = copy.deepcopy(DEFAULTS)
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
	"""The value of a setting. Lists and dicts come as copies, so a caller
	that changes what it got cannot change the setting behind set()'s back"""
	with _lock:
		if _settings is None:
			_load()
		return copy.deepcopy(_settings.get(key, DEFAULTS.get(key)))


def set(key, value):
	with _lock:
		if _settings is None:
			_load()
		_settings[key] = copy.deepcopy(value)
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
