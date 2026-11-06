"""Test battery for name_parser.py. Run with: python3 test_name_parser.py"""

from name_parser import (companion_subtitle_name, is_sample_file, parse_metadata, render_template, suggest_file_name,
						suggest_folder_name, series_identity, suggest_name, validate_template, TemplateError, _PLATFORM_TAGS,
						CANONICAL_FIELDS, DEFAULT_MOVIE_TEMPLATE, DEFAULT_SERIES_TEMPLATE, DEFAULT_SEASON_PACK_TEMPLATE)

FAILED = []


def check(description, actual, expected):
	if actual != expected:
		FAILED.append(f"{description}\n  expected: {expected!r}\n  actual:   {actual!r}")


def check_fields(filename, **expected):
	fields = parse_metadata(filename)
	for key, value in expected.items():
		check(f"{filename} [{key}]", fields[key], value)


# --- User's reference example -------------------------------------------
check_fields(
	"Minions.and.Monsters.2026.1080p.AMZN.WEB-DL.AAC2.0.H.264-HDZ.mkv",
	title="Minions and Monsters", year="2026", resolution="1080p", hdr="",
	chapter="", extension="mkv", video_codec="H.264", audio_codec="AAC 2.0",
	source="WEB-DL", group="HDZ", is_series=False,
)

# --- Movies ---------------------------------------------------------------
check_fields("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv",
	title="The Matrix", year="1999", resolution="1080p", video_codec="H.264", source="BluRay", group="GROUP")
check_fields("Interstellar (2014) 2160p HDR10 DTS-HD.MA 5.1 x265.mkv",
	title="Interstellar", year="2014", resolution="4K", hdr="HDR", audio_codec="DTS-HD 5.1", video_codec="H.265")
check_fields("Dune.Part.Two.2024.UHDRemux.DV.TrueHD.Atmos.Castellano.mkv",
	title="Dune Part Two", year="2024", resolution="UHDRemux", hdr="HDR", audio_codec="TrueHD Atmos", language="Castellano")
check_fields("J.F.K.1991.720p.mkv", title="J.F.K", year="1991", resolution="720p")
check_fields("20.000.Leguas.De.Viaje.Submarino.1954.DVDRip.avi",
	title="20.000 Leguas De Viaje Submarino", year="1954", source="DVDRip", extension="avi")
check_fields("S.W.A.T.S01E01.1080p.mkv", title="S.W.A.T", chapter="1x01")
# A single letter followed by a whole word is a separator, not an acronym
check_fields("Viernes.13.Parte.V.Un.nuevo.comienzo.1985.1080p.BluRay.x265-TMd.mkv",
	title="Viernes 13 Parte V Un nuevo comienzo", year="1985", resolution="1080p")
# 'Cap.NNNN' that looks like a year is junk only when another year is present
check_fields("Viernes.13.Parte.V.Un.nuevo.comienzo.1985.capitulo.1984.mHD.10Bits.1080p.BluRay.DD2.0.x265-TMd.mkv",
	title="Viernes 13 Parte V Un nuevo comienzo", year="1985", chapter="", resolution="1080p")
check_fields("Los.Simpson.1989.Capitulo.1901.1080p.mkv", title="Los Simpson", year="1989", chapter="")
check_fields("Serie.Capitulo.1901.1080p.mkv", title="Serie", chapter="19x01", year="")
check_fields("Serie.Capitulo.2001.1080p.mkv", title="Serie", chapter="20x01", year="")
check_fields("Serie.Capitulo.1984.1080p.mkv", title="Serie", chapter="19x84", year="")
check_fields("Serie.Capitulo.1899.1080p.mkv", title="Serie", chapter="18x99", year="")
check_fields("Serie.Capitulo.105.1080p.mkv", title="Serie", chapter="1x05")
check_fields("Serie.Capitulo.512.1080p.mkv", title="Serie", chapter="5x12")
check_fields("La.Que.Se.Avecina.Capitulo.1301.1080p.mkv", title="La Que Se Avecina", chapter="13x01")
check_fields("Movie.Without.Year.1080p.mkv", year="", title="Movie Without Year")
check_fields("Pelicula.2023.BDRemux.EAC3.5.1.Latino.mkv",
	title="Pelicula", year="2023", resolution="BDRemux", audio_codec="EAC3 5.1", language="Latino")

# --- Series ----------------------------------------------------------------
check_fields("Breaking.Bad.S01E03.720p.HDTV.x264.mkv",
	title="Breaking Bad", chapter="1x03", season="1", episode_number="3",
	resolution="720p", source="HDTV", is_series=True)
check_fields("La.Casa.de.Papel.3x05.1080p.WEB-DL.Castellano.mkv",
	title="La Casa de Papel", chapter="3x05", language="Castellano", is_series=True)
check_fields("Serie Cap.103 HDTV.avi", title="Serie", chapter="1x03", season="1", episode_number="3")
# T for temporada, as Spanish releases write it
check_fields("El.Pueblo (2019) T01E03 AMZN WEBDL 1080p ESP DD5.1.SUB x264 HDBart.mkv",
	title="El Pueblo", year="2019", chapter="1x03", season="1", episode_number="3", is_series=True)
check_fields("El.Pueblo.T02E05-E08.1080p", title="El Pueblo", season="2", episode_number="5-8", chapter="2x05-08")
check_fields("El.Pueblo.T01-T03.1080p", title="El Pueblo", season="1-3", chapter="T1-T3")
check_fields("T01E03 - El Pueblo.mkv", chapter="1x03")
# The T has to start a word, it is not the end of the title
check_fields("Lost01E01.mkv", chapter="", is_series=False)
check("a T01E03 file inside a season pack folder", suggest_file_name(
	"El.Pueblo (2019) T01E03 AMZN WEBDL 1080p ESP DD5.1.SUB x264 HDBart.mkv", parent_name="El Pueblo S01 (2019)",
	template_series="{temporada}x{episode.2} - {title}[ - {titulo_episodio}][.{extension}]"), "1x03 - El Pueblo.mkv")
check_fields("Otra.Serie.Capitulo.1204.mp4", chapter="12x04", season="12", episode_number="4")
check_fields("Show.Name.S02.Complete.1080p.mkv",
	title="Show Name", season="2", chapter="T2", episode_number="", is_series=True)
check_fields("Serie.Temporada.2.Completa.720p.mkv", season="2", chapter="T2", is_series=True)

# --- User battery -----------------------------------------------------------
check_fields("Muertos.S.L.S04.1080p.NF.WEB-DL.DD+5.1.AV1-TSeD",
	title="Muertos S.L", chapter="T4", season="4", resolution="1080p",
	audio_codec="EAC3 5.1", video_codec="AV1", source="WEB-DL", group="TSeD", is_series=True)
check_fields("Mi.pie.izquierdo.1989.mHD.BluRay.DD2.0.x264-TMd.mkv",
	title="Mi pie izquierdo", year="1989", resolution="mHD", audio_codec="AC3 2.0", group="TMd")
check_fields("Metalocalipsis (2006) S03 1080p MAX WEB-DL DD+2.0 HEVC-HDZ",
	title="Metalocalipsis", year="2006", chapter="T3", resolution="1080p", is_series=True)
check_fields("La.noche.mas.oscura.2012.mHD.10Bits.1080p.BluRay.DD5.1.AV1-TMd.mkv",
	title="La noche mas oscura", year="2012", resolution="1080p", video_codec="AV1")
check_fields("Blade.Runner.2049.2017.2160p.UHD.BluRay.HDR.DTS-HD.MA.5.1.HEVC-Group",
	title="Blade Runner 2049", year="2017", resolution="4K", hdr="HDR", audio_codec="DTS-HD 5.1")
check_fields("Interstellar.2014.1080p.BluRay.DTS.x264-Group.mkv",
	title="Interstellar", year="2014", audio_codec="DTS", video_codec="H.264")
check_fields("Dune.Parte.Dos.2024.1080p.MAX.WEB-DL.DDP5.1.Atmos.H.264-Group",
	title="Dune Parte Dos", year="2024", audio_codec="EAC3 5.1 Atmos", video_codec="H.264")
check_fields("El.Codigo.Da.Vinci.TRILOGIA.2006.-.2016.Version.Extendida.mHD.10Bits.1080p.BluRay.DD5.1.AV1-TMd",
	title="El Codigo Da Vinci TRILOGIA", year="2006-2016", resolution="1080p")
check_fields("Star.Wars.Saga.Completa.1977-2019.1080p.BluRay.MULTi.DD5.1.x265-Group",
	title="Star Wars Saga Completa", year="1977-2019", language="Multi")
check_fields("House.M.D.S01-S08.1080p.BluRay.MULTi.AAC.2.0.x265-GRP",
	title="House M.D", chapter="T1-T8", season="1-8", audio_codec="AAC 2.0", is_series=True)
check_fields("The.Boys.S04E01-E08.1080p.AMZN.WEB-DL.DDP5.1.Atmos.HEVC-GRP",
	title="The Boys", chapter="4x01-08", season="4", episode_number="1-8",
	audio_codec="EAC3 5.1 Atmos", is_series=True)
check_fields("The.Office.US.S03E12.1080p.NF.WEB-DL.ENG.ESP.DDP5.1.SUBS.HEVC-GRP",
	title="The Office US", chapter="3x12", language="Castellano", is_series=True)
check_fields("Terminator.2.1991.4K.REMASTERED.2160p.UHD.BluRay.DV.HDR10.DTS-HD.MA.5.1.HEVC-GRP.mkv",
	title="Terminator 2", year="1991", resolution="4K", hdr="HDR", audio_codec="DTS-HD 5.1")
check_fields("Spider-Man.No.Way.Home.2021.2160p.UHD.BluRay.10Bits.DV.HDR.DTS-X.HEVC-GRP.mkv",
	title="Spider-Man No Way Home", year="2021", audio_codec="DTS-X", hdr="HDR")

# --- Template engine --------------------------------------------------------
movie_fields = parse_metadata("Minions.and.Monsters.2026.1080p.AMZN.WEB-DL.AAC2.0.H.264-HDZ.mkv")
check("default movie template",
	render_template("{title} ({year}) - {resolution}[ {hdr}][.{extension}]", movie_fields),
	"Minions and Monsters (2026) - 1080p.mkv")
check("user example template",
	render_template("{titulo} ({año}) - {resolucion}[ {hdr}][ - {codec_video}].{extension}", movie_fields),
	"Minions and Monsters (2026) - 1080p - H.264.mkv")

series_fields = parse_metadata("Breaking.Bad.S01E03.720p.HDTV.x264.mkv")
check("default series template",
	render_template("{chapter} - {title}[ - {resolution}][ {hdr}][.{extension}]", series_fields),
	"1x03 - Breaking Bad - 720p.mkv")
check("episode without padding is the raw number",
	render_template("{season}x{episode} - {title}", series_fields),
	"1x3 - Breaking Bad")
check("spanish series aliases",
	render_template("{capitulo} - {titulo}.{extension}", series_fields),
	"1x03 - Breaking Bad.mkv")

# --- Zero-padded season/episode ({field.N}) ----------------------------------
# Fields used across this whole section
big_episode_fields = parse_metadata("Show.Name.S12E123.mkv")
season_range_fields = parse_metadata("House.M.D.S01-S08.1080p.BluRay.MULTi.AAC.2.0.x265-GRP.mkv")
episode_range_fields = parse_metadata("The.Boys.S04E01-E08.1080p.AMZN.WEB-DL.DDP5.1.Atmos.HEVC-GRP.mkv")


def render(template, **fields):
	"""Shortcut to render_template with a synthetic fields dict, for testing
	the padding math in isolation without going through parse_metadata"""
	return render_template(template, fields)


# --- Basic padding, single-digit values --------------------------------------
check("season/episode padding together (spanish aliases)",
	render_template("{temporada.2}x{episodio.3}", series_fields),
	"01x003")
check("season/episode padding together (english aliases)",
	render_template("{season.2}x{episode.3}", series_fields),
	"01x003")
check("episode padding width 1 == natural width",
	render_template("{episodio.1}", series_fields), "3")
check("episode padding width 2",
	render_template("{episodio.2}", series_fields), "03")
check("episode padding width 3",
	render_template("{episodio.3}", series_fields), "003")
check("episode padding width 5",
	render_template("{episodio.5}", series_fields), "00003")
check("season padding width 1 == natural width",
	render_template("{temporada.1}", series_fields), "1")
check("season padding width 2",
	render_template("{temporada.2}", series_fields), "01")
check("season padding width 4",
	render_template("{temporada.4}", series_fields), "0001")
check("explicit .2 reproduces the old fixed-width behavior",
	render_template("{season}x{episode.2} - {title}", series_fields),
	"1x03 - Breaking Bad")

# --- Padding never truncates a longer number ---------------------------------
check("episode.1 on a 3-digit episode keeps all digits",
	render_template("{episodio.1}", big_episode_fields), "123")
check("episode.2 on a 3-digit episode keeps all digits",
	render_template("{episodio.2}", big_episode_fields), "123")
check("episode.3 on a 3-digit episode is a no-op",
	render_template("{episodio.3}", big_episode_fields), "123")
check("episode.4 on a 3-digit episode adds one zero",
	render_template("{episodio.4}", big_episode_fields), "0123")
check("season.1 on a 2-digit season keeps both digits",
	render_template("{temporada.1}", big_episode_fields), "12")
check("season.3 on a 2-digit season adds one zero",
	render_template("{temporada.3}", big_episode_fields), "012")

# --- Ranges: each side padded independently -----------------------------------
check("season range, width == natural width is a no-op",
	render_template("{season.1}", season_range_fields), "1-8")
check("season range padding width 2",
	render_template("{season.2}", season_range_fields), "01-08")
check("season range padding width 3",
	render_template("{season.3}", season_range_fields), "001-008")
check("episode range, width == natural width is a no-op",
	render_template("{episode.1}", episode_range_fields), "1-8")
check("episode range padding width 2",
	render_template("{episode.2}", episode_range_fields), "01-08")
check("episode range padding width 3",
	render_template("{episode.3}", episode_range_fields), "001-008")
check("chapter built from a padded range keeps the fixed 2-digit width",
	render_template("{chapter}", episode_range_fields), "4x01-08")

# --- Synthetic edge cases (bypass parse_metadata entirely) --------------------
check("uneven range widths are padded independently",
	render("{season.3}", season="1-12"), "001-012")
check("uneven range widths, smaller target width",
	render("{season.2}", season="1-12"), "01-12")
check("padding width 0 is a no-op",
	render("{episode.0}", episode_number="7"), "7")
check("missing required paddable field still yields None",
	render("{season.2}", season=""), None)
check("same field rendered twice with the same width",
	render("{episode.2}-{episode.2}", episode_number="5"), "05-05")
check("same field rendered twice with different widths",
	render("{episode.1}-{episode.3}", episode_number="5"), "5-005")
check("field name with stray whitespace still resolves with padding",
	render("{ temporada .3}", season="1"), "001")
check("field name is case-insensitive with padding",
	render("{TEMPORADA.2}", season="1"), "01")
check("large padding width",
	render("{episode.10}", episode_number="7"), "0000000007")

# --- Padding inside optional blocks -------------------------------------------
check("padded field present inside an optional block",
	render_template("{title}[ - {episode.3}]", series_fields),
	"Breaking Bad - 003")
check("padded field absent drops the whole optional block",
	render_template("{title}[ - {episode.3}]", movie_fields),
	"Minions and Monsters")

# --- End-to-end through suggest_name / suggest_file_name ----------------------
check("suggest_name with padded custom template",
	suggest_name("Breaking.Bad.S01E03.720p.HDTV.x264.mkv",
		template_series="{season.2}x{episode.3} - {title}[ - {resolution}][.{extension}]"),
	"01x003 - Breaking Bad - 720p.mkv")
check("suggest_file_name: bare episode inside a season pack, padded",
	suggest_file_name("01.mkv", parent_name="Show.Name.S02.Complete.1080p.WEB-DL",
		template_series="{season.3}x{episode.3} - {title}[ - {resolution}][.{extension}]"),
	"002x001 - Show Name - 1080p.mkv")
check("suggest_name with padded episode range",
	suggest_name("The.Boys.S04E01-E08.1080p.AMZN.WEB-DL.DDP5.1.Atmos.HEVC-GRP.mkv",
		template_series="{season}x{episode.3} - {title}"),
	"4x001-008 - The Boys")
check("suggest_name with padded season-only pack",
	suggest_name("House.M.D.S01-S08.1080p.BluRay.MULTi.AAC.2.0.x265-GRP.mkv",
		template_season="{season.2} - {title}"),
	"01-08 - House M.D")

# Required field missing -> None
no_year = parse_metadata("Movie.Without.Year.1080p.mkv")
check("missing required year", render_template("{title} ({year})", no_year), None)
# Optional block with missing field disappears (no leftover spaces/dashes)
check("optional hdr dropped",
	render_template("{title} - {resolution}[ {hdr}]", movie_fields),
	"Minions and Monsters - 1080p")

# --- Template validation ----------------------------------------------------
for bad, code in [("{title} [unclosed", "unbalanced_brackets"), ("{title} ]bad[", "unbalanced_brackets"),
					("{title} {bad_field}", "unknown_field"), ("no fields at all", "empty"),
					("", "empty"), ("{title} {year", "unbalanced_braces"),
					("{title.3}", "padding_not_allowed"), ("{chapter.2}", "padding_not_allowed"),
					# Malformed padding syntax: no digit after the dot, a
					# negative number, or two dots. None of these fully match
					# a token, so they surface as unbalanced/stray braces
					("{episodio.}", "unbalanced_braces"), ("{episodio.-1}", "unbalanced_braces"),
					("{episodio.1.2}", "unbalanced_braces")]:
	try:
		validate_template(bad)
		check(f"validate({bad!r}) should fail", "no error", code)
	except TemplateError as e:
		check(f"validate({bad!r}) error code", e.code, code)
check("validate ok", validate_template("{titulo} ({año})[ {hdr}]"), ["title", "year", "hdr"])
check("validate ok with padding (spanish)",
	validate_template("{temporada.2}x{episodio.3}"), ["season", "episode_number"])
check("validate ok with padding (english)",
	validate_template("{season.2}x{episode.3}"), ["season", "episode_number"])
check("validate ok, canonical episode_number alias with padding",
	validate_template("{episode_number.2}"), ["episode_number"])

# Systematically confirm padding is rejected on every field except season and
# episode_number -- if a future field is added to CANONICAL_FIELDS without
# updating _PADDABLE_FIELDS this loop will pass it silently instead of raising,
# which is the failure mode we most want to catch here
_PADDABLE = {"season", "episode_number"}
for field in sorted(CANONICAL_FIELDS - _PADDABLE):
	try:
		validate_template("{" + field + ".2}")
		check(f"padding on {{{field}.2}} should be rejected", "no error", "padding_not_allowed")
	except TemplateError as e:
		check(f"padding on {{{field}.2}} error code", e.code, "padding_not_allowed")
		check(f"padding on {{{field}.2}} error detail", e.detail, field)
# ...and confirm both paddable fields are actually accepted (the loop above
# would stay green even if _PADDABLE_FIELDS were accidentally emptied)
for field in sorted(_PADDABLE):
	check(f"padding on {{{field}.2}} is accepted",
		validate_template("{" + field + ".2}"), [field])
check("validate ok with padding", validate_template("{temporada.2}x{episodio.3}"), ["season", "episode_number"])

# --- suggest_name high level -------------------------------------------------
check("suggest movie", suggest_name("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv"),
	"The Matrix (1999) - 1080p.mkv")
check("suggest series", suggest_name("Breaking.Bad.S01E03.720p.HDTV.x264.mkv"),
	"1x03 - Breaking Bad - 720p.mkv")
check("suggest none without year", suggest_name("Some.Random.File.mkv"), None)
check("suggest season pack", suggest_name("Show.Name.S02.Complete.1080p.mkv"),
	"T2 - Show Name - 1080p.mkv")
check("suggest with custom template",
	suggest_name("Pelicula.2023.BDRemux.EAC3.5.1.Latino.mkv", template_movie="{titulo} ({año}) [{resolucion}]"),
	"Pelicula (2023) BDRemux")

# --- suggest_file_name: files inside a torrent -------------------------------
SEASON_PACK = "Show.Name.S02.Complete.1080p.WEB-DL"

# Bare episode numbers only work because the torrent says it is a season pack
check("bare episode 01", suggest_file_name("01.mkv", parent_name=SEASON_PACK),
	"2x01 - Show Name - 1080p.mkv")
check("bare episode E05", suggest_file_name("E05.mkv", parent_name=SEASON_PACK),
	"2x05 - Show Name - 1080p.mkv")
check("bare episode [07]", suggest_file_name("[07].mkv", parent_name=SEASON_PACK),
	"2x07 - Show Name - 1080p.mkv")
check("bare episode ep12", suggest_file_name("ep12.mkv", parent_name=SEASON_PACK),
	"2x12 - Show Name - 1080p.mkv")
check("bare episode dash", suggest_file_name("Show - 03.mkv", parent_name=SEASON_PACK),
	"2x03 - Show Name - 1080p.mkv")
# Own episode marker wins, the rest is inherited
check("own episode marker", suggest_file_name("Show.S02E09.720p.mkv", parent_name=SEASON_PACK),
	"2x09 - Show Name - 720p.mkv")
# No episode at all inside a series: renaming would collapse every file
check("series file without episode", suggest_file_name("random.mkv", parent_name=SEASON_PACK), None)
check("series extras without episode", suggest_file_name("extras.mkv", parent_name=SEASON_PACK), None)
# The episode is never inherited from the torrent
check("episode not inherited", suggest_file_name("bonus.mkv", parent_name="Show.Name.S02E04.1080p"), None)
# Bare numbers are ignored when the torrent is not a series
check("bare episode without series context",
	suggest_file_name("01.mkv", parent_name="Code.Geass.R2.2008.1080p.WEB-DL"), None)
# Movies: a single video takes the whole torrent name, one of many does not
check("single video takes torrent name",
	suggest_file_name("video.mkv", parent_name="The.Matrix.1999.1080p.BluRay.x264-GROUP", single_video=True),
	"The Matrix (1999) - 1080p.mkv")
check("one video among many without metadata",
	suggest_file_name("video.mkv", parent_name="The.Matrix.1999.1080p.BluRay.x264-GROUP", single_video=False),
	None)
check("file with its own year inside a pack",
	suggest_file_name("Otra.Peli.2001.720p.mkv", parent_name="Coleccion", single_video=False),
	"Otra Peli (2001) - 720p.mkv")
check("no parent behaves like suggest_name",
	suggest_file_name("The.Matrix.1999.1080p.mkv"), "The Matrix (1999) - 1080p.mkv")
check("spanish season prefix",
	suggest_file_name("01.mkv", parent_name="Serie.Temporada.2.1080p", season_prefix="T"),
	"2x01 - Serie - 1080p.mkv")

# --- episode_title -----------------------------------------------------------
# The text between the episode marker and the metadata tokens
check_fields("The.Wire.S01E03.The.Buys.720p.BluRay.x264.mkv", episode_title="The Buys")
check_fields("Chernobyl S01E02 Please Remain Calm 2160p HDR.mkv", episode_title="Please Remain Calm")
check_fields("Los.Simpson.5x08.Bart.el.Asesino.avi", episode_title="Bart el Asesino")
# Nothing after the marker
check_fields("Breaking.Bad.S01E03.720p.HDTV.x264.mkv", episode_title="")
check_fields("Family.Guy.S01E01.1080p.WEB-DL.x264-de4d.mkv", episode_title="")
# Release junk is not a title: a lone token mixing letters and digits
check_fields("The Office US S03E10 1of2.mkv", episode_title="")
# Season packs and movies never have one
check_fields("Show.Name.S02.Complete.1080p.mkv", episode_title="")
check_fields("The.Matrix.1999.1080p.BluRay.x264.mkv", episode_title="")
# Platform tags are metadata, never part of the episode title
check_fields("Estafadores.de.Tokio.2024.S01E07.Episodio 7.NF.WEB-DL.1080P.ESP.JAP.EAC3 5.1.SUB-Txv2.mkv",
	title="Estafadores de Tokio", chapter="1x07", episode_title="Episodio 7", year="2024")
check_fields("Serie.S01E01.Episodio 1.MAX.WEB-DL.1080p.mkv", episode_title="Episodio 1")
check_fields("Show.S01E02.The.Buys.AMZN.WEB-DL.1080p.mkv", episode_title="The Buys")
check_fields("Show.S01E02.The.Buys.DSNP.WEB-DL.1080p.mkv", episode_title="The Buys")
check_fields("Show.S01E02.The.Buys.NFLX.WEB-DL.1080p.mkv", episode_title="The Buys")
check_fields("Show.S01E02.Episodio 3.ITVX.WEB-DL.1080p.mkv", episode_title="Episodio 3")
check_fields("Serie.S01E01.Capitulo.1.MOVISTAR.WEB-DL.1080p.mkv", episode_title="Capitulo 1")
check_fields("Serie.S01E01.Capitulo.1.ATRESPLAYER.WEB-DL.1080p.mkv", episode_title="Capitulo 1")
check_fields("Doctor.Who.S01E01.Rose.BBC.WEB-DL.1080p.mkv", episode_title="Rose")
check_fields("La.Casa.de.Papel.S01E01.El.Plan.RTVE.1080p.mkv", episode_title="El Plan")
check_fields("Love.is.in.the.air.S01E011.MITL.WEBDL.720p.ESP.AAC2.0.x264-Kowalski.mkv",
	title="Love is in the air", chapter="1x11", episode_title="", resolution="720p")
# Words that merely start with a tag are left untouched
check_fields("Show.S01E02.The.Plexus.WEB-DL.1080p.mkv", episode_title="The Plexus")
check_fields("Show.S01E02.Bravo.Team.WEB-DL.1080p.mkv", episode_title="Bravo Team")
check_fields("Show.S01E02.Ite.Missa.WEB-DL.1080p.mkv", episode_title="Ite Missa")
check_fields("Show.S01E02.Mgm.Studios.WEB-DL.1080p.mkv", episode_title="Mgm Studios")
# Chained tags before the metadata
check_fields("Show.S01E02.The.Buys.NF.DSNP.WEB-DL.1080p.mkv", episode_title="The Buys")
check_fields("Show.S01E02.Episodio 7.MAX.1080p.mkv", episode_title="Episodio 7")
# A tag not followed by metadata is an ordinary word, not a tag
check_fields("Show.S01E02.Max.mkv", episode_title="Max")
check_fields("South.Park.S01E02.Stan.mkv", episode_title="Stan")
# A parenthesis group cut in half by the metadata leaves nothing behind
check_fields("Stuart no consigue salvar el universo 01x05 - Spoiler Bert se casa (HD 1080p x265).mkv",
	title="Stuart no consigue salvar el universo", chapter="1x05",
	episode_title="Spoiler Bert se casa", resolution="1080p", video_codec="H.265")
check_fields("Serie.S01E02.The.Buys.(HD.1080p.x265).mkv", episode_title="The Buys")
# A closed group is kept as it is
check_fields("Serie.S01E02.The.Buys.(Extended).1080p.mkv", episode_title="The Buys (Extended)")
# The same letters inside a word are left untouched
check_fields("Show.S01E02.The.Maximum.Effort.WEB-DL.1080p.mkv", episode_title="The Maximum Effort")
check_fields("Show.S01E02.Crash.Landing.WEB-DL.1080p.mkv", episode_title="Crash Landing")
check_fields("Show.S01E02.Stanley.and.Nfl.WEB-DL.1080p.mkv", episode_title="Stanley and Nfl")
check_fields("Mad.Max.Fury.Road.2015.1080p.BluRay.x264.mkv", title="Mad Max Fury Road", year="2015")
# Non numeric resolution aliases are metadata, never part of the episode title
check_fields("13x01-FullHD1080p.mkv", episode_title="", resolution="1080p")
check_fields("13x02-FHD.mkv", episode_title="", resolution="1080p")
check_fields("13x03-WEBDL-FullHD1080p.mkv", episode_title="", resolution="1080p")
check_fields("13x04-FullHD-WEB-DL-1080p.mkv", episode_title="", resolution="1080p")
check_fields("Serie.S01E05.Full.HD.mkv", episode_title="", resolution="1080p")
check_fields("Serie.S01E06.El.Titulo.HD.mkv", episode_title="El Titulo", resolution="HD")
# The same letters inside other tokens are left untouched
check_fields("Serie.S01E01.1080p.TrueHD.Atmos.mkv", resolution="1080p", audio_codec="TrueHD Atmos")
check_fields("Serie.S01E01.1080p.DTS-HD.MA.5.1.mkv", resolution="1080p", audio_codec="DTS-HD 5.1")
check_fields("Serie.S01E01.HDTV.x264.mkv", resolution="", source="HDTV")
check_fields("Peli.2020.2160p.UHD.BluRay.HDR.mkv", title="Peli", resolution="4K", hdr="HDR")
check_fields("Serie.S01E02.mHD.mkv", resolution="mHD")
# Every declared platform tag stays out of the episode title, and every
# resolution alias is recognised, so the lists cannot grow untested
for tag in _PLATFORM_TAGS:
	check_fields(f"Show.S01E02.The.Buys.{tag}.WEB-DL.1080p.mkv", episode_title="The Buys")
for alias, expected in [("FullHD", "1080p"), ("FHD", "1080p"), ("HD", "HD"), ("mHD", "mHD"),
		("720p", "720p"), ("1080p", "1080p"), ("2160p", "4K"), ("4K", "4K")]:
	check_fields(f"Show.S01E02.The.Buys.{alias}.mkv", episode_title="The Buys", resolution=expected)

check("platform tag out of the rendered episode title",
	suggest_name("Estafadores.de.Tokio.2024.S01E07.Episodio 7.NF.WEB-DL.1080P.ESP.JAP.EAC3 5.1.SUB-Txv2.mkv",
		template_series="{season}x{episode.2} - {title} - {episode_title}[.{extension}]"),
	"1x07 - Estafadores de Tokio - Episodio 7.mkv")

EPISODE_TITLE_TEMPLATE = "{season}x{episode.2} - {title}[ - {episode_title}][.{extension}]"
# Bare episodes inside a season pack keep the title of the episode
check("bare episode keeps its title",
	suggest_file_name("E1 - Death Has a Shadow.mkv", parent_name="Family Guy - S1",
		template_series=EPISODE_TITLE_TEMPLATE),
	"1x01 - Family Guy - Death Has a Shadow.mkv")
# Optional block disappears when the file does not carry one
check("no episode title drops the block",
	suggest_file_name("01.mkv", parent_name="Family Guy - S1", template_series=EPISODE_TITLE_TEMPLATE),
	"1x01 - Family Guy.mkv")
# Never inherited: otherwise every file would repeat the first episode title
check("episode title not inherited",
	suggest_file_name("E2.mkv", parent_name="Show.Name.S02E01.The.Buys.1080p",
		template_series=EPISODE_TITLE_TEMPLATE),
	"2x02 - Show Name.mkv")
# Templates without the field are unaffected
check("default template ignores episode title",
	suggest_file_name("E1 - Death Has a Shadow.mkv", parent_name="Family Guy - S1"),
	"1x01 - Family Guy.mkv")
# A file named after its own series carries no episode title
check("series name is not an episode title",
	suggest_file_name("2x01 - Succession [x265].mkv", parent_name="Succession - Temporada 2",
		template_series=EPISODE_TITLE_TEMPLATE),
	"2x01 - Succession.mkv")
check("series name is not an episode title without parent",
	suggest_name("Succession.2x01.Succession.1080p.mkv", template_series=EPISODE_TITLE_TEMPLATE),
	"2x01 - Succession.mkv")
# Renaming twice does not repeat the series name
check("already renamed file is left alone",
	suggest_file_name("13x05 - La que se avecina - El titulo.mkv",
		parent_name="La que se avecina - Temporada 13", template_series=EPISODE_TITLE_TEMPLATE),
	None)
check("series name prefix dropped from the episode title",
	suggest_file_name("13x05 - La que se avecina - El titulo.mkv",
		parent_name="La que se avecina - Temporada 13",
		template_series="{season}x{episode.2}. {title}[ - {episode_title}][.{extension}]"),
	"13x05. La que se avecina - El titulo.mkv")
# A title that merely starts with the series name keeps its own text
check("episode title starting like the series is kept",
	suggest_file_name("1x02 - Succession Day.mkv", parent_name="Succession - Temporada 1",
		template_series=EPISODE_TITLE_TEMPLATE),
	"1x02 - Succession - Succession Day.mkv")

# --- suggest_folder_name: folders inside a complete series --------------------
SERIES = "Mad Men (2007) - 1080p"
check("season folder with a release name",
	suggest_folder_name("Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1.ESP.AAC 2.0.ING.EAC3 5.1-Txv2", SERIES),
	"T1 - Mad Men - 1080p")
check("bare season folder takes the title from the torrent", suggest_folder_name("Temporada 2", SERIES),
	"T2 - Mad Men - 1080p")
check("english season folder", suggest_folder_name("Season 3", SERIES), "T3 - Mad Men - 1080p")
check("short season folder", suggest_folder_name("S04", SERIES), "T4 - Mad Men - 1080p")
check("season folder with the english prefix", suggest_folder_name("Season 3", SERIES, season_prefix="S"),
	"S3 - Mad Men - 1080p")
check("season folder follows the season template",
	suggest_folder_name("Temporada 2", SERIES, template_season="{title} - Temporada {season.2}"),
	"Mad Men - Temporada 02")
check("episode folder uses the series template without extension",
	suggest_folder_name("Mad.Men.S05E01.1080p.WEB-DL", SERIES), "5x01 - Mad Men - 1080p")
check("movie folder of a saga", suggest_folder_name("El.Padrino.1972.1080p.BluRay", "Trilogia El Padrino"),
	"El Padrino (1972) - 1080p")
check("a dotted folder name is not an extension",
	suggest_folder_name("Show.S01.mkv", "Show (2020) - 1080p"), "T1 - Show - 1080p")
check("extras folder is left alone", suggest_folder_name("Extras", SERIES), None)
check("subtitles folder is left alone", suggest_folder_name("Subs", SERIES), None)
check("an already renamed folder gives no suggestion", suggest_folder_name("T1 - Mad Men - 1080p", SERIES), None)
check("a season folder without any title anywhere", suggest_folder_name("Temporada 2"), None)

# The season folder knows the season better than a torrent named after all of them
check("the season comes from the folder, not the S01-S07 torrent",
	suggest_file_name("01.mkv", parent_name="Mad Men S01-S07 1080p", folder_name="Temporada 3"),
	"3x01 - Mad Men - 1080p.mkv")
check("the title still comes from the torrent",
	suggest_file_name("01.mkv", parent_name="Mad Men (2007) - 1080p", folder_name="Temporada 3"),
	"3x01 - Mad Men - 1080p.mkv")

# --- Download site prefixes ----------------------------------------------------
check_fields("www.DonTorrent.xyz - Gladiator II (2024) [BluRay 1080p].mkv", title="Gladiator II", year="2024")
check_fields("[ www.TorrentDay.com ] - Gladiator.II.2024.1080p.WEB-DL.mkv", title="Gladiator II", year="2024")
check_fields("[www.site.org] Gladiator II (2024) 1080p.mkv", title="Gladiator II", year="2024")
check_fields("(www.site.net) - Gladiator II (2024) 1080p.mkv", title="Gladiator II", year="2024")
check_fields("【www.site.com】Gladiator II (2024) 1080p.mkv", title="Gladiator II", year="2024")
check_fields("www.mejor-torrent.com.es - Gladiator II (2024) 1080p.mkv", title="Gladiator II", year="2024")
check_fields("WWW.SITE.COM - Gladiator II (2024) 1080p.mkv", title="Gladiator II", year="2024")
check_fields("www.DonTorrent.xyz - Chicago Fire S14E04 1080p.mkv", title="Chicago Fire", chapter="14x04")
check("a site prefix is not part of the suggestion",
	suggest_name("www.DonTorrent.xyz - Gladiator II (2024) [BluRay 1080p].mkv"), "Gladiator II (2024) - 1080p.mkv")
# Only at the start, and only a real www address
check_fields("Gladiator II (2024) [www.site.com] 1080p.mkv", title="Gladiator II", year="2024")
check_fields("Gladiator.II.2024.1080p.WEB-DL-[DonTorrent].mkv", title="Gladiator II", year="2024")
check_fields("Wwwesley.Show.S01E01.mkv", title="Wwwesley Show")
# A leading dash left by a tag is not part of the title
check_fields("[Tag] - Gladiator II (2024) 1080p.mkv", title="Gladiator II")

# --- 'Temporada N' in front of the episode ------------------------------------
check_fields("The Last of Us - Temporada 2 [HDTV 720p][Cap.201][AC3 5.1 Castellano]",
	title="The Last of Us", season="2", episode_number="1", chapter="2x01", is_series=True)
check_fields("Rick y Morty Temporada 7 [HDTV 1080p][Cap.701].mkv", title="Rick y Morty", chapter="7x01")
check_fields("Chicago Fire - Temporada 14 [HDTV 720p][Cap.1404].mkv", title="Chicago Fire", chapter="14x04")
check_fields("Show Name Season 2 [Cap.205].mkv", title="Show Name", chapter="2x05")
check_fields("Show Name 2ª Temporada [Cap.205].mkv", title="Show Name", chapter="2x05")
check_fields("Show Name S02 [Cap.205].mkv", title="Show Name", chapter="2x05")
check_fields("Show.Name.T02.Cap.205.mkv", title="Show Name", chapter="2x05")
check("the season is not repeated in the suggestion",
	suggest_name("The Last of Us - Temporada 2 [HDTV 720p][Cap.201][AC3 5.1 Castellano]"), "2x01 - The Last of Us - 720p")
# Another season is not the one of the episode, so it is left alone
check_fields("Serie Temporada 3 [Cap.201].mkv", title="Serie Temporada 3", chapter="2x01")
# Nothing would be left of the title
check_fields("Temporada 2 [Cap.201].mkv", title="Temporada 2", chapter="2x01")

# --- Chapter ranges: Cap.201_207 ----------------------------------------------
check_fields("The Last of Us - Temporada 2 [HDTV 720p][Cap.201_207][AC3 5.1 Castellano]",
	title="The Last of Us", season="2", episode_number="1-7", chapter="2x01-07", is_series=True)
check_fields("Serie [HDTV][Cap.101-110].mkv", title="Serie", season="1", episode_number="1-10", chapter="1x01-10")
check_fields("Serie [Cap.301_Cap.305].mkv", title="Serie", episode_number="1-5", chapter="3x01-05")
check_fields("Serie Capitulo 1001_1010.mkv", title="Serie", season="10", episode_number="1-10", chapter="10x01-10")
check_fields("Serie [Cap.201 _ 207].mkv", episode_number="1-7")
check("a chapter range suggestion",
	suggest_name("The Last of Us - Temporada 2 [HDTV 720p][Cap.201_207][AC3 5.1 Castellano]"),
	"2x01-07 - The Last of Us - 720p")
check("a chapter range with padding", suggest_name("Serie [Cap.101-110] 1080p.mkv",
	template_series="{temporada.2}x{episodio.2} - {titulo}[.{extension}]"), "01x01-10 - Serie.mkv")
# Both ends belong to the same season and go forwards, otherwise it is no range
check_fields("Serie [Cap.108_201].mkv", episode_number="8", chapter="1x08")
check_fields("Serie [Cap.207_201].mkv", episode_number="7", chapter="2x07")
check_fields("Serie [Cap.201_201].mkv", episode_number="1", chapter="2x01")

# --- Spanish season packs: 2ª Temporada, Temporada Completa -------------------
check_fields("La Casa de Papel 2ª Temporada [HDTV 720p][Castellano]",
	title="La Casa de Papel", season="2", chapter="T2", episode_number="", is_series=True)
check_fields("La Casa de Papel - 1a Temporada Completa [HDTV]", title="La Casa de Papel", season="1", chapter="T1")
check_fields("La Casa de Papel 3º Temporada [HDTV]", title="La Casa de Papel", season="3")
check_fields("La Casa de Papel 4o Temporada [HDTV]", title="La Casa de Papel", season="4")
check_fields("La.Casa.de.Papel.5ª.Temporada.1080p", title="La Casa de Papel", season="5", resolution="1080p")
check_fields("La Casa de Papel 10ª temporada", title="La Casa de Papel", season="10")
check_fields("Serie Temporada Completa 2 [HDTV]", title="Serie", season="2", chapter="T2")
check("an ordinal season pack", suggest_name("La Casa de Papel 2ª Temporada [HDTV 720p][Castellano]"),
	"T2 - La Casa de Papel - 720p")
check("an ordinal season pack in English", suggest_name("La Casa de Papel 2ª Temporada [HDTV 720p]", season_prefix="S"),
	"S2 - La Casa de Papel - 720p")
# Not a season: no 'Temporada' word, or digits inside a word
check_fields("2 Fast 2 Furious (2003) 1080p.mkv", title="2 Fast 2 Furious", is_series=False)
check_fields("La Temporada de Patos (2004) 1080p.mkv", title="La Temporada de Patos", is_series=False)
check_fields("Agent47 Temporada Final.mkv", season="", is_series=False)

# --- Spanish multi-season packs ----------------------------------------------
check_fields("Breaking Bad Temporada Completa 1-5 [BluRay 1080p]",
	title="Breaking Bad", season="1-5", chapter="T1-T5", is_series=True)
check_fields("Breaking Bad Temporadas 1 a 5 [BluRay 1080p]", title="Breaking Bad", season="1-5")
check_fields("Breaking Bad Temporadas 1 al 5 [BluRay 1080p]", title="Breaking Bad", season="1-5")
check_fields("Breaking Bad Temporadas 1 - 5 [BluRay 1080p]", title="Breaking Bad", season="1-5")
check_fields("Breaking Bad Temporada 1-5 [BluRay 1080p]", title="Breaking Bad", season="1-5")
check_fields("Breaking Bad Temporada 1 a 5 [BluRay 1080p]", title="Breaking Bad", season="1-5")
check_fields("Breaking.Bad.Seasons.1-5.1080p", title="Breaking Bad", season="1-5", resolution="1080p")
check("a multi-season pack", suggest_name("Breaking Bad Temporada Completa 1-5 [BluRay 1080p]"),
	"T1-T5 - Breaking Bad - 1080p")
check("a multi-season pack with padding", suggest_name("Breaking Bad Temporadas 1 a 5 [BluRay 1080p]",
	template_season="{titulo} - T{temporada.2}"), "Breaking Bad - T01-05")
# A spaced dash after a single season is an episode, and digits must stand alone
check_fields("Show Season 2 - 05 [1080p].mkv", season="2", episode_number="5", chapter="2x05")
check_fields("Show Temporada 2 a 1080p.mkv", season="2", resolution="1080p")
check_fields("Show Temporada 2 a 4K.mkv", season="2", resolution="4K")

# --- Season and episode in words: Temporada 2 Capitulo 3 ----------------------
check_fields("Los Simpson - Temporada 35 Capitulo 1.mkv",
	title="Los Simpson", season="35", episode_number="1", chapter="35x01", is_series=True)
check_fields("Los Simpson T35 Cap 01.mkv", title="Los Simpson", season="35", episode_number="1", chapter="35x01")
check_fields("Los.Simpson.T35.Cap.12.1080p.mkv", title="Los Simpson", chapter="35x12", resolution="1080p")
check_fields("Serie Temporada 2 Episodio 3.mkv", title="Serie", chapter="2x03")
check_fields("Serie Temporada 2 Capítulo 3.mkv", title="Serie", chapter="2x03")
check_fields("Serie Season 1 Episode 10.mkv", title="Serie", chapter="1x10")
check_fields("Serie Season 1 Ep 7.mkv", title="Serie", chapter="1x07")
check_fields("Serie.Temporada.1.Capitulo.100.mkv", title="Serie", episode_number="100")
# The episode written the Cap.NNN way, with its season in front
check_fields("Serie Temporada 2 Cap.201.mkv", title="Serie", chapter="2x01")
check_fields("Serie Temporada 12 Cap.1205.mkv", title="Serie", chapter="12x05")
check("season and episode in words", suggest_name("Los Simpson - Temporada 35 Capitulo 1.mkv"), "35x01 - Los Simpson.mkv")
check("episode title after words", suggest_name("Serie Temporada 2 Capitulo 3 El Regreso.mkv",
	template_series="{temporada}x{episodio.2} - {titulo}[ - {titulo_episodio}][.{extension}]"), "2x03 - Serie - El Regreso.mkv")
# The T has to start a word
check_fields("Robot35 Cap 01.mkv", chapter="")

# --- Double episodes ----------------------------------------------------------
check_fields("Friends.S01E01E02.1080p.mkv", title="Friends", season="1", episode_number="1-2", chapter="1x01-02")
check_fields("Friends.S01E01-E02.1080p.mkv", episode_number="1-2", chapter="1x01-02")
check_fields("Friends 1x01-02 1080p.mkv", title="Friends", episode_number="1-2", chapter="1x01-02")
check_fields("Friends 1x01-1x02 1080p.mkv", title="Friends", episode_number="1-2", chapter="1x01-02")
check_fields("1x01-02 - Friends - 1080p.mkv", episode_number="1-2", chapter="1x01-02")
check("a double episode", suggest_name("Friends.S01E01E02.1080p.mkv"), "1x01-02 - Friends - 1080p.mkv")
check("a renamed double episode stays the same", suggest_name("1x01-02 - Friends - 1080p.mkv"), None)
# A resolution glued to the episode is no range, nor is another season
check_fields("Friends 1x01-1080p.mkv", episode_number="1", resolution="1080p")
check_fields("Friends 1x01-2x02.mkv", episode_number="1")

# --- Movies without a year (default template) ---------------------------------
check("a movie without a year", suggest_name("El Hoyo 2 [WEB-DL 1080p][AC3 5.1 Castellano]"), "El Hoyo 2 - 1080p")
check("a movie without a year keeps HDR",
	suggest_name("Mufasa El Rey Leon [4K 2160p][HDR10][Dual]"), "Mufasa El Rey Leon - 4K HDR")
check("a movie without a year and extension",
	suggest_name("Movie.Without.Year.1080p.mkv"), "Movie Without Year - 1080p.mkv")
check("a movie with its year is unchanged",
	suggest_name("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv"), "The Matrix (1999) - 1080p.mkv")
check("still nothing without a resolution", suggest_name("Pelicula sin ano [DVDRip][Spanish].avi"), None)
check("a custom template that requires the year",
	suggest_name("El Hoyo 2 [WEB-DL 1080p]", template_movie="{titulo} ({año}) - {resolucion}"), None)
# An episode with no season marker is not a movie either
check("an anime episode is not a movie",
	suggest_name("[SubsPlease] Sousou no Frieren - 12 (1080p) [ABCD1234].mkv"), None)
check("a long running episode is not a movie", suggest_name("One Piece - 1100 [1080p].mkv"), None)
check("a numbered title with a year is still a movie",
	suggest_name("Rocky - 2 (1979) 1080p.mkv"), "Rocky - 2 (1979) - 1080p.mkv")
check("a sequel number without a dash is still a movie", suggest_name("El Hoyo 2 1080p.mkv"), "El Hoyo 2 - 1080p.mkv")
# One video among many still needs its own year to be named
check("a pack file without a year", suggest_file_name("Otra.Peli.720p.mkv", parent_name="Coleccion", single_video=False), None)
check("a single video without a year takes the torrent name",
	suggest_file_name("video.mkv", parent_name="El Hoyo 2 [WEB-DL 1080p]", single_video=True), "El Hoyo 2 - 1080p.mkv")
check("a folder without a year is left alone", suggest_folder_name("Extras 1080p", parent_name="El Hoyo 2 [WEB-DL 1080p]"), None)

# --- Spanish names through files, folders and series --------------------------
SPANISH_PACK = "The Last of Us - Temporada 2 [HDTV 720p][Cap.201_207][AC3 5.1 Castellano]"
check("an episode inside a Spanish pack",
	suggest_file_name("The Last of Us - Temporada 2 [HDTV 720p][Cap.203][AC3 5.1 Castellano].mkv", parent_name=SPANISH_PACK),
	"2x03 - The Last of Us - 720p.mkv")
check("a bare episode inside a Spanish pack", suggest_file_name("03.mkv", parent_name=SPANISH_PACK),
	"2x03 - The Last of Us - 720p.mkv")
check("a bare episode inside an ordinal pack", suggest_file_name("05.mkv", parent_name="La Casa de Papel 2ª Temporada [HDTV 720p]"),
	"2x05 - La Casa de Papel - 720p.mkv")
check("an ordinal season folder", suggest_folder_name("2ª Temporada", parent_name="Mad Men (2007) - 1080p"),
	"T2 - Mad Men - 1080p")
check("a season and episode folder", suggest_folder_name("Temporada 3 Capitulo 4", parent_name="Mad Men (2007) - 1080p"),
	"3x04 - Mad Men - 1080p")
# The series folder finds the other episodes by title: a Spanish name has to
# give the same one as any other release of the series
def series_key(name):
	identity = series_identity(name)
	return identity and (identity["key"], identity["season"])
check("series of a Spanish episode", series_key("The Last of Us - Temporada 2 [HDTV 720p][Cap.201]"), ("the last of us", 2))
check("series of a Spanish range", series_key(SPANISH_PACK), ("the last of us", 2))
check("series of an ordinal pack", series_key("La Casa de Papel 2ª Temporada [HDTV 720p]"), ("la casa de papel", 2))
check("series of a multi-season pack", series_key("Breaking Bad Temporada Completa 1-5 [BluRay 1080p]"), ("breaking bad", None))
check("series of season and episode in words", series_key("Los Simpson T35 Cap 01.mkv"), ("los simpson", 35))
check("series behind a site prefix", series_key("www.DonTorrent.xyz - Chicago Fire S14E04 1080p.mkv"), ("chicago fire", 14))
check("series of a renamed range", series_key("2x01-07 - The Last of Us - 720p"), ("the last of us", 2))
check("series of a renamed double episode", series_key("1x01-02 - Friends - 1080p.mkv"), ("friends", 1))
check("a site prefix on the torrent", suggest_file_name("01.mkv", parent_name="www.DonTorrent.xyz - Mad Men S02 1080p"),
	"2x01 - Mad Men - 1080p.mkv")

# --- companion_subtitle_name -------------------------------------------------
check("subtitle keeps language suffix",
	companion_subtitle_name("01.es.srt", "01.mkv", "2x01 - Show Name.mkv"), "2x01 - Show Name.es.srt")
check("subtitle without suffix",
	companion_subtitle_name("01.srt", "01.mkv", "2x01 - Show Name.mkv"), "2x01 - Show Name.srt")
check("subtitle ass extension kept",
	companion_subtitle_name("01.ass", "01.mkv", "2x01 - Show Name.mkv"), "2x01 - Show Name.ass")
check("subtitle of another video", companion_subtitle_name("02.srt", "01.mkv", "2x01 - Show Name.mkv"), None)
check("not a subtitle", companion_subtitle_name("01.nfo", "01.mkv", "2x01 - Show Name.mkv"), None)
check("subtitle without extension", companion_subtitle_name("srt", "01.mkv", "2x01 - Show Name.mkv"), None)
# The video name has to end where a separator starts: '10' is not '1' with a suffix
check("a longer number is another video", companion_subtitle_name("10.srt", "1.mkv", "New.mkv"), None)
check("nor a word glued to it", companion_subtitle_name("1x.srt", "1.mkv", "New.mkv"), None)
for subtitle, expected in [("1.srt", "New.srt"), ("1.es.srt", "New.es.srt"), ("1_es.srt", "New_es.srt"),
							("1-forced.srt", "New-forced.srt"), ("1 English.srt", "New English.srt")]:
	check(f"subtitle {subtitle} goes with 1.mkv", companion_subtitle_name(subtitle, "1.mkv", "New.mkv"), expected)


# ---------------------------------------------------------------------------
# DIFFERENT RELEASES NEVER SHARE A NAME
# ---------------------------------------------------------------------------
# With the automatic rename and replace on, two releases that get the same
# suggestion are taken for two versions of the same thing, and the older one
# is deleted with its data. So when a name cannot be told apart from its
# siblings, there is no suggestion at all

def differ(description, first, second, **kwargs):
	"""Two names of different releases: never the same suggestion (None for
	both is fine, it only means they are left alone)"""
	a, b = suggest_name(first, **kwargs), suggest_name(second, **kwargs)
	check(f"{description}: different names or none", a is None or a != b, True)


# --- A season with something more than the season ----------------------------
# Anime: the episode follows the season after a spaced dash
check("an anime episode behind the season",
	suggest_name("[SubsPlease] Jujutsu Kaisen S2 - 05 (1080p) [ABCD1234].mkv"), "2x05 - Jujutsu Kaisen - 1080p.mkv")
check("the next one", suggest_name("[SubsPlease] Jujutsu Kaisen S2 - 06 (1080p) [ABCD1234].mkv"), "2x06 - Jujutsu Kaisen - 1080p.mkv")
check_fields("[SubsPlease] Jujutsu Kaisen S2 - 05 (1080p) [ABCD1234].mkv",
	title="Jujutsu Kaisen", season="2", episode_number="5", chapter="2x05", resolution="1080p", is_series=True)
check("a version of an episode is the same episode",
	suggest_name("[SubsPlease] Frieren S2 - 03v2 (1080p) [ABCD1234].mkv"), "2x03 - Frieren - 1080p.mkv")
check("an ordinal season", suggest_name("[Group] Show Name 2nd Season - 05 [1080p].mkv"), "2x05 - Show Name - 1080p.mkv")
check_fields("[Group] Show Name 3rd Season - 12 [1080p].mkv", title="Show Name", season="3", episode_number="12")
check("an ordinal season pack", suggest_name("[Group] Show Name 2nd Season [1080p]"), "T2 - Show Name - 1080p")
check("a batch of episodes", suggest_name("[SubsPlease] Vinland Saga S2 (01-24) (1080p) [Batch]"), "2x01-24 - Vinland Saga - 1080p")
check_fields("[Erai-raws] Kimetsu no Yaiba Season 2 - 11 [1080p][Multiple Subtitle].mkv",
	title="Kimetsu no Yaiba", season="2", episode_number="11")
# A single digit could be the end of a range of seasons as well: no guessing
check("a single digit after the dash is no episode", suggest_name("Show Name Season 2 - 5.mkv"), None)
check("nor a range of seasons", suggest_name("Breaking Bad Temporada 1 - 5 [BluRay 1080p]"), None)
check("a recap episode (12.5) is not episode 12", suggest_name("[ASW] Mushoku Tensei S2 - 12.5 [1080p HEVC].mkv"), None)
# The episode written in words after S02
check("S02.Episode.5", suggest_name("Show.Name.S02.Episode.5.1080p.WEB.mkv"), "2x05 - Show Name - 1080p.mkv")
check("S02.Ep05", suggest_name("Show.Name.S02.Ep05.1080p.WEB.mkv"), "2x05 - Show Name - 1080p.mkv")
check_fields("Show.S01EP05.1080p.WEB.mkv", title="Show", chapter="1x05")
check_fields("Show.S01.EP01-EP08.1080p.WEB", title="Show", season="1", episode_number="1-8", chapter="1x01-08")
check_fields("Los.Simpson.S35.Cap.01.mkv", title="Los Simpson", chapter="35x01")
# Parts and volumes of a season: nothing can hold them, so no suggestion
for first, second in [("Stranger.Things.S04.Vol.1.1080p.NF.WEB-DL", "Stranger.Things.S04.Vol.2.1080p.NF.WEB-DL"),
						("La.Casa.de.Papel.S05.Part.1.1080p.NF.WEB-DL", "La.Casa.de.Papel.S05.Part.2.1080p.NF.WEB-DL"),
						("Show.Name.S02.Disc.1.1080p.BluRay", "Show.Name.S02.Disc.2.1080p.BluRay"),
						("Show.Name.S02.Extras.1080p.BluRay", "Show.Name.S02.1080p.BluRay"),
						("Show.Name.Season.2.Recap.1080p.WEB.mkv", "Show.Name.Season.2.1080p.WEB.mkv"),
						("Show.S2.OVA.1080p.mkv", "Show.S2.1080p.mkv"),
						("[SubsPlease] Jujutsu Kaisen S2 - 05 (1080p) [ABCD1234].mkv", "[SubsPlease] Jujutsu Kaisen S2 - 06 (1080p) [ABCD1234].mkv"),
						("Show.Name.S02.Episode.5.1080p.WEB.mkv", "Show.Name.S02.Episode.6.1080p.WEB.mkv"),
						("[Group] Show Name 2nd Season - 05 [1080p].mkv", "[Group] Show Name 2nd Season - 06 [1080p].mkv")]:
	differ(f"{first} / {second}", first, second)
	differ(f"{first} / {second} (S prefix)", first, second, season_prefix="S")
check("a volume of a season is left alone", suggest_name("Stranger.Things.S04.Vol.1.1080p.NF.WEB-DL"), None)
check("a part of a season is left alone", suggest_name("La.Casa.de.Papel.S05.Part.2.1080p.NF.WEB-DL"), None)
check("so is its folder", suggest_folder_name("Stranger.Things.S04.Vol.1", "Stranger Things (2016)"), None)
check("but it is still that season of that show", series_identity("Stranger.Things.S04.Vol.1.1080p.NF.WEB-DL")["season"], 4)
# Its files: an episode marker of their own is enough, a bare number is not
# (a second volume may count from 01 again)
check("an episode inside a volume",
	suggest_file_name("Stranger.Things.S04E08.Chapter.Eight.1080p.NF.WEB-DL.mkv", parent_name="Stranger.Things.S04.Vol.2.1080p.NF.WEB-DL"),
	"4x08 - Stranger Things - 1080p.mkv")
check("a bare number inside a volume", suggest_file_name("01.mkv", parent_name="Stranger.Things.S04.Vol.2.1080p.NF.WEB-DL"), None)
check("a bare number inside an anime batch",
	suggest_file_name("05.mkv", parent_name="[SubsPlease] Vinland Saga S2 (01-24) (1080p) [Batch]"), "2x05 - Vinland Saga - 1080p.mkv")
# What every season pack carries is no reason to leave it alone
for name, expected in [("Show.Name.S02.Complete.1080p.mkv", "T2 - Show Name - 1080p.mkv"),
						("Show.Name.S02.REPACK.1080p.WEB", "T2 - Show Name - 1080p"),
						("Show.Name.S02.ESP.AC3.5.1.1080p.WEB", "T2 - Show Name - 1080p"),
						("Show.Name.S02.2019.1080p.WEB", "T2 - Show Name - 1080p"),
						("Metalocalipsis (2006) S03 1080p MAX WEB-DL DD+2.0 HEVC-HDZ", "T3 - Metalocalipsis - 1080p"),
						("Serie Temporada 2 [HDTV 720p][AC3 5.1 Castellano]", "T2 - Serie - 720p"),
						("Serie.Temporada.2.Completa.720p.mkv", "T2 - Serie - 720p.mkv"),
						("House.M.D.S01-S08.1080p.BluRay.MULTi.AAC.2.0.x265-GRP", "T1-T8 - House M.D - 1080p")]:
	check(f"a season pack: {name}", suggest_name(name), expected)

# --- Air dates and rounds are no movie year -----------------------------------
for first, second in [("The.Daily.Show.2024.11.05.Guest.Name.1080p.WEB.h264-GRP.mkv", "The.Daily.Show.2024.11.06.Guest.Name.1080p.WEB.h264-GRP.mkv"),
						("Formula1.2024.Round20.Mexico.Race.1080p.WEB.mkv", "Formula1.2024.Round21.Mexico.Race.1080p.WEB.mkv"),
						("NFL.2024.Week.10.Chiefs.vs.Bills.1080p.WEB.mkv", "NFL.2024.Week.11.Chiefs.vs.Bills.1080p.WEB.mkv"),
						("Formula1.2024.Mexico.Grand.Prix.Race.1080p.WEB.mkv", "Formula1.2024.Brazil.Grand.Prix.Race.1080p.WEB.mkv"),
						("Movie.2019.Part.1.1080p.BluRay.mkv", "Movie.2019.Part.2.1080p.BluRay.mkv")]:
	differ(f"{first} / {second}", first, second)
check("a dated episode is left alone", suggest_name("The.Daily.Show.2024.11.05.Guest.Name.1080p.WEB.h264-GRP.mkv"), None)
check("a round is left alone", suggest_name("Formula1.2024.Round20.Mexico.Race.1080p.WEB.mkv"), None)
check("a trailer is not the movie", suggest_name("Movie (2019) [Trailer] 1080p.mkv"), None)
check("nor a dated show inside a single video torrent",
	suggest_file_name("video.mkv", parent_name="The.Daily.Show.2024.11.05.Guest.Name.1080p.WEB", single_video=True), None)
# What movies carry after the year is still fine
for name, expected in [("Movie.2019.MULTi.VFF.AC3.5.1.1080p.BluRay.mkv", "Movie (2019) - 1080p.mkv"),
						("Movie.2019.EXTENDED.1080p.BluRay.x264-GRP.mkv", "Movie (2019) - 1080p.mkv"),
						("Movie.2019.Special.Edition.1080p.BluRay.mkv", "Movie (2019) - 1080p.mkv"),
						("Movie.2019.25th.Anniversary.Edition.1080p.mkv", "Movie (2019) - 1080p.mkv"),
						("Movie.2019.FRENCH.5.1.1080p.mkv", "Movie (2019) - 1080p.mkv"),
						("Pelicula.2023.BDRemux.EAC3.5.1.Latino.mkv", "Pelicula (2023) - BDRemux.mkv"),
						("Harry.Potter.and.the.Deathly.Hallows.Part.1.2010.1080p.BluRay.mkv",
							"Harry Potter and the Deathly Hallows Part 1 (2010) - 1080p.mkv"),
						("Kill.Bill.Vol.1.2003.1080p.BluRay.mkv", "Kill Bill Vol 1 (2003) - 1080p.mkv"),
						("UFC.300.Pereira.vs.Hill.2024.1080p.WEB.mkv", "UFC 300 Pereira vs Hill (2024) - 1080p.mkv"),
						("El.Codigo.Da.Vinci.TRILOGIA.2006.-.2016.Version.Extendida.mHD.10Bits.1080p.BluRay.DD5.1.AV1-TMd",
							"El Codigo Da Vinci TRILOGIA (2006-2016) - 1080p")]:
	check(f"a movie: {name}", suggest_name(name), expected)

# --- 'Cap' starts a word ------------------------------------------------------
check("Kneecap is a movie", suggest_name("Kneecap.2024.1080p.WEB.h264-GRP.mkv"), "Kneecap (2024) - 1080p.mkv")
check_fields("Kneecap.2024.1080p.WEB.h264-GRP.mkv", title="Kneecap", year="2024", chapter="", is_series=False)
check_fields("Nightcap.1080p.WEB.mkv", title="Nightcap", chapter="", is_series=False)
check_fields("Show.Name.Season.2.Recap.1080p.WEB.mkv", season="2", episode_number="", chapter="T2")
check("Recap is no episode 10x80", suggest_name("Show.Name.Season.2.Recap.1080p.WEB.mkv"), None)
check("Recap as a title", suggest_name("Recap.2019.1080p.mkv"), "Recap (2019) - 1080p.mkv")
check("Cap.103 still is an episode", suggest_name("Serie Cap.103 HDTV.avi"), "1x03 - Serie.avi")
check("Capitulo 103 too", suggest_name("Serie Capitulo 103 HDTV.avi"), "1x03 - Serie.avi")
check_fields("Serie.Cap.103.HDTV.avi", title="Serie", chapter="1x03")
check_fields("Chicago Fire - Temporada 14 [HDTV 720p][Cap.1404].mkv", title="Chicago Fire", chapter="14x04")
check_fields("Serie [HDTV][Cap.101-110].mkv", episode_number="1-10")

# --- 'WEB' as a word of the title ---------------------------------------------
check("Charlottes Web", suggest_name("Charlottes.Web.2006.1080p.BluRay.x264-GRP.mkv"), "Charlottes Web (2006) - 1080p.mkv")
check_fields("Charlottes.Web.2006.1080p.BluRay.x264-GRP.mkv", title="Charlottes Web", source="BluRay")
check("Unfriended Dark Web", suggest_name("Unfriended.Dark.Web.2018.1080p.BluRay.x264-GRP.mkv"),
	"Unfriended Dark Web (2018) - 1080p.mkv")
check("Web of Lies", suggest_name("Web.of.Lies.S01E01.1080p.mkv"), "1x01 - Web of Lies - 1080p.mkv")
check_fields("Web.of.Lies.S01E01.1080p.mkv", title="Web of Lies", source="")
check_fields("charlottes.web.2006.1080p.bluray.x264-grp.mkv", title="charlottes web", source="BluRay")
check_fields("Spider-Web.2019.1080p.BluRay.mkv", title="Spider-Web")
check_fields("The.Web.2019.1080p.mkv", title="The Web", source="")
# The release tag still is the source, and still ends the title
check_fields("Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL", title="Chicago Fire", source="WEB-DL")
check_fields("the.office.us.s05e14.720p.web.x264-grp.mkv", title="the office us", source="WEB-DL")
check_fields("Barbie.2023.WEB.1080p.h264-ETHEL.mkv", title="Barbie", year="2023", source="WEB-DL")
check_fields("Movie.Name.WEB.1080p.mkv", title="Movie Name", source="WEB-DL")
check_fields("Show.S01E01.WEB.H264-GRP.mkv", title="Show", chapter="1x01", source="WEB-DL")
check_fields("Show.S01E02.The.Buys.WEB.1080p.mkv", episode_title="The Buys", source="WEB-DL")
check_fields("Dark.Web.Cicada.3301.2021.1080p.WEB.h264-GRP.mkv", title="Dark Web Cicada 3301", year="2021", source="WEB-DL")
check_fields("Movie.2019.WEBRip.1080p.mkv", title="Movie", source="WEBRip")

# --- A T and a number in front of a movie title ---------------------------------
check("T2 Trainspotting", suggest_name("T2.Trainspotting.2017.1080p.BluRay.x264-GRP.mkv"), "T2 Trainspotting (2017) - 1080p.mkv")
check_fields("T2.Trainspotting.2017.1080p.BluRay.x264-GRP.mkv", title="T2 Trainspotting", year="2017", season="", is_series=False)
check("is no series", series_identity("T2.Trainspotting.2017.1080p.BluRay.x264-GRP.mkv"), None)
check("with spaces", suggest_name("T2 Trainspotting (2017) 1080p.mkv"), "T2 Trainspotting (2017) - 1080p.mkv")
check("T2 Judgment Day", suggest_name("T2.Judgment.Day.1991.1080p.BluRay.x264-GRP.mkv"), "T2 Judgment Day (1991) - 1080p.mkv")
# The Spanish season behind the title, or a renamed pack, still is a season
check_fields("Serie.Name.T2.1080p.WEB-DL", title="Serie Name", season="2", chapter="T2", is_series=True)
check("a Spanish T2 season pack", suggest_name("Serie.Name.T2.1080p.WEB-DL"), "T2 - Serie Name - 1080p")
check_fields("Muertos.S.L.T4.1080p", title="Muertos S.L", season="4")
check("a renamed T2 pack", series_identity("T2 - Mad Men - 1080p")["season"], 2)
check("a renamed T2 pack with a year in it", series_identity("T2 - Mad Men (2007) - 1080p")["season"], 2)
check_fields("T01.Serie.2019.1080p", season="1", is_series=True)

# --- Acronyms -------------------------------------------------------------------
check("Episode IV A New Hope", suggest_name("Star.Wars.Episode.IV.A.New.Hope.1977.1080p.BluRay.x264-GRP.mkv"),
	"Star Wars Episode IV A New Hope (1977) - 1080p.mkv")
check_fields("Star.Wars.Episode.V.The.Empire.Strikes.Back.1980.1080p.mkv", title="Star Wars Episode V The Empire Strikes Back")
check_fields("Agents.of.S.H.I.E.L.D.S01E01.1080p.mkv", title="Agents of S.H.I.E.L.D", chapter="1x01")
check_fields("Marvels.Agents.of.S.H.I.E.L.D.S07.1080p.WEB", title="Marvels Agents of S.H.I.E.L.D", season="7")
check_fields("E.T.the.Extra-Terrestrial.1982.1080p.mkv", title="E.T the Extra-Terrestrial", year="1982")
check_fields("L.A.Confidential.1997.1080p.mkv", title="L.A Confidential", year="1997")

# --- Sample clips ----------------------------------------------------------------
SAMPLE_PACK = "Show.Name.S01.1080p.WEB.h264-GRP"
check("a sample clip gets no name", suggest_file_name("show.name.s01e01.1080p.web.h264-grp-sample.mkv", parent_name=SAMPLE_PACK), None)
check("the episode it samples does", suggest_file_name("show.name.s01e01.1080p.web.h264-grp.mkv", parent_name=SAMPLE_PACK),
	"1x01 - Show Name - 1080p.mkv")
check("nor anything inside a Sample folder",
	suggest_file_name("show.name.s01e01.1080p.web.h264-grp.mkv", parent_name=SAMPLE_PACK, folder_name="Sample"), None)
check("a movie sample", suggest_file_name("movie.2019.1080p.bluray.x264-grp-sample.mkv", parent_name="Movie.2019.1080p.BluRay.x264-GRP"),
	None)
for path in ["show.name.s01e01.1080p.web.h264-grp-sample.mkv", "sample.mkv", "SAMPLE.mkv", "Sample/show.name.s01e01.mkv",
			"Show/Samples/x.mkv", "D:\\Show\\Sample\\x.mkv", "sample-grp-show.s01e01.mkv", "Movie.2019.Sample.1080p.mkv",
			"show.s01e01.sample-grp.mkv", "Show [Sample].mkv", "Movie.2019.SAMPLE.mkv", "show.s01e01.samples.mkv", "Samples/"]:
	check(f"{path} is a sample", is_sample_file(path), True)
for path in ["Samples.of.Life.2020.mkv", "Movies/Samples.of.Life.2020.mkv", "Sample.People.2000.1080p.mkv",
			"Free.Sample.2016.1080p.mkv", "The.Sample.2020.mkv", "show.name.s01e01.mkv", "", None]:
	check(f"{path!r} is no sample", is_sample_file(path), False)
check("a title starting with Samples is renamed",
	suggest_file_name("Samples.of.Life.2020.1080p.mkv", parent_name="Coleccion"), "Samples of Life (2020) - 1080p.mkv")

# --- Collection words of the torrent ----------------------------------------------
check("The Complete Series is no part of the title",
	suggest_file_name("Friends.S01E01.1080p.BluRay.x264-GRP.mkv", parent_name="Friends.The.Complete.Series.1080p.BluRay.x264-GRP"),
	"1x01 - Friends - 1080p.mkv")
for words in ["The.Complete.Series", "Complete.Series", "Complete", "Collection", "Box.Set", "Boxset", "Integral",
				"Serie.Completa", "Completa", "Pack", "The.Complete.Collection", "Complete.Box.Set", "S01-S10.Complete"]:
	check(f"nor {words}", suggest_file_name("Friends.S01E01.1080p.mkv", parent_name=f"Friends.{words}.1080p"),
		"1x01 - Friends - 1080p.mkv")
check("a season folder of a complete series", suggest_folder_name("Temporada 2", "Friends.The.Complete.Series.1080p"),
	"T2 - Friends - 1080p")
# Titles that end with those words keep them
check("a show called Wolf Pack", suggest_file_name("Wolf.Pack.S01E01.1080p.mkv", parent_name="Wolf.Pack.S01.1080p.WEB"),
	"1x01 - Wolf Pack - 1080p.mkv")
check("a bare episode of Wolf Pack", suggest_file_name("01.mkv", parent_name="Wolf.Pack.S01.1080p.WEB"), "1x01 - Wolf Pack - 1080p.mkv")
check("Wolf Pack, the complete series",
	suggest_file_name("Wolf.Pack.S01E01.1080p.mkv", parent_name="Wolf.Pack.Complete.Series.1080p"), "1x01 - Wolf Pack - 1080p.mkv")
check("a season folder of Wolf Pack, the complete series",
	suggest_folder_name("Season 2", "Wolf.Pack.Complete.Series.1080p"), "T2 - Wolf Pack - 1080p")
check("Wolf Pack with no season in the torrent",
	suggest_file_name("Wolf.Pack.S01E01.1080p.mkv", parent_name="Wolf.Pack.1080p.WEB"), "1x01 - Wolf Pack - 1080p.mkv")
check("a show called The Collection", suggest_file_name("01.mkv", parent_name="The.Collection.S01.1080p"),
	"1x01 - The Collection - 1080p.mkv")
check("a movie called The Rat Pack",
	suggest_file_name("video.mkv", parent_name="The.Rat.Pack.1998.1080p", single_video=True), "The Rat Pack (1998) - 1080p.mkv")
check("a movie called The Collection",
	suggest_file_name("video.mkv", parent_name="The.Collection.2012.1080p", single_video=True), "The Collection (2012) - 1080p.mkv")

# --- A season range is no season of a file ----------------------------------------
check("a bare episode inside several seasons", suggest_file_name("01.mkv", parent_name="Show.Name.S01-S03.1080p"), None)
check("not even with a template without the season",
	suggest_file_name("01.mkv", parent_name="Show.Name.S01-S03.1080p", template_series="{title} - {episode.2}"), None)
check("its season folder tells it", suggest_file_name("01.mkv", parent_name="Show.Name.S01-S03.1080p", folder_name="Temporada 2"),
	"2x01 - Show Name - 1080p.mkv")
check("and so does its own marker", suggest_file_name("Show.Name.S02E01.mkv", parent_name="Show.Name.S01-S03.1080p"),
	"2x01 - Show Name - 1080p.mkv")

# --- Templates that would not make a file name -------------------------------------
for bad, code, detail in [("{title}/{season}x{episode.2}", "invalid_char", "/"), ("../{title}", "invalid_char", ".."),
							("{title}\n{year}", "invalid_char", "\\n"), ("{title}\\{year}", "invalid_char", "\\"),
							("{title}\t{year}", "invalid_char", "\\t"), ("{title}\r\n{year}", "invalid_char", "\\r"),
							("{title}\u2028{year}", "invalid_char", "\\u2028"), ("{title}\x00", "invalid_char", "\\x00"),
							("{title}..{year}", "invalid_char", ".."), ("[../]{title}", "invalid_char", ".."),
							("{title}[/{year}]", "invalid_char", "/"),
							("{episode.100000000}", "padding_too_wide", "episode.100000000"),
							("{episodio.5}", "padding_too_wide", "episodio.5"), ("{EPISODE.10}", "padding_too_wide", "EPISODE.10"),
							("{ temporada .7}", "padding_too_wide", "temporada.7"), ("{season.05}", "padding_too_wide", "season.05"),
							# The field comes first: padding on the title is the error
							("{title.10}", "padding_not_allowed", "title")]:
	try:
		validate_template(bad)
		check(f"validate({bad!r}) should fail", "no error", code)
	except TemplateError as e:
		check(f"validate({bad!r}) error code", e.code, code)
		check(f"validate({bad!r}) error detail", e.detail, detail)
for good, fields in [("{titulo}.{año}.{extension}", ["title", "year", "extension"]), ("{title} ({year})", ["title", "year"]),
						("{season.4}x{episode.4}", ["season", "episode_number"]), ("{title}. {episode_title}", ["title", "episode_title"]),
						("{title} - {resolution}[.{extension}]", ["title", "resolution", "extension"])]:
	check(f"validate({good!r}) is fine", validate_template(good), fields)
for default in (DEFAULT_MOVIE_TEMPLATE, DEFAULT_SERIES_TEMPLATE, DEFAULT_SEASON_PACK_TEMPLATE):
	check(f"the default {default!r} is valid", bool(validate_template(default)), True)
# A template saved before these checks still never builds a path or a huge name
check("a rendered name never holds a path", suggest_name("AC/DC.Live.at.River.Plate.2011.1080p.mkv"), None)
check("a huge padding renders nothing", render("{episode.1000}", episode_number="7"), None)
check("inside an optional block, just the block", render("{title}[ {episode.1000}]", title="x", episode_number="7"), "x")

# --- A number behind a dash in the name of a folder ------------------------------
# 'Friends Season 1 - 10' may be ten seasons as well as the tenth episode, and
# the episode would then take the pack's name (and the automatic replace would
# delete the pack). Only the name of a video, or a version ('05v2'), makes it
# an episode. A list or a count of episodes never is one
for name in ["Friends Season 1 - 10 [1080p BluRay x265]", "Supernatural Season 1 - 15 [1080p]", "Friends S01 - 10 [1080p]",
			"Friends T1 - 10", "Friends Temporada 1 - 10", "Serie - Temporada 1 - 01 al 10 [HDTV]", "Serie S01 - 01 to 10 [1080p]",
			"Serie S01 - 01 & 02 [1080p]", "Serie S01 - 01+02 [1080p]", "Serie S01 - 01, 02 [1080p]",
			"Serie S01 - 10 Episodes [1080p]", "Serie - Temporada 2 - 10 Capitulos [HDTV]", "Show 2nd Season - 05 [1080p]",
			"[SubsPlease] Jujutsu Kaisen S2 - 05 (1080p) [ABCD1234]", "Serie S01 - 01 & 02 [1080p].mkv", "Serie S01 - 01+02 [1080p].mkv",
			"Serie S01 - 01, 02 [1080p].mkv", "Serie - Temporada 1 - 01 al 10 [HDTV].mkv", "Serie S01 - 01 y 02 [HDTV].mkv",
			"Serie S01 - 10 Episodes [1080p].mkv", "Serie - Temporada 3 - 13 episodios [HDTV 720p].mkv"]:
	check(f"no single episode in {name!r}", suggest_name(name), None)
	check(f"no single episode in {name!r} (S prefix)", suggest_name(name, season_prefix="S"), None)
differ("ten seasons / the tenth episode", "Friends Season 1 - 10 [1080p BluRay x265]", "Friends.S01E10.1080p.BluRay.x265")
differ("ten seasons / the tenth episode (S prefix)", "Friends Season 1 - 10 [1080p BluRay x265]", "Friends.S01E10.1080p.BluRay.x265",
	season_prefix="S")
differ("a list of episodes / its first one", "Serie S01 - 01 & 02 [1080p].mkv", "Serie.S01E01.1080p.mkv")
check("the tenth episode still is one", suggest_name("Friends.S01E10.1080p.BluRay.x265"), "1x10 - Friends - 1080p")
check("and the pack still is that show's", (series_identity("Friends Season 1 - 10 [1080p BluRay x265]") or {}).get("key"), "friends")
check("as a video it is an episode", suggest_name("Friends Season 1 - 10 [1080p].mkv"), "1x10 - Friends - 1080p.mkv")
check("and so it is with a version", suggest_name("[SubsPlease] Frieren S2 - 03v2 (1080p) [ABCD1234]"), "2x03 - Frieren - 1080p")
check("an episode followed by its title", suggest_name("[Group] Show S2 - 05 - The Title [1080p].mkv"), "2x05 - Show - 1080p.mkv")
check("or by the resolution behind a dash", suggest_name("Show S2 - 05 - 1080p.mkv"), "2x05 - Show - 1080p.mkv")
check("a batch of episodes still is a range", suggest_name("[Group] Show S2 01-12 [1080p]"), "2x01-12 - Show - 1080p")
check("the video inside such a folder", suggest_file_name("[Group] Show S2 - 05 [1080p].mkv", parent_name="[Group] Show S2 - 05 [1080p]"),
	"2x05 - Show - 1080p.mkv")
check("but not one with no name of its own", suggest_file_name("video.mkv", parent_name="[Group] Show S2 - 05 [1080p]", single_video=True),
	None)

# --- Audio written without a dot ------------------------------------------------
# The channels of 'DD51' or 'AC3.51' are no stray number, nor is 'R5' a round
for name, expected in [("Movie.2019.German.DL.DD51.1080p.BluRay.x264-GRP", "Movie (2019) - 1080p"),
						("Movie.2019.German.DDP51.DL.1080p.WEB.h264-GRP", "Movie (2019) - 1080p"),
						("Movie.2019.German.DD20.1080p.WEB.h264-GRP", "Movie (2019) - 1080p"),
						("Movie.2019.MULTi.DTS51.1080p", "Movie (2019) - 1080p"),
						("Movie.2019.iTA.AC3.51.720p.mkv", "Movie (2019) - 720p.mkv"),
						("Movie.2019.AAC20.1080p", "Movie (2019) - 1080p"),
						("Movie.2019.German.EAC3D.DL.1080p.WEB.h264-GRP", "Movie (2019) - 1080p"),
						("Movie.2019.DTS:X.1080p", "Movie (2019) - 1080p"), ("Movie.2019.HE-AAC.2.0.1080p", "Movie (2019) - 1080p"),
						("Movie.2019.DDPA5.1.1080p.WEB", "Movie (2019) - 1080p"), ("Movie.2019.6.CH.1080p.mkv", "Movie (2019) - 1080p.mkv"),
						("Show.S02.6CH.1080p", "T2 - Show - 1080p"),
						("Show.S01.German.DD51.DL.1080p.BluRay.x264-GRP", "T1 - Show - 1080p"),
						("Movie.2009.R5.720p.XviD-GRP", "Movie (2009) - 720p"),
						("Movie.2009.R5.LiNE.720p.x264-GRP", "Movie (2009) - 720p")]:
	check(f"the audio of {name}", suggest_name(name), expected)
check("a bare file inside such a pack", suggest_file_name("01.mkv", parent_name="Show.S01.German.DD51.DL.1080p.BluRay.x264-GRP"),
	"1x01 - Show - 1080p.mkv")
check("and the single video of such a movie",
	suggest_file_name("video.mkv", parent_name="Movie.2019.German.DL.DD51.1080p.BluRay.x264-GRP", single_video=True),
	"Movie (2019) - 1080p.mkv")
for first, second in [("F1.2024.R20.Mexico.720p.WEB", "F1.2024.R21.Brazil.720p.WEB"), ("F1.2024.R20.720p.WEB", "F1.2024.R21.720p.WEB"),
						("Movie.2019.CD1.avi", "Movie.2019.CD2.avi"), ("Show.S01.3.5.1080p", "Show.S01.1080p")]:
	differ(f"{first} / {second}", first, second)

# --- Episode numbers glued to letters --------------------------------------------
check("Season.1.E05", suggest_name("Show.Season.1.E05.1080p"), "1x05 - Show - 1080p")
check("Season 1 EP5", suggest_name("Show Season 1 EP5 1080p"), "1x05 - Show - 1080p")
check("Temporada 1 CAP5", suggest_name("Show Temporada 1 CAP5 1080p"), "1x05 - Show - 1080p")
check("SEASON 2 EPISODE 3", suggest_name("Show SEASON 2 EPISODE 3 1080p"), "2x03 - Show - 1080p")
check_fields("Show Temporada 1 e 2 1080p", season="1", episode_number="")  # Portuguese: seasons 1 and 2
check("which is not season 1 alone", suggest_name("Show Temporada 1 e 2 1080p"), None)
for first, second in [("Show.Season.1.E05.1080p", "Show.Season.1.E06.1080p"), ("Show Season 1 EP5 1080p", "Show Season 1 EP6 1080p"),
						("Show Temporada 1 CAP5 1080p", "Show Temporada 1 CAP6 1080p"),
						("Show 2nd Season EP05 1080p", "Show 2nd Season EP06 1080p"),
						("Show.2023.EP01.1080p.WEB", "Show.2023.EP02.1080p.WEB"), ("Show.2023.E01.1080p.WEB", "Show.2023.E02.1080p.WEB"),
						("Doc.2017.1of7.Title.1080p", "Doc.2017.2of7.Title.1080p")]:
	differ(f"{first} / {second}", first, second)
	differ(f"{first} / {second} (S prefix)", first, second, season_prefix="S")
check("an episode after a year is left alone", suggest_name("Show.2023.EP01.1080p.WEB"), None)
check("so is a part of a series", suggest_name("Doc.2017.1of7.Title.1080p"), None)

# --- Only what every release carries goes unnoticed --------------------------------
# Whatever a movie carries between its year and the metadata, or a season pack
# between its season and the metadata, may be what tells it apart. Plain words
# too: a race, a match, a documentary of a series, the extras of a movie
for first, second in [("Formula1.2024.Mexico.Race.1080p.WEB.mkv", "Formula1.2024.Brazil.Race.1080p.WEB.mkv"),
						("NFL.2024.Chiefs.at.Bills.1080p.WEB.mkv", "NFL.2024.Packers.at.Bears.1080p.WEB.mkv"),
						("Wimbledon.2024.Mens.Final.1080p.WEB.mkv", "Wimbledon.2024.Womens.Final.1080p.WEB.mkv"),
						("Wimbledon.2024.Final.1080p.WEB.mkv", "Wimbledon.2024.Semi.Final.1080p.WEB.mkv"),
						("Movie.2019.Behind.the.Scenes.1080p.mkv", "Movie.2019.1080p.mkv"),
						("Movie.2019.Making.Of.1080p.mkv", "Movie.2019.1080p.mkv"),
						("Movie.2019.Deleted.Scenes.1080p.mkv", "Movie.2019.1080p.mkv"),
						("Movie.2019.Interviews.1080p.mkv", "Movie.2019.1080p.mkv"),
						("Movie.2019.Bloopers.1080p.mkv", "Movie.2019.1080p.mkv"),
						("Planet.Earth.II.2016.Islands.1080p.BluRay.x264-GRP", "Planet.Earth.II.2016.Mountains.1080p.BluRay.x264-GRP"),
						("BBC.2019.The.Planets.1080p.WEB.h264-GRP", "BBC.2019.Dynasties.1080p.WEB.h264-GRP"),
						("Show.S01.Behind.The.Scenes.1080p", "Show.S01.1080p"), ("Show.S01.Special.1080p", "Show.S01.1080p"),
						("Show.S01.Gag.Reel.1080p", "Show.S01.1080p"), ("Show.S01.S02.1080p", "Show.S01.1080p"),
						("Show S01 + S02 1080p", "Show S01 1080p"), ("Movie.2019.II.1080p.mkv", "Movie.2019.1080p.mkv"),
						# A movie followed by the words of a pack may be several of them
						("Movie.2001.Collection.1080p.mkv", "Movie.2001.1080p.mkv"), ("Movie.2001.Saga.1080p", "Movie.2001.1080p"),
						("Movie.2001.Trilogy.1080p", "Movie.2001.1080p")]:
	differ(f"{first} / {second}", first, second)
	differ(f"{first} / {second} (S prefix)", first, second, season_prefix="S")
for name, expected in [
		# Editions and cuts
		("Blade.Runner.1982.The.Final.Cut.1080p.BluRay.x264-GRP", "Blade Runner (1982) - 1080p"),
		("Apocalypse.Now.1979.Redux.1080p.BluRay.x264-GRP", "Apocalypse Now (1979) - 1080p"),
		("Avatar.2009.Extended.Collectors.Edition.1080p.BluRay.x264-GRP", "Avatar (2009) - 1080p"),
		("Alien.1979.Director's.Cut.1080p.BluRay.x264-GRP", "Alien (1979) - 1080p"),
		("Terminator.2.1991.Extended.Special.Edition.1080p.BluRay", "Terminator 2 (1991) - 1080p"),
		("Kingdom.of.Heaven.2005.Directors.Cut.Roadshow.1080p.BluRay", "Kingdom of Heaven (2005) - 1080p"),
		("Avengers.Endgame.2019.IMAX.Enhanced.2160p.WEB-DL.DDP5.1.Atmos.HDR.HEVC-GRP", "Avengers Endgame (2019) - 4K HDR"),
		("Movie.2019.Open.Matte.1080p.WEB-DL.x264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.3D.Half-SBS.1080p.BluRay.x264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.Anniversary.Edition.1080p.BluRay", "Movie (2019) - 1080p"),
		("Movie.1960.Criterion.Collection.1080p.BluRay", "Movie (1960) - 1080p"),
		("Pelicula (2019) Version Extendida [BluRay 1080p]", "Pelicula (2019) - 1080p"),
		("Pelicula (2019) Montaje del Director [BluRay 1080p]", "Pelicula (2019) - 1080p"),
		("Pelicula (2019) Edición Especial 25 Aniversario [BluRay 1080p]", "Pelicula (2019) - 1080p"),
		# Release flags, languages, subtitles, the video
		("Movie.2019.REPACK2.1080p.BluRay.x264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.iNTERNAL.1080p.BluRay.x264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.MULTi.TRUEFRENCH.1080p.BluRay.x264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.German.Dubbed.DL.1080p.WEB.h264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.iTA-ENG.1080p.WEB.h264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.Dual.Audio.Hindi.English.1080p.WEB.h264-GRP", "Movie (2019) - 1080p"),
		("Movie.2019.HFR.1080p.WEB-DL.x264-GRP", "Movie (2019) - 1080p"),
		("Movie (2019) 10-bit 1080p", "Movie (2019) - 1080p"),
		("Movie 2019 1080p WEB-DL x264 [Eng Subs]", "Movie (2019) - 1080p"),
		("Pelicula (2019) [BluRay 1080p][AC3 5.1 Castellano DTS 5.1-Ingles+Subs][ES-EN]", "Pelicula (2019) - 1080p"),
		("Pelicula (2019) [MicroHD 1080p][AC3 5.1-Castellano-AC3 5.1 Ingles+Subs][ES-EN]", "Pelicula (2019) - 1080p"),
		("Pelicula (2019) [Dual Latino] [1080p]", "Pelicula (2019) - 1080p"),
		("Pelicula (2019) [Cast/Ing] [1080p]", "Pelicula (2019) - 1080p"),
		("Pelicula (2019) [Español] [1080p]", "Pelicula (2019) - 1080p"),
		("Pelicula (2019) Subtitulada 1080p", "Pelicula (2019) - 1080p"),
		# A site, a checksum, the group
		("Pelicula (2019) [www.DonTorrent.com] [1080p]", "Pelicula (2019) - 1080p"),
		("Gladiator II (2024) [www.site.com] 1080p.mkv", "Gladiator II (2024) - 1080p.mkv"),
		("[Group] Movie (2019) [ABCD1234].mkv", None),  # No resolution: the default template needs one
		# Packs: the words of a pack are fine after a range of years or a season
		("Harry Potter (2001-2011) Complete Collection [1080p]", "Harry Potter (2001-2011) - 1080p"),
		("Friends.S01-S10.Complete.Series.1080p", "T1-T10 - Friends - 1080p"),
		("Show.S01.Season.Pack.1080p", "T1 - Show - 1080p"),
		("Show.S01.REPACK.1080p.WEB.H264-GRP", "T1 - Show - 1080p"),
		("Show.S01.Uncensored.1080p.WEB.H264-GRP", "T1 - Show - 1080p"),
		("Show - Season 1 - Complete [1080p]", "T1 - Show - 1080p"),
		("[Group] Show S2 [Batch] [1080p]", "T2 - Show - 1080p"),
		("[Group] Show S2 (BD 1080p) [Dual Audio]", "T2 - Show - 1080p"),
		("Serie Temporada 1 Completa [HDTV 720p]", "T1 - Serie - 720p"),
		("Serie - Temporada 2 [www.DonTorrent.com] [HDTV 720p]", "T2 - Serie - 720p"),
		("Serie S01 [1080p][Castellano][Dual]", "T1 - Serie - 1080p")]:
	check(f"what every release carries: {name}", suggest_name(name), expected)
check("a checksum is no number",
	suggest_name("[Group] Movie (2019) [ABCD1234].mkv", template_movie="{title} ({year})[.{extension}]"), "Movie (2019).mkv")
check("nor is the group", suggest_name("Show.S01-GRP", template_season="{titulo} - T{temporada}"), "Show - T1")
check("unless it is just a number", suggest_name("Breaking Bad - T01-05", template_season="{titulo} - T{temporada}"), None)

# --- 'Pack' and 'Collection' with no title of the file's own -----------------------
# They may be the end of the title ('Wolf Pack'), so they only go when the
# file's own title shows it. Folders and files of the torrent agree
check("a bare episode of Wolf Pack", suggest_file_name("01.mkv", parent_name="Wolf.Pack.S01-S02.1080p", folder_name="Season 1"),
	"1x01 - Wolf Pack - 1080p.mkv")
check("and its folder", suggest_folder_name("Season 1", "Wolf.Pack.S01-S02.1080p"), "T1 - Wolf Pack - 1080p")
check("a file with just the episode", suggest_file_name("S01E01.mkv", parent_name="Wolf.Pack.S01-S02.1080p"),
	"1x01 - Wolf Pack - 1080p.mkv")
check("a bare episode of Rat Pack", suggest_file_name("01.mkv", parent_name="Rat.Pack.S01-S02.1080p", folder_name="Season 1"),
	"1x01 - Rat Pack - 1080p.mkv")
check("the single video of Wolf Pack", suggest_file_name("video.mkv", parent_name="Wolf.Pack.1080p.WEB", single_video=True),
	"Wolf Pack - 1080p.mkv")
check("the folder of a collection", suggest_folder_name("Temporada 2", "Friends.Collection.1080p"), "T2 - Friends Collection - 1080p")
check("agrees with its bare files", suggest_file_name("01.mkv", parent_name="Friends.Collection.1080p", folder_name="Temporada 2"),
	"2x01 - Friends Collection - 1080p.mkv")
check("a file of its own title shows the show",
	suggest_file_name("Friends.S02E01.mkv", parent_name="Friends.Collection.1080p", folder_name="Temporada 2"), "2x01 - Friends - 1080p.mkv")
check("and so does a folder of its own title", suggest_folder_name("Friends.S02", "Friends.Collection.1080p"), "T2 - Friends - 1080p")
check("a complete collection is no title",
	suggest_file_name("01.mkv", parent_name="Friends.The.Complete.Collection.1080p", folder_name="Season 1"), "1x01 - Friends - 1080p.mkv")
check("nor a complete series", suggest_folder_name("Season 1", "Wolf.Pack.Complete.Series.1080p"), "T1 - Wolf Pack - 1080p")

# --- 'Season - N' ------------------------------------------------------------------
check_fields("Show - Season - 1 - 1080p", title="Show", season="1", chapter="T1", is_series=True)
check("a season behind a spaced dash", (series_identity("Show Season - 1 1080p") or {}).get("season"), 1)
check("is renamed", suggest_name("Show - Season - 1 - 1080p"), "T1 - Show - 1080p")
check("the ordinal one still wins", suggest_name("[Group] Show Name 2nd Season - 05 [1080p].mkv"), "2x05 - Show Name - 1080p.mkv")

# --- More sample clips -------------------------------------------------------------
for path in ["sample.show.name.s01e01.720p.hdtv.x264-grp.mkv", "Sample.Show.Name.S01E01.mkv", "sample_show.name.s01e01.mkv",
			"sample_movie.name.2019.1080p.mkv", "Show.S01E01.sample.WEB.mkv", "Show.S01E01.sample.WEB-GRP.mkv",
			"Show.S01E01.sample.WEB.1080p.mkv"]:
	check(f"{path} is a sample", is_sample_file(path), True)
for path in ["Samples_of_Life_2020.mkv", "Samples.of.Life.S01E01.mkv", "Free.Sample.Web.Series.2016.mkv", "Sample.People.2000.1080p.mkv"]:
	check(f"{path} is no sample", is_sample_file(path), False)
check("a leading sample at the torrent root gets no name",
	suggest_file_name("sample.show.name.s01e01.720p.hdtv.x264-grp.mkv", parent_name="Show.Name.S01.720p.HDTV.x264-GRP"), None)
check("its episode does", suggest_file_name("show.name.s01e01.720p.hdtv.x264-grp.mkv", parent_name="Show.Name.S01.720p.HDTV.x264-GRP"),
	"1x01 - Show Name - 720p.mkv")

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print("ALL TESTS OK")
