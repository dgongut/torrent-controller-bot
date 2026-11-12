"""
What the automatic mover decides, apart from the bot so it can be tested alone.

One rule: a source folder, a target folder with the same layout below it, and
three margins. When the free space of the source drops under a minimum, the
torrents that finished longest ago are moved to the same place under the
target, only as many as it takes to get back to the free space wanted.

Torrents that share their data (the same name in the same folder, as cross
seeding leaves them) are one unit: moving only one of them takes the data
away from the others, so they go together or not at all.
"""

from collections import Counter

from torrent_clients import TorrentStatus

GB = 1024 ** 3
DAY = 86400

# Why a torrent in the source is not moved (the keys of Unit-less leftovers)
SKIP_RECENT = "recent"  # Finished less than the minimum ago
SKIP_UNFINISHED = "unfinished"  # Still downloading, or with nothing downloaded
SKIP_BUSY = "busy"  # Being checked, or in error
SKIP_MANAGED = "managed"  # The manager places it by itself (qBittorrent automatic management)
SKIP_UNDATED = "undated"  # The manager does not tell when it finished
SKIP_REASONS = (SKIP_RECENT, SKIP_UNFINISHED, SKIP_BUSY, SKIP_MANAGED, SKIP_UNDATED)


def normalize_dir(path):
	return (path or "").strip().rstrip("/") or "/"


def _key(path):
	"""A folder as it is compared: Download Station tells its folders without
	the leading slash (ssd/PELICULAS) and takes them either way"""
	return normalize_dir(path).strip("/")


def is_under(path, root):
	"""Whether path is root or a folder below it"""
	path, root = _key(path), _key(root)
	return path == root or not root or path.startswith(root + "/")


def paths_error(source, target):
	"""Why these two folders cannot be a rule: "same" or "nested" (one inside
	the other: what is moved would still be in the source, or would be picked
	again). None when they can, or while one of them is still missing"""
	if not (source or "").strip() or not (target or "").strip():
		return None
	if _key(source) == _key(target):
		return "same"
	if is_under(source, target) or is_under(target, source):
		return "nested"
	return None


def target_dir(download_dir, source, target):
	"""Where a torrent in download_dir goes: the same place under target.
	None for a folder that is not in the source"""
	if not is_under(download_dir, source):
		return None
	rest = _key(download_dir)[len(_key(source)):].strip("/")
	target = normalize_dir(target)
	return normalize_dir(f"{target.rstrip('/')}/{rest}") if rest else target


def root_folders(dirs):
	"""The folders worth offering for a rule, out of the ones in use: the one
	above each of them (downloads go to /ssd/PELICULAS and /ssd/SERIES/Lost,
	the rule is about /ssd), leaving out the ones inside another. A folder at
	the top stands for itself"""
	parents = []
	for directory in dirs:
		directory = normalize_dir(directory)
		parts = [part for part in directory.split("/") if part]
		if not parts:
			continue
		lead = "/" if directory.startswith("/") else ""
		parent = lead + "/".join(parts[:-1] if len(parts) > 1 else parts)
		if parent not in parents:
			parents.append(parent)
	return [parent for parent in parents if not any(other != parent and is_under(parent, other) for other in parents)]


def finished_seconds(torrent, now):
	"""Seconds since the torrent finished. A manager that does not tell when
	gives the time it has been seeding instead. None when neither is known"""
	completed = torrent.completed_date
	if isinstance(completed, (int, float)) and not isinstance(completed, bool) and completed > 0:
		return max(0, now - completed)
	if isinstance(torrent.seeding_time, (int, float)) and torrent.seeding_time >= 0:
		return torrent.seeding_time
	return None


def in_error(torrent):
	"""Whether the manager has a problem with the data of the torrent. A
	tracker that does not answer is none: old torrents, the ones to move,
	are the ones whose trackers are gone"""
	return torrent.status == TorrentStatus.ERROR and not torrent.error_is_tracker


class Unit:
	"""The torrents that share one copy of the data, moved as one"""

	def __init__(self, torrents, age):
		self.torrents = torrents
		self.age = age  # Seconds since the most recent of them finished
		self.size = max(torrent.total_size or 0 for torrent in torrents)  # One copy on disk
		self.download_dir = torrents[0].download_dir


def _skip_reason(torrent, min_seconds, now):
	"""Why the torrent cannot be moved, None when it can"""
	if torrent.status == TorrentStatus.CHECKING or in_error(torrent):
		return SKIP_BUSY
	if not torrent.is_finished or (torrent.total_size or 0) <= 0 or torrent.status == TorrentStatus.DOWNLOADING:
		return SKIP_UNFINISHED
	if torrent.auto_managed:
		return SKIP_MANAGED
	age = finished_seconds(torrent, now)
	if age is None:
		return SKIP_UNDATED
	if age < min_seconds:
		return SKIP_RECENT
	return None


def candidates(torrents, source, min_seconds, now):
	"""(units, skipped): the units the rule may move, the ones finished
	longest ago first, and how many torrents of the source are left out for
	each reason. A unit is left out whole, for the reason of the torrent that
	holds it back"""
	shared = {}
	for torrent in torrents:
		if torrent.download_dir and is_under(torrent.download_dir, source):
			shared.setdefault((_key(torrent.download_dir), torrent.name), []).append(torrent)
	units, skipped = [], Counter()
	for group in shared.values():
		reasons = [reason for reason in (_skip_reason(torrent, min_seconds, now) for torrent in group) if reason]
		if reasons:
			# The first reason in the order of SKIP_REASONS that anyone has
			skipped[next(reason for reason in SKIP_REASONS if reason in reasons)] += len(group)
			continue
		units.append(Unit(group, min(finished_seconds(torrent, now) for torrent in group)))
	units.sort(key=lambda unit: -unit.age)
	return units, skipped


def pick(units, free, wanted_free):
	"""The first units whose size adds up to what is missing to have
	wanted_free bytes free. All of them when they do not add up to it"""
	missing = wanted_free - free
	picked = []
	for unit in units:
		if missing <= 0:
			break
		picked.append(unit)
		missing -= unit.size
	return picked


def group_by_target(units, source, target):
	"""[(target folder, [torrents])] in the order the folders first show up:
	a move order takes one destination, and each subfolder has its own"""
	groups = {}
	for unit in units:
		groups.setdefault(target_dir(unit.download_dir, source, target), []).extend(unit.torrents)
	return list(groups.items())
