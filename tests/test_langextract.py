"""Tests for LangExtract provider module."""

from datetime import date
from unittest.mock import MagicMock

from src.analysis.langextract_provider import LangExtractProvider
from src.analysis.models import (
    CertitudeLevel,
    LawModification,
    ModificationNature,
)

# -- Helpers --

def _make_article_info():
    """Create a mock ArticleInfo."""
    info = MagicMock()
    info.reference = "150-0 B ter"
    info.code = "CGI"
    info.titre = "Report d'imposition des plus-values d'apport"
    info.keywords = ["apport-cession", "report d'imposition", "réinvestissement"]
    return info


def _make_mod():
    """Create a test LawModification."""
    return LawModification(
        law_type="LF",
        law_year=2019,
        law_numero="2018-1317",
        date=date(2018, 12, 28),
        article_in_law="Art. 24",
        nature=ModificationNature.MODIFICATION,
    )


def _make_extraction(extraction_class, extraction_text, **attrs):
    """Create a mock LangExtract Extraction object."""
    ext = MagicMock()
    ext.extraction_class = extraction_class
    ext.extraction_text = extraction_text
    ext.attributes = attrs if attrs else {}
    return ext


def _make_annotated_doc(extractions):
    """Create a mock AnnotatedDocument."""
    doc = MagicMock()
    doc.extractions = extractions
    return doc


# -- Provider construction --


def test_langextract_provider_init():
    provider = LangExtractProvider(api_key="test-key", model_id="gemini-2.5-flash")
    assert provider.api_key == "test-key"
    assert provider.model_id == "gemini-2.5-flash"


# -- Source document building --


def test_build_source_document_all_parts():
    provider = LangExtractProvider(api_key="test")
    doc = provider._build_source_document(
        debate_text="Debate content here",
        amendment_text="Amendment content here",
        expose_motifs="Exposé des motifs content",
    )
    assert "EXPOSÉ DES MOTIFS" in doc
    assert "DÉBATS PARLEMENTAIRES" in doc
    assert "AMENDEMENTS" in doc
    assert "Debate content here" in doc
    assert "Amendment content here" in doc
    assert "Exposé des motifs content" in doc


def test_build_source_document_partial():
    provider = LangExtractProvider(api_key="test")
    doc = provider._build_source_document(
        debate_text="Debate only",
        amendment_text="",
        expose_motifs="",
    )
    assert "DÉBATS PARLEMENTAIRES" in doc
    assert "AMENDEMENTS" not in doc
    assert "EXPOSÉ DES MOTIFS" not in doc


def test_build_source_document_empty():
    provider = LangExtractProvider(api_key="test")
    doc = provider._build_source_document("", "", "")
    assert doc == ""


# -- Result conversion with AnnotatedDocument format --


def test_to_section_analysis_annotated_doc():
    """Test conversion from AnnotatedDocument (real LangExtract output)."""
    provider = LangExtractProvider(api_key="test")
    mod = _make_mod()

    lx_result = _make_annotated_doc([
        _make_extraction("contexte", "Context législatif important"),
        _make_extraction("intention", "Lutter contre l'optimisation fiscale"),
        _make_extraction(
            "citation_an",
            "Le dispositif sera renforcé",
            orateur="M. le rapporteur",
            fonction="rapporteur général",
            date="2018-11-15",
        ),
        _make_extraction(
            "citation_senat",
            "Nous soutenons cette mesure",
            orateur="Mme la sénatrice",
            fonction="sénatrice",
            date="2018-12-10",
        ),
        _make_extraction("vigilance", "Risque de contournement"),
    ])

    section = provider._to_section_analysis(lx_result, mod, "source text")

    assert section.contexte == "Context législatif important"
    assert section.intention == "Lutter contre l'optimisation fiscale"
    assert len(section.sources_an) == 1
    assert section.sources_an[0].chambre == "AN"
    assert section.sources_an[0].orateur == "M. le rapporteur"
    assert len(section.sources_senat) == 1
    assert section.sources_senat[0].chambre == "Sénat"
    assert len(section.points_vigilance) == 1
    assert section.niveau_certitude == CertitudeLevel.ELEVE


def test_to_section_analysis_list_result():
    """Test conversion when lx_result is a list of AnnotatedDocuments."""
    provider = LangExtractProvider(api_key="test")
    mod = _make_mod()

    lx_result = [
        _make_annotated_doc([
            _make_extraction("intention", "Première intention"),
        ]),
        _make_annotated_doc([
            _make_extraction("citation_an", "Citation AN"),
        ]),
    ]

    section = provider._to_section_analysis(lx_result, mod, "source text")

    assert section.intention == "Première intention"
    assert len(section.sources_an) == 1


def test_to_section_analysis_no_sources():
    provider = LangExtractProvider(api_key="test")
    mod = _make_mod()

    lx_result = _make_annotated_doc([])
    section = provider._to_section_analysis(lx_result, mod, "source text")

    assert section.niveau_certitude == CertitudeLevel.MOYEN
    assert len(section.sources_an) == 0
    assert len(section.sources_senat) == 0


# -- Fallback section --


def test_create_fallback_section():
    provider = LangExtractProvider(api_key="test")
    mod = _make_mod()

    section = provider._create_fallback_section(mod)

    assert section.niveau_certitude == CertitudeLevel.FAIBLE
    assert "LangExtract" in section.certitude_justification
    assert section.modification == mod


# -- Default routing --


def test_langextract_disabled_by_default():
    """Verify default extraction_provider='llm' does not route to LangExtract."""
    from src.utils.config import Settings

    settings = Settings(
        _env_file=None,
        extraction_provider="llm",
    )
    assert settings.extraction_provider == "llm"


def test_langextract_enabled_when_configured():
    from src.utils.config import Settings

    settings = Settings(
        _env_file=None,
        extraction_provider="langextract",
        langextract_api_key="test-key",
        langextract_model="gemini-2.5-flash",
    )
    assert settings.extraction_provider == "langextract"
    assert settings.langextract_api_key == "test-key"


# -- Examples --


def test_build_examples():
    """Examples should use ExampleData and Extraction data classes."""
    provider = LangExtractProvider(api_key="test")
    examples = provider._build_examples()
    assert len(examples) >= 2

    from langextract.data import ExampleData, Extraction

    for ex in examples:
        assert isinstance(ex, ExampleData)
        assert isinstance(ex.text, str)
        assert len(ex.text) > 0
        assert len(ex.extractions) > 0
        for extraction in ex.extractions:
            assert isinstance(extraction, Extraction)
            assert extraction.extraction_class in (
                "contexte", "intention", "citation_an", "citation_senat",
                "evolution", "vigilance",
            )
            assert len(extraction.extraction_text) > 0
