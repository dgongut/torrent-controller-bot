"""Torrent name metadata parser and rename template engine.

Extracts as much metadata as possible from a release name (title, year,
season/episode, resolution, HDR, extension, language, codecs, source and
release group) and renders user-defined templates with these constructs:
  {field}    required field: if missing, no suggestion is produced
  {field.N}  same, but zero-padded to N digits, 4 at most (only 'season'
             and 'episode_number' support this; e.g. {episode.3} -> "007")
  [ ... ]    optional block: dropped entirely (literals included) when any
             field inside it has no value
Field names are accepted both in Spanish and English."""

import functools
import re
import unicodedata

# Valid video extensions
VALID_EXTENSIONS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "mpg", "mpeg", "m4v", "ts", "m2ts"}

# Subtitle extensions: they follow the video file name so players keep matching them
SUBTITLE_EXTENSIONS = {"srt", "ass", "ssa", "sub", "idx", "vtt", "sup"}

# Season marker: S01, or T01 (temporada) in Spanish releases. The T has to
# start a word, so it is never the last letter of the title ('Lost01E01')
_SEASON_MARK = r'(?:[Ss]|(?<![A-Za-z0-9])[Tt])'

# Episode patterns: S01E03 / T01E03 / S01Ep03, 1x03, Temporada 2 Capitulo 3 /
# T2 Cap 03 / S02.Episode.5 / Season 1 E05 / Season 1 EP5, Cap.103 / Capitulo
# 103. 'Cap' has to start a word and its number has to end: 'Kneecap.2024' or
# 'Nightcap.1080p' hold no episode. A bare 'E' after a long season ('Season 1
# E05') has to be glued to its number: 'Temporada 1 e 2' is Portuguese for 'and'
EPISODE_PATTERNS = [
	re.compile(_SEASON_MARK + r'(\d{1,2})[\s._-]?[Ee][Pp]?(\d{1,3})'),
	re.compile(r'(?<![\dxX])(\d{1,2})[xX](\d{2,3})(?!\d)'),
	re.compile(r'(?:(?<![A-Za-z])(?:temporada|season)|(?<![A-Za-z0-9])t|(?<![A-Za-z0-9])s(?=\d))[\s._-]*(\d{1,2})[\s._-]*'
		r'(?:(?:cap(?:[íi]tulo)?|episodio|episode|ep)[\s._-]*|e(?=\d))(\d{1,4})(?!\d)', re.IGNORECASE),
	re.compile(r'(?<![A-Za-z0-9])[Cc]ap(?:[íi]tulo)?[\s._-]?(\d{3,4})(?!\d)'),
]
_SEASON_AND_EPISODE = 2  # Index of the 'Temporada 2 Capitulo 3' pattern
_CHAPTER = 3  # Index of the 'Cap.103' pattern

# Bare episode numbers used inside season packs (E01, - 01, [01], 01). Only
# tried when the context already says the file belongs to a series, since on
# their own these are far too ambiguous
_BARE_EPISODE_PATTERNS = [
	re.compile(r'(?<![A-Za-z0-9])[Ee][Pp]?[\s._-]?(\d{1,3})(?![\dpPiI])'),
	re.compile(r'[\s._]-[\s._](\d{1,3})(?![\dpPiI])'),
	re.compile(r'\[(\d{1,3})\](?![\dpPiI])'),
	re.compile(r'^(\d{1,3})$'),
]

# Season-only patterns (packs): S02, Temporada 2, Temporada Completa 2,
# 2ª Temporada / 1a Temporada, 2nd Season, Season 2, T2. The number has to end
# where the digits do: 'Season 2010' is no season 20. The ordinal goes before
# 'Season N', so in '2nd Season - 05' the season is the 2 and not the 05
SEASON_ONLY_PATTERNS = [
	re.compile(r'\b[Ss](\d{1,2})\b(?![\s._-]?[Ee]\d)'),
	re.compile(r'(?<![A-Za-z])[Tt]emporada(?:[\s._-]+[Cc]ompleta)?[\s._-]+(\d{1,2})(?!\d)'),
	re.compile(r'(?<![A-Za-z0-9])(\d{1,2})(?:ª|º|[ao])?[\s._-]*[Tt]emporada\b'),
	re.compile(r'(?<![A-Za-z0-9])(\d{1,2})(?:st|nd|rd|th)[\s._-]*[Ss]eason\b'),
	re.compile(r'(?<![A-Za-z])[Ss]eason[\s._-]+(\d{1,2})(?!\d)'),
	re.compile(r'\bT(\d{1,2})\b(?![\s._-]?[Ee]\d)'),
]
_BARE_T_SEASON = SEASON_ONLY_PATTERNS[-1]

# 'T2.Trainspotting.2017': a T and a number in front of a title is how some
# movie titles start, while Spanish releases put the season behind the title
# ('Serie.Name.T2'). Only read as a title when nothing but words follows it
# (no ' - ' as in the renamed 'T2 - Serie') and the name carries a year
_TITLE_T = re.compile(r'T[1-9]\d?[._ ]+[A-Za-z]')

# '[SubsPlease] Show S2 - 05.mkv': anime releases write the episode behind the
# season after a spaced dash. Two or three digits, as they always pad it, and
# only in the name of a video or with a version ('05v2'): a single digit, or
# the name of a folder ('Friends Season 1 - 10'), could just as well be a range
# of seasons. Their batches carry the range of episodes the same way: 'Show S2
# (01-24)'
_SEASON_DASH_EPISODE = re.compile(r'[\s._]+[-–][\s._]+(\d{2,3})([vV]\d{1,2})?(?![A-Za-z0-9]|[.,]\d)')
_SEASON_EPISODE_RANGE = re.compile(
	r'[\s._]+(?:[-–][\s._]+)?[\[(]?(\d{2,3})[\s._]*[-–~][\s._]*(\d{2,3})[\])]?(?![A-Za-z0-9]|[.,]\d)')

# What makes that number a list or a count of episodes instead of one: '01 al
# 10', '01 & 02', '01, 02', '01+02', '10 Episodes', '10 Capitulos'
_MORE_EPISODES = re.compile(
	r'[\s._]*(?:[-–~&+,]|(?<![A-Za-z])(?:al?|y|to|and|et|und)(?![A-Za-z]))[\s._]*\d{1,3}(?![\dpPiIkKbBxX])'
	r'|[\s._]+(?:episod(?:e|io)s|cap[ií]tulos|chapters|eps|caps)(?![A-Za-z])', re.IGNORECASE)

# Multi-episode/multi-season packs: S04E01-E08, S01E01E02, 1x01-02, S01-S08
# (or with T), Temporadas 1-5, Temporada Completa 1 a 5, Seasons 1-5
_EPISODE_RANGE_PATTERNS = [
	# Every further episode may repeat the marker and the season, after a dot
	# or not ('S01E05.E06', 'S01E05.S01E06', 'S01E05E06E07'): the range runs to
	# the last one, so a triple never reads as the double it starts with
	re.compile(_SEASON_MARK + r'(\d{1,2})[\s._-]?[Ee][Pp]?(\d{1,3})'
		r'(?:(?:[\s._-]?-[\s._-]?(?:' + _SEASON_MARK + r'0*\1[\s._-]?)?(?:[Ee][Pp]?)?'
		r'|[\s._]?(?:' + _SEASON_MARK + r'0*\1[\s._-]?)?[Ee][Pp]?)(\d{1,3}))+(?![\dpP])'),
	re.compile(r'(?<![\dxX])(\d{1,2})[xX](\d{2,3})(?:[-xX](?:\1[xX])?(\d{2,3}))+(?![\dpP])'),
]
_SEASON_RANGE_PATTERNS = [
	re.compile(r'\b[Ss](\d{1,2})[\s._-]?-[\s._-]?[Ss](\d{1,2})\b|\b[Tt](\d{1,2})[\s._-]?-[\s._-]?[Tt](\d{1,2})\b'),
	# Plural or 'Completa' leave no doubt. Otherwise the dash has to be tight
	# ('Temporada 1-5'), since 'Season 2 - 05' is an episode in anime releases
	re.compile(r'(?:[Tt]emporadas|[Ss]easons|(?:[Tt]emporada|[Ss]eason)[\s._-]+[Cc]ompletas?)[\s._-]+'
		r'(\d{1,2})[\s._]*(?:-|al?)[\s._]*(\d{1,2})(?![\dpPkKxX])'),
	re.compile(r'(?:[Tt]emporada|[Ss]eason)[\s._-]+(\d{1,2})(?:-|[\s._]+al?[\s._]+)(\d{1,2})(?![\dpPkKxX])'),
]

# 'Cap.201_207': every episode from the first to the second, same season
_CHAPTER_RANGE_PATTERN = re.compile(
	r'(?<![A-Za-z0-9])[Cc]ap(?:[íi]tulo)?[\s._-]?(\d{3,4})[\s._]*[_-][\s._]*(?:[Cc]ap(?:[íi]tulo)?[\s._-]?)?(\d{3,4})(?!\d)')

# Download sites stamped at the start of the name: 'www.Site.com - Title',
# '[ www.Site.com ] - Title', '(www.Site.org) Title'
_SITE_PREFIX = re.compile(
	r'^\s*(?:[\[(【]\s*www\.[^\])】]*[\])】]|www\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*?\.[A-Za-z]{2,6}(?=[\s_\-–]))'
	r'[\s._\-–]*', re.IGNORECASE)

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
]

# A bare 'WEB' (a WEB-DL) is also an English word ('Charlottes.Web.2006',
# 'Web.of.Lies.S01E01'), so it is handled apart from the other sources: see
# _find_bare_web
_BARE_WEB = re.compile(r'\bWEB\b', re.IGNORECASE)
_YEAR_TOKEN = re.compile(r'(?:^|[\s._\-(\[])(?:19|20)\d{2}(?=[\s._\-)\]]|$)')

# Tokens that are metadata: the title ends at the first one
_META_PATTERNS = ([p for p, _ in _REMUX_PATTERNS] + [p for p, _ in _RESOLUTION_PATTERNS]
	+ [p for p, _ in _SOURCES] + [p for p, _ in _VIDEO_CODECS] + [_HDR_PATTERN])

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

# The text a name drops because no field holds it: from the season marker of a
# season pack, or the year of a movie, up to the metadata. Only what every
# sibling carries alike may go unnoticed there: an edition ('Extended'), a
# release flag ('REPACK'), a language, the audio ('AC3.5.1', 'DD51'). Anything
# else may be just what tells a release apart from the next one, and renaming
# it would give them all the same suggestion: a number ('S2 - 5', the '11.05'
# of an air date), a part ('Vol.1', 'CD1'), an episode ('E05', '1of7'), extra
# material ('Recap', 'Behind the Scenes') or a sports event ('Round20', 'Mexico
# Race', 'Chiefs at Bills'). So these are listed, not the others
_DROPPED_AUDIO = re.compile(
	r'(?<![A-Za-z0-9])(?:DD\+?|DDPA?|E[\s._-]?AC[\s._-]?3D?|AC[\s._-]?3D?|DTS(?:[\s._-]?HD)?(?:[\s._-]?MA)?(?:[\s._:-]?X)?'
	r'|DTSD|(?:HE[\s._-]?)?AAC(?:[\s._-]?LC)?|True[\s._-]?HD|FLAC|OPUS|L?PCM|MP[23])(?:[\s._-]?[1-9](?:[.,]?[01])?)?'
	r'(?![A-Za-z0-9])', re.IGNORECASE)
_DROPPED_CHANNELS = re.compile(r'(?<![0-9])[1-9][.,][01](?![0-9])')
_DROPPED_YEAR = re.compile(r'(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])')
_DROPPED_CRC = re.compile(r'\[[0-9A-Fa-f]{8}\]')
_DROPPED_SITE = re.compile(
	r'(?<![A-Za-z0-9])www\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,6}(?![A-Za-z0-9])', re.IGNORECASE)
# Shared only as a whole: 'Special', 'Final', 'Open' or a number on their own
# are no edition ('10 bit', '60 fps' and '6 CH' are no stray number either)
_DROPPED_PHRASES = re.compile(
	r'(?<![A-Za-z0-9])(?:special[\s._-]+(?:extended[\s._-]+)?edition|edici[oó]n[\s._-]+especial|final[\s._-]+cut'
	r'|open[\s._-]?matte|criterion[\s._-]+collection|pre[\s._-]?dvd|h(?:alf)?[\s._-]?(?:sbs|ou|tab)'
	r'|[0-9]{1,2}[\s._-]?bits?|[0-9]{2,3}[\s._-]?fps|[1-9](?:[.,][01])?[\s._-]?ch(?:annels?)?'
	r'|[0-9]{1,3}(?:st|nd|rd|th|º|ª|o)?[\s._-]*(?:anniversary|aniversario))(?![A-Za-z0-9])', re.IGNORECASE)
_SHARED_TOKEN = re.compile(r'[0-9]{1,2}bits?|hi10p?|[1-9]ch|v[1-9]|[0-9]{2,3}fps|(?:repack|proper|rerip)[1-9]')
_SHARED_WORDS = frozenset((
	# Editions and cuts
	"extended extendida extendido unrated uncut uncensored censored remastered remaster remasterizada remasterizado "
	"remasterised restored restaurada restaurado theatrical cinema cine imax enhanced directors director dc cut "
	"version edition edicion montaje collectors coleccionista ultimate definitive definitiva criterion deluxe redux "
	"roadshow anniversary aniversario editado editada edited alternate alt despecialized colorized colourized om oar ws fs widescreen "
	"fullscreen 3d sbs hsbs hou se ce ue "
	# Release flags and subtitles
	"proper repack rerip real internal int limited ltd retail hybrid readnfo read nfo nfofix dirfix syncfix subfix "
	"audiofix festival stv docu dubbed dub dubs subbed sub subs subtitled subtitles subtitulos subtitulado "
	"subtitulada forced forzados hc hardsub hardsubs hardcoded softsub softsubs multisub multisubs msub msubs esub "
	"esubs korsub complete completa completo full "
	# Sources and video
	"cam camrip hdcam ts telesync hdts tc telecine hdtc scr screener dvdscr bdscr ddc workprint wp r5 dvd "
	"dvdr dvd5 dvd9 bd bd25 bd50 bd66 bd100 bdmv iso vhs vhsrip tvrip satrip dvb dvbrip dsr dsrip pdtv sdtv ppv vod "
	"predvd rip microhd fullhd uhdrip line mic md ld hq ntsc pal secam sdr hlg hfr mpeg mpeg2 mpeg4 vc1 fps "
	# Audio
	"audio dolby digital plus atmos stereo mono surround ch ma "
	# Languages
	"multi multilang dual dl ml vo vos vose vost vostfr vf vff vfq vfi vfb vf2 subfrench truefrench french fr fra "
	"fre castellano cast latino lat spanish spa esp es espanol ingles ing english eng en frances aleman italiano "
	"japones coreano chino ruso portugues german ger deu de italian ita it nordic swedish swe danish dan norwegian "
	"nor finnish fin icelandic dutch nl nld flemish polish pol pl czech cze cz slovak hungarian hun romanian rom "
	"bulgarian bul greek gre turkish tur russian rus ru ukrainian ukr arabic ara hebrew heb persian farsi thai "
	"vietnamese vie indonesian ind malay filipino tagalog hindi hin tamil tam telugu tel malayalam mal kannada "
	"bengali punjabi marathi urdu chinese chi chs cht mandarin cantonese japanese jap jpn jp korean kor kr "
	"portuguese por pt ptbr br brazilian catalan cat galician basque euskera serbian croatian org "
	# What joins them
	"and y et und con with mas the of del la el los las le les en sin"
).split())
# A season pack is the whole season anyway, while a movie followed by them may
# be a pack of several ('Movie.2001.Collection' next to 'Movie.2001')
_PACK_WORDS = _SHARED_WORDS | frozenset("series serie season temporada pack batch collection coleccion saga".split())

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
		# 'Title . mkv' is still a .mkv: the spaces belong to neither part
		if potential_ext.strip().lower() in VALID_EXTENSIONS:
			return potential_name.rstrip(), potential_ext.strip()
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
			if i == _CHAPTER:  # Cap.NNN(N): last two digits are the episode
				digits = match.group(1)
				season = int(digits[:-2]) if len(digits) > 2 else 1
				episode = int(digits[-2:])
			elif i == _SEASON_AND_EPISODE:
				# 'Temporada 2 Cap.201' repeats the season in front of the
				# episode, the way Cap.NNN always does
				season = int(match.group(1))
				digits = match.group(2)
				episode = int(digits)
				if len(digits) > 2 and int(digits[:-2]) == season and int(digits[-2:]):
					episode = int(digits[-2:])
			else:
				season = int(match.group(1))
				episode = int(match.group(2))
			return season, episode, match.start(), match.end()
	return None


def _has_year(name):
	return bool(re.search(r'\((19|20)\d{2}', name) or _YEAR_PLAIN.search(name))


def _find_season_only(name):
	"""Returns the match of the season marker (the season in group 1) or None"""
	for pattern in SEASON_ONLY_PATTERNS:
		for match in pattern.finditer(name):
			if pattern is _BARE_T_SEASON and match.start() == 0 and _TITLE_T.match(name) and _has_year(name):
				continue
			return match
	return None


def _find_bare_web(name, boundary):
	"""The first bare 'WEB' that is the source tag, or None. It is one when the
	title is over by then (it comes after boundary, the episode or season
	marker or the first metadata, or after a year) or when more metadata
	follows it ('Show.WEB.1080p'). Anywhere else it is a word of the title"""
	for match in _BARE_WEB.finditer(name):
		if match.start() >= boundary or _YEAR_TOKEN.search(name, 0, match.start()):
			return match
		following = _TAG_SEPARATORS.match(name, match.end()).end()
		if any(pattern.match(name, following) for pattern in _META_PATTERNS):
			return match
	return None


def _tells_apart(dropped, group="", pack=False):
	"""True when the text a name drops may tell the release apart from its
	siblings: it holds anything but what all of them carry (see _SHARED_WORDS,
	and _PACK_WORDS for a pack: a season or a range of years). group is the
	release group, which ends the name when no metadata does.
	The audio goes first, with its channels ('DD51', 'AC3.5.1'), so they are
	never read as a stray number"""
	text = re.sub(r"['’´`]", "", dropped)
	if re.search(r'[A-Za-z]', group) and text.rstrip().endswith(f"-{group}"):
		text = text.rstrip()[:-len(group) - 1]
	for pattern in (_DROPPED_SITE, _DROPPED_PHRASES, _DROPPED_AUDIO, _DROPPED_CHANNELS, _DROPPED_YEAR, _DROPPED_CRC,
					_PLATFORM_TAG_PATTERN):
		text = pattern.sub(" ", text)
	shared = _PACK_WORDS if pack else _SHARED_WORDS
	for token in re.findall(r'[^\W_]+', text):
		word = _series_text(token)
		if word not in shared and not _SHARED_TOKEN.fullmatch(word):
			return True
	return False


def _find_episode_range(name):
	"""The first range of episodes that runs forwards: 'S01E05.E3.Conference'
	is episode 5 with 'E3' in its title, never the range 5 to 3"""
	for pattern in _EPISODE_RANGE_PATTERNS:
		for match in pattern.finditer(name):
			if int(match.group(3)) > int(match.group(2)):
				return match
	return None


def _find_first(patterns, name):
	for pattern in patterns:
		match = pattern.search(name)
		if match:
			return match
	return None


def _find_chapter_range(name):
	"""Returns (season, first, last, match_start) for 'Cap.201_207', or None.
	Both ends have to belong to the same season"""
	match = _CHAPTER_RANGE_PATTERN.search(name)
	if not match:
		return None
	first, last = match.group(1), match.group(2)
	season = int(first[:-2])
	if int(last[:-2]) != season or int(last[-2:]) <= int(first[-2:]):
		return None
	return season, int(first[-2:]), int(last[-2:]), match.start()


def _season_title_end(name, title_end, season):
	"""Spanish releases repeat the season before the episode marker ('The Last
	of Us - Temporada 2 [HDTV][Cap.201]'). It belongs to the episode, not to
	the title, so the title ends where it starts. Only when it is the same
	season and some title is left in front of it"""
	head = name[:title_end]
	for pattern in SEASON_ONLY_PATTERNS:
		for match in pattern.finditer(head):
			if int(match.group(1)) == season and _clean_title(head[:match.start()]):
				return match.start()
	return title_end


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
	# an acronym stands alone on both sides, so 'Parte V.Un nuevo' and
	# 'Episode IV.A New Hope' are just word separators
	title = re.sub(r'(?<![A-Za-z0-9])([A-Z])\.(?=[A-Z](?:[\s._-]|$))', r'\1§PUNTO§', title)
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

	# Remove leftover separators ('[ www.Site.com ] - Title' leaves a dash)
	title = re.sub(r'^[\s\-–_.]+|[\s\-–_.]+$', '', title).strip()
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
	plus 'is_series' (bool) and 'ambiguous' (bool): the name drops something
	that tells it apart from its siblings and no field holds ('S04.Vol.1',
	an air date), so no name built from the fields is safe for it.
	season_prefix is used for the 'chapter' field on season-only packs (T2
	in Spanish, S2 in English).
	allow_bare_episode enables matching episode numbers without a season
	marker, only safe when the caller already knows it is a series file"""
	name, ext = _split_extension(filename)
	name = _SITE_PREFIX.sub("", name, count=1)

	fields = {key: "" for key in CANONICAL_FIELDS}
	fields["extension"] = ext
	fields["is_series"] = False
	fields["ambiguous"] = False

	# Episode/season detection determines the title boundary
	title_end = len(name)
	episode_end = None
	episode_span = None
	pack_end = None  # End of the marker of a season pack: what follows it is dropped
	episode_range = _find_episode_range(name)
	chapter_range = None if episode_range else _find_chapter_range(name)
	chapter_year = _find_chapter_year(name)
	episode_info = _find_episode(name, chapter_year.span() if chapter_year else None)
	season_range = _find_first(_SEASON_RANGE_PATTERNS, name)
	if episode_range or chapter_range:
		if episode_range:
			season, ep1, ep2 = (int(episode_range.group(i)) for i in (1, 2, 3))
			start = episode_range.start()
		else:
			season, ep1, ep2, start = chapter_range
		fields["is_series"] = True
		fields["season"] = str(season)
		fields["episode_number"] = f"{ep1}-{ep2}"
		fields["chapter"] = f"{season}x{ep1:02d}-{ep2:02d}"
		title_end = _season_title_end(name, start, season)
	elif episode_info:
		season, episode, start, end = episode_info
		fields["is_series"] = True
		fields["season"] = str(season)
		fields["episode_number"] = str(episode)
		fields["chapter"] = f"{season}x{episode:02d}"
		title_end = _season_title_end(name, start, season)
		episode_end = end
		episode_span = (start, end)
	elif season_range:
		s1, s2 = (int(group) for group in season_range.groups() if group is not None)
		fields["is_series"] = True
		fields["season"] = f"{s1}-{s2}"
		fields["chapter"] = f"{season_prefix}{s1}-{season_prefix}{s2}"
		title_end = season_range.start()
		pack_end = season_range.end()
	else:
		season_match = _find_season_only(name)
		if season_match:
			season = int(season_match.group(1))
			fields["is_series"] = True
			fields["season"] = str(season)
			title_end = season_match.start()
			episodes = _SEASON_EPISODE_RANGE.match(name, season_match.end())
			dash_episode = _SEASON_DASH_EPISODE.match(name, season_match.end())
			# Anything else leaves the number to the pack, whose name it makes
			# ambiguous: 'Season 1 - 10' may be ten seasons, '01 al 10' or
			# '10 Episodes' are several episodes
			if dash_episode and (not (ext or dash_episode.group(2)) or _MORE_EPISODES.match(name, dash_episode.end())):
				dash_episode = None
			if episodes and int(episodes.group(1)) < int(episodes.group(2)):
				ep1, ep2 = int(episodes.group(1)), int(episodes.group(2))
				fields["episode_number"] = f"{ep1}-{ep2}"
				fields["chapter"] = f"{season}x{ep1:02d}-{ep2:02d}"
			elif dash_episode:
				episode = int(dash_episode.group(1))
				fields["episode_number"] = str(episode)
				fields["chapter"] = f"{season}x{episode:02d}"
				episode_end = dash_episode.end()
				episode_span = (season_match.start(), episode_end)
			else:
				fields["chapter"] = f"{season_prefix}{season}"
				pack_end = season_match.end()

	# The title also ends at the first metadata token (resolution, source...)
	meta_start = len(name)
	for pattern in _META_PATTERNS:
		match = pattern.search(name)
		if match and match.start() < meta_start:
			meta_start = match.start()
	web = _find_bare_web(name, min(title_end, meta_start))
	if web and web.start() < meta_start:
		meta_start = web.start()
	if meta_start < title_end:
		title_end = meta_start

	# Files inside a season pack usually carry the episode number alone
	if allow_bare_episode and not fields["episode_number"]:
		bare = _find_bare_episode(name, meta_start)
		if bare:
			episode, start, end = bare
			fields["is_series"] = True
			fields["episode_number"] = str(episode)
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
	year_end = None
	year_range = _YEAR_RANGE.search(name)
	if year_range:
		fields["year"] = f"{year_range.group(1)}-{year_range.group(3)}"
		year_end = year_range.end()
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
			year_end = year_match.end()
			if year_match.start() < title_end:
				title_end = year_match.start()

	fields["title"] = _clean_title(name[:title_end])
	fields["resolution"] = _detect_resolution(name)
	fields["hdr"] = "HDR" if _HDR_PATTERN.search(name) else ""
	fields["language"] = _detect_from_list(name, _LANGUAGES)
	fields["video_codec"] = _detect_from_list(name, _VIDEO_CODECS)
	fields["audio_codec"] = _detect_audio(name)
	fields["source"] = _detect_from_list(name, _SOURCES) or ("WEB-DL" if web else "")
	fields["group"] = _detect_group(name)

	# An episode is told apart by its number. A season pack or a movie only by
	# its title and season or year, so whatever else it carries before the
	# metadata must not tell it apart from its siblings ('S04.Vol.1' and
	# 'S04.Vol.2', 'Show.2024.11.05' and 'Show.2024.11.06'). Whatever names a
	# pack of several is fine in a season pack, or after a range of years
	if not fields["episode_number"]:
		dropped_from = pack_end if fields["is_series"] else year_end
		pack = fields["is_series"] or "-" in fields["year"]
		if dropped_from is not None and _tells_apart(name[dropped_from:meta_start], fields["group"], pack):
			fields["ambiguous"] = True
	return fields


# ---------------------------------------------------------------------------
# TEMPLATE ENGINE
# ---------------------------------------------------------------------------

_TOKEN_PATTERN = re.compile(r'\{([^{}\[\].]+)(?:\.(\d+))?\}')

# Fields whose value is a plain number (or a "N-M" range of them) and can
# therefore be zero-padded with the {field.N} syntax
_PADDABLE_FIELDS = {"season", "episode_number"}

# No season or episode needs more digits than this
_MAX_PADDING = 4

# A rendered name cannot be longer than this anyway: a template saved before
# _MAX_PADDING existed must not make the bot build a huge string
_MAX_RENDER_PADDING = 255

# Line and paragraph breaks (a newline, U+2028...) and other control characters
_CONTROL_CATEGORIES = {"Cc", "Zl", "Zp"}


def _resolve_field(raw_name):
	return FIELD_ALIASES.get(raw_name.strip().lower())


def _apply_padding(value, width):
	"""Zero-pads value to width digits. Handles "N-M" ranges by padding each
	side independently. Never truncates a longer number"""
	return "-".join(part.zfill(width) if part.isdigit() else part for part in value.split("-"))


def _literal_text(template):
	"""The pieces of the template between its field tokens"""
	last = 0
	for match in _TOKEN_PATTERN.finditer(template):
		yield template[last:match.start()]
		last = match.end()
	yield template[last:]


def _invalid_text(text):
	"""The first thing in the text that has no place in a file name, as the
	user should see it: a path separator, a '..' (a path component that leaves
	the folder) or a control character such as a line break ('\\n'). None
	when there is nothing of the kind"""
	for index, char in enumerate(text):
		if char in "/\\":
			return char
		if text.startswith("..", index):
			return ".."
		if unicodedata.category(char) in _CONTROL_CATEGORIES:
			return repr(char)[1:-1]
	return None


def _is_safe_name(name):
	"""A rendered name is a single file or folder name: no path separator, no
	control character and not a '.' or '..' that points somewhere else"""
	return name not in (".", "..") and not any(
		char in "/\\" or unicodedata.category(char) in _CONTROL_CATEGORIES for char in name)


def validate_template(template):
	"""Raises TemplateError if the template is malformed. Returns the list of
	canonical fields used. The codes: empty, invalid_char (detail: the text
	that cannot be in a file name), nested_brackets, unbalanced_brackets,
	unbalanced_braces, unknown_field (detail: the field), padding_not_allowed
	(detail: the field) and padding_too_wide (detail: the token, 'episode.10')"""
	if not template.strip():
		raise TemplateError("empty")
	for text in _literal_text(template):
		invalid = _invalid_text(text)
		if invalid:
			raise TemplateError("invalid_char", invalid)
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
	for raw_name, padding in _TOKEN_PATTERN.findall(template):
		field = _resolve_field(raw_name)
		if field is None:
			raise TemplateError("unknown_field", raw_name.strip())
		if padding and field not in _PADDABLE_FIELDS:
			raise TemplateError("padding_not_allowed", raw_name.strip())
		if padding and int(padding) > _MAX_PADDING:
			raise TemplateError("padding_too_wide", f"{raw_name.strip()}.{padding}")
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
		padding = match.group(2)
		if padding:
			if int(padding) > _MAX_RENDER_PADDING:
				return None
			value = _apply_padding(value, int(padding))
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

DEFAULT_MOVIE_TEMPLATE = "{title}[ ({year})] - {resolution}[ {hdr}][.{extension}]"
DEFAULT_SERIES_TEMPLATE = "{season}x{episode.2} - {title}[ - {resolution}][ {hdr}][.{extension}]"
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


# 'Frieren - 12', 'One Piece - 1100': a number behind a dash ends the title of an
# episode with no season marker, which no template can name without its season.
# Also with the version of a fixed release ('Frieren - 12v2')
_UNMARKED_EPISODE = re.compile(r'\s[-–]\s*\d{1,4}(?:[vV]\d{1,2})?$')
# 'Show.S2024E01': a season numbered by its year, which the season of no
# template can hold (a renamed '2024x01' would not even read back as one)
_YEAR_SEASON_EPISODE = re.compile(r'(?<![A-Za-z0-9])[SsTt](?:19|20)\d{2}[\s._-]?[Ee]\d{1,3}(?!\d)')


def _render(template, fields, filename):
	# A name that drops what tells it apart from its siblings would give them
	# all the same suggestion
	if fields.get("ambiguous"):
		return None
	# Without a year, nothing tells a movie apart from such an episode
	if not fields["is_series"] and not fields["year"] and _UNMARKED_EPISODE.search(fields["title"]):
		return None
	# Nor is an episode of a season numbered by its year a movie
	if not fields["is_series"] and _YEAR_SEASON_EPISODE.search(fields["title"]):
		return None
	try:
		suggested = render_template(template, fields)
	except Exception:
		return None
	if not suggested or suggested == filename or not _is_safe_name(suggested):
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


def _inherit_from_parents(fields, parents, keys):
	"""Fills the fields the name lacks from its parents, nearest parent first:
	the season folder a file sits in knows its season better than a torrent
	named after the whole series (S01-S07)"""
	for key in keys:
		if fields[key]:
			continue
		for parent in parents:
			if parent[key]:
				fields[key] = parent[key]
				break


# Words that name a collection, not the show: a torrent with the whole series
# ('Friends.The.Complete.Series') would lend them to every episode inside.
# These hardly ever end a real title. 'Collection' and 'Pack' on their own
# often do ('Wolf Pack', 'The Collection'): see _collection_titles
_COLLECTION_SUFFIX = re.compile(
	r'(?:^|\s)(?:the\s+)?(?:complete\s+(?:series|collection)|box\s*set|(?:serie|colecci[oó]n)\s+completa'
	r'|complete|completa|integral)$', re.IGNORECASE)
_COLLECTION_TITLE_WORD = re.compile(r'(?:^|\s)(?:the\s+)?(?:collection|pack)$', re.IGNORECASE)

# What is left is no title on its own
_ARTICLES = {"", "the", "a", "an", "el", "la", "los", "las", "lo", "le", "les", "il", "der", "die", "das"}


def _collection_titles(title):
	"""The title followed by its shorter forms without the collection words at
	its end, the shortest last ('Friends The Complete Series', 'Friends'), as
	(form, guessed) pairs. guessed tells it took dropping a 'Collection' or
	'Pack', which may just as well be part of the title ('Wolf Pack'): only
	the outermost word, and never down to nothing but an article ('The
	Collection' stays as it is)"""
	forms = [(title, False)]
	while True:
		current, guessed = forms[-1]
		match = _COLLECTION_SUFFIX.search(current)
		word = not match and len(forms) == 1
		if word:
			match = _COLLECTION_TITLE_WORD.search(current)
		if not match:
			return forms
		rest = current[:match.start()].rstrip(" -–_.,")
		if _series_text(rest) in _ARTICLES:
			return forms
		forms.append((rest, guessed or word))


def _parents_title(parents, own_title="", series=False):
	"""Title for a name that has no year of its own: its own title is just the
	leftovers of the name (numbers, tags...). The torrent, the last parent, is
	the most reliable one. For an episode or a season, the collection words
	the parent carries are dropped. own_title, the title the name has before
	inheriting anything, tells which form is the show's ('Wolf.Pack.S01E01'
	inside 'Wolf.Pack.Collection'). Without one, a 'Collection' or 'Pack' stays
	where it is, as it may well belong to the title: '01.mkv' inside
	'Wolf.Pack.S01-S02' is an episode of Wolf Pack"""
	own = _series_text(own_title)
	for parent in reversed(parents):
		if parent["title"]:
			if not series:
				return parent["title"]
			forms = _collection_titles(parent["title"])
			if own:
				for form, _guessed in forms:
					if _series_text(form) == own:
						return form
			return [form for form, guessed in forms if not guessed][-1]
	return ""


def suggest_file_name(filename, parent_name=None, single_video=False, template_movie=None,
						template_series=None, template_season=None, season_prefix="T", folder_name=None):
	"""Suggests a name for a single video file inside a torrent. parent_name is
	the torrent name, used as context: the fields missing in the file are
	inherited from it (the season included, never the episode), so files named
	just '01.mkv' inside a season pack can still be renamed. folder_name is the
	folder holding the file when it is not the torrent folder itself (a season
	folder of a complete series), and is looked at before the torrent name.
	single_video tells whether the torrent holds just one video, which is what
	allows a file with no metadata of its own to take the whole torrent name.
	Returns None when the file cannot be identified, and for a sample clip
	(see is_sample_file), which would take the name of the real video"""
	if is_sample_file(f"{folder_name}/{filename}" if folder_name else filename):
		return None
	parents = [parse_metadata(name, season_prefix=season_prefix) for name in (folder_name, parent_name) if name]
	parent_is_series = any(parent["is_series"] for parent in parents)
	fields = parse_metadata(filename, season_prefix=season_prefix, allow_bare_episode=parent_is_series)
	own_year = fields["year"]
	own_title = fields["title"]
	# Its own year, or its own season and episode, tell the file apart from
	# the others with nothing taken from the parents
	self_named = bool(own_year or (fields["season"].isdigit() and fields["episode_number"]))

	if parents:
		# A parent that drops what tells it apart ('S04.Vol.2', whose files
		# may count from 01 again, or a dated show) cannot name its files
		if not self_named and any(parent["ambiguous"] for parent in parents):
			return None
		_inherit_from_parents(fields, parents, _INHERITABLE_FIELDS)
		if parent_is_series:
			fields["is_series"] = True
		if not own_year:
			title = _parents_title(parents, own_title, series=fields["is_series"])
			if title:
				fields["title"] = title
		if not fields["chapter"] and fields["season"] and "-" not in fields["season"]:
			if fields["episode_number"]:
				fields["chapter"] = f"{fields['season']}x{int(fields['episode_number']):02d}"
			else:
				fields["chapter"] = f"{season_prefix}{fields['season']}"

	_drop_series_title_from_episode_title(fields)

	if fields["is_series"]:
		# Every file needs its own episode and a single season, otherwise
		# several of them would collapse into the same name: an episode of
		# 'S01-S03' could belong to any of the three
		if not fields["episode_number"] or not fields["season"].isdigit():
			return None
	elif not single_video and not own_year:
		# One video among many with nothing identifying it: the torrent name
		# belongs to the pack, not to this particular file
		return None

	template = _pick_template(fields, template_movie, template_series, template_season)
	return _render(template, fields, filename)


def suggest_folder_name(folder_name, parent_name=None, template_movie=None, template_series=None,
						template_season=None, season_prefix="T"):
	"""Suggests a name for a folder inside a torrent, not the torrent folder
	itself. parent_name is the torrent name, which lends the title, year,
	resolution... the folder lacks ('Temporada 2' inside 'Mad Men (2007)').
	Only folders that identify something of their own are renamed: a season
	(the season pack template), an episode (the series template) or a movie
	with its year (the movie template). Anything else ('Extras', 'Subs') is
	left alone, so this returns None"""
	fields = parse_metadata(folder_name, season_prefix=season_prefix)
	own_year = fields["year"]
	own_title = fields["title"]
	# A folder name never carries an extension, whatever the dots suggest
	fields["extension"] = ""
	if fields["is_series"]:
		if not fields["season"] and not fields["episode_number"]:
			return None
	elif not own_year:
		return None

	if parent_name:
		parent = parse_metadata(parent_name, season_prefix=season_prefix)
		# The season is what tells two season folders apart: never inherited
		_inherit_from_parents(fields, [parent], [k for k in _INHERITABLE_FIELDS if k != "season"])
		if not own_year and parent["title"]:
			fields["title"] = _parents_title([parent], own_title, series=fields["is_series"])
	if not fields["title"]:
		return None

	_drop_series_title_from_episode_title(fields)
	template = _pick_template(fields, template_movie, template_series, template_season)
	return _render(template, fields, folder_name)


_SAMPLE_TOKEN = re.compile(r'(?:^|(?<=[\s._\-\[\]()]))samples?(?=$|[\s._\-\[\]()])', re.IGNORECASE)
_TRAILING_GROUP = re.compile(r'\s*-[A-Za-z0-9]{2,20}\s*$')


def _only_metadata(text):
	"""True when the text holds nothing but metadata from its start: the end
	of the name, the group ('-grp'), a metadata token or 'WEB' tags followed
	by one of them"""
	while True:
		following = _TAG_SEPARATORS.match(text).end()
		if following == len(text) or _TRAILING_GROUP.fullmatch(text):
			return True
		if any(pattern.match(text, following) for pattern in _META_PATTERNS):
			return True
		web = _BARE_WEB.match(text, following)
		if not web:
			return False
		text = text[web.end():]


def is_sample_file(path):
	"""True for the sample clip a release ships next to the real video:
	anything inside a Sample(s) folder, or a name with a 'sample' token that
	ends it ('show.s01e01-grp-sample', 'sample'), is followed only by
	metadata or the group ('movie.2019.sample.1080p', 'show.sample.WEB',
	'show.sample-grp') or sits in brackets ('[Sample]'). In front of the name
	it has to be glued to it ('sample-grp-show', 'sample_show.s01e01') or come
	before an episode ('Sample.Show.S01E01'). A title that merely has the word
	('Samples.of.Life.2020', 'Sample.People.2000') is no sample. path may hold
	folders, with either separator"""
	parts = [part for part in re.split(r'[/\\]', path or "") if part]
	if not parts:
		return False
	if any(part.lower() in ("sample", "samples") for part in parts[:-1]):
		return True
	stem = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
	for match in _SAMPLE_TOKEN.finditer(stem):
		rest = stem[match.end():]
		if _TAG_SEPARATORS.match(rest).end() == len(rest):
			return True
		if match.start() == 0:
			# A title made of words joined by '_' starts with one too
			# ('Samples_of_Life'), so the rest must be written another way
			if rest.lstrip().startswith("-") or (rest[:1] == "_" and "_" not in rest[1:]):
				return True
			if match.group().lower() == "sample" and _find_episode(rest):
				return True
			continue
		if stem[match.start() - 1] in "[(" and rest[:1] in "])":
			return True
		if _only_metadata(rest):
			return True
	return False


def companion_subtitle_name(subtitle_name, video_name, new_video_name):
	"""Returns the name a subtitle must take so it keeps matching its video
	after the video is renamed, preserving any language suffix and its own
	extension. Returns None when the subtitle does not belong to the video:
	its name has to be the video's, alone or followed by a separator ('01.es'
	goes with '01', '10' does not go with '1'), and what follows can not start
	with a number: 'Alien 3.srt' or 'Movie-2.srt' is the sequel's, never a
	language of 'Alien' or 'Movie'"""
	subtitle_stem, _, subtitle_ext = subtitle_name.rpartition(".")
	if not subtitle_stem or subtitle_ext.lower() not in SUBTITLE_EXTENSIONS:
		return None
	video_stem = _split_extension(video_name)[0]
	if not subtitle_stem.startswith(video_stem):
		return None
	suffix = subtitle_stem[len(video_stem):]
	if suffix and suffix[0] not in "._- ":
		return None
	if re.match(r'[._\- ]+\d+(?![A-Za-z])', suffix):
		return None
	new_stem = _split_extension(new_video_name)[0]
	return f"{new_stem}{suffix}.{subtitle_ext}"


# ---------------------------------------------------------------------------
# SERIES FOLDERS
# ---------------------------------------------------------------------------
# Where the other episodes of a series already are, so a new one can be sent
# to the same place. Two names are the same series when their titles match
# once case, accents and punctuation are left out: never a partial match,
# since "Chicago Fire" and "Chicago Med" share a word and are different shows

# A name that starts with the episode or season ('14x04 - Chicago Fire', the
# default series template) has nothing before the marker for the title
_LEADING_SERIES_MARKER = re.compile(
	r'^\s*(?:[SsTt]\d{1,2}(?:[\s._-]?[Ee]\d{1,3}(?:[\s._-]?-[\s._-]?[Ee]?\d{1,3})?)?(?:[\s._-]?-[\s._-]?[Ss]\d{1,2})?'
	r'|\d{1,2}[xX]\d{2,3}(?:-\d{2,3})?'
	r'|[Tt]\d{1,2}(?:-[Tt]?\d{1,2})?'
	r'|(?:[Tt]emporada|[Ss]eason)[\s._-]+\d{1,2})'
	r'(?=[\s._\-–\[(]|$)')

_PATH_SEPARATOR = re.compile(r'[/\\]')


def _series_text(text):
	"""The comparable form of a title: lowercase words without accents or
	punctuation, so 'Chicago.Fire' and 'chicago fire' are the same thing"""
	text = unicodedata.normalize("NFKD", text or "")
	text = "".join(char for char in text if not unicodedata.combining(char))
	return " ".join(re.findall(r'[a-z0-9]+', text.casefold()))


def _parse_series(name):
	fields = parse_metadata(name)
	if fields["is_series"] and not fields["title"]:
		stem, ext = _split_extension(name)
		marker = _LEADING_SERIES_MARKER.match(stem)
		if marker:
			rest = stem[marker.end():].lstrip(" -–._")
			if rest:
				# The marker goes behind the title, where releases carry it
				fields = parse_metadata(f"{rest} {marker.group().strip()}")
	return fields


@functools.lru_cache(maxsize=4096)
def series_identity(name):
	"""What tells a series apart: {"key", "title", "year", "season"}, or None
	when the name is not an episode or a season pack of a series. season is
	None for a pack of several seasons. Every torrent in the manager goes
	through here each time a suggestion is worked out, hence the cache"""
	if not name:
		return None
	if name.lower().endswith(".torrent"):
		name = name[:-len(".torrent")]
	fields = _parse_series(name)
	key = _series_text(fields["title"])
	if not fields["is_series"] or not key:
		return None
	season = int(fields["season"]) if fields["season"].isdigit() else None
	return {"key": key, "title": fields["title"], "year": fields["year"], "season": season}


def _same_series(identity, other_key, other_year):
	"""Same title, and the same year when both carry one: 'Doctor Who (2005)'
	is not 'Doctor Who (1963)', but a name without a year matches either"""
	if identity["key"] != other_key:
		return False
	return not (identity["year"] and other_year and identity["year"] != other_year)


def _split_last(path):
	"""(parent, last segment) of a path, keeping whatever separator it uses
	(a Windows Deluge reports backslashes) and dropping a trailing one"""
	path = path.rstrip("/\\")
	positions = [match.start() for match in _PATH_SEPARATOR.finditer(path)]
	if not positions:
		return "", path
	return path[:positions[-1]], path[positions[-1] + 1:]


def _folder_season(segment):
	"""(fields, season, start, end) for a folder of a single season and no
	episode, be it a bare season folder ('Temporada 2', 'Season 02', 'S02') or
	one named after the show too ('Chicago Fire - Temporada 13'). start and
	end delimit the number of the season, so it can be swapped. None for any
	other folder"""
	fields = parse_metadata(segment)
	if not fields["is_series"] or not fields["season"].isdigit() or fields["episode_number"]:
		return None
	season = int(fields["season"])
	for pattern in SEASON_ONLY_PATTERNS:
		for match in pattern.finditer(segment):
			if int(match.group(1)) == season:
				return fields, season, match.start(1), match.end(1)
	return None


def _season_folder(segment):
	"""The season number of a folder that only says the season ('Temporada 2',
	'Season 02', 'S02'), or None when the folder is anything else"""
	found = _folder_season(segment)
	if found and not _series_text(found[0]["title"]):
		return found[1]
	return None


def _names_series(path, identity):
	"""True when the folder is named after the series ('Chicago Fire',
	'Chicago Fire (2012)'), or is a season folder right inside one"""
	parent, last = _split_last(path)
	if _season_folder(last) is not None:
		parent, last = _split_last(parent)
	if not last:
		return False
	fields = parse_metadata(last)
	return _same_series(identity, _series_text(fields["title"]), fields["year"])


def _retarget_season(path, season, identity=None):
	"""Episodes of the previous season sit in 'Temporada 13' (or in 'Chicago
	Fire - Temporada 13'), but this one belongs in 'Temporada 14' next to it:
	the number is swapped keeping its padding ('Season 09' -> 'Season 10',
	'S13' -> 'S02', but 'Temporada 13' -> 'Temporada 2'). A pack of several
	seasons belongs in the folder holding the season folders. A folder named
	after part of another season ('Show S04 Vol 1') is no place for it: ""
	then. Any other folder (one named after another show included) is
	returned as it is, never raising"""
	path = path.rstrip("/\\")
	parent, last = _split_last(path)
	found = _folder_season(last)
	if found is None or not parent:
		return path
	fields, folder_season, start, end = found
	title = _series_text(fields["title"])
	if title and not (identity and _same_series(identity, title, fields["year"])):
		return path
	if folder_season == season:
		return path
	if season is None:
		return parent
	if fields["ambiguous"]:
		return ""
	# The compact markers ('S01', 'T13') always have a fixed width, and a
	# number with a leading zero shows the folders are padded. Any other is
	# written as it is: 'Temporada 2' already exists next to 'Temporada 13'
	old = last[start:end]
	width = len(old) if old.startswith("0") or (start and last[start - 1] in "SsTt") else 1
	rest = last[end:]
	# An English ordinal goes with its number: '1st Season' -> '2nd Season'
	suffix = re.match(r'(?:st|nd|rd|th)(?![A-Za-z])', rest, re.IGNORECASE)
	if suffix:
		new_suffix = _ordinal_suffix(season)
		rest = (new_suffix.upper() if suffix.group().isupper() else new_suffix) + rest[suffix.end():]
	return f"{path[:len(parent) + 1]}{last[:start]}{str(season).zfill(width)}{rest}"


def _ordinal_suffix(number):
	"""'st', 'nd', 'rd' or 'th', the English ordinal ending of the number"""
	if number % 100 in (11, 12, 13):
		return "th"
	return {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def series_dir_candidates(name, torrents, known_dirs=(), current_dir=None, generic_dirs=()):
	"""The folders the series of name already lives in, best first, as a list
	of (dir, named) where named tells the folder is called after the series.
	torrents are the other torrents of the manager as (name, download_dir,
	added timestamp); known_dirs are folders the bot knows of (favorites) that
	count when they are named after the series. generic_dirs (the default and
	the automatic download dirs) are where anything lands before being sorted,
	so they only count when named after the series.
	Nothing is suggested when the best folder is current_dir: the torrent is
	already where the series is, and a stray episode elsewhere is no reason
	to move it"""
	identity = series_identity(name)
	if identity is None:
		return []
	found = {}  # dir -> [named, episodes, latest]

	def consider(directory, episodes, added):
		directory = _retarget_season(directory, identity["season"], identity)
		if not directory:
			return
		entry = found.setdefault(directory, [_names_series(directory, identity), 0, 0])
		entry[1] += episodes
		entry[2] = max(entry[2], added or 0)

	for other_name, directory, added in torrents:
		other = series_identity(other_name)
		if directory and other and _same_series(identity, other["key"], other["year"]):
			consider(directory, 1, added)
	for directory in known_dirs:
		if directory and _names_series(directory.rstrip("/\\"), identity):
			consider(directory, 0, 0)

	generic = {d.rstrip("/\\") for d in generic_dirs if d}
	ranked = sorted(((d, e) for d, e in found.items() if e[0] or d not in generic),
					key=lambda item: (not item[1][0], -item[1][1], -item[1][2], item[0]))
	current = (current_dir or "").rstrip("/\\")
	if ranked and ranked[0][0] == current:
		return []
	return [(d, e[0]) for d, e in ranked if d != current]


def series_auto_dir(candidates):
	"""The folder a torrent can be moved to without asking: the only one named
	after the series. Any doubt (none, or two of them) means asking instead"""
	named = [directory for directory, is_named in candidates if is_named]
	return named[0] if len(named) == 1 else None
