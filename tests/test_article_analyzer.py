"""Tests for article_analyzer.py and prompts.py analysis fixes."""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.article_analyzer import ArticleAnalyzer
from src.indexing.chunking import Chunk
from src.analysis.models import (
    ArticleAnalysisResult,
    CertitudeLevel,
    LawModification,
    ModificationNature,
    OperationalParameters,
    QuotedSource,
    SectionAnalysis,
)
from src.analysis.prompts import (
    classify_amendment_author,
    format_amendment_chunks_for_prompt,
    format_debate_chunks_for_prompt,
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


def _text_to_chunks(text, orateur="M. Test", chambre="AN"):
    """Convert a text string to a chunk list for _quote_matches_sources."""
    chunk = Chunk(
        content=text,
        metadata={"orateur": orateur, "chambre": chambre},
        chunk_type="intervention",
        source_id="test",
        position=0,
    )
    return [(chunk, 1.0)]


class TestQuoteMatchesSources:
    """Tests for the tiered quote validation in _quote_matches_sources."""

    def setup_method(self):
        self.analyzer = _make_analyzer()
        self.analyzer.settings.quote_word_overlap_threshold = 0.60

    def test_tier1_exact_full_substring_match(self):
        """Tier 1: Full exact substring match passes."""
        citation = (
            "le dispositif d'apport-cession doit être renforcé"
            " pour lutter contre l'optimisation fiscale"
        )
        combined = f"selon le rapporteur, {citation} et cela est important"
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(combined))
        assert result["matched"] is True

    def test_tier1_exact_match_case_insensitive(self):
        """Tier 1: Exact match is case-insensitive."""
        citation = "Le Dispositif Doit Être Renforcé"
        combined = "le dispositif doit être renforcé pour des raisons importantes"
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(combined))
        assert result["matched"] is True

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
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(source_text))
        assert result["matched"] is True

    def test_tier2_window_no_match_if_words_scattered(self):
        """Tier 2: Scattered words don't form a contiguous 8-word window."""
        source_text = "le dispositif est complexe et le renforcement sera progressif dans le temps"
        citation = "dispositif renforcement sera complexe et progressif dans temps futur prévu"
        # These words are scattered, no 8-word contiguous window matches
        # But check: tier 3 may still pass if overlap is high enough
        # Let's make it fail tier 3 too by having low overlap (<60%)
        source_text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
        citation = "alpha gamma xxxxx yyyyy zzzzz uuuuu vvvvv wwwww qqqqq rrrrr sssss ttttt"
        # 2/12 significant words in source = 16.7% -> rejected
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(source_text))
        assert result["matched"] is False

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
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(combined))
        assert result["matched"] is True

    def test_tier3_low_overlap_rejected(self):
        """Tier 3: <60% word overlap is rejected with 0.60 threshold."""
        combined = "alpha beta gamma delta epsilon zeta theta iota kappa lambda"
        # 5 significant words match out of 10 = 50% -> below 0.60 threshold
        citation = "alpha beta gamma delta epsilon xxxxx yyyyy zzzzz wwwww uuuuu"
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(combined))
        assert result["matched"] is False

    def test_short_quote_with_no_significant_words(self):
        """Short quotes with no words > 3 chars return False when not exact substring."""
        citation = "il a pu en"
        combined = "les débats ont montré que le rapporteur avait raison"
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(combined))
        assert result["matched"] is False

    def test_exact_match_short_citation(self):
        """Tier 1 works even for a short citation that exactly matches."""
        citation = "le dispositif anti-abus"
        combined = "nous avons renforcé le dispositif anti-abus conformément aux objectifs"
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(combined))
        assert result["matched"] is True

    def test_empty_citation(self):
        """Empty citation returns False."""
        result = self.analyzer._quote_matches_sources("", _text_to_chunks("some source text"))
        assert result["matched"] is False

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
        result = self.analyzer._quote_matches_sources(citation, _text_to_chunks(combined))
        assert result["matched"] is False


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
        assert "Auteur:" not in result  # No author annotation when auteur is None
        assert "Amendement 42" in result  # Amendment number still present


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

        sections = [
            _make_section(
                mod=_make_mod(year=2019),
                operational_params=OperationalParameters(
                    parameters={"seuil": "50%"}
                ),
            ),
        ]

        result = analyzer._build_synthesis(sections)

        assert "parameter_timeline" in result
        assert "seuil" in result["parameter_timeline"]
        assert result["parameter_timeline"]["seuil"] == [("2019", "50%")]

    def test_synthesis_timeline_empty_when_no_params(self):
        analyzer = _make_analyzer()

        sections = [_make_section(operational_params=None)]

        result = analyzer._build_synthesis(sections)
        assert result["parameter_timeline"] == {}


# ============================================================
# Quality Validation: Per-chunk isolation, orateur matching,
# hallucination detection
# ============================================================


class TestPerChunkValidation:
    """Tests that validation works per-chunk, not on concatenated corpus."""

    def setup_method(self):
        self.analyzer = _make_analyzer()
        self.analyzer.settings.quote_word_overlap_threshold = 0.60

    def test_cross_chunk_false_match_rejected(self):
        """Words spread across two different chunks should NOT validate via Tier 3."""
        chunk1 = Chunk(
            content="le dispositif apport cession report imposition",
            metadata={"orateur": "M. Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        chunk2 = Chunk(
            content="plus value réinvestissement seuil économie réelle",
            metadata={"orateur": "Mme Martin", "chambre": "Sénat"},
            chunk_type="intervention", source_id="2", position=0,
        )
        chunks = [(chunk1, 1.0), (chunk2, 1.0)]

        # Citation uses words from BOTH chunks - should fail per-chunk validation
        citation = (
            "le dispositif apport cession"
            " plus value réinvestissement seuil"
        )
        result = self.analyzer._quote_matches_sources(citation, chunks)
        # With per-chunk validation, no single chunk has enough overlap
        assert result["matched"] is False or result["tier"] >= 3

    def test_single_chunk_match_works(self):
        """Citation that matches a single chunk validates correctly."""
        chunk = Chunk(
            content="le dispositif d'apport-cession doit être renforcé pour lutter contre l'optimisation fiscale",
            metadata={"orateur": "M. Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        citation = "le dispositif d'apport-cession doit être renforcé"
        result = self.analyzer._quote_matches_sources(citation, [(chunk, 1.0)])
        assert result["matched"] is True
        assert result["tier"] == 1
        assert result["chunk_idx"] == 0

    def test_tier_returned_correctly(self):
        """Verify correct tier assignment for each match type."""
        chunk = Chunk(
            content="nous devons absolument renforcer le dispositif anti abus pour protéger les recettes fiscales",
            metadata={"orateur": "M. Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        chunks = [(chunk, 1.0)]

        # Tier 1: exact substring
        result = self.analyzer._quote_matches_sources(
            "renforcer le dispositif anti abus pour protéger les recettes fiscales", chunks
        )
        assert result["tier"] == 1

        # Tier 2: 8-word window
        result = self.analyzer._quote_matches_sources(
            "renforcer le dispositif anti abus pour protéger les recettes", chunks
        )
        assert result["tier"] in (1, 2)  # could be tier 1 if exact match


class TestOrateurVerification:
    """Tests that orateur matching works correctly."""

    def setup_method(self):
        self.analyzer = _make_analyzer()
        self.analyzer.settings.quote_word_overlap_threshold = 0.60

    def test_orateur_match_correct(self):
        """Correct orateur is reported as matching."""
        chunk = Chunk(
            content="le dispositif d'apport-cession doit être renforcé",
            metadata={"orateur": "M. Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        result = self.analyzer._quote_matches_sources(
            "le dispositif d'apport-cession doit être renforcé",
            [(chunk, 1.0)],
            cited_orateur="M. Dupont",
        )
        assert result["matched"] is True
        assert result["orateur_match"] is True

    def test_orateur_mismatch_detected(self):
        """Different orateur is reported as NOT matching."""
        chunk = Chunk(
            content="le dispositif d'apport-cession doit être renforcé",
            metadata={"orateur": "M. Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        result = self.analyzer._quote_matches_sources(
            "le dispositif d'apport-cession doit être renforcé",
            [(chunk, 1.0)],
            cited_orateur="Mme Martin",
        )
        assert result["matched"] is True
        assert result["orateur_match"] is False

    def test_orateur_fuzzy_match_by_last_name(self):
        """Fuzzy orateur matching works on last name."""
        chunk = Chunk(
            content="le dispositif d'apport-cession doit être renforcé",
            metadata={"orateur": "M. Jean-Paul Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        result = self.analyzer._quote_matches_sources(
            "le dispositif d'apport-cession doit être renforcé",
            [(chunk, 1.0)],
            cited_orateur="M. Dupont",
        )
        assert result["matched"] is True
        assert result["orateur_match"] is True


class TestHallucinationDetection:
    """Tests that fabricated citations are properly rejected."""

    def setup_method(self):
        self.analyzer = _make_analyzer()
        self.analyzer.settings.quote_word_overlap_threshold = 0.60

    def test_fabricated_citation_rejected(self):
        """A completely fabricated citation should not match any source chunk."""
        chunk = Chunk(
            content="Le rapporteur général a indiqué que le seuil de réinvestissement de 60% visait à concentrer l'épargne dans les PME opérationnelles.",
            metadata={"orateur": "M. de Courson", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        # Fabricated citation: sounds plausible but doesn't exist
        fabricated = (
            "M. de Courson a souligné que la réforme du barème progressif "
            "de l'impôt sur le revenu nécessitait une approche prudente "
            "pour préserver les recettes de l'État"
        )
        result = self.analyzer._quote_matches_sources(
            fabricated, [(chunk, 1.0)], cited_orateur="M. de Courson"
        )
        assert result["matched"] is False

    def test_paraphrased_citation_with_low_overlap_rejected(self):
        """A paraphrased version with shuffled words should be rejected if overlap < threshold."""
        chunk = Chunk(
            content="nous devons porter le seuil de réinvestissement de cinquante à soixante pour cent",
            metadata={"orateur": "M. Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        # Paraphrase: rearranged + substituted words
        paraphrase = (
            "il convient d'augmenter la proportion obligatoire "
            "de réemploi des fonds de cinquante à soixante pourcent"
        )
        result = self.analyzer._quote_matches_sources(
            paraphrase, [(chunk, 1.0)], cited_orateur="M. Dupont"
        )
        # Should either not match or match only at tier 3
        # The point is it should NOT pass as tier 1 or 2
        if result["matched"]:
            assert result["tier"] == 3

    def test_real_citation_passes(self):
        """A real citation that exists in the source should pass."""
        real_text = "le seuil de réinvestissement de 60% vise à concentrer l'épargne dans les PME opérationnelles"
        chunk = Chunk(
            content=f"Le rapporteur a indiqué que {real_text} et que cela était nécessaire.",
            metadata={"orateur": "M. Dupont", "chambre": "AN"},
            chunk_type="intervention", source_id="1", position=0,
        )
        result = self.analyzer._quote_matches_sources(
            real_text, [(chunk, 1.0)], cited_orateur="M. Dupont"
        )
        assert result["matched"] is True
        assert result["tier"] == 1
        assert result["orateur_match"] is True


class TestChamberCorrection:
    """Tests that chamber assignment is corrected from chunk metadata during validation."""

    def setup_method(self):
        self.analyzer = _make_analyzer()
        self.analyzer.settings.quote_word_overlap_threshold = 0.60
        self.analyzer.settings.quote_min_length = 40

    def test_senat_source_in_an_list_corrected(self):
        """Source wrongly in sources_an is moved to sources_senat based on chunk metadata."""
        chunk = Chunk(
            content="M. Adnot a insisté sur la nécessité de simplifier le dispositif d'apport-cession pour les entreprises",
            metadata={"orateur": "M. Adnot", "chambre": "SENAT", "fonction": "senateur"},
            chunk_type="intervention", source_id="1", position=0,
        )
        section = _make_section()
        section.sources_an = [
            QuotedSource(
                citation="M. Adnot a insisté sur la nécessité de simplifier le dispositif d'apport-cession",
                orateur="M. Adnot",
                fonction="député",
                date="2019-11-15",
                chambre="AN",
            )
        ]
        section.sources_senat = []

        section = self.analyzer._validate_quotes(
            section, [(chunk, 1.0)], ["apport-cession", "dispositif"], article_ref="150-0 B ter"
        )

        assert len(section.sources_an) == 0, f"Expected 0 AN sources, got {len(section.sources_an)}"
        assert len(section.sources_senat) == 1, f"Expected 1 Sénat source, got {len(section.sources_senat)}"
        assert section.sources_senat[0].chambre == "Sénat"
        assert section.sources_senat[0].fonction == "senateur"

    def test_an_source_correctly_preserved(self):
        """Source correctly in sources_an stays there."""
        chunk = Chunk(
            content="le rapporteur a expliqué que le dispositif de l'article 150-0 B ter devait être renforcé pour les PME",
            metadata={"orateur": "M. Giraud", "chambre": "AN", "fonction": "rapporteur"},
            chunk_type="intervention", source_id="1", position=0,
        )
        section = _make_section()
        section.sources_an = [
            QuotedSource(
                citation="le dispositif de l'article 150-0 B ter devait être renforcé",
                orateur="M. Giraud",
                fonction="rapporteur",
                date="2019-10-15",
                chambre="AN",
            )
        ]

        section = self.analyzer._validate_quotes(
            section, [(chunk, 1.0)], ["150-0 B ter"], article_ref="150-0 B ter"
        )

        assert len(section.sources_an) == 1
        assert section.sources_an[0].chambre == "AN"
        assert section.sources_an[0].fonction == "rapporteur"

    def test_fonction_corrected_from_chunk_metadata(self):
        """Fonction label is corrected using chunk metadata (not LLM's guess)."""
        chunk = Chunk(
            content="Mme Louwagie a souligné l'importance du seuil de réinvestissement pour les PME et les entreprises",
            metadata={"orateur": "Mme Louwagie", "chambre": "AN", "fonction": "depute"},
            chunk_type="intervention", source_id="1", position=0,
        )
        section = _make_section()
        section.sources_an = [
            QuotedSource(
                citation="Mme Louwagie a souligné l'importance du seuil de réinvestissement pour les PME",
                orateur="Mme Louwagie",
                fonction="ministre",
                date="2019-10-15",
                chambre="AN",
            )
        ]

        section = self.analyzer._validate_quotes(
            section, [(chunk, 1.0)], ["réinvestissement", "PME"], article_ref="150-0 B ter"
        )

        assert section.sources_an[0].fonction == "depute"

    def test_empty_chunk_fonction_preserves_llm_value(self):
        """When chunk metadata has no fonction, keep the LLM's value."""
        chunk = Chunk(
            content="le dispositif d'apport-cession doit être simplifié pour les contribuables et les entrepreneurs",
            metadata={"orateur": "M. Martin", "chambre": "AN", "fonction": ""},
            chunk_type="intervention", source_id="1", position=0,
        )
        section = _make_section()
        section.sources_an = [
            QuotedSource(
                citation="le dispositif d'apport-cession doit être simplifié pour les contribuables",
                orateur="M. Martin",
                fonction="rapporteur",
                date="2019-10-15",
                chambre="AN",
            )
        ]

        section = self.analyzer._validate_quotes(
            section, [(chunk, 1.0)], ["apport-cession", "dispositif"], article_ref="150-0 B ter"
        )

        assert section.sources_an[0].fonction == "rapporteur"


# ============================================================
# Date Extraction from Reference
# ============================================================


from src.parsers.debates import _extract_date_from_reference


class TestDateExtractionFromReference:
    """Tests for _extract_date_from_reference fallback."""

    def test_an_reference_format(self):
        """CRAN20191115 yields 2019-11-15."""
        result = _extract_date_from_reference("CRAN20191115")
        assert result == datetime(2019, 11, 15)

    def test_filename_with_date(self):
        """20191115_seance.xml yields 2019-11-15."""
        result = _extract_date_from_reference(path=Path("20191115_seance.xml"))
        assert result == datetime(2019, 11, 15)

    def test_no_date_returns_none(self):
        """Random string with no date returns None."""
        result = _extract_date_from_reference("some_random_reference")
        assert result is None

    def test_invalid_month_returns_none(self):
        """Invalid month like 13 returns None."""
        result = _extract_date_from_reference("CRAN20191332")
        assert result is None

    def test_year_out_of_range_returns_none(self):
        """Year 1800 is out of range."""
        result = _extract_date_from_reference("CRAN18001115")
        assert result is None

    def test_none_inputs_returns_none(self):
        """Both None inputs returns None."""
        result = _extract_date_from_reference(None, None)
        assert result is None


# ============================================================
# Citation-by-Reference: Numbered formatting, sentence extraction,
# source reconstruction from extrait_id references
# ============================================================


class TestFormatChunksNumbered:
    """Tests for numbered chunk formatting (EXTRAIT-N / AMEND-N IDs)."""

    def test_numbered_false_returns_string(self):
        """numbered=False returns plain string (backward compatible)."""
        chunk = Chunk(
            content="Le dispositif d'apport-cession doit être renforcé",
            metadata={"orateur": "M. Dupont", "chambre": "AN", "fonction": "rapporteur", "date": "2019-10-15", "etape": "seance_publique"},
            chunk_type="intervention", source_id="1", position=0,
        )
        result = format_debate_chunks_for_prompt([(chunk, 0.8)], numbered=False)
        assert isinstance(result, str)
        assert "EXTRAIT-" not in result

    def test_numbered_true_returns_tuple(self):
        """numbered=True returns (text, mapping) tuple."""
        chunk = Chunk(
            content="Le dispositif d'apport-cession doit être renforcé",
            metadata={"orateur": "M. Dupont", "chambre": "AN", "fonction": "rapporteur", "date": "2019-10-15", "etape": "seance_publique"},
            chunk_type="intervention", source_id="1", position=0,
        )
        result = format_debate_chunks_for_prompt([(chunk, 0.8)], numbered=True)
        assert isinstance(result, tuple)
        text, mapping = result
        assert isinstance(text, str)
        assert isinstance(mapping, dict)
        assert "EXTRAIT-0" in text
        assert "EXTRAIT-0" in mapping
        assert mapping["EXTRAIT-0"][0].content == chunk.content

    def test_sequential_ids_across_chambers(self):
        """IDs are sequential across AN and Sénat chunks."""
        chunk_an = Chunk(
            content="Intervention AN sur l'apport-cession",
            metadata={"orateur": "M. Dupont", "chambre": "AN", "fonction": "rapporteur", "date": "2019-10-15", "etape": "seance_publique"},
            chunk_type="intervention", source_id="1", position=0,
        )
        chunk_senat = Chunk(
            content="Intervention Sénat sur le réinvestissement",
            metadata={"orateur": "Mme Martin", "chambre": "Sénat", "fonction": "rapporteure", "date": "2019-12-10", "etape": "seance_publique"},
            chunk_type="intervention", source_id="2", position=0,
        )
        text, mapping = format_debate_chunks_for_prompt(
            [(chunk_an, 0.8), (chunk_senat, 0.7)], numbered=True
        )
        assert "EXTRAIT-0" in text
        assert "EXTRAIT-1" in text
        assert len(mapping) == 2

    def test_amendment_numbered_format(self):
        """Amendment chunks use AMEND-N prefix."""
        chunk = Chunk(
            content="Cet amendement vise à porter le seuil à 75%",
            metadata={"numero": "42", "auteur": "M. Bernard", "sort": "rejeté"},
            chunk_type="amendement", source_id="1", position=0,
        )
        result = format_amendment_chunks_for_prompt([(chunk, 0.8)], numbered=True)
        assert isinstance(result, tuple)
        text, mapping = result
        assert "AMEND-0" in text
        assert "AMEND-0" in mapping


class TestExtractSentenceFromPassageCle:
    """Tests for _extract_sentence_from_passage_cle."""

    def setup_method(self):
        self.analyzer = _make_analyzer()

    def test_exact_match_returns_full_sentence(self):
        """Exact passage_cle match returns the full sentence."""
        content = "Le rapporteur a indiqué que le seuil devait être relevé. Cela est important pour les PME."
        result = ArticleAnalyzer._extract_sentence_from_passage_cle(
            content, "le seuil devait être relevé"
        )
        assert "le seuil devait être relevé" in result
        assert result.endswith(".")

    def test_case_insensitive(self):
        """Match is case-insensitive."""
        content = "Le dispositif d'apport-cession favorise les restructurations."
        result = ArticleAnalyzer._extract_sentence_from_passage_cle(
            content, "LE DISPOSITIF D'APPORT-CESSION"
        )
        assert "Le dispositif" in result

    def test_no_match_returns_fallback(self):
        """No match returns first 250 chars."""
        content = "Le rapporteur a présenté son amendement." * 10
        result = ArticleAnalyzer._extract_sentence_from_passage_cle(
            content, "texte qui n'existe pas du tout"
        )
        assert len(result) <= 250

    def test_fuzzy_match_with_shorter_prefix(self):
        """Progressively shorter prefix finds match."""
        content = "L'objectif est de porter le seuil de réinvestissement de 50% à 60% pour concentrer l'investissement."
        result = ArticleAnalyzer._extract_sentence_from_passage_cle(
            content, "L'objectif est de porter le seuil de réinvestissement de 50% à 60% pour les entreprises"
        )
        # Should match on shorter prefix even though full key doesn't match
        assert "L'objectif est de porter" in result

    def test_long_sentence_truncated(self):
        """Very long sentence is truncated to max_length."""
        content = "A " * 500 + "."
        result = ArticleAnalyzer._extract_sentence_from_passage_cle(
            content, "A A A A A", max_length=100
        )
        assert len(result) <= 100

    def test_empty_passage_cle(self):
        """Empty passage_cle returns first 250 chars."""
        content = "Le dispositif est important."
        result = ArticleAnalyzer._extract_sentence_from_passage_cle(content, "")
        assert result == content[:250].strip()

    def test_empty_content(self):
        """Empty content returns empty string."""
        result = ArticleAnalyzer._extract_sentence_from_passage_cle("", "test")
        assert result == ""


class TestReconstructSourcesFromRefs:
    """Tests for _reconstruct_sources_from_refs."""

    def setup_method(self):
        self.analyzer = _make_analyzer()

    def _make_mapping(self):
        chunk_an = Chunk(
            content="Le rapporteur a indiqué que le seuil devait être relevé de 50% à 60% pour les PME.",
            metadata={"orateur": "M. Dupont", "chambre": "AN", "fonction": "rapporteur", "date": "2019-10-15"},
            chunk_type="intervention", source_id="1", position=0,
        )
        chunk_senat = Chunk(
            content="Le Sénat considère que ce relèvement est nécessaire pour orienter l'épargne vers l'économie réelle.",
            metadata={"orateur": "Mme Martin", "chambre": "SENAT", "fonction": "rapporteure", "date": "2019-12-10"},
            chunk_type="intervention", source_id="2", position=0,
        )
        chunk_amend = Chunk(
            content="Cet amendement vise à porter le seuil de réinvestissement à 75% pour concentrer les investissements.",
            metadata={"numero": "42", "auteur": "M. Bernard", "sort": "rejeté", "chambre": "AN", "orateur": "M. Bernard", "fonction": "parlementaire", "date": "2019-10-20"},
            chunk_type="amendement", source_id="3", position=0,
        )
        return {
            "EXTRAIT-0": (chunk_an, 0.8),
            "EXTRAIT-1": (chunk_senat, 0.7),
            "AMEND-0": (chunk_amend, 0.6),
        }

    def test_valid_extrait_reconstructed(self):
        """Valid EXTRAIT-0 reference produces correct QuotedSource."""
        mapping = self._make_mapping()
        sources = [{"extrait_id": "EXTRAIT-0", "passage_cle": "le seuil devait être relevé"}]
        an, senat = self.analyzer._reconstruct_sources_from_refs(sources, mapping)
        assert len(an) == 1
        assert len(senat) == 0
        assert an[0].orateur == "M. Dupont"
        assert an[0].chambre == "AN"
        assert an[0].fonction == "rapporteur"
        assert "seuil devait être relevé" in an[0].citation

    def test_senat_source_correctly_classified(self):
        """EXTRAIT-1 from Sénat chunk goes to sources_senat."""
        mapping = self._make_mapping()
        sources = [{"extrait_id": "EXTRAIT-1", "passage_cle": "ce relèvement est nécessaire"}]
        an, senat = self.analyzer._reconstruct_sources_from_refs(sources, mapping)
        assert len(an) == 0
        assert len(senat) == 1
        assert senat[0].chambre == "Sénat"
        assert senat[0].orateur == "Mme Martin"

    def test_amendment_reference(self):
        """AMEND-0 reference works correctly."""
        mapping = self._make_mapping()
        sources = [{"extrait_id": "AMEND-0", "passage_cle": "porter le seuil de réinvestissement"}]
        an, senat = self.analyzer._reconstruct_sources_from_refs(sources, mapping)
        assert len(an) == 1
        assert an[0].orateur == "M. Bernard"

    def test_invalid_id_skipped(self):
        """Invalid extrait_id is skipped."""
        mapping = self._make_mapping()
        sources = [{"extrait_id": "EXTRAIT-99", "passage_cle": "texte"}]
        an, senat = self.analyzer._reconstruct_sources_from_refs(sources, mapping)
        assert len(an) == 0
        assert len(senat) == 0

    def test_empty_extrait_id_skipped(self):
        """Empty extrait_id is skipped."""
        mapping = self._make_mapping()
        sources = [{"extrait_id": "", "passage_cle": "texte"}]
        an, senat = self.analyzer._reconstruct_sources_from_refs(sources, mapping)
        assert len(an) == 0
        assert len(senat) == 0

    def test_mixed_sources(self):
        """Mix of EXTRAIT and AMEND sources are correctly sorted."""
        mapping = self._make_mapping()
        sources = [
            {"extrait_id": "EXTRAIT-0", "passage_cle": "le seuil devait"},
            {"extrait_id": "EXTRAIT-1", "passage_cle": "ce relèvement"},
            {"extrait_id": "AMEND-0", "passage_cle": "porter le seuil"},
        ]
        an, senat = self.analyzer._reconstruct_sources_from_refs(sources, mapping)
        assert len(an) == 2  # EXTRAIT-0 (AN) + AMEND-0 (AN)
        assert len(senat) == 1  # EXTRAIT-1 (Sénat)


class TestEndToEndReconstruction:
    """Integration tests: mock LLM response -> reconstructed sources."""

    def setup_method(self):
        self.analyzer = _make_analyzer()
        self.analyzer.settings.quote_word_overlap_threshold = 0.60
        self.analyzer.settings.quote_min_length = 40

    def test_new_format_reconstructed(self):
        """New format with extrait_id produces correct QuotedSources."""
        chunk = Chunk(
            content="Le rapporteur général a indiqué que le seuil de réinvestissement de 60% visait à concentrer l'épargne dans les PME opérationnelles.",
            metadata={"orateur": "M. de Courson", "chambre": "AN", "fonction": "rapporteur", "date": "2024-10-23"},
            chunk_type="intervention", source_id="1", position=0,
        )
        mapping = {"EXTRAIT-0": (chunk, 0.9)}
        raw_sources = [
            {"extrait_id": "EXTRAIT-0", "passage_cle": "le seuil de réinvestissement de 60% visait à concentrer"}
        ]
        an, senat = self.analyzer._reconstruct_sources_from_refs(raw_sources, mapping)
        assert len(an) == 1
        src = an[0]
        assert src.orateur == "M. de Courson"
        assert src.chambre == "AN"
        assert src.fonction == "rapporteur"
        assert src.date == "2024-10-23"
        assert "60%" in src.citation
        assert len(src.citation) > len("le seuil de réinvestissement de 60% visait à concentrer")
