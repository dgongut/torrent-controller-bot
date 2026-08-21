"""Torrent name metadata parser and rename template engine.

Extracts as much metadata as possible from a release name (title, year,
season/episode, resolution, HDR, extension, language, codecs, source and
release group) and renders user-defined templates with two constructs:
  {field}   required field: if missing, no suggestion is produced
  [ ... ]   optional block: dropped entirely (literals included) when any
            field inside it has no value
Field names are accepted both in Spanish and English."""

import re

# Valid video extensions
VALID_EXTENSIONS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "mpg", "mpeg", "m4v", "ts", "m2ts"}

# Subtitle extensions: they follow the video file name so players keep matching them
SUBTITLE_EXTENSIONS = {"srt", "ass", "ssa", "sub", "idx", "vtt", "sup"}

# Episode patterns: S01E03, 1x03, Cap.103 / Capitulo 103
EPISODE_PATTERNS = [
	re.compile(r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})'),
	re.compile(r'(?<![\dxX])(\d{1,2})[xX](\d{2,3})(?!\d)'),
	re.compile(r'[Cc]ap(?:[íi]tulo)?[\s._-]?(\d{3,4})'),
]

# Bare episode numbers used inside season packs (E01, - 01, [01], 01). Only
# tried when the context already says the file belongs to a series, since on
# their own these are far too ambiguous
_BARE_EPISODE_PATTERNS = [
	re.compile(r'(?<![A-Za-z0-9])[Ee][Pp]?[\s._-]?(\d{1,3})(?![\dpPiI])'),
	re.compile(r'[\s._]-[\s._](\d{1,3})(?![\dpPiI])'),
	re.compile(r'\[(\d{1,3})\](?![\dpPiI])'),
	re.compile(r'^(\d{1,3})$'),
]

# Season-only patterns (packs): S02, Temporada 2, Season 2, T2
SEASON_ONLY_PATTERNS = [
	re.compile(r'\b[Ss](\d{1,2})\b(?![\s._-]?[Ee]\d)'),
	re.compile(r'[Tt]emporada[\s._-]+(\d{1,2})'),
	re.compile(r'[Ss]eason[\s._-]+(\d{1,2})'),
	re.compile(r'\bT(\d{1,2})\b(?![\s._-]?[Ee]\d)'),
]

# Multi-episode/multi-season packs: S04E01-E08, S01-S08
_EPISODE_RANGE_PATTERN = re.compile(r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})[\s._-]?-[\s._-]?[Ee]?(\d{1,3})(?![\dpP])')
_SEASON_RANGE_PATTERN = re.compile(r'\b[Ss](\d{1,2})[\s._-]?-[\s._-]?[Ss](\d{1,2})\b')

# 'Cap.1984': four digits that look like a year. On their own they are still a
# valid chapter (1901 is season 19, episode 01), so they only count as junk
# when the name already carries a year somewhere else
_CHAPTER_YEAR = re.compile(r'[\s._-][Cc]ap(?:[íi]tulo)?[\s._-]?(19|20)\d{2}(?!\d)')

# Year ranges for collections/trilogies: 1977-2019, 2006.-.2016
_YEAR_RANGE = re.compile(r'(?<!\d)((19|20)\d{2})[\s._]*-[\s._]*((19|20)\d{2})(?!\d)')
_YEAR_PLAIN = re.compile(r'[\s._-]((19|20)\d{2})(?=[\s._-]|$)')

_REMUX_PATTERNS = [
	(re.compile(r'UHD[\s._-]?remux', re.IGNORECASE), "UHDRemux"),
	(re.compile(r'BD[\s._-]?remux', re.IGNORECASE), "BDRemux"),
	(re.compile(r'remux', re.IGNORECASE), "Remux"),
]

_RESOLUTION_PATTERNS = [
	(re.compile(r'2160p|\b4K\b|\bUHD\b', re.IGNORECASE), "4K"),
	(re.compile(r'1080([pi])', re.IGNORECASE), "1080{0}"),
	(re.compile(r'720([pi])', re.IGNORECASE), "720{0}"),
	(re.compile(r'480([pi])', re.IGNORECASE), "480{0}"),
	(re.compile(r'(?<![A-Za-z0-9])(?:Full[\s._-]?HD|FHD)(?![A-Za-z])', re.IGNORECASE), "1080p"),
	(re.compile(r'\bmHD\b', re.IGNORECASE), "mHD"),
	# Bare HD: not part of TrueHD/DTS-HD/HDTV/HDR nor of the aliases above
	(re.compile(r'(?<![A-Za-z0-9])(?<!DTS[\s._-])(?<!True[\s._-])HD(?![A-Za-z])', re.IGNORECASE), "HD"),
]

_HDR_PATTERN = re.compile(r'\bHDR10\+?|\bHDR\b|Dolby[\s._-]?Vision\b|\bDoVi\b|\bDV\b')

_VIDEO_CODECS = [
	(re.compile(r'[HXhx][\s._-]?264\b|\bAVC\b', re.IGNORECASE), "H.264"),
	(re.compile(r'[HXhx][\s._-]?265\b|\bHEVC\b', re.IGNORECASE), "H.265"),
	(re.compile(r'\bAV1\b', re.IGNORECASE), "AV1"),
	(re.compile(r'\bXviD\b', re.IGNORECASE), "XviD"),
	(re.compile(r'\bDivX\b', re.IGNORECASE), "DivX"),
	(re.compile(r'\bVP9\b', re.IGNORECASE), "VP9"),
]

# Order matters: more specific codecs first
_AUDIO_CODECS = [
	(re.compile(r'DTS[\s._-]?HD(?:[\s._-]?MA)?(?![A-Za-z])', re.IGNORECASE), "DTS-HD"),
	(re.compile(r'DTS[\s._-]?X(?![A-Za-z0-9])', re.IGNORECASE), "DTS-X"),
	(re.compile(r'\bDTS(?![A-Za-z])', re.IGNORECASE), "DTS"),
	(re.compile(r'True[\s._-]?HD(?![A-Za-z])', re.IGNORECASE), "TrueHD"),
	(re.compile(r'\bE[\s._-]?AC[\s._-]?3(?![A-Za-z])|\bDDP(?![A-Za-z])|\bDD\+', re.IGNORECASE), "EAC3"),
	(re.compile(r'\bAC[\s._-]?3(?![A-Za-z])|\bDD(?![A-Za-z+])', re.IGNORECASE), "AC3"),
	(re.compile(r'\bAAC(?![A-Za-z])', re.IGNORECASE), "AAC"),
	(re.compile(r'\bFLAC(?![A-Za-z])', re.IGNORECASE), "FLAC"),
	(re.compile(r'\bMP3(?![A-Za-z])', re.IGNORECASE), "MP3"),
	(re.compile(r'\bOPUS(?![A-Za-z])', re.IGNORECASE), "OPUS"),
]

# Atmos is a complement to the base codec (TrueHD Atmos, EAC3 5.1 Atmos...)
_ATMOS_PATTERN = re.compile(r'\bAtmos(?![A-Za-z])', re.IGNORECASE)

_AUDIO_CHANNELS = re.compile(r'^[\s._-]{0,2}(\d[.,]\d)')

_SOURCES = [
	(re.compile(r'WEB[\s._-]?DL', re.IGNORECASE), "WEB-DL"),
	(re.compile(r'WEB[\s._-]?Rip', re.IGNORECASE), "WEBRip"),
	(re.compile(r'Blu[\s._-]?Ray', re.IGNORECASE), "BluRay"),
	(re.compile(r'\bBDRip\b', re.IGNORECASE), "BDRip"),
	(re.compile(r'\bBRRip\b', re.IGNORECASE), "BRRip"),
	(re.compile(r'\bHDTV\b', re.IGNORECASE), "HDTV"),
	(re.compile(r'\bDVDRip\b', re.IGNORECASE), "DVDRip"),
	(re.compile(r'\bHDRip\b', re.IGNORECASE), "HDRip"),
	(re.compile(r'\bWEB\b', re.IGNORECASE), "WEB-DL"),
]

# Streaming platform/provider tags. They are metadata, not part of the episode
# title (Episodio 7.NF.WEB-DL...). Matched only as standalone tokens, so a word
# containing the same letters (Max, Stan, Crash...) is never touched
_PLATFORM_TAGS = [
	"ATRESPLAYER", "PARAMOUNT", "MOVISTAR", "SONYLIV", "BGLOBAL",
	"HIDIVE", "KOCOWA", "ANPLUS", "MITELE", "FILMIN", "10PLAY",
	"STARZ", "TVING", "WAVVE", "JIOHS", "ATRES", "PLAYZ", "SKYST",
	"HULU", "AMZN", "AMZP", "HMAX", "DSNP", "DSNY", "ATVP", "PCOK", "PMTP",
	"STAN", "VIKI", "CRAV", "APTV", "AVPT", "MVSP", "NFLX", "HBOM", "PEAC",
	"DSCP", "MUBI", "TUBI", "ROKU", "PLEX", "CRKL", "EPIX", "BRAV", "ITVX",
	"ALL4", "UKTV", "FUNI", "WETV", "CPNG", "HTSR", "ZEE5", "RTVE", "FLMN",
	"SKST", "GLBP", "AUBC", "9NOW",
	"MITL",
	"AMZ", "ATV", "MAX", "PMT", "SHO", "MGM", "LGP", "STZ", "ITV", "MY5",
	"BBC", "RTE", "VRV", "WKN", "VIU", "VIX", "CBC", "4OD",
	"NF", "CR", "iP",
]

_PLATFORM_TAG_PATTERN = re.compile(
	r'(?<![A-Za-z0-9])(?:' + "|".join(_PLATFORM_TAGS) + r')(?![A-Za-z0-9])', re.IGNORECASE)

# Separators allowed between a platform tag and the metadata that follows it
_TAG_SEPARATORS = re.compile(r'[\s._\-\[\]()]*')

_LANGUAGES = [
	(re.compile(r'\bCastellano\b|\bSpanish\b|\bEspañol\b|\bESP\b', re.IGNORECASE), "Castellano"),
	(re.compile(r'\bLatino\b', re.IGNORECASE), "Latino"),
	(re.compile(r'\bVOSE\b', re.IGNORECASE), "VOSE"),
	(re.compile(r'\bDUAL\b', re.IGNORECASE), "Dual"),
	(re.compile(r'\bMULTI\b', re.IGNORECASE), "Multi"),
	(re.compile(r'\bVOSTFR\b|\bFrench\b', re.IGNORECASE), "French"),
	(re.compile(r'\bEnglish\b|\bENG\b', re.IGNORECASE), "English"),
	(re.compile(r'\bGerman\b', re.IGNORECASE), "German"),
	(re.compile(r'\bItalian\b', re.IGNORECASE), "Italian"),
]

_GROUP_PATTERN = re.compile(r'-([A-Za-z0-9]{2,20})\s*$')
_GROUP_BLACKLIST = {"dl", "hd", "ma", "rip", "x264", "x265", "264", "265"}

CANONICAL_FIELDS = {
	"title", "episode_title", "year", "chapter", "season", "episode_number", "resolution",
	"hdr", "extension", "language", "video_codec", "audio_codec", "source", "group",
}

# Template field names accepted in both languages (Spanish and English)
FIELD_ALIASES = {
	# Spanish
	"titulo": "title", "título": "title",
	"titulo_episodio": "episode_title", "título_episodio": "episode_title",
	"año": "year", "anio": "year",
	"capitulo": "chapter", "capítulo": "chapter",
	"temporada": "season",
	"episodio": "episode_number",
	"resolucion": "resolution", "resolución": "resolution",
	"extension": "extension", "extensión": "extension",
	"idioma": "language",
	"codec_video": "video_codec", "códec_video": "video_codec",
	"codec_audio": "audio_codec", "códec_audio": "audio_codec",
	"fuente": "source",
	"grupo": "group",
	# English
	"title": "title",
	"episode_title": "episode_title",
	"year": "year",
	"chapter": "chapter",
	"season": "season",
	"episode": "episode_number",
	"episode_number": "episode_number",
	"resolution": "resolution",
	"language": "language",
	"video_codec": "video_codec",
	"audio_codec": "audio_codec",
	"source": "source",
	"group": "group",
	# Both
	"hdr": "hdr",
}


class TemplateError(Exception):
	def __init__(self, code, detail=""):
		super().__init__(code)
		self.code = code
		self.detail = detail


# ---------------------------------------------------------------------------
# METADATA EXTRACTION
# ---------------------------------------------------------------------------

def _split_extension(filename):
	if "." in filename:
		potential_name, potential_ext = filename.rsplit(".", 1)
		if potential_ext.lower() in VALID_EXTENSIONS:
			return potential_name, potential_ext
	return filename, ""


def _find_chapter_year(name):
	"""Returns the 'Cap.<year>' match when its digits are junk rather than a
	chapter: only when the name carries another year outside of it, which is
	what tells 'Viernes 13 ... 1985 capitulo 1984' from 'Serie Capitulo 1901'"""
	match = _CHAPTER_YEAR.search(name)
	if not match:
		return None
	for year in _YEAR_PLAIN.finditer(name):
		if year.start() >= match.end() or year.end() <= match.start():
			return match
	return None


def _find_episode(name, skip_span=None):
	"""Returns (season, episode, match_start, match_end) or None. skip_span is
	a region of the name that must not be read as an episode marker"""
	for i, pattern in enumerate(EPISODE_PATTERNS):
		position = 0
		while True:
			match = pattern.search(name, position)
			if not match:
				break
			if skip_span and match.start() < skip_span[1] and match.end() > skip_span[0]:
				position = match.start() + 1
				continue
			if i == 2:  # Cap.NNN(N): last two digits are the episode
				digits = match.group(1)
				season = int(digits[:-2]) if len(digits) > 2 else 1
				episode = int(digits[-2:])
			else:
				season = int(match.group(1))
				episode = int(match.group(2))
			return season, episode, match.start(), match.end()
	return None


def _find_season_only(name):
	"""Returns (season, match_start) or None"""
	for pattern in SEASON_ONLY_PATTERNS:
		match = pattern.search(name)
		if match:
			return int(match.group(1)), match.start()
	return None


def _find_bare_episode(name, limit):
	"""Returns (episode, match_start, match_end) for an episode number without
	a season marker, or None. Only matches before limit (the first metadata
	token) to keep resolutions, codecs and channel counts out"""
	for pattern in _BARE_EPISODE_PATTERNS:
		match = pattern.search(name)
		if match and match.start() < limit:
			return int(match.group(1)), match.start(), match.end()
	return None


def _extract_episode_title(name, start, limit):
	"""Returns the episode title: the text between the episode marker and the
	first metadata token. Release junk lands here too ('1of2', '2v3'), so only
	plain words are accepted: a single token mixing letters and digits is
	discarded, and so is anything without letters"""
	candidate = _clean_title(name[start:limit])
	candidate = re.sub(r'^[\s\-–_.]+', '', candidate).strip()
	if not candidate or not re.search(r'[A-Za-zÀ-ÿ]', candidate):
		return ""
	if " " not in candidate and re.search(r'\d', candidate):
		return ""
	return candidate


def _episode_title_limit(name, start, limit):
	"""Platform tags are metadata as well, so the episode title also ends at
	the first one found after the episode marker. Only counted when real
	metadata follows and nothing but separators or further tags sit in
	between (NF.WEB-DL, AMZN.DSNP.1080p): standing alone at the end of the
	name they cannot be told apart from a legitimate word"""
	if limit >= len(name):
		return limit
	for match in _PLATFORM_TAG_PATTERN.finditer(name, start, limit):
		position = match.end()
		while position < limit:
			position = _TAG_SEPARATORS.match(name, position, limit).end()
			following = _PLATFORM_TAG_PATTERN.match(name, position, limit)
			if not following:
				break
			position = following.end()
		if position >= limit:
			return match.start()
	return limit


def _clean_title(title_part):
	# Remove bracketed tags ([HDTV 1080p], trailing unclosed bracket, etc.)
	title = re.sub(r'\[[^\]]*\]?', ' ', title_part)

	# Replace underscores with spaces
	title = re.sub(r'_', ' ', title)

	# Preserve dots in acronyms (J.F.K.) and numbers (20.000). Every letter of
	# an acronym stands alone, so 'Parte V.Un nuevo' is just a word separator
	title = re.sub(r'([A-Z])\.(?=[A-Z](?:[\s._-]|$))', r'\1§PUNTO§', title)
	title = re.sub(r'(\d)\.(?=\d)', r'\1§PUNTO§', title)

	# Replace remaining dots (word separators) with spaces
	title = re.sub(r'\.', ' ', title)

	# Restore protected dots
	title = re.sub(r'§PUNTO§', '.', title)

	# Collapse multiple spaces
	title = re.sub(r'\s+', ' ', title).strip()

	# Drop an unclosed parenthesis group and whatever follows it: cutting at
	# the first metadata token can leave one behind ('(HD' out of '(HD 1080p)')
	depth = 0
	opener = -1
	for index, char in enumerate(title):
		if char == "(":
			if depth == 0:
				opener = index
			depth += 1
		elif char == ")" and depth:
			depth -= 1
	if depth:
		title = title[:opener]

	# Remove leftover separators
	title = re.sub(r'[\s\-–_.]+$', '', title).strip()
	return title


def _detect_resolution(name):
	# REMUX takes priority and shares the field with the resolution
	for pattern, value in _REMUX_PATTERNS:
		if pattern.search(name):
			return value
	for pattern, value in _RESOLUTION_PATTERNS:
		match = pattern.search(name)
		if match:
			return value.format(match.group(1).lower()) if "{0}" in value else value
	return ""


def _detect_from_list(name, patterns):
	for pattern, value in patterns:
		if pattern.search(name):
			return value
	return ""


def _detect_audio(name):
	base = ""
	for pattern, value in _AUDIO_CODECS:
		match = pattern.search(name)
		if match:
			channels = _AUDIO_CHANNELS.match(name[match.end():])
			if channels:
				base = f"{value} {channels.group(1).replace(',', '.')}"
			else:
				base = value
			break
	if _ATMOS_PATTERN.search(name):
		return f"{base} Atmos".strip()
	return base


def _detect_group(name):
	match = _GROUP_PATTERN.search(name)
	if match and match.group(1).lower() not in _GROUP_BLACKLIST:
		return match.group(1)
	return ""


def parse_metadata(filename, season_prefix="T", allow_bare_episode=False):
	"""Extracts all detectable metadata from a release name.
	Returns a dict with all CANONICAL_FIELDS (empty string when missing)
	plus 'is_series' (bool). season_prefix is used for the 'chapter' field
	on season-only packs (T2 in Spanish, S2 in English).
	allow_bare_episode enables matching episode numbers without a season
	marker, only safe when the caller already knows it is a series file"""
	name, ext = _split_extension(filename)

	fields = {key: "" for key in CANONICAL_FIELDS}
	fields["extension"] = ext
	fields["is_series"] = False

	# Episode/season detection determines the title boundary
	title_end = len(name)
	episode_end = None
	episode_span = None
	episode_range = _EPISODE_RANGE_PATTERN.search(name)
	chapter_year = _find_chapter_year(name)
	episode_info = _find_episode(name, chapter_year.span() if chapter_year else None)
	season_range = _SEASON_RANGE_PATTERN.search(name)
	if episode_range:
		season, ep1, ep2 = int(episode_range.group(1)), int(episode_range.group(2)), int(episode_range.group(3))
		fields["is_series"] = True
		fields["season"] = str(season)
		fields["episode_number"] = f"{ep1:02d}-{ep2:02d}"
		fields["chapter"] = f"{season}x{ep1:02d}-{ep2:02d}"
		title_end = episode_range.start()
	elif episode_info:
		season, episode, start, end = episode_info
		fields["is_series"] = True
		fields["season"] = str(season)
		fields["episode_number"] = f"{episode:02d}"
		fields["chapter"] = f"{season}x{episode:02d}"
		title_end = start
		episode_end = end
		episode_span = (start, end)
	elif season_range:
		s1, s2 = int(season_range.group(1)), int(season_range.group(2))
		fields["is_series"] = True
		fields["season"] = f"{s1}-{s2}"
		fields["chapter"] = f"{season_prefix}{s1}-{season_prefix}{s2}"
		title_end = season_range.start()
	else:
		season_info = _find_season_only(name)
		if season_info:
			season, start = season_info
			fields["is_series"] = True
			fields["season"] = str(season)
			fields["chapter"] = f"{season_prefix}{season}"
			title_end = start

	# The title also ends at the first metadata token (resolution, source...)
	meta_start = len(name)
	for patterns in ([p for p, _ in _REMUX_PATTERNS], [p for p, _ in _RESOLUTION_PATTERNS],
						[p for p, _ in _SOURCES], [p for p, _ in _VIDEO_CODECS], [_HDR_PATTERN]):
		for pattern in patterns:
			match = pattern.search(name)
			if match and match.start() < meta_start:
				meta_start = match.start()
	if meta_start < title_end:
		title_end = meta_start

	# Files inside a season pack usually carry the episode number alone
	if allow_bare_episode and not fields["episode_number"]:
		bare = _find_bare_episode(name, meta_start)
		if bare:
			episode, start, end = bare
			fields["is_series"] = True
			fields["episode_number"] = f"{episode:02d}"
			if fields["season"] and "-" not in fields["season"]:
				fields["chapter"] = f"{fields['season']}x{episode:02d}"
			else:
				fields["chapter"] = ""
			if start < title_end:
				title_end = start
			episode_end = end

	# Whatever sits between the episode marker and the metadata is its title
	if episode_end is not None and episode_end < meta_start:
		fields["episode_title"] = _extract_episode_title(
			name, episode_end, _episode_title_limit(name, episode_end, meta_start))

	# A junk 'Cap.1984' is neither a chapter nor the release year, but it does
	# end the title: everything from it on is junk
	if chapter_year:
		if chapter_year.start() < title_end:
			title_end = chapter_year.start()
		if chapter_year.start() < meta_start:
			meta_start = chapter_year.start()

	# Year: range (collections) > parentheses > last plain year before the
	# metadata tags (in "Blade Runner 2049 2017" the release year is 2017)
	year_range = _YEAR_RANGE.search(name)
	if year_range:
		fields["year"] = f"{year_range.group(1)}-{year_range.group(3)}"
		if year_range.start() < title_end:
			title_end = year_range.start()
	else:
		year_match = re.search(r'\((19|20)\d{2}', name)
		if not year_match:
			# 'Capitulo.1901' is season 19, episode 01: those digits are not a year
			plain_years = [m for m in _YEAR_PLAIN.finditer(name)
				if not (episode_span and m.start() < episode_span[1] and m.end() > episode_span[0])]
			before_meta = [m for m in plain_years if m.start() <= meta_start]
			year_match = before_meta[-1] if before_meta else (plain_years[0] if plain_years else None)
		if year_match:
			fields["year"] = re.search(r'(19|20)\d{2}', year_match.group()).group()
			if year_match.start() < title_end:
				title_end = year_match.start()

	fields["title"] = _clean_title(name[:title_end])
	fields["resolution"] = _detect_resolution(name)
	fields["hdr"] = "HDR" if _HDR_PATTERN.search(name) else ""
	fields["language"] = _detect_from_list(name, _LANGUAGES)
	fields["video_codec"] = _detect_from_list(name, _VIDEO_CODECS)
	fields["audio_codec"] = _detect_audio(name)
	fields["source"] = _detect_from_list(name, _SOURCES)
	fields["group"] = _detect_group(name)
	return fields


# ---------------------------------------------------------------------------
# TEMPLATE ENGINE
# ---------------------------------------------------------------------------

_TOKEN_PATTERN = re.compile(r'\{([^{}\[\]]+)\}')


def _resolve_field(raw_name):
	return FIELD_ALIASES.get(raw_name.strip().lower())


def validate_template(template):
	"""Raises TemplateError if the template is malformed. Returns the list of
	canonical fields used"""
	if not template.strip():
		raise TemplateError("empty")
	depth = 0
	for char in template:
		if char == "[":
			depth += 1
			if depth > 1:
				raise TemplateError("nested_brackets")
		elif char == "]":
			depth -= 1
			if depth < 0:
				raise TemplateError("unbalanced_brackets")
	if depth != 0:
		raise TemplateError("unbalanced_brackets")
	if "{" in _TOKEN_PATTERN.sub("", template) or "}" in _TOKEN_PATTERN.sub("", template):
		raise TemplateError("unbalanced_braces")
	used = []
	for raw_name in _TOKEN_PATTERN.findall(template):
		field = _resolve_field(raw_name)
		if field is None:
			raise TemplateError("unknown_field", raw_name.strip())
		used.append(field)
	if not used:
		raise TemplateError("empty")
	return used


def _render_fragment(fragment, fields):
	"""Renders a fragment without brackets. Returns the text, or None when
	any field inside it has no value"""
	result = ""
	last = 0
	for match in _TOKEN_PATTERN.finditer(fragment):
		field = _resolve_field(match.group(1))
		value = fields.get(field, "")
		if not value:
			return None
		result += fragment[last:match.start()] + value
		last = match.end()
	return result + fragment[last:]


def render_template(template, fields):
	"""Renders the template with the given metadata. Returns the resulting
	name, or None when a required field (outside brackets) is missing"""
	result = ""
	pos = 0
	while pos < len(template):
		open_bracket = template.find("[", pos)
		if open_bracket == -1:
			fragment = _render_fragment(template[pos:], fields)
			if fragment is None:
				return None
			result += fragment
			break
		fragment = _render_fragment(template[pos:open_bracket], fields)
		if fragment is None:
			return None
		result += fragment
		close_bracket = template.find("]", open_bracket)
		optional_text = _render_fragment(template[open_bracket + 1:close_bracket], fields)
		if optional_text is not None:
			result += optional_text
		pos = close_bracket + 1
	return result.strip() or None


# ---------------------------------------------------------------------------
# HIGH-LEVEL API
# ---------------------------------------------------------------------------

DEFAULT_MOVIE_TEMPLATE = "{title} ({year}) - {resolution}[ {hdr}][.{extension}]"
DEFAULT_SERIES_TEMPLATE = "{season}x{episode} - {title}[ - {resolution}][ {hdr}][.{extension}]"
DEFAULT_SEASON_PACK_TEMPLATE = "{chapter} - {title}[ - {resolution}][ {hdr}][.{extension}]"


def _pick_template(fields, template_movie, template_series, template_season):
	if fields["is_series"] and fields["season"] and not fields["episode_number"]:
		return template_season or DEFAULT_SEASON_PACK_TEMPLATE
	if fields["is_series"]:
		return template_series or DEFAULT_SERIES_TEMPLATE
	return template_movie or DEFAULT_MOVIE_TEMPLATE


def _comparable(text):
	return re.sub(r'[^a-z0-9]', '', text.lower())


def _drop_series_title_from_episode_title(fields):
	"""Files already named after the series ('2x01 - Succession [x265]') leave
	the series name where the episode title should be, and files renamed twice
	repeat it as a prefix ('13x05 - Serie - El titulo'). Neither is a title"""
	title = fields["title"]
	episode_title = fields["episode_title"]
	if not title or not episode_title:
		return
	if _comparable(episode_title) == _comparable(title):
		fields["episode_title"] = ""
		return
	# Only a dash tells the repeated series name apart from an episode title
	# that merely begins with the same words ('Succession Day')
	if episode_title[:len(title)].lower() == title.lower():
		remainder = re.sub(r'^\s*[-–]\s*', '', episode_title[len(title):])
		if remainder and remainder != episode_title[len(title):]:
			fields["episode_title"] = remainder.strip()


def _render(template, fields, filename):
	try:
		suggested = render_template(template, fields)
	except Exception:
		return None
	if not suggested or suggested == filename:
		return None
	return suggested


def suggest_name(filename, template_movie=None, template_series=None, template_season=None, season_prefix="T"):
	"""Parses the name and renders the matching template: season pack when a
	season without episode is found, series when an episode marker is found,
	movie otherwise. Returns the suggested name or None when a required field
	is missing or the result equals the original name"""
	fields = parse_metadata(filename, season_prefix=season_prefix)
	_drop_series_title_from_episode_title(fields)
	template = _pick_template(fields, template_movie, template_series, template_season)
	return _render(template, fields, filename)


# Metadata taken from the torrent name when the file itself does not carry it.
# The episode and its title are deliberately excluded: both must always come
# from the file, otherwise every file would end up with the same name
_INHERITABLE_FIELDS = (
	"title", "year", "season", "resolution", "hdr", "language",
	"video_codec", "audio_codec", "source", "group",
)


def suggest_file_name(filename, parent_name=None, single_video=False, template_movie=None,
						template_series=None, template_season=None, season_prefix="T"):
	"""Suggests a name for a single video file inside a torrent. parent_name is
	the torrent name, used as context: the fields missing in the file are
	inherited from it (the season included, never the episode), so files named
	just '01.mkv' inside a season pack can still be renamed. single_video tells
	whether the torrent holds just one video, which is what allows a file with
	no metadata of its own to take the whole torrent name.
	Returns None when the file cannot be identified"""
	parent = parse_metadata(parent_name, season_prefix=season_prefix) if parent_name else None
	parent_is_series = bool(parent and parent["is_series"])
	fields = parse_metadata(filename, season_prefix=season_prefix, allow_bare_episode=parent_is_series)
	own_year = fields["year"]

	if parent:
		for key in _INHERITABLE_FIELDS:
			if not fields[key] and parent[key]:
				fields[key] = parent[key]
		# Without a year of its own the file title is just the leftovers of the
		# file name (numbers, tags...), so the torrent title is more reliable
		if not own_year and parent["title"]:
			fields["title"] = parent["title"]
		if parent_is_series:
			fields["is_series"] = True
		if not fields["chapter"] and fields["season"] and "-" not in fields["season"]:
			if fields["episode_number"]:
				fields["chapter"] = f"{fields['season']}x{fields['episode_number']}"
			else:
				fields["chapter"] = f"{season_prefix}{fields['season']}"

	_drop_series_title_from_episode_title(fields)

	if fields["is_series"]:
		# Every file needs its own episode, otherwise all of them would
		# collapse into the same name
		if not fields["episode_number"]:
			return None
	elif not single_video and not own_year:
		# One video among many with nothing identifying it: the torrent name
		# belongs to the pack, not to this particular file
		return None

	template = _pick_template(fields, template_movie, template_series, template_season)
	return _render(template, fields, filename)


def companion_subtitle_name(subtitle_name, video_name, new_video_name):
	"""Returns the name a subtitle must take so it keeps matching its video
	after the video is renamed, preserving any language suffix and its own
	extension. Returns None when the subtitle does not belong to the video"""
	subtitle_stem, _, subtitle_ext = subtitle_name.rpartition(".")
	if not subtitle_stem or subtitle_ext.lower() not in SUBTITLE_EXTENSIONS:
		return None
	video_stem = _split_extension(video_name)[0]
	if not subtitle_stem.startswith(video_stem):
		return None
	suffix = subtitle_stem[len(video_stem):]
	new_stem = _split_extension(new_video_name)[0]
	return f"{new_stem}{suffix}.{subtitle_ext}"
