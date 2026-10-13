"""Tests for archive PDF debate parser (src/parsers/archive_debates.py)."""

import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.parsers.archive_debates import (
    clean_archive_text,
    detect_archive_speaker_function,
    extract_date_from_session_name,
    extract_date_from_text,
    parse_archive_debate,
)


# ─── Real OCR text fixtures (based on actual Journal Officiel format) ───

# 1958-era AN archive text (from legislature 1, 1958-1959 session)
SAMPLE_TEXT_1958 = """\
JOURNAL OFFICIEL DE LA RÉPUBLIQUE FRANÇAISE

DÉBATS PARLEMENTAIRES

ASSEMBLÉE NATIONALE

COMPTE RENDU INTÉGRAL

1re SÉANCE DU MERCREDI 15 JANVIER 1959

3467

PRÉSIDENCE DE M. CHABAN-DELMAS

M. LE PRÉSIDENT. — La séance est ouverte.
La parole est à M. le rapporteur général.

M. GILBERT-JULES, rapporteur général. — Mes chers collègues,
nous sommes appelés à examiner aujourd'hui l'article 8 du projet
de loi de finances pour 1959. Cet article modifie les dispositions
relatives aux plus-values mobilières prévues à l'article 150 du
code général des impôts.

M. PINAY, ministre des finances et des affaires économiques. —
Le Gouvernement a déposé cet amendement en vue de simplifier
le dispositif fiscal applicable aux cessions de valeurs mobilières.
Il s'agit d'une mesure de justice fiscale.

M. CHABAN-DELMAS, président. — La parole est à M. Ramadier.

M. RAMADIER. — Monsieur le Ministre, je souhaite appeler votre
attention sur les conséquences de cette disposition pour les petits
porteurs.
"""

# 1988-era AN archive text (from legislature 9)
SAMPLE_TEXT_1988 = """\
JOURNAL OFFICIEL DE LA RÉPUBLIQUE FRANÇAISE

ASSEMBLÉE NATIONALE

COMPTE RENDU INTÉGRAL

2e SÉANCE DU MARDI 4 OCTOBRE 1988

6789

M. LE PRÉSIDENT. — L'ordre du jour appelle la discussion
du projet de loi de finances pour 1989.

M. BÉRÉGOVOY, ministre d'État, ministre de l'économie,
des finances et du budget. — Monsieur le président,
mesdames, messieurs les députés, le budget que je vous
présente aujourd'hui est un budget de rigueur et de solidarité.

M. AUBERGER, rapporteur général de la commission des
finances. — Ce projet de loi de finances comporte des dispositions
importantes en matière de fiscalité des entreprises, notamment
l'article 12 qui modifie le régime des plus-values professionnelles.

M. BALLADUR. — Je voudrais interroger M. le ministre sur
les mesures prévues à l'article 15 bis concernant l'impôt
de solidarité sur la fortune.
"""

# 1998-era AN archive text (from legislature 11)
SAMPLE_TEXT_1998 = """\
ASSEMBLÉE NATIONALE

COMPTE RENDU INTÉGRAL

SÉANCE DU VENDREDI 2 OCTOBRE 1998

5432

PRÉSIDENCE DE M. FABIUS

M. Le Président. – La séance est ouverte.
Nous reprenons l'examen du projet de loi de finances pour 1999.

M. Strauss-Kahn, ministre de l'économie, des finances et de
l'industrie. – Monsieur le président, mesdames et messieurs
les députés, nous abordons la deuxième partie du projet de loi
de finances, relative aux moyens des services.

M. Migaud, rapporteur général de la commission des finances. –
L'article 20 du projet comporte une disposition importante
qui concerne la taxe sur la valeur ajoutée applicable aux
travaux de construction.

Mme Bricq. – Je souhaite poser une question au ministre
sur l'article 3 du projet relatif à l'impôt sur le revenu.
"""


class TestCleanArchiveText:
    def test_removes_jo_headers(self):
        text = (
            "JOURNAL OFFICIEL DE LA RÉPUBLIQUE FRANÇAISE\n"
            "DÉBATS PARLEMENTAIRES\n"
            "Some actual content here.\n"
        )
        result = clean_archive_text(text)
        assert "JOURNAL OFFICIEL" not in result
        assert "DÉBATS PARLEMENTAIRES" not in result
        assert "Some actual content here." in result

    def test_removes_page_numbers(self):
        text = "Some text before\n3467\nSome text after\n"
        result = clean_archive_text(text)
        assert "3467" not in result
        assert "Some text before" in result
        assert "Some text after" in result

    def test_removes_seance_headers(self):
        text = (
            "1re SÉANCE DU MERCREDI 15 JANVIER 1959\n"
            "PRÉSIDENCE DE M. CHABAN-DELMAS\n"
            "Content here.\n"
        )
        result = clean_archive_text(text)
        assert "Content here." in result

    def test_normalizes_whitespace(self):
        text = "First paragraph.\n\n\n\n\nSecond paragraph.\n"
        result = clean_archive_text(text)
        assert "\n\n\n" not in result
        assert "First paragraph." in result
        assert "Second paragraph." in result

    def test_removes_form_feeds(self):
        text = "Before page break.\fAfter page break."
        result = clean_archive_text(text)
        assert "\f" not in result
        assert "Before page break." in result
        assert "After page break." in result

    def test_fixes_numero_spacing(self):
        text = "L'amendement N ° 42 est adopté."
        result = clean_archive_text(text)
        assert "N° 42" in result


class TestCleanArchiveTextAllcapsNormalization:
    def test_simple_name(self):
        text = "M. DUPONT. — Bonjour."
        result = clean_archive_text(text)
        assert "M. Dupont" in result

    def test_mme_name(self):
        text = "Mme MARTIN. — Je suis d'accord."
        result = clean_archive_text(text)
        assert "Mme Martin" in result

    def test_compound_name(self):
        text = "M. LE PRÉSIDENT. — La séance est ouverte."
        result = clean_archive_text(text)
        assert "M. Le Président" in result

    def test_hyphenated_name(self):
        text = "M. CHABAN-DELMAS, président. — La parole est à M. Ramadier."
        result = clean_archive_text(text)
        assert "M. Chaban-Delmas" in result

    def test_hyphenated_name_gilbert_jules(self):
        text = "M. GILBERT-JULES, rapporteur général. — Mes chers collègues."
        result = clean_archive_text(text)
        assert "M. Gilbert-Jules" in result

    def test_mlle_name(self):
        text = "Mlle DUPONT. — Merci."
        result = clean_archive_text(text)
        assert "Mlle Dupont" in result

    def test_multiple_names_in_text(self):
        text = "M. DUPONT. — Merci.\nMme MARTIN. — Oui.\nM. LE RAPPORTEUR. — Non."
        result = clean_archive_text(text)
        assert "M. Dupont" in result
        assert "Mme Martin" in result
        assert "M. Le Rapporteur" in result

    def test_preserves_already_titlecase(self):
        text = "M. Dupont. — Already fine."
        result = clean_archive_text(text)
        assert "M. Dupont" in result

    def test_does_not_convert_non_speaker_allcaps(self):
        # Short single ALL-CAPS words (< 2 chars per word) should not be touched
        # since the regex requires at least 2 chars
        text = "La TVA est à 20%."
        result = clean_archive_text(text)
        assert "TVA" in result  # Should NOT be lowercased (not after M./Mme)


class TestExtractDateFromText:
    def test_date_1959(self):
        text = "SÉANCE DU MERCREDI 15 JANVIER 1959\nSome content"
        result = extract_date_from_text(text)
        assert result == datetime(1959, 1, 15)

    def test_date_1988(self):
        text = "2e SÉANCE DU MARDI 4 OCTOBRE 1988\nSome content"
        result = extract_date_from_text(text)
        assert result == datetime(1988, 10, 4)

    def test_date_1998(self):
        text = "SÉANCE DU VENDREDI 2 OCTOBRE 1998\nSome content"
        result = extract_date_from_text(text)
        assert result == datetime(1998, 10, 2)

    def test_date_with_accented_month(self):
        text = "SÉANCE DU LUNDI 3 FÉVRIER 1975\nContent"
        result = extract_date_from_text(text)
        assert result == datetime(1975, 2, 3)

    def test_date_without_day_name(self):
        text = "15 janvier 1959 suite des débats"
        result = extract_date_from_text(text)
        assert result == datetime(1959, 1, 15)

    def test_no_date_found(self):
        text = "No date information in this text at all."
        result = extract_date_from_text(text)
        assert result is None


class TestExtractDateFromSessionName:
    def test_an_session_name(self):
        result = extract_date_from_session_name("1988-1989-ordinaire1")
        assert result == datetime(1988, 10, 1)

    def test_senat_filename(self):
        result = extract_date_from_session_name("s19881004_0001_0120")
        assert result == datetime(1988, 10, 4)

    def test_senat_date_only(self):
        result = extract_date_from_session_name("19750215")
        assert result == datetime(1975, 2, 15)

    def test_empty_input(self):
        result = extract_date_from_session_name("")
        assert result is None

    def test_none_input(self):
        result = extract_date_from_session_name(None)
        assert result is None

    def test_unrecognized_format(self):
        result = extract_date_from_session_name("random-text")
        assert result is None


class TestDetectArchiveSpeakerFunction:
    def test_president(self):
        assert detect_archive_speaker_function(
            "La séance est ouverte.", "M. Le Président"
        ) == "president"

    def test_ministre(self):
        assert detect_archive_speaker_function(
            "ministre de l'économie, des finances", "M. Bérégovoy"
        ) == "ministre"

    def test_rapporteur(self):
        assert detect_archive_speaker_function(
            "rapporteur général de la commission des finances", "M. Auberger"
        ) == "rapporteur"

    def test_secretaire_etat(self):
        assert detect_archive_speaker_function(
            "secrétaire d'État auprès du ministre", "M. Durand"
        ) == "ministre"

    def test_regular_deputy(self):
        assert detect_archive_speaker_function(
            "Monsieur le ministre, je voudrais", "M. Balladur"
        ) == "autre"

    def test_no_false_positive_on_content(self):
        # A deputy saying "Monsieur le Président" in their speech should not
        # be classified as president — we only check the intro line
        assert detect_archive_speaker_function(
            "Je souhaite poser une question", "M. Dupont"
        ) == "autre"


class TestParseArchiveDebateSpeakerDetection:
    def test_detects_speakers_in_1958_text(self):
        """Verify speaker splitting on 1958-era text after ALL-CAPS normalization."""
        from src.parsers.archive_debates import clean_archive_text, _SPEAKER_PATTERN
        import re

        cleaned = clean_archive_text(SAMPLE_TEXT_1958)
        speakers = re.findall(_SPEAKER_PATTERN, cleaned)
        # Should find multiple speakers
        assert len(speakers) >= 3
        # Should include normalized names
        speaker_names = [(s[0] or s[1]).strip() for s in speakers]
        assert any("Gilbert-Jules" in s or "Gilbert" in s for s in speaker_names)

    def test_detects_speakers_in_1988_text(self):
        """Verify speaker splitting on 1988-era text."""
        from src.parsers.archive_debates import clean_archive_text, _SPEAKER_PATTERN
        import re

        cleaned = clean_archive_text(SAMPLE_TEXT_1988)
        speakers = re.findall(_SPEAKER_PATTERN, cleaned)
        assert len(speakers) >= 3

    def test_detects_speakers_in_1998_text(self):
        """Verify speaker splitting on 1998-era text (already Title Case)."""
        from src.parsers.archive_debates import clean_archive_text, _SPEAKER_PATTERN
        import re

        cleaned = clean_archive_text(SAMPLE_TEXT_1998)
        speakers = re.findall(_SPEAKER_PATTERN, cleaned)
        assert len(speakers) >= 3


class TestParseArchiveDebateEmptyText:
    def test_empty_pdf_returns_none(self):
        """Verify graceful handling of PDF with very little text."""
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = "Short"
            result = parse_archive_debate(
                Path("/fake/path.pdf"), chambre="AN", session_name="1988-1989-ordinaire1"
            )
            assert result is None


class TestParseArchiveDebateNoDate:
    def test_returns_none_when_no_date_found(self):
        """Verify returns None when neither text nor session yields a date."""
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = "A" * 500  # Enough text but no date
            result = parse_archive_debate(
                Path("/fake/path.pdf"), chambre="AN", session_name="random-text"
            )
            assert result is None


class TestParseArchiveDebateNoSpeakers:
    def test_no_speakers_returns_empty_interventions(self):
        """Verify ParsedDebat with 0 interventions when no speakers detected."""
        text_no_speakers = (
            "Texte sans aucun orateur identifiable. "
            "Ce texte ne contient que du contenu administratif "
            "relatif au budget de l'État pour l'année 1975. " * 10
        )
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = text_no_speakers
            result = parse_archive_debate(
                Path("/fake/path.pdf"),
                chambre="AN",
                session_name="1975-1976-ordinaire1",
            )
            # Should return a ParsedDebat (has a date from session name)
            # but with 0 interventions
            assert result is not None
            assert result.interventions == []
            assert result.date == datetime(1975, 10, 1)


class TestParseArchiveDebateIntegration:
    """Integration tests using mocked PDF text extraction."""

    def test_full_parse_1988(self):
        """Full parse of 1988-era text produces valid ParsedDebat."""
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = SAMPLE_TEXT_1988
            result = parse_archive_debate(
                Path("/fake/leg9/1988-1989-ordinaire1/001.pdf"),
                chambre="AN",
                session_name="9/1988-1989-ordinaire1",
            )
            assert result is not None
            assert result.chambre == "AN"
            assert result.date == datetime(1988, 10, 4)
            assert result.etape == "seance_publique"
            assert len(result.interventions) >= 1
            assert result.contenu_integral == ""  # Archive PDFs don't store full text

    def test_full_parse_1998(self):
        """Full parse of 1998-era text produces valid ParsedDebat."""
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = SAMPLE_TEXT_1998
            result = parse_archive_debate(
                Path("/fake/leg11/1998-1999-ordinaire1/001.pdf"),
                chambre="AN",
                session_name="11/1998-1999-ordinaire1",
            )
            assert result is not None
            assert result.chambre == "AN"
            assert result.date == datetime(1998, 10, 2)
            assert len(result.interventions) >= 1

    def test_article_mentions_extracted(self):
        """Verify article mentions are extracted from interventions."""
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = SAMPLE_TEXT_1988
            result = parse_archive_debate(
                Path("/fake/path.pdf"),
                chambre="AN",
                session_name="9/1988-1989-ordinaire1",
            )
            assert result is not None
            # The 1988 text mentions articles 12 and 15 bis
            all_articles = []
            for interv in result.interventions:
                all_articles.extend(interv.articles_mentionnes)
            assert len(all_articles) > 0

    def test_reference_key_format_an(self):
        """Verify reference key format for AN debates."""
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = SAMPLE_TEXT_1988
            result = parse_archive_debate(
                Path("/fake/leg9/1988-1989-ordinaire1/045.pdf"),
                chambre="AN",
                session_name="9/1988-1989-ordinaire1",
            )
            assert result is not None
            assert result.reference.startswith("archive_an:")

    def test_reference_key_format_senat(self):
        """Verify reference key format for Sénat debates."""
        with patch("src.parsers.archive_debates.extract_text_from_pdf") as mock:
            mock.return_value = SAMPLE_TEXT_1998
            result = parse_archive_debate(
                Path("/fake/senat/s19981002_0001_0120.pdf"),
                chambre="SENAT",
                session_name="s19981002_0001_0120",
            )
            assert result is not None
            assert result.reference.startswith("archive_senat:")
