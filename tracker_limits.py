"""
Limits per tracker: the ratio and speed limits that every torrent of a tracker
gets, the ones already there and the ones that arrive later.

The rules are a setting (bot_settings "tracker_limits"):
	{hostname: {"ratio": None | -1 | float, "down": KB/s, "up": KB/s}}
where ratio None is the manager's own and -1 seeds forever, and a speed of 0
is no limit. Those defaults are what any torrent has when added, so they are
never applied to an arriving torrent: only a rule's own values are.

The limits live in each torrent inside the manager, so a rule is applied once
per torrent and remembered (CONFIG_PATH/tracker_limits.json, torrent id ->
hostname). That way a torrent changed by hand from its own screen stays the
exception it was made, even across restarts of the bot, and the torrents added
while the bot was down still get the rule on the first poll.
"""

import hashlib
import json
import os
import threading
import bot_settings
from config import CONFIG_PATH
from logger import error, warning

SETTING = "tracker_limits"
STATE_FILE = os.path.join(CONFIG_PATH, "tracker_limits.json")

EMPTY_RULE = {"ratio": None, "down": 0, "up": 0}

_lock = threading.RLock()
_applied = None  # torrent id -> hostname whose rule it got


def tracker_key(tracker):
	"""Short stable id of a tracker, for the callback data of its buttons"""
	return hashlib.sha1(tracker.encode("utf-8")).hexdigest()[:8]


def _clean(rule):
	rule = dict(EMPTY_RULE, **{key: value for key, value in (rule or {}).items() if key in EMPTY_RULE})
	if rule["ratio"] is not None:
		rule["ratio"] = float(rule["ratio"])
	rule["down"] = max(0, int(rule["down"] or 0))
	rule["up"] = max(0, int(rule["up"] or 0))
	return rule


def rules():
	"""{hostname: rule} of every tracker with a rule, as copies"""
	stored = bot_settings.get(SETTING) or {}
	result = {}
	for tracker, rule in stored.items():
		try:
			result[tracker] = _clean(rule)
		except (TypeError, ValueError):
			warning(f"Ignoring the unreadable limits of the tracker {tracker}: {rule!r}")
	return result


def get_rule(tracker):
	"""The rule of the tracker, or None when it has none"""
	return rules().get(tracker)


def set_field(tracker, field, value):
	"""Sets one value of the rule of the tracker, creating it if needed.
	Returns the rule as it ends up"""
	if field not in EMPTY_RULE:
		raise ValueError(f"Unknown tracker limit: {field}")
	with _lock:
		current = rules()
		rule = current.get(tracker, dict(EMPTY_RULE))
		rule[field] = value
		current[tracker] = _clean(rule)
		bot_settings.set(SETTING, current)
		return current[tracker]


def delete_rule(tracker):
	with _lock:
		current = rules()
		current.pop(tracker, None)
		bot_settings.set(SETTING, current)
		forget(tracker)


def tracker_by_key(key, known_trackers=()):
	"""The hostname behind a tracker_key: one with a rule, or one of known_trackers"""
	for tracker in list(rules()) + list(known_trackers):
		if tracker_key(tracker) == key:
			return tracker
	return None


def rule_tracker(trackers, current_rules):
	"""Which tracker's rule a torrent with these trackers follows: the first of
	its trackers that has one, so its main tracker wins. None when no rule"""
	for tracker in trackers or ():
		if tracker in current_rules:
			return tracker
	return None


def non_default_fields(rule):
	"""The fields of a rule that differ from what a torrent gets when added"""
	fields = []
	if rule["ratio"] is not None:
		fields.append("ratio")
	if rule["down"] > 0:
		fields.append("down")
	if rule["up"] > 0:
		fields.append("up")
	return fields


# ---------------------------------------------------------------------------
# APPLIED STATE
# ---------------------------------------------------------------------------

def _load():
	global _applied
	if _applied is not None:
		return
	try:
		with open(STATE_FILE, "r", encoding="utf-8") as file:
			stored = json.load(file)
		_applied = {str(key): str(value) for key, value in (stored.get("applied") or {}).items()}
	except FileNotFoundError:
		_applied = {}
	except Exception as e:
		warning(f"Cannot read {STATE_FILE}, the tracker limits will be applied again: {e}")
		_applied = {}


def _save():
	try:
		os.makedirs(CONFIG_PATH, exist_ok=True)
		tmp_file = STATE_FILE + ".tmp"
		with open(tmp_file, "w", encoding="utf-8") as file:
			json.dump({"applied": _applied}, file)
		os.replace(tmp_file, STATE_FILE)
	except Exception as e:
		error(f"Cannot write {STATE_FILE}: {e}")


def pending(torrents):
	"""[(tracker, [torrent ids])] of the torrents that follow a rule they did
	not get yet, grouped by the tracker of the rule"""
	current_rules = rules()
	if not current_rules:
		return []
	groups = {}
	with _lock:
		_load()
		for torrent in torrents:
			tracker = rule_tracker(torrent.trackers, current_rules)
			if tracker is not None and _applied.get(str(torrent.id)) != tracker:
				groups.setdefault(tracker, []).append(str(torrent.id))
	return list(groups.items())


def applied_tracker(torrent_id):
	"""The tracker whose rule the torrent got, if that rule still exists"""
	with _lock:
		_load()
		tracker = _applied.get(str(torrent_id))
	return tracker if tracker in rules() else None


def mark_applied(torrent_ids, tracker):
	with _lock:
		_load()
		changed = False
		for torrent_id in torrent_ids:
			if _applied.get(str(torrent_id)) != tracker:
				_applied[str(torrent_id)] = tracker
				changed = True
		if changed:
			_save()


def forget(tracker):
	"""Drops the memory of the torrents that got the rule of the tracker"""
	with _lock:
		_load()
		before = len(_applied)
		for torrent_id in [key for key, value in _applied.items() if value == tracker]:
			del _applied[torrent_id]
		if len(_applied) != before:
			_save()


def prune(present_ids):
	"""Forgets torrents that are no longer in the manager"""
	present = {str(torrent_id) for torrent_id in present_ids}
	with _lock:
		_load()
		gone = [key for key in _applied if key not in present]
		for key in gone:
			del _applied[key]
		if gone:
			_save()


def reset_for_tests():
	global _applied
	with _lock:
		_applied = None
