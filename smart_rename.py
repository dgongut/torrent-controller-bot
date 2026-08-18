"""Smart rename parser for torrents (same logic as transmission-renamer,
extended with series support and extension-less names).
Movies: Title (Year) - Resolution[ HDR][.ext]
Series: Title - SxEE[ - Resolution][ HDR][.ext]"""

import re

# Valid video extensions
VALID_EXTENSIONS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "mpg", "mpeg", "m4v", "ts", "m2ts"}

# Episode patterns: S01E03, Cap.103 / Capitulo 103, 1x03
EPISODE_PATTERNS = [
	re.compile(r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})'),
	re.compile(r'[Cc]ap(?:[íi]tulo)?[\s._-]?(\d{3,4})'),
	re.compile(r'(?<![\dxX])(\d{1,2})[xX](\d{2})(?!\d)'),
]


def _split_extension(filename):
	if "." in filename:
		potential_name, potential_ext = filename.rsplit(".", 1)
		if potential_ext.lower() in VALID_EXTENSIONS:
			return potential_name, potential_ext
	return filename, ""


def _find_episode(name):
	"""Returns (season, episode, match_start) or None"""
	for i, pattern in enumerate(EPISODE_PATTERNS):
		match = pattern.search(name)
		if match:
			if i == 1:  # Cap.NNN(N): last two digits are the episode
				digits = match.group(1)
				season = int(digits[:-2]) if len(digits) > 2 else 1
				episode = int(digits[-2:])
			else:
				season = int(match.group(1))
				episode = int(match.group(2))
			return season, episode, match.start()
	return None


def _clean_title(title_part):
	# Remove bracketed tags ([HDTV 1080p], trailing unclosed bracket, etc.)
	title = re.sub(r'\[[^\]]*\]?', ' ', title_part)

	# Replace underscores with spaces
	title = re.sub(r'_', ' ', title)

	# Preserve dots in acronyms (J.F.K.) and numbers (20.000)
	title = re.sub(r'([A-Z])\.(?=[A-Z])', r'\1§PUNTO§', title)
	title = re.sub(r'(\d)\.(?=\d)', r'\1§PUNTO§', title)

	# Replace remaining dots (word separators) with spaces
	title = re.sub(r'\.', ' ', title)

	# Restore protected dots
	title = re.sub(r'§PUNTO§', '.', title)

	# Collapse multiple spaces
	title = re.sub(r'\s+', ' ', title).strip()

	# Remove trailing opening parenthesis and leftover separators
	title = re.sub(r'\s*\(\s*$', '', title).strip()
	title = re.sub(r'[\s\-–_.]+$', '', title).strip()
	return title


def _detect_resolution(name, default=None):
	# Detect REMUX first (takes priority over resolution)
	remux_match = re.search(r'(UHD)?\.?remux', name, re.IGNORECASE)
	if remux_match:
		if remux_match.group(1):  # UHDRemux
			return "UHDRemux"
		if re.search(r'BD\.?remux', name, re.IGNORECASE):
			return "BDRemux"
		return "Remux"

	# Normal resolution (detect and preserve i/p)
	if re.search(r'2160p|4k|uhd', name, re.IGNORECASE):
		return "4K"
	if match := re.search(r'1080([pi])', name, re.IGNORECASE):
		return "1080" + match.group(1).lower()
	if match := re.search(r'720([pi])', name, re.IGNORECASE):
		return "720" + match.group(1).lower()
	if match := re.search(r'480([pi])', name, re.IGNORECASE):
		return "480" + match.group(1).lower()
	return default


def _detect_hdr(name):
	if re.search(r'HDR|HDR10|Dolby.?Vision|DoVi|DV', name, re.IGNORECASE):
		return " HDR"
	return ""


def parse_name(filename):
	"""Returns the suggested name, or None if the filename cannot be parsed
	(no episode marker for series nor year for movies)"""
	name, ext = _split_extension(filename)
	suffix = f".{ext}" if ext else ""

	# Series: episode marker takes priority
	episode_info = _find_episode(name)
	if episode_info:
		season, episode, start = episode_info
		title = _clean_title(name[:start])
		if not title:
			return None
		result = f"{season}x{episode:02d} - {title}"
		resolution = _detect_resolution(name)
		if resolution:
			result += f" - {resolution}"
		return f"{result}{_detect_hdr(name)}{suffix}"

	# Movie: year required.
	# Prioritize years in parentheses (even if not immediately closed)
	year_match = re.search(r'\((19|20)\d{2}', name)

	# If no year in parentheses, look for a loose year
	if not year_match:
		year_match = re.search(r'[\s._-](19|20)\d{2}[\s._-]', name)

	if not year_match:
		return None

	year = re.search(r'(19|20)\d{2}', year_match.group()).group()

	title = _clean_title(name[:year_match.start()])
	if not title:
		return None

	resolution = _detect_resolution(name, default="1080p")
	return f"{title} ({year}) - {resolution}{_detect_hdr(name)}{suffix}"
