"""Tests for AN HTML debate parser (src/parsers/an_html_debates.py)."""

from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from src.parsers.an_html_debates import (
    HtmlFormat,
    _build_reference_key,
    _extract_function_from_html,
    _reconstruct_url,
    detect_format,
    extract_date_from_html,
    parse_an_html_debate,
    parse_font_b,
    parse_leg13,
    parse_semantic,
    parse_strong,
)

# ─── Inline HTML fixtures ───

SAMPLE_LEG13_HTML = """\
<html>
<head>
<meta name="LEGISLATURE_ID" content="13">
<meta name="NUMERO_DOCUMENT" content="20120140">
<meta name="QUANTIEME_SEANCE" content="Deuxieme seance du mardi 28 fevrier 2012">
<meta name="ID_DEPUTES_INTERVENANTS" content="ID_350;ID_356">
<meta name="TXT_DEPUTES_INTERVENANTS" content="Jean-Pierre Balligand;Gerard Bapt">
<title>Assemblee nationale - 2eme seance du mardi 28 fevrier 2012</title>
</head>
<body>
<!-- RSPEAK_START -->
<div id="englobe">
  <h2 class="entete">Assemblee nationale<br>XIIIe legislature<br>Session ordinaire de 2011-2012</h2>
  <h1 class="seance">Deuxieme seance du mardi 28 fevrier 2012</h1>

  <h5 class="presidence"><b>PRESIDENCE DE M. BERNARD ACCOYER</b></h5>

  <p><a name="INTER_0"></a><b><a href="/tribun/fiches_id/267087.asp">\
M. Jean Dionis du Sejour</a></b>. Ma question s'adresse au ministre \
de l'economie. L'article 8 ter du projet pose un probleme majeur.</p>

  <p><b>M. le president.</b> La parole est au ministre.</p>

  <p><a name="INTER_1"></a><b>M. Alain Juppe,</b>\
<i> ministre d'Etat, ministre des affaires etrangeres.</i> \
Monsieur le depute, le Gouvernement a examine cette question.</p>

  <p><a name="INTER_ADT_80"></a><b><a href="/tribun/fiches_id/1234.asp">\
M. Gilles Carrez</a>, </b><i>rapporteur</i>. \
Cet amendement vise a modifier l'article 12 du projet.</p>

  <p><i>(L'amendement no 40 n'est pas adopte.)</i></p>
</div>
<!-- RSPEAK_STOP -->
</body>
</html>
"""

SAMPLE_SEMANTIC_HTML = """\
<html>
<head>
<meta name="LEGISLATURE_ID" content="12">
<meta name="NUMERO_DOCUMENT" content="20060023">
<meta name="ID_DEPUTES_INTERVENANTS" content="ID_551;ID_2671">
<meta name="TXT_DEPUTES" content="LISTE_NON_ETABLIE">
<title>Assemblee nationale ~ Premiere seance du mardi 4 octobre 2005</title>
</head>
<body>
<div id="englobe">
  <h2 class="entete">Assemblee nationale<br><br>Compte rendu<br>integral</h2>
  <h1 class="seance">Premiere seance du mardi 4 octobre 2005</h1>
  <h1 class="session">23e seance de la session ordinaire 2005-2006</h1>

  <h5 class="presidence"><b>PRESIDENCE DE M. MAURICE LEROY,</b></h5>
  <h5 class="presidence">vice-president</h5>

  <p><b>M.\u00a0le president</b>. La seance est ouverte.</p>
  <p><i>(La seance est ouverte a vingt et une heures trente.)</i></p>

  <h2 class="titre1"><a name="P36_1261"></a>Loi de finances pour 2006</h2>
  <h2 class="titre2"><a name="P37_1286"></a>Suite de la discussion d'un projet de loi</h2>

  <p><b><!-- TRANSIT - HYPERLINK --><a href="/12/tribun/fiches_id/2671.asp">\
M. Jean-Claude Sandrier</a></b>. Monsieur le president, \
je souhaite aborder l'article 15 de ce projet.</p>

  <p><b>M.\u00a0Dupont</b>. Je suis d'accord avec mon collegue sur ce point important.</p>
</div>
</body>
</html>
"""

SAMPLE_FONTB_HTML = """\
<html>
<head>
<meta name="LEGISLATURE_ID" content="12">
<meta name="TYPE_DOCUMENT" content="Compte rendu integral">
<meta name="NUMERO_DOCUMENT" content="20040055">
<title>Deuxieme seance du vendredi 7 novembre 2003</title>
</head>
<body>
<p align="center"><a name="P8_86"></a>
<font face="Arial" size="2">Deuxieme seance du vendredi 7 novembre 2003</font></p>

<p align="justify"><b><font face="Arial" size="2">
PRESIDENCE DE MME HELENE MIGNON</font></b></p>

<p align="justify"><font face="Arial" size="2"><b>Mme\u00a0la\u00a0presidente.</b>
La seance est ouverte.</font></p>

<p><i><font face="Arial" size="2">(La seance est ouverte a quinze heures.)</font></i></p>

<p align="center"><b><font face="Arial" size="2">
LOI DE FINANCES POUR 2004</font></b></p>

<p align="justify"><font face="Arial" size="2"><b>M.\u00a0Daniel\u00a0Garrigue</b>.
Madame la presidente, cet article 3 du projet necessite une attention particuliere.</font></p>

<p align="justify"><font face="Arial" size="2">\
<b>M.\u00a0Pierre\u00a0Mehaignerie,</b><i> ministre de la justice.</i>
Le Gouvernement partage cette preoccupation.</font></p>
</body>
</html>
"""

SAMPLE_STRONG_HTML = """\
<html>
<head>
<title>Journaux Officiels - 1re seance du mardi 1er octobre 2002</title>
<meta name="description" content="Compte rendu integral">
</head>
<body>
<p align="CENTER"><font size=2 color=#0000FF>COMPTE RENDU INTEGRAL</font><br>
<strong>PRESIDENCE DE M. JEAN-LOUIS DEBRE</strong></p>

<p align="JUSTIFY"><strong>\u00a0\u00a0\u00a0\u00a0M. le president.</strong>
La seance est ouverte.</p>

<p><i>\u00a0\u00a0\u00a0\u00a0(La seance est ouverte a quinze heures.)</i></p>

<p align="CENTER">OUVERTURE DE LA SESSION ORDINAIRE</p>

<p align="JUSTIFY"><strong>\u00a0\u00a0\u00a0\u00a0M. Jean-Marc Roubaud.</strong>
Monsieur le president, ma question concerne l'article 5 du projet de loi de finances.</p>

<p align="JUSTIFY"><strong>\u00a0\u00a0\u00a0\u00a0Mme Roselyne Bachelot-Narquin.</strong>
Je souhaite egalement intervenir sur ce sujet important pour nos concitoyens.</p>
</body>
</html>
"""


# ─── Test classes ───


class TestDetectFormat:
    def test_detects_leg13(self):
        assert detect_format(SAMPLE_LEG13_HTML) == HtmlFormat.LEG13

    def test_detects_semantic(self):
        assert detect_format(SAMPLE_SEMANTIC_HTML) == HtmlFormat.SEMANTIC

    def test_detects_font_b(self):
        assert detect_format(SAMPLE_FONTB_HTML) == HtmlFormat.FONT_B

    def test_detects_strong(self):
        assert detect_format(SAMPLE_STRONG_HTML) == HtmlFormat.STRONG

    def test_leg13_priority_over_semantic(self):
        """LEG13 pages also have <div id="englobe"> — LEG13 must be detected first."""
        html = (
            '<html><!-- RSPEAK_START --><div id="englobe">'
            '<h2 class="titre1">X</h2><h1 class="seance">Y</h1>'
            '</div><!-- RSPEAK_STOP --></html>'
        )
        assert detect_format(html) == HtmlFormat.LEG13

    def test_leg13_via_meta_tag(self):
        """LEG13 detected via LEGISLATURE_ID meta tag when no RSPEAK marker."""
        html = (
            '<html><head><meta name="LEGISLATURE_ID" content="13">'
            '</head><body></body></html>'
        )
        assert detect_format(html) == HtmlFormat.LEG13


class TestExtractDateFromHtml:
    def test_date_from_h1_seance(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = extract_date_from_html(soup, HtmlFormat.LEG13)
        assert result == datetime(2012, 2, 28)

    def test_date_from_title(self):
        soup = BeautifulSoup(SAMPLE_STRONG_HTML, "html.parser")
        result = extract_date_from_html(soup, HtmlFormat.STRONG)
        assert result == datetime(2002, 10, 1)

    def test_date_from_meta_quantieme(self):
        html = (
            '<html><head><meta name="QUANTIEME_SEANCE" '
            'content="Premiere seance du lundi 15 janvier 2007">'
            '</head><body></body></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        result = extract_date_from_html(soup, HtmlFormat.LEG13)
        assert result == datetime(2007, 1, 15)

    def test_no_date_returns_none(self):
        soup = BeautifulSoup("<html><head></head><body></body></html>", "html.parser")
        result = extract_date_from_html(soup, HtmlFormat.LEG13)
        assert result is None

    def test_date_from_font_b_title(self):
        soup = BeautifulSoup(SAMPLE_FONTB_HTML, "html.parser")
        result = extract_date_from_html(soup, HtmlFormat.FONT_B)
        assert result == datetime(2003, 11, 7)

    def test_date_from_semantic_h1(self):
        soup = BeautifulSoup(SAMPLE_SEMANTIC_HTML, "html.parser")
        result = extract_date_from_html(soup, HtmlFormat.SEMANTIC)
        assert result == datetime(2005, 10, 4)


class TestExtractFunctionFromHtml:
    def _make_tag(self, html_str):
        soup = BeautifulSoup(html_str, "html.parser")
        return soup

    def test_ministre_from_italic(self):
        soup = self._make_tag('<p><b>M. Juppe,</b><i> ministre de l\'economie</i></p>')
        bold = soup.find("b")
        italic = soup.find("i")
        assert _extract_function_from_html(bold, italic, "M. Juppe") == "ministre"

    def test_rapporteur_from_italic(self):
        soup = self._make_tag('<p><b>M. Carrez,</b><i> rapporteur</i></p>')
        bold = soup.find("b")
        italic = soup.find("i")
        assert _extract_function_from_html(bold, italic, "M. Carrez") == "rapporteur"

    def test_president_from_name(self):
        result = _extract_function_from_html(None, None, "M. le président")
        assert result == "president"

    def test_no_function_tag(self):
        soup = self._make_tag('<p><b>M. Dupont</b></p>')
        bold = soup.find("b")
        assert _extract_function_from_html(bold, None, "M. Dupont") == "autre"

    def test_vocative_not_treated_as_function(self):
        """<i> tag with 'monsieur le ministre' is a vocative, NOT a function."""
        soup = self._make_tag('<p><b>M. Dupont,</b><i> monsieur le ministre, je voudrais</i></p>')
        bold = soup.find("b")
        italic = soup.find("i")
        assert _extract_function_from_html(bold, italic, "M. Dupont") == "autre"

    def test_secretaire_etat(self):
        soup = self._make_tag("<p><b>M. Martin,</b><i> secrétaire d'état</i></p>")
        bold = soup.find("b")
        italic = soup.find("i")
        assert _extract_function_from_html(bold, italic, "M. Martin") == "ministre"


class TestParseLeg13:
    def test_deputy_with_link(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        deputies = [i for i in result.interventions if "Dionis" in i.orateur]
        assert len(deputies) == 1
        assert deputies[0].orateur == "M. Jean Dionis du Sejour"

    def test_president_extraction(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        presidents = [i for i in result.interventions if i.fonction == "president"]
        assert len(presidents) >= 1

    def test_minister_extraction(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        ministers = [i for i in result.interventions if i.fonction == "ministre"]
        assert len(ministers) >= 1
        assert any("Juppe" in m.orateur for m in ministers)

    def test_rapporteur_extraction(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        rapporteurs = [i for i in result.interventions if i.fonction == "rapporteur"]
        assert len(rapporteurs) >= 1
        assert any("Carrez" in r.orateur for r in rapporteurs)

    def test_article_mentions(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        all_articles = []
        for interv in result.interventions:
            all_articles.extend(interv.articles_mentionnes)
        assert "8 ter" in all_articles

    def test_chambre_and_etape(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        assert result.chambre == "AN"
        assert result.etape == "seance_publique"

    def test_contenu_integral_populated(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        assert len(result.contenu_integral) > 0
        assert "Dionis" in result.contenu_integral

    def test_date_extracted(self):
        soup = BeautifulSoup(SAMPLE_LEG13_HTML, "html.parser")
        result = parse_leg13(soup)
        assert result is not None
        assert result.date == datetime(2012, 2, 28)


class TestParseSemantic:
    def test_speaker_with_transit_link(self):
        soup = BeautifulSoup(SAMPLE_SEMANTIC_HTML, "html.parser")
        result = parse_semantic(soup)
        assert result is not None
        sandrier = [i for i in result.interventions if "Sandrier" in i.orateur]
        assert len(sandrier) == 1

    def test_speaker_without_link(self):
        soup = BeautifulSoup(SAMPLE_SEMANTIC_HTML, "html.parser")
        result = parse_semantic(soup)
        assert result is not None
        dupont = [i for i in result.interventions if "Dupont" in i.orateur]
        assert len(dupont) == 1

    def test_presidency_not_intervention(self):
        """Presidency change header should not be treated as an intervention."""
        soup = BeautifulSoup(SAMPLE_SEMANTIC_HTML, "html.parser")
        result = parse_semantic(soup)
        assert result is not None
        # MAURICE LEROY should not appear as a speaker
        leroy = [i for i in result.interventions if "LEROY" in i.orateur.upper()]
        assert len(leroy) == 0

    def test_reference_key_set_by_parent(self):
        """Reference key is set by parse_an_html_debate, not the format parser."""
        soup = BeautifulSoup(SAMPLE_SEMANTIC_HTML, "html.parser")
        result = parse_semantic(soup)
        assert result is not None
        assert result.reference is None  # Set by parse_an_html_debate()

    def test_date(self):
        soup = BeautifulSoup(SAMPLE_SEMANTIC_HTML, "html.parser")
        result = parse_semantic(soup)
        assert result is not None
        assert result.date == datetime(2005, 10, 4)

    def test_president_detected(self):
        soup = BeautifulSoup(SAMPLE_SEMANTIC_HTML, "html.parser")
        result = parse_semantic(soup)
        assert result is not None
        presidents = [i for i in result.interventions if i.fonction == "president"]
        assert len(presidents) >= 1


class TestParseFontB:
    def test_speaker_with_nbsp(self):
        soup = BeautifulSoup(SAMPLE_FONTB_HTML, "html.parser")
        result = parse_font_b(soup)
        assert result is not None
        garrigue = [i for i in result.interventions if "Garrigue" in i.orateur]
        assert len(garrigue) == 1

    def test_section_header_not_intervention(self):
        soup = BeautifulSoup(SAMPLE_FONTB_HTML, "html.parser")
        result = parse_font_b(soup)
        assert result is not None
        # "LOI DE FINANCES POUR 2004" should not be an intervention
        for interv in result.interventions:
            assert "LOI DE FINANCES" not in interv.contenu

    def test_didascalie_skipped(self):
        soup = BeautifulSoup(SAMPLE_FONTB_HTML, "html.parser")
        result = parse_font_b(soup)
        assert result is not None
        for interv in result.interventions:
            assert "La seance est ouverte a quinze heures" not in interv.contenu

    def test_minister_detection(self):
        soup = BeautifulSoup(SAMPLE_FONTB_HTML, "html.parser")
        result = parse_font_b(soup)
        assert result is not None
        ministers = [i for i in result.interventions if i.fonction == "ministre"]
        assert len(ministers) >= 1
        assert any("Mehaignerie" in m.orateur for m in ministers)

    def test_date(self):
        soup = BeautifulSoup(SAMPLE_FONTB_HTML, "html.parser")
        result = parse_font_b(soup)
        assert result is not None
        assert result.date == datetime(2003, 11, 7)


class TestParseStrong:
    def test_speaker_with_nbsp_indentation(self):
        soup = BeautifulSoup(SAMPLE_STRONG_HTML, "html.parser")
        result = parse_strong(soup)
        assert result is not None
        roubaud = [i for i in result.interventions if "Roubaud" in i.orateur]
        assert len(roubaud) == 1

    def test_multiple_speakers(self):
        soup = BeautifulSoup(SAMPLE_STRONG_HTML, "html.parser")
        result = parse_strong(soup)
        assert result is not None
        assert len(result.interventions) >= 2

    def test_date_from_jo_title(self):
        soup = BeautifulSoup(SAMPLE_STRONG_HTML, "html.parser")
        result = parse_strong(soup)
        assert result is not None
        assert result.date == datetime(2002, 10, 1)

    def test_president_detected(self):
        soup = BeautifulSoup(SAMPLE_STRONG_HTML, "html.parser")
        result = parse_strong(soup)
        assert result is not None
        presidents = [i for i in result.interventions if i.fonction == "president"]
        assert len(presidents) >= 1

    def test_didascalie_skipped(self):
        soup = BeautifulSoup(SAMPLE_STRONG_HTML, "html.parser")
        result = parse_strong(soup)
        assert result is not None
        for interv in result.interventions:
            assert "La seance est ouverte a quinze heures" not in interv.contenu

    def test_section_header_not_content(self):
        soup = BeautifulSoup(SAMPLE_STRONG_HTML, "html.parser")
        result = parse_strong(soup)
        assert result is not None
        for interv in result.interventions:
            assert "OUVERTURE DE LA SESSION" not in interv.contenu


class TestParseAnHtmlDebate:
    def test_full_pipeline_leg13(self, tmp_path):
        """Full parse pipeline for LEG13 input."""
        html_file = tmp_path / "an_html" / "leg13" / "2011-2012" / "20120140.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_text(SAMPLE_LEG13_HTML, encoding="utf-8")

        result = parse_an_html_debate(html_file)
        assert result is not None
        assert result.chambre == "AN"
        assert result.etape == "seance_publique"
        assert len(result.interventions) >= 2
        assert result.reference is not None
        assert result.reference.startswith("archive_an_html:")

    def test_returns_none_for_empty_html(self, tmp_path):
        html_file = tmp_path / "an_html" / "leg13" / "2011-2012" / "empty.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_text("<html></html>", encoding="utf-8")

        result = parse_an_html_debate(html_file)
        assert result is None

    def test_returns_none_for_malformed_html(self, tmp_path):
        html_file = tmp_path / "an_html" / "leg13" / "2011-2012" / "bad.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_text("not even html", encoding="utf-8")

        result = parse_an_html_debate(html_file)
        assert result is None

    def test_minimum_intervention_threshold(self, tmp_path):
        """Pages with < 2 interventions return None."""
        html = """\
<html><head>
<title>Premiere seance du mardi 4 octobre 2005</title>
</head><body>
<div id="englobe">
<h1 class="seance">Premiere seance du mardi 4 octobre 2005</h1>
<h2 class="titre1">X</h2>
<p><b>M. Dupont</b>. Un seul orateur ici.</p>
</div></body></html>"""
        html_file = tmp_path / "an_html" / "leg12" / "2005-2006" / "test.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_text(html, encoding="utf-8")

        result = parse_an_html_debate(html_file)
        assert result is None

    def test_semantic_full_pipeline(self, tmp_path):
        html_file = tmp_path / "an_html" / "leg12" / "2005-2006" / "20060023.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_bytes(SAMPLE_SEMANTIC_HTML.encode("iso-8859-1"))

        result = parse_an_html_debate(html_file)
        assert result is not None
        assert result.reference.startswith("archive_an_html:leg12:")

    def test_font_b_full_pipeline(self, tmp_path):
        html_file = tmp_path / "an_html" / "leg12" / "2003-2004" / "20040055.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_bytes(SAMPLE_FONTB_HTML.encode("iso-8859-1"))

        result = parse_an_html_debate(html_file)
        assert result is not None
        assert result.chambre == "AN"

    def test_strong_full_pipeline(self, tmp_path):
        html_file = tmp_path / "an_html" / "leg12" / "2002-2003" / "20030001.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_bytes(SAMPLE_STRONG_HTML.encode("iso-8859-1"))

        result = parse_an_html_debate(html_file)
        assert result is not None
        assert result.chambre == "AN"

    def test_commission_elargie_etape(self, tmp_path):
        """Commission élargie files should have etape='commission'."""
        html_file = tmp_path / "an_html" / "leg13" / "commission_elargie" / "plf2010" / "C001.html"
        html_file.parent.mkdir(parents=True)
        html_file.write_text(SAMPLE_LEG13_HTML, encoding="utf-8")

        result = parse_an_html_debate(html_file)
        assert result is not None
        assert result.etape == "commission"
        assert "commission_elargie" in result.reference

    def test_utf8_bom_encoding(self, tmp_path):
        """File with UTF-8 BOM should be read correctly."""
        html_file = tmp_path / "an_html" / "leg13" / "2011-2012" / "utf8bom.html"
        html_file.parent.mkdir(parents=True)
        # Write with BOM
        with open(html_file, "wb") as f:
            f.write(b"\xef\xbb\xbf")
            f.write(SAMPLE_LEG13_HTML.encode("utf-8"))

        result = parse_an_html_debate(html_file)
        assert result is not None
        assert len(result.interventions) >= 2

    def test_iso_8859_1_encoding(self, tmp_path):
        """File with iso-8859-1 encoding should be read correctly."""
        html_file = tmp_path / "an_html" / "leg13" / "2011-2012" / "latin1.html"
        html_file.parent.mkdir(parents=True)
        # Replace some chars with accented ones that are iso-8859-1 specific
        html_with_accents = SAMPLE_LEG13_HTML.replace("economie", "économie")
        html_file.write_bytes(html_with_accents.encode("iso-8859-1"))

        result = parse_an_html_debate(html_file)
        assert result is not None


class TestBuildReferenceKey:
    def test_regular_session(self):
        path = Path("/data/raw/an_html/leg13/2011-2012/20120140.html")
        result = _build_reference_key(path)
        assert result == "archive_an_html:leg13:2011-2012:20120140"

    def test_commission_elargie(self):
        path = Path("/data/raw/an_html/leg13/commission_elargie/plf2010/C001.html")
        result = _build_reference_key(path)
        assert result == "archive_an_html:leg13:commission_elargie/plf2010:C001"

    def test_leg12(self):
        path = Path("/data/raw/an_html/leg12/2003-2004/20040055.html")
        result = _build_reference_key(path)
        assert result == "archive_an_html:leg12:2003-2004:20040055"

    def test_no_an_html_in_path(self):
        path = Path("/some/other/path/file.html")
        assert _build_reference_key(path) is None

    def test_too_short_path(self):
        path = Path("/data/raw/an_html/leg13/file.html")
        assert _build_reference_key(path) is None


class TestReconstructUrl:
    def test_regular_session(self):
        path = Path("/data/raw/an_html/leg13/2011-2012/20120140.html")
        result = _reconstruct_url(path)
        assert result == "https://www.assemblee-nationale.fr/13/cri/2011-2012/20120140.asp"

    def test_commission_elargie(self):
        path = Path("/data/raw/an_html/leg13/commission_elargie/plf2010/C001.html")
        result = _reconstruct_url(path)
        assert result == "https://www.assemblee-nationale.fr/13/budget/plf2010/commissions_elargies/cr/C001.asp"

    def test_leg12(self):
        path = Path("/data/raw/an_html/leg12/2005-2006/20060023.html")
        result = _reconstruct_url(path)
        assert result == "https://www.assemblee-nationale.fr/12/cri/2005-2006/20060023.asp"

    def test_no_an_html_in_path(self):
        path = Path("/some/other/path/file.html")
        assert _reconstruct_url(path) is None

    def test_too_short_path(self):
        path = Path("/data/raw/an_html/leg13/file.html")
        assert _reconstruct_url(path) is None
