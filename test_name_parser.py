"""Test battery for name_parser.py. Run with: python3 test_name_parser.py"""

from name_parser import (companion_subtitle_name, parse_metadata, render_template, suggest_file_name,
						suggest_name, validate_template, TemplateError, _PLATFORM_TAGS, CANONICAL_FIELDS)

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

if FAILED:
	print(f"{len(FAILED)} FAILED:\n")
	print("\n\n".join(FAILED))
	raise SystemExit(1)
print("ALL TESTS OK")
