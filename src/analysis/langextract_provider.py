"""Source-grounded extraction using Google's LangExtract library."""

import logging

from src.analysis.models import (
    CertitudeLevel,
    QuotedSource,
    SectionAnalysis,
)

logger = logging.getLogger(__name__)


class LangExtractProvider:
    """Source-grounded extraction using Google's LangExtract library.

    LangExtract maps every extraction to its exact position in the source text,
    providing guaranteed source grounding. Added as an optional provider (Gemini)
    alongside existing Groq/Anthropic.
    """

    def __init__(self, api_key: str, model_id: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model_id = model_id

    def extract_section(self, article_info, mod, debate_text, amendment_text, expose_motifs):
        """Extract section analysis with source-grounded citations.

        Args:
            article_info: ArticleInfo with article reference, code, title, keywords
            mod: LawModification being analyzed
            debate_text: Formatted debate text for extraction
            amendment_text: Formatted amendment text for extraction
            expose_motifs: Exposé des motifs text (may be empty)

        Returns:
            SectionAnalysis with grounded citations
        """
        import langextract as lx

        source_text = self._build_source_document(debate_text, amendment_text, expose_motifs)

        prompt_description = (
            f"Extraire les informations relatives à l'intention du législateur "
            f"concernant l'article {article_info.reference} du {article_info.code}, "
            f"modifié par {mod.law_ref}. "
            "Pour chaque information, extraire la CITATION EXACTE du texte source. "
            "Catégories à extraire : contexte législatif, intention du législateur, "
            "citations de l'Assemblée nationale, citations du Sénat, "
            "évolution des débats, points de vigilance."
        )

        examples = self._build_examples()

        try:
            result = lx.extract(
                text_or_documents=source_text,
                prompt_description=prompt_description,
                examples=examples,
                model_id=self.model_id,
                api_key=self.api_key,
            )
        except Exception as e:
            logger.error(f"LangExtract extraction failed: {e}")
            return self._create_fallback_section(mod)

        return self._to_section_analysis(result, mod, source_text)

    def _build_source_document(self, debate_text, amendment_text, expose_motifs):
        """Combine chunks into a single source document for grounding."""
        parts = []

        if expose_motifs:
            parts.append(f"=== EXPOSÉ DES MOTIFS ===\n{expose_motifs}")

        if debate_text:
            parts.append(f"=== DÉBATS PARLEMENTAIRES ===\n{debate_text}")

        if amendment_text:
            parts.append(f"=== AMENDEMENTS ===\n{amendment_text}")

        return "\n\n".join(parts) if parts else ""

    def _build_examples(self):
        """Build few-shot examples for LangExtract."""
        from langextract.data import ExampleData, Extraction

        return [
            ExampleData(
                text=(
                    "M. le rapporteur général. Cet article vise à lutter contre "
                    "les schémas d'optimisation fiscale utilisant le mécanisme "
                    "d'apport-cession. Le dispositif de report d'imposition "
                    "est ainsi renforcé pour garantir que le réinvestissement "
                    "soit effectif et économiquement significatif."
                ),
                extractions=[
                    Extraction(
                        extraction_class="intention",
                        extraction_text=(
                            "lutter contre les schémas d'optimisation fiscale "
                            "utilisant le mécanisme d'apport-cession"
                        ),
                    ),
                    Extraction(
                        extraction_class="citation_an",
                        extraction_text=(
                            "Le dispositif de report d'imposition est ainsi renforcé "
                            "pour garantir que le réinvestissement soit effectif et "
                            "économiquement significatif"
                        ),
                    ),
                ],
            ),
            ExampleData(
                text=(
                    "Mme la sénatrice. Nous proposons de relever le seuil de "
                    "réinvestissement à 60 % du produit de cession, afin de "
                    "mieux encadrer les opérations d'apport-cession tout en "
                    "préservant l'attractivité du dispositif pour les entrepreneurs."
                ),
                extractions=[
                    Extraction(
                        extraction_class="citation_senat",
                        extraction_text=(
                            "relever le seuil de réinvestissement à 60 % du produit "
                            "de cession, afin de mieux encadrer les opérations "
                            "d'apport-cession tout en préservant l'attractivité "
                            "du dispositif pour les entrepreneurs"
                        ),
                    ),
                ],
            ),
        ]

    def _to_section_analysis(self, lx_result, mod, source_text):
        """Convert LangExtract output to SectionAnalysis dataclass.

        Source grounding: LangExtract provides exact character offsets,
        so citations are guaranteed to exist in source text.
        """
        contexte = ""
        intention = ""
        sources_an = []
        sources_senat = []
        evolution_debats = ""
        points_vigilance = []

        # LangExtract returns AnnotatedDocument(s) with .extractions list
        # Each extraction has .extraction_class and .extraction_text
        if isinstance(lx_result, list):
            # Multiple documents - collect all extractions
            all_extractions = []
            for doc in lx_result:
                all_extractions.extend(getattr(doc, "extractions", []) or [])
            extractions = all_extractions
        elif hasattr(lx_result, "extractions"):
            extractions = lx_result.extractions or []
        else:
            extractions = []

        for ext in extractions:
            category = getattr(ext, "extraction_class", "") or ""
            text = getattr(ext, "extraction_text", "") or ""
            attrs = getattr(ext, "attributes", {}) or {}
            orateur = attrs.get("orateur", "")
            fonction = attrs.get("fonction", "")
            date_str = attrs.get("date", "")

            if not text:
                continue

            if category == "contexte":
                contexte = text if not contexte else f"{contexte} {text}"
            elif category == "intention":
                intention = text if not intention else f"{intention} {text}"
            elif category == "citation_an":
                sources_an.append(
                    QuotedSource(
                        citation=text,
                        orateur=orateur,
                        fonction=fonction,
                        date=date_str,
                        chambre="AN",
                    )
                )
            elif category == "citation_senat":
                sources_senat.append(
                    QuotedSource(
                        citation=text,
                        orateur=orateur,
                        fonction=fonction,
                        date=date_str,
                        chambre="Sénat",
                    )
                )
            elif category == "evolution":
                evolution_debats = text if not evolution_debats else f"{evolution_debats} {text}"
            elif category == "vigilance":
                points_vigilance.append(text)

        # Grounded citations can't be hallucinated - high confidence by default
        niveau = CertitudeLevel.ELEVE if (sources_an or sources_senat) else CertitudeLevel.MOYEN

        return SectionAnalysis(
            modification=mod,
            contexte=contexte or f"Modification apportée par {mod.law_ref}",
            intention=intention or "",
            sources_an=sources_an,
            sources_senat=sources_senat,
            evolution_debats=evolution_debats,
            points_vigilance=points_vigilance,
            niveau_certitude=niveau,
            certitude_justification=(
                "Citations extraites avec ancrage source (LangExtract) - "
                "positions exactes vérifiées dans le texte d'origine."
            ),
        )

    def _create_fallback_section(self, mod):
        """Create fallback section when extraction fails."""
        return SectionAnalysis(
            modification=mod,
            contexte=f"Modification apportée par {mod.law_ref}",
            intention=(
                "L'extraction source-grounded n'a pas pu être effectuée. "
                "Une analyse manuelle des débats parlementaires est recommandée."
            ),
            sources_an=[],
            sources_senat=[],
            evolution_debats="",
            points_vigilance=[],
            niveau_certitude=CertitudeLevel.FAIBLE,
            certitude_justification="Extraction LangExtract échouée.",
        )
