"""Tests for amendment filtering (1A) and article linking (2A) fixes."""


class TestNormalizeArticleRef:
    """Tests for normalize_article_ref helper."""

    def test_strips_article_prefix(self):
        from src.db.import_data import normalize_article_ref

        assert normalize_article_ref("Article 12") == "12"
        assert normalize_article_ref("article 12") == "12"
        assert normalize_article_ref("Art. 12") == "12"
        assert normalize_article_ref("art. 12") == "12"
        assert normalize_article_ref("Art 12") == "12"

    def test_normalizes_whitespace(self):
        from src.db.import_data import normalize_article_ref

        assert normalize_article_ref("1   bis") == "1 bis"
        assert normalize_article_ref("  12  ") == "12"
        assert normalize_article_ref("150-0  B   ter") == "150-0 b ter"

    def test_lowercases(self):
        from src.db.import_data import normalize_article_ref

        assert normalize_article_ref("1 BIS") == "1 bis"
        assert normalize_article_ref("12 A") == "12 a"

    def test_bare_number(self):
        from src.db.import_data import normalize_article_ref

        assert normalize_article_ref("1") == "1"
        assert normalize_article_ref("100") == "100"


class TestArticleRefsMatch:
    """Tests for article_refs_match (2A fix)."""

    def test_exact_match(self):
        from src.db.import_data import article_refs_match

        assert article_refs_match("12", "12") is True
        assert article_refs_match("1 bis", "1 bis") is True

    def test_prefix_stripped_match(self):
        from src.db.import_data import article_refs_match

        assert article_refs_match("Art. 12", "12") is True
        assert article_refs_match("article 1 bis", "1 bis") is True

    def test_no_false_substring_match(self):
        """The old code matched '1' to '10', '100', '1 bis' via substring."""
        from src.db.import_data import article_refs_match

        assert article_refs_match("1", "10") is False
        assert article_refs_match("1", "100") is False
        assert article_refs_match("1", "1 bis") is False
        assert article_refs_match("1", "12") is False
        assert article_refs_match("1", "21") is False
        assert article_refs_match("10", "100") is False

    def test_case_insensitive(self):
        from src.db.import_data import article_refs_match

        assert article_refs_match("1 BIS", "1 bis") is True
        assert article_refs_match("12 A", "12 a") is True

    def test_whitespace_insensitive(self):
        from src.db.import_data import article_refs_match

        assert article_refs_match("1  bis", "1 bis") is True
        assert article_refs_match("Art.  12", "12") is True


class TestIsFinanceLawAmendment:
    """Tests for is_finance_law_amendment (1A fix)."""

    def test_existing_refs_still_match(self):
        """BTC, PLF, PLFSS, PLFR should still match."""
        from src.db.import_data import is_finance_law_amendment

        assert is_finance_law_amendment("BTC-15-12345") is True
        assert is_finance_law_amendment("PLF-2024-001") is True
        assert is_finance_law_amendment("PLFSS2024") is True
        assert is_finance_law_amendment("PLFR-2023") is True

    def test_law_numero_pattern(self):
        """Numeros like '2023-1322' should match."""
        from src.db.import_data import is_finance_law_amendment

        assert is_finance_law_amendment("TEXTE_2023-1322") is True
        assert is_finance_law_amendment("REF_2012-1510") is True

    def test_pjl_prefixes(self):
        """PJL, PRJL, PRJLFIN should match."""
        from src.db.import_data import is_finance_law_amendment

        assert is_finance_law_amendment("PJL-123") is True
        assert is_finance_law_amendment("PRJL-456") is True
        assert is_finance_law_amendment("PRJLFIN2024") is True

    def test_nor_codes(self):
        """NOR codes like ECOX2300001L should match."""
        from src.db.import_data import is_finance_law_amendment

        assert is_finance_law_amendment("ECOX2300001L") is True
        assert is_finance_law_amendment("REF_FINU2400123A") is True

    def test_known_law_numeros(self):
        """Should match against known law numeros from DB."""
        from src.db.import_data import is_finance_law_amendment

        known = {"2023-1322", "2012-1510"}
        assert is_finance_law_amendment("TEXTE_2023-1322_ART1", known) is True
        assert is_finance_law_amendment("LOI_2012-1510", known) is True

    def test_empty_ref(self):
        from src.db.import_data import is_finance_law_amendment

        assert is_finance_law_amendment("") is False
        assert is_finance_law_amendment(None) is False

    def test_unrelated_ref_rejected(self):
        """Unrelated text references should be rejected."""
        from src.db.import_data import is_finance_law_amendment

        assert is_finance_law_amendment("SOME_RANDOM_TEXT") is False
        assert is_finance_law_amendment("CODE_CIVIL_ART123") is False


class TestExtractJorfIdFromUrl:
    """Tests for extract_jorf_id_from_url (3A fix)."""

    def test_normal_url(self):
        from src.db.import_data import extract_jorf_id_from_url

        url = "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000031733082"
        assert extract_jorf_id_from_url(url) == "JORFTEXT000031733082"

    def test_trailing_slash(self):
        from src.db.import_data import extract_jorf_id_from_url

        url = "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000031733082/"
        assert extract_jorf_id_from_url(url) == "JORFTEXT000031733082"

    def test_none_url(self):
        from src.db.import_data import extract_jorf_id_from_url

        assert extract_jorf_id_from_url(None) is None

    def test_empty_url(self):
        from src.db.import_data import extract_jorf_id_from_url

        assert extract_jorf_id_from_url("") is None
        assert extract_jorf_id_from_url("   ") is None

    def test_url_without_jorftext(self):
        """If the last segment is not a JORFTEXT ID, return None."""
        from src.db.import_data import extract_jorf_id_from_url

        url = "https://www.legifrance.gouv.fr/jorf/id/SOMEOTHERTEXT"
        assert extract_jorf_id_from_url(url) is None

    def test_url_with_multiple_slashes(self):
        from src.db.import_data import extract_jorf_id_from_url

        url = "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000048688788///"
        assert extract_jorf_id_from_url(url) == "JORFTEXT000048688788"


class TestDeduplicateDebates:
    """Tests for deduplicate_debates (5A fix)."""

    def test_deduplicate_keeps_most_interventions(self):
        """The debate with the most interventions should be kept."""
        from datetime import datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from src.db.import_data import deduplicate_debates
        from src.db.models import Base, Debat, DossierLegislatif, Intervention, Loi

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        session = TestSession()

        # Create a law and dossier
        loi = Loi(
            numero="2023-1322", type="LF", annee=2024,
            date_promulgation=datetime(2023, 12, 29), titre="LF 2024",
        )
        session.add(loi)
        session.flush()

        dossier = DossierLegislatif(
            loi_id=loi.id, chambre="AN", url_source="http://example.com",
        )
        session.add(dossier)
        session.flush()

        # Create two debates with the same reference_externe
        debat1 = Debat(
            dossier_id=dossier.id, chambre="AN",
            date=datetime(2023, 11, 1), etape="seance_publique",
            reference_externe="CRAN20231101",
        )
        debat2 = Debat(
            dossier_id=dossier.id, chambre="AN",
            date=datetime(2023, 11, 1), etape="seance_publique",
            reference_externe="CRAN20231101",
        )
        session.add_all([debat1, debat2])
        session.flush()

        # debat1 gets 2 interventions, debat2 gets 5
        for i in range(2):
            session.add(Intervention(
                debat_id=debat1.id, orateur=f"Speaker {i}",
                contenu="Content", ordre=i,
            ))
        for i in range(5):
            session.add(Intervention(
                debat_id=debat2.id, orateur=f"Speaker {i}",
                contenu="Content", ordre=i,
            ))
        session.commit()

        removed = deduplicate_debates(session)
        assert removed == 1

        # Only the debate with 5 interventions should remain
        remaining = session.query(Debat).filter_by(
            reference_externe="CRAN20231101"
        ).all()
        assert len(remaining) == 1
        assert remaining[0].id == debat2.id

    def test_no_duplicates_returns_zero(self):
        """If there are no duplicates, nothing should be removed."""
        from datetime import datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from src.db.import_data import deduplicate_debates
        from src.db.models import Base, Debat, DossierLegislatif, Loi

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        session = TestSession()

        loi = Loi(
            numero="2023-1322", type="LF", annee=2024,
            date_promulgation=datetime(2023, 12, 29), titre="LF 2024",
        )
        session.add(loi)
        session.flush()

        dossier = DossierLegislatif(
            loi_id=loi.id, chambre="AN", url_source="http://example.com",
        )
        session.add(dossier)
        session.flush()

        debat = Debat(
            dossier_id=dossier.id, chambre="AN",
            date=datetime(2023, 11, 1), etape="seance_publique",
            reference_externe="UNIQUE_REF",
        )
        session.add(debat)
        session.commit()

        removed = deduplicate_debates(session)
        assert removed == 0


class TestImportDebatUpsert:
    """Tests for the upsert logic in import_debat (5A fix)."""

    def test_skips_duplicate_reference(self):
        """import_debat should return existing record if reference_externe matches."""
        from datetime import datetime

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from src.db.import_data import import_debat
        from src.db.models import Base, Debat, DossierLegislatif, Loi
        from src.parsers.debates import ParsedDebat

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)
        session = TestSession()

        loi = Loi(
            numero="2023-1322", type="LF", annee=2024,
            date_promulgation=datetime(2023, 12, 29), titre="LF 2024",
        )
        session.add(loi)
        session.flush()

        dossier = DossierLegislatif(
            loi_id=loi.id, chambre="AN", url_source="http://example.com",
        )
        session.add(dossier)
        session.flush()

        # Insert a debate
        existing = Debat(
            dossier_id=dossier.id, chambre="AN",
            date=datetime(2023, 11, 1), etape="seance_publique",
            reference_externe="REF123",
        )
        session.add(existing)
        session.flush()

        # Try to import a debate with the same reference
        parsed = ParsedDebat(
            chambre="AN",
            date=datetime(2023, 11, 1),
            etape="seance_publique",
            url_source=None,
            reference="REF123",
            interventions=[],
            contenu_integral="Some content",
        )

        result = import_debat(session, dossier, parsed)
        assert result.id == existing.id

        # Should still be only 1 debate
        assert session.query(Debat).count() == 1
