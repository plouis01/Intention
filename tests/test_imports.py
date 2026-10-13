"""Basic import tests to verify project structure."""

import pytest


def test_import_models():
    """Test that database models can be imported."""
    from src.db.models import (
        Amendement,
        Article,
        Debat,
        DossierLegislatif,
        Intervention,
        Loi,
    )

    assert Loi is not None
    assert Article is not None
    assert DossierLegislatif is not None
    assert Debat is not None
    assert Intervention is not None
    assert Amendement is not None


def test_import_config():
    """Test that config can be imported."""
    from src.utils.config import Settings, get_settings

    settings = get_settings()
    assert settings is not None
    assert hasattr(settings, "database_url")


def test_import_collectors():
    """Test that collectors can be imported."""
    from src.collectors.legifrance import LegifranceClient
    from src.collectors.assemblee import AssembleeCollector
    from src.collectors.senat import SenatCollector

    assert LegifranceClient is not None
    assert AssembleeCollector is not None
    assert SenatCollector is not None


def test_import_parsers():
    """Test that parsers can be imported."""
    from src.parsers.debates import (
        ParsedDebat,
        Intervention,
        extract_article_mentions,
    )
    from src.parsers.amendments import (
        ParsedAmendement,
        normalize_sort,
    )

    assert ParsedDebat is not None
    assert ParsedAmendement is not None


def test_extract_article_mentions():
    """Test article extraction from text."""
    from src.parsers.debates import extract_article_mentions

    text = "Cet amendement modifie l'article 12 et l'article 15 bis."
    mentions = extract_article_mentions(text)

    assert "12" in mentions
    assert "15 bis" in mentions


def test_extract_article_mentions_premier():
    """Test 'article premier' is extracted as '1'."""
    from src.parsers.debates import extract_article_mentions

    assert "1" in extract_article_mentions("L'article premier du projet de loi")
    assert "1" in extract_article_mentions("l'article 1er de la loi")


def test_extract_article_mentions_liminaire():
    """Test 'article liminaire' is extracted."""
    from src.parsers.debates import extract_article_mentions

    assert "liminaire" in extract_article_mentions("L'article liminaire prévoit que")


def test_extract_article_mentions_cgi_compound():
    """Test CGI compound references like 150-0 B ter."""
    from src.parsers.debates import extract_article_mentions

    mentions = extract_article_mentions("l'article 150-0 B ter du CGI")
    assert "150-0 b ter" in mentions

    mentions = extract_article_mentions("l'article 199 terdecies-0 A du CGI")
    assert any("199 terdecies-0 a" in m for m in mentions)


def test_extract_article_mentions_extended_latin():
    """Test latin suffixes beyond septies."""
    from src.parsers.debates import extract_article_mentions

    assert "3 octies" in extract_article_mentions("article 3 octies")
    assert "5 nonies" in extract_article_mentions("l'article 5 nonies")
    assert "7 decies" in extract_article_mentions("article 7 decies")
    assert "8 undecies" in extract_article_mentions("l'article 8 undecies")
    assert "10 terdecies" in extract_article_mentions("article 10 terdecies")


def test_extract_article_mentions_ranges():
    """Test article ranges and pairs."""
    from src.parsers.debates import extract_article_mentions

    mentions = extract_article_mentions("les articles 1 à 12 du projet")
    assert "1" in mentions
    assert "12" in mentions

    mentions = extract_article_mentions("les articles 3 et 5 sont modifiés")
    assert "3" in mentions
    assert "5" in mentions


def test_extract_article_mentions_avant_apres():
    """Test 'avant/après l'article X' captures article X."""
    from src.parsers.debates import extract_article_mentions

    assert "21" in extract_article_mentions("avant l'article 21")
    assert "3" in extract_article_mentions("après l'article 3")


def test_normalize_sort():
    """Test sort normalization."""
    from src.parsers.amendments import normalize_sort

    assert normalize_sort("Adopté") == "adopte"
    assert normalize_sort("Rejeté") == "rejete"
    assert normalize_sort("retiré") == "retire"
    assert normalize_sort(None) is None


def test_extract_law_reference_pre2020():
    """Test that extract_law_reference_from_content works for pre-2020 debates."""
    from src.db.import_data import extract_law_reference_from_content

    # Pre-2020 content that was blocked by the old 2020 <= year filter
    result = extract_law_reference_from_content(
        "Discussion du projet de loi de finances pour 2015"
    )
    assert result is not None
    assert result == ("LF", 2015)

    result = extract_law_reference_from_content(
        "Examen du PLFSS 2014 en séance publique"
    )
    assert result is not None
    assert result == ("LFSS", 2014)

    result = extract_law_reference_from_content(
        "Le PLFR 2012 prévoit des dispositions relatives..."
    )
    assert result is not None
    assert result == ("LFR", 2012)

    # Still works for post-2020
    result = extract_law_reference_from_content(
        "Le PLF 2024 modifie l'article 150-0 B ter"
    )
    assert result is not None
    assert result == ("LF", 2024)


def test_senat_url_generation():
    """Test Sénat URL generation from debate windows."""
    from datetime import datetime
    from scripts.import_senat_debats import generate_senat_urls, compute_debate_windows

    # Test URL generation for a specific window
    urls = generate_senat_urls(
        datetime(2023, 11, 1),
        datetime(2023, 11, 3),
    )
    assert len(urls) == 3
    assert urls[0] == (
        datetime(2023, 11, 1),
        "https://www.senat.fr/seances/s202311/s20231101/s20231101_mono.html",
    )
    assert urls[2] == (
        datetime(2023, 11, 3),
        "https://www.senat.fr/seances/s202311/s20231103/s20231103_mono.html",
    )

    # Test debate window computation - LF
    laws = [
        {
            "numero": "2023-1322",
            "type": "LF",
            "annee_budgetaire": 2024,
            "date_promulgation": "2023-12-29",
            "titre": "LF 2024",
        }
    ]
    windows = compute_debate_windows(laws)
    assert len(windows) == 1
    assert windows[0]["start_date"] == datetime(2023, 10, 1)
    assert windows[0]["end_date"] == datetime(2023, 12, 31)

    # Test debate window computation - LFR (date-relative)
    from datetime import timedelta
    lfr_laws = [
        {
            "numero": "2012-1510",
            "type": "LFR",
            "annee_budgetaire": 2012,
            "date_promulgation": "2012-12-29",
            "titre": "LFR 2012 (3)",
        }
    ]
    lfr_windows = compute_debate_windows(lfr_laws)
    assert len(lfr_windows) == 1
    assert lfr_windows[0]["end_date"] == datetime(2012, 12, 29)
    assert lfr_windows[0]["start_date"] == datetime(2012, 12, 29) - timedelta(days=90)

    # Test debate window computation - LFSS
    lfss_laws = [
        {
            "numero": "2023-1250",
            "type": "LFSS",
            "annee_budgetaire": 2024,
            "date_promulgation": "2023-12-26",
            "titre": "LFSS 2024",
        }
    ]
    lfss_windows = compute_debate_windows(lfss_laws)
    assert len(lfss_windows) == 1
    assert lfss_windows[0]["start_date"] == datetime(2023, 10, 1)
    assert lfss_windows[0]["end_date"] == datetime(2023, 12, 15)


def test_cgi_extractor_recovers_missing_articles():
    """Test that the CGI extractor correctly extracts all 22 originally-missing articles.

    All 22 articles are extractable from synthetic "du CGI"/"du code général" text.
    However, only 11 actually appear in the finance law database (2012-2025):
      1, 150 VH bis, 211, 259, 575, 757 B, 788, 790 B, 982, 1395, 1599

    The other 11 don't appear in law article text as bare references:
      - Not referenced at all: 1 A, 294, 750, 1649-0 A
      - Only suffixed variants (already in the index): 217 (→217 undecies),
        1384 (→1384 A/B/C), 1388 (→1388 ter/sexies), 1466 (→1466 A/D/E/F),
        1679 (→1679 septies), 1681 (→1681 ter/sexies), 1691 (→1691 ter)
    """
    from src.analysis.cgi_extractor import CGIExtractor

    extractor = CGIExtractor()

    # Articles recovered via _is_accidental_capture fix ("du CGI" / "du code général")
    accidental_capture_cases = {
        "1": "L'article 1 du code général des impôts est ainsi modifié",
        "575": "L'article 575 du code général des impôts est complété",
        "788": "L'article 788 du CGI est ainsi modifié",
        "982": "L'article 982 du CGI est ainsi rédigé",
    }

    for expected_ref, text in accidental_capture_cases.items():
        refs = extractor.extract_cgi_references(text)
        found = {r.article_ref for r in refs}
        assert expected_ref in found, (
            f"Expected '{expected_ref}' from: {text!r}, got: {found}"
        )

    # Articles recovered via _is_other_code_reference narrowing
    # These appear in contexts that also mention other codes nearby
    other_code_cases = {
        "1 A": "l'article 1 A du code général des impôts",
        "211": "l'article 211 du code général des impôts est complété",
        "217": "l'article 217 du CGI est ainsi modifié",
        "259": "l'article 259 du code général des impôts",
        "294": "l'article 294 du CGI est ainsi modifié",
        "750": "l'article 750 du code général des impôts",
        "757 B": "l'article 757 B du CGI est ainsi modifié",
        "790 B": "l'article 790 B du code général des impôts",
        "1384": "l'article 1384 du code général des impôts est complété",
        "1388": "l'article 1388 du CGI est ainsi modifié",
        "1395": "l'article 1395 du code général des impôts",
        "1466": "l'article 1466 du CGI est ainsi modifié",
        "1599": "l'article 1599 du code général des impôts",
        "1649-0 A": "l'article 1649-0 A du CGI est ainsi modifié",
        "1679": "l'article 1679 du code général des impôts",
        "1681": "l'article 1681 du CGI est ainsi modifié",
        "1691": "l'article 1691 du code général des impôts",
        "150 VH bis": "l'article 150 VH bis du CGI est ainsi modifié",
    }

    for expected_ref, text in other_code_cases.items():
        refs = extractor.extract_cgi_references(text)
        found = {r.article_ref for r in refs}
        assert expected_ref in found, (
            f"Expected '{expected_ref}' from: {text!r}, got: {found}"
        )


def test_cgi_extractor_still_rejects_false_positives():
    """Test that the CGI extractor still filters out non-CGI references."""
    from src.analysis.cgi_extractor import CGIExtractor

    extractor = CGIExtractor()

    # CSS-style references (L. prefix) should be rejected
    refs = extractor.extract_cgi_references(
        "L'article L. 111-1 du code de la sécurité sociale est modifié"
    )
    css_refs = {r.article_ref for r in refs}
    assert "111-1" not in css_refs, f"Should reject CSS ref L.111-1, got: {css_refs}"

    # Accidental captures should still be rejected
    refs = extractor.extract_cgi_references(
        "l'article 2 de la présente loi prévoit"
    )
    found = {r.article_ref for r in refs}
    assert "2 d" not in found, f"Should reject accidental capture '2 d', got: {found}"

    # Ordinal continuations should still be rejected
    refs = extractor.extract_cgi_references(
        "à l'article 278 ter du CGI"
    )
    found = {r.article_ref for r in refs}
    assert "278 t" not in found, f"Should reject partial ordinal '278 t', got: {found}"
    assert "278 ter" in found, f"Should find '278 ter', got: {found}"


def test_extract_jorf_id_from_url():
    """Test safe JORF ID extraction from Légifrance URLs (3A fix)."""
    from src.db.import_data import extract_jorf_id_from_url

    # Normal URL
    assert extract_jorf_id_from_url(
        "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000031733082"
    ) == "JORFTEXT000031733082"

    # Trailing slash
    assert extract_jorf_id_from_url(
        "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000031733082/"
    ) == "JORFTEXT000031733082"

    # None and empty
    assert extract_jorf_id_from_url(None) is None
    assert extract_jorf_id_from_url("") is None
    assert extract_jorf_id_from_url("   ") is None

    # URL without valid JORFTEXT
    assert extract_jorf_id_from_url(
        "https://www.legifrance.gouv.fr/loda/id/LEGIARTI000006312120"
    ) is None


def test_import_data_helpers_importable():
    """Test that the new helper functions can be imported."""
    from src.db.import_data import (
        article_refs_match,
        deduplicate_debates,
        extract_jorf_id_from_url,
        is_finance_law_amendment,
        normalize_article_ref,
    )

    assert normalize_article_ref is not None
    assert article_refs_match is not None
    assert is_finance_law_amendment is not None
    assert extract_jorf_id_from_url is not None
    assert deduplicate_debates is not None


def test_reference_file_exists():
    """Test that the laws reference file exists and is valid JSON."""
    import json
    from pathlib import Path

    ref_file = Path(__file__).parent.parent / "data" / "lois_finances_2012_2025.json"
    assert ref_file.exists(), f"Reference file not found: {ref_file}"

    with open(ref_file) as f:
        data = json.load(f)

    assert "lois" in data
    assert len(data["lois"]) > 0

    # Check structure
    for loi in data["lois"]:
        assert "numero" in loi
        assert "type" in loi
        assert loi["type"] in ["LF", "LFR", "LFSS"]
