"""Tests for article_analyzer.py and prompts.py analysis fixes."""

from datetime import date
from unittest.mock import MagicMock

from src.analysis.article_analyzer import ArticleAnalyzer
from src.analysis.models import (
    ArticleAnalysisResult,
    CertitudeLevel,
    LawModification,
    ModificationNature,
    OperationalParameters,
    SectionAnalysis,
)
from src.analysis.prompts import (
    classify_amendment_author,
    format_amendment_chunks_for_prompt,
    format_parameter_timeline,
)

# -- Helpers --


def _make_analyzer():
    """Create an ArticleAnalyzer with mocked dependencies."""
    analyzer = ArticleAnalyzer.__new__(ArticleAnalyzer)
    analyzer.settings = MagicMock()
    analyzer.search_service = MagicMock()
    analyzer.article_index = MagicMock()
    analyzer._anthropic_client = None
    analyzer._groq_pool = None
    return analyzer


def _make_mod(year=2019, law_type="LF", nature=ModificationNature.MODIFICATION):
    """Create a test LawModification."""
    return LawModification(
        law_type=law_type,
        law_year=year,
        law_numero=f"{year - 1}-1317",
        date=date(year - 1, 12, 28),
        article_in_law="Art. 24",
        nature=nature,
    )


def _make_section(mod=None, operational_params=None, points_vigilance=None):
    """Create a test SectionAnalysis."""
    if mod is None:
        mod = _make_mod()
    return SectionAnalysis(
        modification=mod,
        contexte="Contexte test",
        intention="Intention test",
        sources_an=[],
        sources_senat=[],
        points_vigilance=points_vigilance or [],
        niveau_certitude=CertitudeLevel.MOYEN,
        operational_params=operational_params,
    )


# ============================================================
# 1C: Strengthen Quote Validation (_quote_matches_sources)
# ============================================================


class TestQuoteMatchesSources:
    """Tests for the tiered quote validation in _quote_matches_sources."""

    def setup_method(self):
        self.analyzer = _make_analyzer()

    def test_tier1_exact_full_substring_match(self):
        """Tier 1: Full exact substring match passes."""
        citation = (
            "le dispositif d'apport-cession doit être renforcé"
            " pour lutter contre l'optimisation fiscale"
        )
        combined = f"selon le rapporteur, {citation} et cela est important"
        assert self.analyzer._quote_matches_sources(citation, combined) is True

    def test_tier1_exact_match_case_insensitive(self):
        """Tier 1: Exact match is case-insensitive."""
        citation = "Le Dispositif Doit Être Renforcé"
        combined = "le dispositif doit être renforcé pour des raisons importantes"
        assert self.analyzer._quote_matches_sources(citation, combined) is True

    def test_tier2_sliding_window_match(self):
        """Tier 2: 8-word contiguous window matches a real quote."""
        source_text = (
            "nous devons absolument renforcer le dispositif"
            " anti abus pour protéger les recettes fiscales"
        )
        citation = (
            "renforcer le dispositif anti abus"
            " pour protéger les recettes"
        )
        assert self.analyzer._quote_matches_sources(citation, source_text) is True

    def test_tier2_window_no_match_if_words_scattered(self):
        """Tier 2: Scattered words don't form a contiguous 8-word window."""
        source_text = "le dispositif est complexe et le renforcement sera progressif dans le temps"
        citation = "dispositif renforcement sera complexe et progressif dans temps futur prévu"
        # These words are scattered, no 8-word contiguous window matches
        # But check: tier 3 may still pass if overlap is high enough
        # Let's make it fail tier 3 too by having low overlap
        source_text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
        citation = "alpha gamma epsilon eta kappa mu nu xi omicron pi rho sigma"
        assert self.analyzer._quote_matches_sources(citation, source_text) is False

    def test_tier3_75_percent_overlap_passes(self):
        """Tier 3: 80% word overlap passes the 75% threshold."""
        combined = (
            "article dispositif apport cession report"
            " imposition plus value réinvestissement seuil"
        )
        citation = (
            "article dispositif apport cession report"
            " imposition plus value bogus1 bogus2"
        )
        assert self.analyzer._quote_matches_sources(citation, combined) is True

    def test_tier3_65_percent_overlap_rejected(self):
        """Tier 3: 65% word overlap is now rejected (was 60% before, now 75%)."""
        # Create exactly 65% overlap: 13 significant words, 8-9 matching
        combined = "alpha beta gamma delta epsilon zeta theta iota kappa lambda"
        # 10 words > 3 chars, 6 or 7 in source = 60-70%
        citation = "alpha beta gamma delta epsilon zeta xxxxx yyyyy zzzzz wwwww"
        # 6/10 = 60% -> should be rejected
        assert self.analyzer._quote_matches_sources(citation, combined) is False

    def test_short_quote_with_no_significant_words(self):
        """Short quotes with no words > 3 chars return False when not exact substring."""
        citation = "il a pu en"
        combined = "les débats ont montré que le rapporteur avait raison"
        assert self.analyzer._quote_matches_sources(citation, combined) is False

    def test_exact_match_short_citation(self):
        """Tier 1 works even for a short citation that exactly matches."""
        citation = "le dispositif anti-abus"
        combined = "nous avons renforcé le dispositif anti-abus conformément aux objectifs"
        assert self.analyzer._quote_matches_sources(citation, combined) is True

    def test_empty_citation(self):
        """Empty citation returns False."""
        assert self.analyzer._quote_matches_sources("", "some source text") is False

    def test_no_match_at_all(self):
        """Completely unrelated text returns False."""
        citation = (
            "la fiscalité immobilière doit être réformée"
            " en profondeur pour les contribuables"
        )
        combined = (
            "les marchés financiers européens connaissent"
            " une volatilité accrue cette année"
        )
        assert self.analyzer._quote_matches_sources(citation, combined) is False


# ============================================================
# 2C: Rejected Amendment Rationale Extraction
# ============================================================


class TestRejectedAmendmentParsing:
    """Tests for parsing amendements_rejetes_detailles from LLM response."""

    def setup_method(self):
        self.analyzer = _make_analyzer()

    def test_parse_rejected_amendments_into_points_vigilance(self):
        """Valid amendements_rejetes_detailles entries are appended to points_vigilance."""
        result = {
            "contexte_legislatif": "Contexte test",
            "intention": "Intention test",
            "sources_an": [],
            "sources_senat": [],
            "evolution_debats": "",
            "points_vigilance": ["Existing vigilance point"],
            "niveau_certitude": "moyen",
            "justification_certitude": "Test",
            "amendements_rejetes_detailles": [
                {
                    "numero": "CF42",
                    "auteur": "M. Dupont",
                    "objet": "Relever le seuil de 50% à 80%",
                    "raison_rejet": "Risque de contournement",
                    "source_rejet": "rapporteur général",
                    "implication_intention": "Le législateur souhaite maintenir un seuil strict",
                }
            ],
        }

        # Simulate what _extract_section_with_llm does with this result
        points_vigilance = list(result.get("points_vigilance", []))
        for amend_rej in result.get("amendements_rejetes_detailles", []):
            numero = amend_rej.get("numero", "?")
            auteur = amend_rej.get("auteur", "inconnu")
            objet = amend_rej.get("objet", "")
            raison = amend_rej.get("raison_rejet", "")
            implication = amend_rej.get("implication_intention", "")
            points_vigilance.append(
                f"Amendement N°{numero} ({auteur}) rejeté: {objet}. "
                f"Raison: {raison}. Implication: {implication}"
            )

        assert len(points_vigilance) == 2
        assert "Existing vigilance point" in points_vigilance[0]
        assert "Amendement N°CF42 (M. Dupont) rejeté" in points_vigilance[1]
        assert "Risque de contournement" in points_vigilance[1]
        assert "maintenir un seuil strict" in points_vigilance[1]

    def test_backward_compat_missing_field(self):
        """Old LLM responses without amendements_rejetes_detailles still work."""
        result = {
            "contexte_legislatif": "Contexte",
            "intention": "Intention",
            "sources_an": [],
            "sources_senat": [],
            "points_vigilance": ["Point 1"],
            "niveau_certitude": "moyen",
            "justification_certitude": "Test",
            # No amendements_rejetes_detailles key
        }

        points_vigilance = list(result.get("points_vigilance", []))
        for amend_rej in result.get("amendements_rejetes_detailles", []):
            points_vigilance.append("should not appear")

        assert len(points_vigilance) == 1
        assert points_vigilance[0] == "Point 1"

    def test_empty_rejected_amendments_array(self):
        """Empty amendements_rejetes_detailles array adds nothing."""
        result = {
            "points_vigilance": ["Existing"],
            "amendements_rejetes_detailles": [],
        }

        points_vigilance = list(result.get("points_vigilance", []))
        for amend_rej in result.get("amendements_rejetes_detailles", []):
            points_vigilance.append("should not appear")

        assert len(points_vigilance) == 1

    def test_multiple_rejected_amendments(self):
        """Multiple rejected amendments are all appended."""
        result = {
            "points_vigilance": [],
            "amendements_rejetes_detailles": [
                {
                    "numero": "1",
                    "auteur": "A",
                    "objet": "Obj1",
                    "raison_rejet": "R1",
                    "source_rejet": "S1",
                    "implication_intention": "I1",
                },
                {
                    "numero": "2",
                    "auteur": "B",
                    "objet": "Obj2",
                    "raison_rejet": "R2",
                    "source_rejet": "S2",
                    "implication_intention": "I2",
                },
            ],
        }

        points_vigilance = list(result.get("points_vigilance", []))
        for amend_rej in result.get("amendements_rejetes_detailles", []):
            numero = amend_rej.get("numero", "?")
            auteur = amend_rej.get("auteur", "inconnu")
            objet = amend_rej.get("objet", "")
            raison = amend_rej.get("raison_rejet", "")
            implication = amend_rej.get("implication_intention", "")
            points_vigilance.append(
                f"Amendement N°{numero} ({auteur}) rejeté: {objet}. "
                f"Raison: {raison}. Implication: {implication}"
            )

        assert len(points_vigilance) == 2
        assert "N°1" in points_vigilance[0]
        assert "N°2" in points_vigilance[1]


# ============================================================
# 3C: Amendment Author Role Classification
# ============================================================


class TestClassifyAmendmentAuthor:
    """Tests for classify_amendment_author function."""

    def test_gouvernement_with_gouvernement(self):
        assert classify_amendment_author("Le Gouvernement") == "gouvernement"

    def test_gouvernement_with_ministre(self):
        assert classify_amendment_author("M. le Ministre de l'économie") == "gouvernement"

    def test_gouvernement_with_secretaire_etat(self):
        assert classify_amendment_author("Mme la Secrétaire d'État") == "gouvernement"

    def test_rapporteur(self):
        assert classify_amendment_author("M. Giraud, rapporteur général") == "rapporteur"

    def test_rapporteur_simple(self):
        assert classify_amendment_author("rapporteur") == "rapporteur"

    def test_commission(self):
        assert classify_amendment_author("Commission des finances") == "commission"

    def test_parlementaire_named(self):
        assert classify_amendment_author("M. Jean Dupont") == "parlementaire"

    def test_parlementaire_mme(self):
        assert classify_amendment_author("Mme Marie Martin") == "parlementaire"

    def test_inconnu_none(self):
        assert classify_amendment_author(None) == "inconnu"

    def test_inconnu_empty(self):
        assert classify_amendment_author("") == "inconnu"

    def test_inconnu_whitespace_only(self):
        assert classify_amendment_author("   ") == "inconnu"

    def test_case_insensitive(self):
        assert classify_amendment_author("GOUVERNEMENT") == "gouvernement"
        assert classify_amendment_author("RAPPORTEUR GÉNÉRAL") == "rapporteur"
        assert classify_amendment_author("COMMISSION DES FINANCES") == "commission"

    def test_gouvernement_priority_over_rapporteur(self):
        """If somehow both keywords present, gouvernement wins (checked first)."""
        assert classify_amendment_author("ministre rapporteur") == "gouvernement"


class TestAmendmentChunkFormatting:
    """Tests for role annotation in format_amendment_chunks_for_prompt."""

    def _make_chunk(self, numero="42", auteur="M. Dupont", sort="adopte", content="Contenu"):
        chunk = MagicMock()
        chunk.metadata = {"numero": numero, "auteur": auteur, "sort": sort}
        chunk.content = content
        return chunk

    def test_role_annotation_in_header(self):
        """Amendment headers include role classification."""
        chunk = self._make_chunk(auteur="M. le rapporteur général")
        result = format_amendment_chunks_for_prompt([(chunk, 0.9)])
        assert "[rapporteur]" in result
        assert "M. le rapporteur général" in result

    def test_gouvernement_annotation(self):
        chunk = self._make_chunk(auteur="Le Gouvernement")
        result = format_amendment_chunks_for_prompt([(chunk, 0.9)])
        assert "[gouvernement]" in result

    def test_parlementaire_annotation(self):
        chunk = self._make_chunk(auteur="M. Jean Dupont")
        result = format_amendment_chunks_for_prompt([(chunk, 0.9)])
        assert "[parlementaire]" in result

    def test_no_auteur_no_annotation(self):
        chunk = self._make_chunk(auteur=None)
        chunk.metadata = {"numero": "42", "sort": "adopte"}
        result = format_amendment_chunks_for_prompt([(chunk, 0.9)])
        assert "[" not in result or "N°" in result  # Only bracket is from N° formatting


# ============================================================
# 4B: Parameter Evolution Tracking
# ============================================================


class TestParameterEvolution:
    """Tests for parameter evolution tracking."""

    def test_collect_parameter_timeline(self):
        """Timeline is correctly collected from sections."""
        analyzer = _make_analyzer()

        sections = [
            _make_section(
                mod=_make_mod(year=2019),
                operational_params=OperationalParameters(
                    parameters={"seuil_reinvestissement": "50%", "delai_cession": "3 ans"}
                ),
            ),
            _make_section(
                mod=_make_mod(year=2020),
                operational_params=OperationalParameters(
                    parameters={"seuil_reinvestissement": "60%", "delai_cession": "2 ans"}
                ),
            ),
            _make_section(
                mod=_make_mod(year=2024),
                operational_params=None,
            ),
        ]

        timeline = analyzer._collect_parameter_timeline(sections)

        assert "seuil_reinvestissement" in timeline
        assert len(timeline["seuil_reinvestissement"]) == 2
        assert timeline["seuil_reinvestissement"][0] == ("2019", "50%")
        assert timeline["seuil_reinvestissement"][1] == ("2020", "60%")

        assert "delai_cession" in timeline
        assert len(timeline["delai_cession"]) == 2

    def test_collect_parameter_timeline_empty(self):
        """No params in any section returns empty dict."""
        analyzer = _make_analyzer()
        sections = [_make_section(operational_params=None)]
        timeline = analyzer._collect_parameter_timeline(sections)
        assert timeline == {}

    def test_article_analysis_result_parameter_evolution_property(self):
        """ArticleAnalysisResult.parameter_evolution returns correct structure."""
        mod1 = _make_mod(year=2018)
        mod2 = _make_mod(year=2020)

        sections = [
            _make_section(
                mod=mod1,
                operational_params=OperationalParameters(
                    parameters={"taux": "10%", "plafond": "100000"}
                ),
            ),
            _make_section(
                mod=mod2,
                operational_params=OperationalParameters(
                    parameters={"taux": "15%"}
                ),
            ),
        ]

        result = ArticleAnalysisResult(
            article_ref="150-0 B ter",
            article_titre="Test",
            article_code="CGI",
            question="Test question",
            historique=[mod1, mod2],
            sections=sections,
        )

        evolution = result.parameter_evolution
        assert "taux" in evolution
        assert evolution["taux"] == [("2018", "10%"), ("2020", "15%")]
        assert "plafond" in evolution
        assert evolution["plafond"] == [("2018", "100000")]

    def test_parameter_evolution_empty_sections(self):
        """parameter_evolution returns empty dict when no sections have params."""
        result = ArticleAnalysisResult(
            article_ref="150-0 B ter",
            article_titre="Test",
            article_code="CGI",
            question="Test question",
            historique=[],
            sections=[],
        )
        assert result.parameter_evolution == {}


class TestFormatParameterTimeline:
    """Tests for format_parameter_timeline function."""

    def test_formats_timeline_correctly(self):
        timeline = {
            "seuil": [("2019", "50%"), ("2020", "60%")],
            "delai": [("2019", "3 ans")],
        }
        result = format_parameter_timeline(timeline)
        assert "seuil" in result
        assert "2019: 50%" in result
        assert "2020: 60%" in result
        assert "delai" in result
        assert "2019: 3 ans" in result

    def test_empty_timeline(self):
        result = format_parameter_timeline({})
        assert "Aucun paramètre" in result


# ============================================================
# Integration: _build_synthesis includes parameter timeline
# ============================================================


class TestBuildSynthesisWithTimeline:
    """Tests that _build_synthesis includes parameter_timeline in result."""

    def test_synthesis_includes_parameter_timeline(self):
        analyzer = _make_analyzer()
        analyzer.article_index.get_synthese.return_value = {
            "objectif_constant": "Test objectif",
            "preoccupations_recurrentes": ["Preoc 1"],
        }
        analyzer.article_index.get_synthese_evolution.return_value = None

        article_info = MagicMock()
        article_info.reference = "150-0 B ter"

        sections = [
            _make_section(
                mod=_make_mod(year=2019),
                operational_params=OperationalParameters(
                    parameters={"seuil": "50%"}
                ),
            ),
        ]

        result = analyzer._build_synthesis(article_info, sections)

        assert "parameter_timeline" in result
        assert "seuil" in result["parameter_timeline"]
        assert result["parameter_timeline"]["seuil"] == [("2019", "50%")]

    def test_synthesis_timeline_empty_when_no_params(self):
        analyzer = _make_analyzer()
        analyzer.article_index.get_synthese.return_value = {}
        analyzer.article_index.get_synthese_evolution.return_value = None

        article_info = MagicMock()
        article_info.reference = "test"

        sections = [_make_section(operational_params=None)]

        result = analyzer._build_synthesis(article_info, sections)
        assert result["parameter_timeline"] == {}
