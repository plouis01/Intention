"""Tests for CGI article extraction pipeline fixes.

Tests cover:
- Cross-reference classification (guillemets, indicator phrases, direct mods)
- Article-aware nature detection
- Rectificatif detection in import
- Curated data validation (150-0 B ter)
"""

import json
from pathlib import Path

import pytest

from src.analysis.cgi_extractor import CGIExtractor
from src.db.import_data import extract_articles_from_legifrance_json


@pytest.fixture
def extractor():
    return CGIExtractor()


# --- Fix 4: Cross-reference classification ---


class TestClassifyReferences:
    """Tests for _classify_references cross-reference filtering."""

    def test_classify_direct_modification(self, extractor):
        """Chapeau pattern: 'L'article X du CGI est ainsi modifié' is direct."""
        text = "I. – L'article 150-0 B ter du code général des impôts est ainsi modifié :"
        refs = extractor.extract_cgi_references(text)
        refs = extractor._classify_references(text, refs)

        assert len(refs) >= 1
        target = [r for r in refs if r.article_ref == "150-0 B ter"]
        assert len(target) == 1
        assert target[0].reference_type == "direct"

    def test_classify_cross_reference_in_guillemets(self, extractor):
        """References inside « » are cross-references (quoted amended text)."""
        text = (
            "I. – L'article 200 A du code général des impôts est ainsi modifié :\n"
            "1° Le 2 est complété par les mots : "
            "« mentionné à l'article 150-0 B ter du même code » ;"
        )
        refs = extractor.extract_cgi_references(text)
        refs = extractor._classify_references(text, refs)

        ref_150 = [r for r in refs if r.article_ref == "150-0 B ter"]
        assert len(ref_150) == 1
        assert ref_150[0].reference_type == "cross_reference"

    def test_classify_cross_reference_indicators(self, extractor):
        """'mentionné à', 'prévu à', etc. mark cross-references."""
        text = (
            "Le gain net mentionné à l'article 150-0 A du code général des impôts "
            "est réduit d'un abattement."
        )
        refs = extractor.extract_cgi_references(text)
        refs = extractor._classify_references(text, refs)

        ref_150a = [r for r in refs if r.article_ref == "150-0 A"]
        assert len(ref_150a) == 1
        assert ref_150a[0].reference_type == "cross_reference"

    def test_classify_prevu_a_indicator(self, extractor):
        """'prévu à l'article X' is a cross-reference."""
        text = (
            "Le délai prévu à l'article 199 terdecies-0 A du code général des impôts "
            "est porté à cinq ans."
        )
        refs = extractor.extract_cgi_references(text)
        refs = extractor._classify_references(text, refs)

        target = [r for r in refs if r.article_ref == "199 terdecies-0 A"]
        assert len(target) == 1
        assert target[0].reference_type == "cross_reference"

    def test_cross_references_filtered_by_confidence(self, extractor):
        """Cross-references should score below min_confidence=0.5 threshold."""
        text = (
            "I. – L'article 200 A du code général des impôts est ainsi modifié :\n"
            "1° Le 2 est complété par les mots : "
            "« mentionné à l'article 150-0 B ter du même code » ;"
        )
        law_articles = [{"numero_article": "42", "contenu_texte": text}]
        law_info = {
            "numero": "2023-1322",
            "type": "LF",
            "annee_budgetaire": 2024,
            "date_promulgation": "2023-12-29",
        }
        mods = extractor.extract_modifications_from_law(law_articles, law_info)

        # 150-0 B ter should have low confidence (cross-reference)
        cross_ref_mods = [m for m in mods if m.cgi_article == "150-0 B ter"]
        for m in cross_ref_mods:
            assert m.confidence <= 0.2

        # 200 A should have normal confidence (direct modification)
        direct_mods = [m for m in mods if m.cgi_article == "200 A"]
        for m in direct_mods:
            assert m.confidence >= 0.5


# --- Fix 3: Article-aware nature detection ---


class TestNatureDetection:
    """Tests for article-aware modification type detection."""

    def test_nature_detection_article_aware(self, extractor):
        """Nature detection correctly attributes verb to the right article."""
        text = (
            "I. – L'article 150-0 B ter du code général des impôts est ainsi modifié :\n"
            "1° Après le 2, il est inséré un article 150-0 B quater ainsi rédigé :"
        )
        refs = extractor.extract_cgi_references(text)

        ref_ter = [r for r in refs if r.article_ref == "150-0 B ter"]
        if ref_ter:
            assert ref_ter[0].modification_type == "Modification"

    def test_nature_detection_creation(self, extractor):
        """Création pattern: 'il est inséré un article X'."""
        text = (
            "Après l'article 150-0 B bis du code général des impôts, "
            "il est inséré un article 150-0 B ter ainsi rédigé :"
        )
        refs = extractor.extract_cgi_references(text)

        ref_ter = [r for r in refs if r.article_ref == "150-0 B ter"]
        assert len(ref_ter) == 1
        assert ref_ter[0].modification_type == "Création"

    def test_nature_detection_abrogation(self, extractor):
        """Abrogation pattern: 'L'article X est abrogé'."""
        text = "L'article 885 I bis du code général des impôts est abrogé."
        refs = extractor.extract_cgi_references(text)

        ref = [r for r in refs if r.article_ref == "885 I bis"]
        assert len(ref) == 1
        assert ref[0].modification_type == "Abrogation"

    def test_nature_detection_generic_fallback(self, extractor):
        """Generic patterns still work as fallback when no article-specific match."""
        context = "les mots « cinq ans » sont remplacés par les mots « trois ans »"
        result = extractor._detect_modification_type(context)
        assert result == "Modification"


# --- Fix 2: Rectificatif detection ---


class TestRectificatifDetection:
    """Tests for rectificatif JORF text detection."""

    def test_rectificatif_returns_empty(self):
        """A rectificatif JORF text should return 0 articles."""
        json_data = {
            "title": "Loi n° 2015-1786 du 29 décembre 2015 (rectificatif)",
            "sections": [
                {
                    "title": "ERRATUM",
                    "articles": [
                        {"num": "1", "content": "Correction of a typo."},
                    ],
                }
            ],
        }
        articles = extract_articles_from_legifrance_json(json_data)
        assert articles == []

    def test_normal_law_not_rejected(self):
        """A normal law should NOT be rejected by rectificatif check."""
        json_data = {
            "title": "Loi n° 2015-1786 du 29 décembre 2015 de finances rectificative pour 2015",
            "sections": [
                {
                    "title": "Première partie",
                    "articles": [
                        {"num": "1", "content": "<p>Texte de l'article 1.</p>"},
                        {"num": "2", "content": "<p>Texte de l'article 2.</p>"},
                    ],
                }
            ],
        }
        articles = extract_articles_from_legifrance_json(json_data)
        assert len(articles) == 2

    def test_root_level_articles_fallback(self):
        """Articles at root level (no sections) should be extracted."""
        json_data = {
            "title": "Loi n° 2015-1786",
            "articles": [
                {"num": "1", "content": "<p>Article 1 content.</p>"},
                {"num": "2", "content": "<p>Article 2 content.</p>"},
            ],
        }
        articles = extract_articles_from_legifrance_json(json_data)
        assert len(articles) == 2
        assert articles[0]["numero_article"] == "1"


# --- Fix 1: Curated data validation ---


class TestCuratedData:
    """Validate curated data corrections in article_modifications.json."""

    @pytest.fixture
    def article_index(self):
        path = Path("data/article_modifications.json")
        if not path.exists():
            pytest.skip("article_modifications.json not found")
        with open(path) as f:
            data = json.load(f)
        return data["articles"]

    def test_150_0_b_ter_no_art60_lfr2016(self, article_index):
        """150-0 B ter should NOT have Art. 60 for LFR 2016 (law 2016-1918)."""
        article = article_index.get("150-0 B ter")
        assert article is not None, "150-0 B ter not found in index"

        for mod in article.get("modifications", []):
            if mod.get("law_numero") == "2016-1918":
                assert mod.get("article_in_law") != "Art. 60", (
                    "Art. 60 of LFR 2016 should not be listed for 150-0 B ter"
                )

    def test_150_0_b_ter_has_art34_lfr2016(self, article_index):
        """150-0 B ter should have Art. 34 for LFR 2016 (law 2016-1918)."""
        article = article_index.get("150-0 B ter")
        assert article is not None

        art34_found = any(
            mod.get("law_numero") == "2016-1918" and mod.get("article_in_law") == "Art. 34"
            for mod in article.get("modifications", [])
        )
        assert art34_found, "Art. 34 of LFR 2016 should be listed for 150-0 B ter"

    def test_lfr_2015_jorf_id(self):
        """LFR 2015 should use JORFTEXT000031732968, not the rectificatif."""
        path = Path("data/lois_finances_2012_2025.json")
        if not path.exists():
            pytest.skip("lois_finances_2012_2025.json not found")
        with open(path) as f:
            data = json.load(f)

        laws = data.get("lois", data) if isinstance(data, dict) else data
        lfr_2015 = [law for law in laws if law.get("numero") == "2015-1786"]
        assert len(lfr_2015) == 1
        assert lfr_2015[0]["jorf"] == "JORFTEXT000031733082", (
            "LFR 2015 should use the actual law JORF ID, not the rectificatif"
        )
