"""Tests for debate-dossier linking with content-based matching."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, Debat, DossierLegislatif, Intervention, Loi


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database with test data."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def laws_and_dossiers(db_session):
    """Create test laws and dossiers for LF and LFSS in 2024."""
    # LF 2024 - promulgated Dec 29, 2023
    lf_2024 = Loi(
        numero="2023-1322",
        type="LF",
        annee=2024,
        date_promulgation=datetime(2023, 12, 29),
        titre="Loi de finances pour 2024",
    )
    db_session.add(lf_2024)
    db_session.flush()

    # LFSS 2024 - promulgated Dec 26, 2023
    lfss_2024 = Loi(
        numero="2023-1250",
        type="LFSS",
        annee=2024,
        date_promulgation=datetime(2023, 12, 26),
        titre="Loi de financement de la sécurité sociale pour 2024",
    )
    db_session.add(lfss_2024)
    db_session.flush()

    # LFR 2023 - promulgated Dec 30, 2023
    lfr_2023 = Loi(
        numero="2023-1323",
        type="LFR",
        annee=2023,
        date_promulgation=datetime(2023, 12, 30),
        titre="Loi de finances rectificative pour 2023",
    )
    db_session.add(lfr_2023)
    db_session.flush()

    # Dossier for LF 2024 at AN
    dossier_lf = DossierLegislatif(
        loi_id=lf_2024.id,
        chambre="AN",
        reference_externe="dossier_2023-1322",
    )
    db_session.add(dossier_lf)

    # Dossier for LFSS 2024 at AN
    dossier_lfss = DossierLegislatif(
        loi_id=lfss_2024.id,
        chambre="AN",
        reference_externe="dossier_2023-1250",
    )
    db_session.add(dossier_lfss)

    # Dossier for LFR 2023 at AN
    dossier_lfr = DossierLegislatif(
        loi_id=lfr_2023.id,
        chambre="AN",
        reference_externe="dossier_2023-1323",
    )
    db_session.add(dossier_lfr)

    db_session.flush()
    db_session.commit()

    return {
        "lf_2024": lf_2024,
        "lfss_2024": lfss_2024,
        "lfr_2023": lfr_2023,
        "dossier_lf": dossier_lf,
        "dossier_lfss": dossier_lfss,
        "dossier_lfr": dossier_lfr,
    }


class TestContentBasedMatching:
    """Test that content-based matching correctly identifies law type."""

    def test_plfss_content_links_to_lfss(self, db_session, laws_and_dossiers):
        """Debate containing 'PLFSS 2024' should link to LFSS law, not LF."""
        from scripts.link_debates_to_dossiers import match_debate_by_content

        laws = db_session.query(Loi).all()
        dossiers_by_loi = {
            d.loi_id: d for d in db_session.query(DossierLegislatif).all()
        }

        content = (
            "Discussion du projet de loi de financement de la sécurité sociale "
            "pour 2024. Le PLFSS 2024 prévoit une hausse des cotisations."
        )

        matched_loi, matched_dossier = match_debate_by_content(
            content, laws, dossiers_by_loi
        )

        assert matched_loi is not None
        assert matched_loi.type == "LFSS"
        assert matched_loi.annee == 2024
        assert matched_dossier is not None
        assert matched_dossier.id == laws_and_dossiers["dossier_lfss"].id

    def test_plf_content_links_to_lf(self, db_session, laws_and_dossiers):
        """Debate containing 'PLF 2024' should link to LF law, not LFSS."""
        from scripts.link_debates_to_dossiers import match_debate_by_content

        laws = db_session.query(Loi).all()
        dossiers_by_loi = {
            d.loi_id: d for d in db_session.query(DossierLegislatif).all()
        }

        content = (
            "Examen du PLF 2024 en séance publique. "
            "Le projet de loi de finances pour 2024 modifie l'article 150-0 B ter."
        )

        matched_loi, matched_dossier = match_debate_by_content(
            content, laws, dossiers_by_loi
        )

        assert matched_loi is not None
        assert matched_loi.type == "LF"
        assert matched_loi.annee == 2024
        assert matched_dossier is not None
        assert matched_dossier.id == laws_and_dossiers["dossier_lf"].id

    def test_plfr_content_links_to_lfr(self, db_session, laws_and_dossiers):
        """Debate containing 'PLFR 2023' should link to LFR law."""
        from scripts.link_debates_to_dossiers import match_debate_by_content

        laws = db_session.query(Loi).all()
        dossiers_by_loi = {
            d.loi_id: d for d in db_session.query(DossierLegislatif).all()
        }

        content = "Discussion du PLFR 2023 en commission des finances."

        matched_loi, matched_dossier = match_debate_by_content(
            content, laws, dossiers_by_loi
        )

        assert matched_loi is not None
        assert matched_loi.type == "LFR"
        assert matched_loi.annee == 2023

    def test_no_match_on_generic_content(self, db_session, laws_and_dossiers):
        """Content without law references should return None."""
        from scripts.link_debates_to_dossiers import match_debate_by_content

        laws = db_session.query(Loi).all()
        dossiers_by_loi = {
            d.loi_id: d for d in db_session.query(DossierLegislatif).all()
        }

        content = "Discussion générale sur les questions de politique économique."

        matched_loi, matched_dossier = match_debate_by_content(
            content, laws, dossiers_by_loi
        )

        assert matched_loi is None
        assert matched_dossier is None

    def test_empty_content_returns_none(self, db_session, laws_and_dossiers):
        """Empty content should return None."""
        from scripts.link_debates_to_dossiers import match_debate_by_content

        laws = db_session.query(Loi).all()
        dossiers_by_loi = {}

        matched_loi, matched_dossier = match_debate_by_content(
            "", laws, dossiers_by_loi
        )
        assert matched_loi is None
        assert matched_dossier is None


class TestDateFallback:
    """Test date-based fallback when content matching fails."""

    def test_date_matching_within_window(self, db_session, laws_and_dossiers):
        """Debate within a law's date window should match."""
        from scripts.link_debates_to_dossiers import match_debate_by_date

        lf = laws_and_dossiers["lf_2024"]
        promulgation = lf.date_promulgation
        law_configs = [(lf, promulgation - timedelta(days=90), promulgation + timedelta(days=15))]

        debat = Debat(
            chambre="AN",
            date=datetime(2023, 11, 15),
            etape="seance_publique",
        )

        matches = match_debate_by_date(debat, law_configs)
        assert len(matches) == 1
        assert matches[0][0].id == lf.id

    def test_date_matching_outside_window(self, db_session, laws_and_dossiers):
        """Debate outside all date windows should not match."""
        from scripts.link_debates_to_dossiers import match_debate_by_date

        lf = laws_and_dossiers["lf_2024"]
        promulgation = lf.date_promulgation
        law_configs = [(lf, promulgation - timedelta(days=90), promulgation + timedelta(days=15))]

        debat = Debat(
            chambre="AN",
            date=datetime(2023, 6, 1),  # Way before the window
            etape="seance_publique",
        )

        matches = match_debate_by_date(debat, law_configs)
        assert len(matches) == 0

    def test_date_matching_no_date(self, db_session, laws_and_dossiers):
        """Debate with no date should not match."""
        from scripts.link_debates_to_dossiers import match_debate_by_date

        lf = laws_and_dossiers["lf_2024"]
        promulgation = lf.date_promulgation
        law_configs = [(lf, promulgation - timedelta(days=90), promulgation + timedelta(days=15))]

        debat = Debat(chambre="AN", date=None, etape="seance_publique")

        matches = match_debate_by_date(debat, law_configs)
        assert len(matches) == 0

    def test_multiple_laws_in_same_window(self, db_session, laws_and_dossiers):
        """Date in overlapping LF+LFSS window should return multiple matches."""
        from scripts.link_debates_to_dossiers import match_debate_by_date

        lf = laws_and_dossiers["lf_2024"]
        lfss = laws_and_dossiers["lfss_2024"]

        lf_start = lf.date_promulgation - timedelta(days=90)
        lf_end = lf.date_promulgation + timedelta(days=15)
        lfss_start = lfss.date_promulgation - timedelta(days=120)
        lfss_end = lfss.date_promulgation + timedelta(days=15)

        law_configs = [
            (lf, lf_start, lf_end),
            (lfss, lfss_start, lfss_end),
        ]

        # Nov 15 is within both windows
        debat = Debat(
            chambre="AN",
            date=datetime(2023, 11, 15),
            etape="seance_publique",
        )

        matches = match_debate_by_date(debat, law_configs)
        assert len(matches) == 2


class TestDisambiguation:
    """Test disambiguation when multiple laws match by date."""

    def test_disambiguate_prefers_matching_chambre(self, db_session, laws_and_dossiers):
        """Should prefer law whose dossier chambre matches debate chambre."""
        from scripts.link_debates_to_dossiers import disambiguate_by_metadata

        lf = laws_and_dossiers["lf_2024"]
        lfss = laws_and_dossiers["lfss_2024"]

        # Create SENAT dossier only for LFSS
        dossier_lfss_senat = DossierLegislatif(
            loi_id=lfss.id,
            chambre="SENAT",
            reference_externe="dossier_lfss_senat",
        )
        db_session.add(dossier_lfss_senat)
        db_session.flush()

        dossiers_by_loi = {
            lf.id: laws_and_dossiers["dossier_lf"],  # AN dossier
            lfss.id: dossier_lfss_senat,  # SENAT dossier
        }

        date_matches = [
            (lf, datetime(2023, 10, 1), datetime(2024, 1, 13)),
            (lfss, datetime(2023, 8, 28), datetime(2024, 1, 10)),
        ]

        # SENAT debate should prefer LFSS (which has a SENAT dossier)
        debat = Debat(chambre="SENAT", date=datetime(2023, 11, 15))
        result = disambiguate_by_metadata(debat, date_matches, dossiers_by_loi)
        assert result.type == "LFSS"

    def test_disambiguate_single_match(self, db_session, laws_and_dossiers):
        """Single date match should return that law directly."""
        from scripts.link_debates_to_dossiers import disambiguate_by_metadata

        lf = laws_and_dossiers["lf_2024"]
        date_matches = [
            (lf, datetime(2023, 10, 1), datetime(2024, 1, 13)),
        ]

        debat = Debat(chambre="AN", date=datetime(2023, 11, 15))
        result = disambiguate_by_metadata(debat, date_matches, {})
        assert result.id == lf.id

    def test_disambiguate_prefers_lf_as_tiebreaker(self, db_session, laws_and_dossiers):
        """When chambre doesn't help, should prefer LF over LFSS."""
        from scripts.link_debates_to_dossiers import disambiguate_by_metadata

        lf = laws_and_dossiers["lf_2024"]
        lfss = laws_and_dossiers["lfss_2024"]

        # Both have AN dossiers, debate is AN -> chambre bonus is equal
        dossiers_by_loi = {
            lf.id: laws_and_dossiers["dossier_lf"],
            lfss.id: laws_and_dossiers["dossier_lfss"],
        }

        date_matches = [
            (lf, datetime(2023, 10, 1), datetime(2024, 1, 13)),
            (lfss, datetime(2023, 8, 28), datetime(2024, 1, 10)),
        ]

        debat = Debat(chambre="AN", date=datetime(2023, 11, 15))
        result = disambiguate_by_metadata(debat, date_matches, dossiers_by_loi)
        # LF gets +2 (chambre match) + 1 (LF tiebreaker) = 3
        # LFSS gets +2 (chambre match) + 0 = 2
        assert result.type == "LF"

    def test_disambiguate_empty_matches(self, db_session):
        """Empty date matches should return None."""
        from scripts.link_debates_to_dossiers import disambiguate_by_metadata

        debat = Debat(chambre="AN", date=datetime(2023, 11, 15))
        result = disambiguate_by_metadata(debat, [], {})
        assert result is None


class TestGetDebateContent:
    """Test content retrieval with intervention fallback."""

    def test_returns_contenu_integral_when_present(self, db_session, laws_and_dossiers):
        """Should return contenu_integral if it exists."""
        from scripts.link_debates_to_dossiers import get_debate_content

        dossier = laws_and_dossiers["dossier_lf"]
        debat = Debat(
            dossier_id=dossier.id,
            chambre="AN",
            date=datetime(2023, 11, 15),
            contenu_integral="Discussion du PLF 2024 en séance publique.",
        )
        db_session.add(debat)
        db_session.flush()

        content = get_debate_content(db_session, debat)
        assert content == "Discussion du PLF 2024 en séance publique."

    def test_falls_back_to_interventions(self, db_session, laws_and_dossiers):
        """Should concatenate intervention content when contenu_integral is absent."""
        from scripts.link_debates_to_dossiers import get_debate_content

        dossier = laws_and_dossiers["dossier_lf"]
        debat = Debat(
            dossier_id=dossier.id,
            chambre="AN",
            date=datetime(2023, 11, 15),
            contenu_integral=None,
        )
        db_session.add(debat)
        db_session.flush()

        interv1 = Intervention(
            debat_id=debat.id,
            orateur="M. Dupont",
            contenu="Le PLFSS 2024 prévoit des mesures importantes.",
            ordre=1,
        )
        interv2 = Intervention(
            debat_id=debat.id,
            orateur="Mme Martin",
            contenu="Je soutiens ce projet de loi de financement.",
            ordre=2,
        )
        db_session.add_all([interv1, interv2])
        db_session.flush()

        content = get_debate_content(db_session, debat)
        assert "PLFSS 2024" in content
        assert "financement" in content

    def test_returns_empty_string_when_no_content(self, db_session, laws_and_dossiers):
        """Should return empty string when neither integral nor interventions exist."""
        from scripts.link_debates_to_dossiers import get_debate_content

        dossier = laws_and_dossiers["dossier_lf"]
        debat = Debat(
            dossier_id=dossier.id,
            chambre="AN",
            date=datetime(2023, 11, 15),
            contenu_integral=None,
        )
        db_session.add(debat)
        db_session.flush()

        content = get_debate_content(db_session, debat)
        assert content == ""


class TestEndToEndLinking:
    """Integration tests for the full linking flow."""

    def test_content_match_overrides_date_proximity(self, db_session, laws_and_dossiers):
        """A PLFSS debate in the LF date window should still link to LFSS via content."""
        from scripts.link_debates_to_dossiers import (
            match_debate_by_content,
            match_debate_by_date,
        )

        laws = db_session.query(Loi).all()
        dossiers_by_loi = {
            d.loi_id: d for d in db_session.query(DossierLegislatif).all()
        }

        # This debate is in the LF date window but mentions PLFSS
        content = "Examen du PLFSS 2024 en première lecture à l'Assemblée nationale."

        lf = laws_and_dossiers["lf_2024"]
        lfss = laws_and_dossiers["lfss_2024"]

        # Both laws would match by date for Nov 15
        lf_start = lf.date_promulgation - timedelta(days=90)
        lf_end = lf.date_promulgation + timedelta(days=15)
        lfss_start = lfss.date_promulgation - timedelta(days=120)
        lfss_end = lfss.date_promulgation + timedelta(days=15)
        law_configs = [
            (lf, lf_start, lf_end),
            (lfss, lfss_start, lfss_end),
        ]

        debat = Debat(chambre="AN", date=datetime(2023, 11, 15))

        # Content match should find LFSS
        matched_loi, matched_dossier = match_debate_by_content(
            content, laws, dossiers_by_loi
        )
        assert matched_loi is not None
        assert matched_loi.type == "LFSS"

        # Date match would find both
        date_matches = match_debate_by_date(debat, law_configs)
        assert len(date_matches) == 2

        # Content match wins - this is the key assertion
        assert matched_loi.type == "LFSS"

    def test_intervention_fallback_for_content_matching(self, db_session, laws_and_dossiers):
        """Content matching should work via intervention fallback."""
        from scripts.link_debates_to_dossiers import (
            get_debate_content,
            match_debate_by_content,
        )

        debat = Debat(
            dossier_id=None,
            chambre="AN",
            date=datetime(2023, 11, 15),
            contenu_integral=None,
        )
        db_session.add(debat)
        db_session.flush()

        interv = Intervention(
            debat_id=debat.id,
            orateur="M. le rapporteur",
            contenu="Le PLFSS 2024 est un texte essentiel pour notre système de santé.",
            ordre=1,
        )
        db_session.add(interv)
        db_session.flush()

        laws = db_session.query(Loi).all()
        dossiers_by_loi = {
            d.loi_id: d for d in db_session.query(DossierLegislatif).all()
        }

        content = get_debate_content(db_session, debat)
        assert "PLFSS 2024" in content

        matched_loi, matched_dossier = match_debate_by_content(
            content, laws, dossiers_by_loi
        )
        assert matched_loi is not None
        assert matched_loi.type == "LFSS"
        assert matched_loi.annee == 2024
