"""Markdown renderer for article analysis results."""

from src.analysis.models import (
    ArticleAnalysisResult,
    CGIModificationSummary,
    ConsolidationPeriod,
    ForwardLookingSection,
    JurisprudenceEntry,
    LawAnalysisResult,
    LawModification,
    ModificationNature,
    QuotedSource,
    SectionAnalysis,
)


class MarkdownRenderer:
    """Render ArticleAnalysisResult as comprehensive markdown document."""

    def render(self, result: ArticleAnalysisResult) -> str:
        """
        Render complete analysis document as markdown.

        Args:
            result: ArticleAnalysisResult to render

        Returns:
            Markdown string
        """
        sections = []

        # Header
        sections.append(self._render_header(result))

        # Historique table
        sections.append(self._render_historique(result.historique))

        # Each modification section with consolidation periods interleaved
        combined_items = result.get_sections_with_consolidations()

        # Build Table of Contents
        toc_lines = ["## Table des matières\n"]
        toc_num = 1
        for item in combined_items:
            if isinstance(item, SectionAnalysis):
                mod = item.modification
                nature_label = mod.nature.value
                if mod.nature == ModificationNature.CREATION and toc_num > 1:
                    nature_label = "Insertion"
                anchor = f"{toc_num}-{nature_label.lower().replace('é', 'e')}-{mod.short_ref.lower().replace(' ', '-')}"
                toc_lines.append(f"{toc_num}. [{nature_label} ({mod.short_ref})](#{anchor})")
                toc_num += 1
            elif isinstance(item, ConsolidationPeriod):
                toc_lines.append(f"   - *{item.title}*")
        toc_lines.append(f"\n[Synthèse](#synthèse) | [Jurisprudence](#jurisprudence) | [Méthodologie](#méthodologie)")
        sections.append("\n".join(toc_lines))

        section_num = 1
        for item in combined_items:
            if isinstance(item, SectionAnalysis):
                sections.append(self._render_section(section_num, item))
                section_num += 1
            elif isinstance(item, ConsolidationPeriod):
                sections.append(self._render_consolidation(item))

        # Synthesis
        sections.append(self._render_synthese(result))

        # Forward-looking (PLF discussions)
        if result.forward_looking:
            sections.append(self._render_forward_looking(result.forward_looking))

        # Jurisprudence
        if result.jurisprudence:
            sections.append(self._render_jurisprudence(result.jurisprudence))

        # Doctrine (BOFIP)
        if result.bofip_url:
            sections.append(self._render_doctrine(result))

        # Methodology
        sections.append(self._render_methodologie(result))

        return "\n\n---\n\n".join(sections)

    def _render_header(self, result: ArticleAnalysisResult) -> str:
        """Render document header."""
        lines = [
            f"# Analyse de l'article {result.article_ref} du {result.article_code}",
            "",
            f"**Objet**: {result.article_titre}",
            "",
            f"**Question posée**: {result.question}",
        ]
        return "\n".join(lines)

    def _render_historique(self, modifications: list[LawModification]) -> str:
        """Render modification history table."""
        lines = [
            "## Historique des modifications",
            "",
            "| Date | Loi | Article | Nature |",
            "|------|-----|---------|--------|",
        ]

        for i, mod in enumerate(modifications):
            # "Création" for non-first modifications means a new paragraph/subsection, not the article itself
            nature_display = mod.nature.value
            if mod.nature == ModificationNature.CREATION and i > 0:
                nature_display = "Insertion"
            nature_bold = f"**{nature_display}**" if nature_display == "Création" else nature_display
            lines.append(
                f"| {mod.date.strftime('%d/%m/%Y')} | {mod.law_ref} | {mod.article_in_law} | {nature_bold} |"
            )

        return "\n".join(lines)

    def _render_section(self, num: int, section: SectionAnalysis) -> str:
        """Render a single law modification section."""
        mod = section.modification
        # "Création" for non-first modifications means a new paragraph/subsection
        nature_label = mod.nature.value
        if mod.nature == ModificationNature.CREATION and num > 1:
            nature_label = "Insertion"
        anchor = f"{num}-{nature_label.lower().replace('é', 'e')}-{mod.short_ref.lower().replace(' ', '-')}"
        lines = [
            f'## <a id="{anchor}"></a>{num}. {nature_label} ({mod.short_ref})',
            "",
            "### Contexte législatif",
            "",
            f"**Loi**: {mod.law_ref}",
            f"**Date de promulgation**: {mod.date.strftime('%d/%m/%Y')}",
            f"**Article dans la loi**: {mod.article_in_law}",
        ]

        if section.contexte:
            lines.extend(["", section.contexte])

        lines.extend([
            "",
            "### Intention du législateur",
            "",
            section.intention,
        ])

        # Sources - now chamber-specific
        if section.has_an_sources:
            lines.extend([
                "",
                "### Sources principales (Assemblée Nationale)",
                "",
            ])
            lines.extend(self._render_sources(section.sources_an))

        if section.has_senat_sources:
            lines.extend([
                "",
                "### Sources principales (Sénat)",
                "",
            ])
            lines.extend(self._render_sources(section.sources_senat))

        # Fallback if no chamber-specific sources but has combined sources
        if not section.has_an_sources and not section.has_senat_sources and section.sources:
            lines.extend([
                "",
                "### Sources principales",
                "",
            ])
            lines.extend(self._render_sources(section.sources))

        # Évolution lors des débats (new section)
        if section.evolution_debats:
            lines.extend([
                "",
                "### Évolution lors des débats",
                "",
                section.evolution_debats,
            ])

        # Points de vigilance
        if section.points_vigilance:
            lines.extend([
                "",
                "### Points de vigilance",
                "",
            ])
            for point in section.points_vigilance:
                lines.append(f"- {point}")

        # Niveau de certitude
        lines.extend([
            "",
            f"### Niveau de certitude: {section.niveau_certitude.value}",
            "",
            section.certitude_justification,
        ])

        # Stats
        if section.debates_analyzed > 0 or section.amendments_analyzed > 0:
            lines.extend([
                "",
                f"*Débats analysés: {section.debates_analyzed} | Amendements analysés: {section.amendments_analyzed}*",
            ])

        return "\n".join(lines)

    def _render_sources(self, sources: list[QuotedSource]) -> list[str]:
        """Render a list of sources as markdown."""
        lines = []
        for source in sources:
            citation_text = f'> "{source.citation}"'
            if source.is_curated:
                citation_text += " *(source verifiee)*"
            lines.append(citation_text)
            source_info = []
            if source.orateur:
                source_info.append(f"**{source.orateur}**")
            if source.fonction:
                source_info.append(f"({source.fonction})")
            if source.date:
                source_info.append(source.date)
            if source.chambre:
                source_info.append(source.chambre)
            if source_info:
                lines.append(f"> — {', '.join(source_info)}")
            lines.append("")
        return lines

    def _render_consolidation(self, consolidation: ConsolidationPeriod) -> str:
        """Render a consolidation period section."""
        lines = [
            f"## {consolidation.title}",
            "",
            "### Contexte",
            "",
            consolidation.doctrine_evolution,
        ]

        # Filter out placeholder amendements
        valid_amendements = [
            a for a in consolidation.amendements_rejetes
            if a and "aucun" not in a.lower() and "non applicable" not in a.lower()
        ]
        if valid_amendements:
            lines.extend([
                "",
                "### Amendements rejetés",
                "",
            ])
            for amendement in valid_amendements:
                lines.append(f"- {amendement}")

        # Filter out placeholder citations (Non applicable, Aucune citation, etc.)
        valid_citations = [
            c for c in consolidation.citations
            if c.citation
            and "non applicable" not in c.citation.lower()
            and "aucune citation" not in c.citation.lower()
            and c.orateur
            and "non applicable" not in c.orateur.lower()
        ]
        if valid_citations:
            lines.extend([
                "",
                "### Citations des débats",
                "",
            ])
            lines.extend(self._render_sources(valid_citations))

        return "\n".join(lines)

    def _render_synthese(self, result: ArticleAnalysisResult) -> str:
        """Render synthesis section with multi-column table separating seuil vs quota."""
        lines = [
            "## Synthèse de l'évolution de l'intention législative",
            "",
        ]

        # Objectif constant
        if result.synthese_objectif:
            lines.extend([
                "### Objectif constant",
                "",
                result.synthese_objectif,
                "",
            ])

        # Evolution table with dynamic columns per article
        if result.synthese_evolution:
            # Collect all parameter keys across rows (preserving insertion order)
            all_keys: list[str] = []
            for row in result.synthese_evolution:
                for key in row.parameters:
                    if key not in all_keys:
                        all_keys.append(key)

            if all_keys:
                # Build dynamic header
                col_headers = [k.replace("_", " ").capitalize() for k in all_keys]
                header = "| Période | " + " | ".join(col_headers) + " |"
                separator = "|---------|" + "|".join("-" * (len(h) + 2) for h in col_headers) + "|"

                lines.extend([
                    "### Évolution des conditions",
                    "",
                    header,
                    separator,
                ])
                for row in result.synthese_evolution:
                    values = [row.parameters.get(k, "N/A") for k in all_keys]
                    lines.append("| " + row.periode + " | " + " | ".join(values) + " |")
                lines.append("")

        # Preoccupations
        if result.synthese_preoccupations:
            lines.extend([
                "### Préoccupations récurrentes du législateur",
                "",
            ])
            for i, preoc in enumerate(result.synthese_preoccupations, 1):
                lines.append(f"{i}. {preoc}")

        return "\n".join(lines)

    def _render_forward_looking(self, forward_looking: list[ForwardLookingSection]) -> str:
        """Render forward-looking PLF discussions section."""
        lines = [
            "## Discussions PLF en cours (projets de modification)",
            "",
        ]

        for section in forward_looking:
            lines.extend([
                f"### PLF {section.plf_year} ({section.status})",
                "",
            ])

            # Propositions
            if section.propositions:
                lines.append("**Propositions de modification** :")
                lines.append("")
                for prop in section.propositions:
                    lines.append(f"- {prop}")
                lines.append("")

            # Citations
            if section.citations:
                lines.append("**Citations des débats** :")
                lines.append("")
                lines.extend(self._render_sources(section.citations))

            # Certitude
            lines.extend([
                f"*Niveau de certitude: {section.niveau_certitude.value}*",
                "",
            ])

        return "\n".join(lines)

    def _render_jurisprudence(self, jurisprudence: list[JurisprudenceEntry]) -> str:
        """Render jurisprudence section with full Faits/Solution/Portée structure."""
        lines = [
            "## Jurisprudence",
            "",
        ]

        # Group by jurisdiction type
        ce_entries = [e for e in jurisprudence if e.juridiction == "CE"]
        cc_entries = [e for e in jurisprudence if e.juridiction == "CC"]
        cadf_entries = [e for e in jurisprudence if e.juridiction == "CADF"]
        other_entries = [e for e in jurisprudence if e.juridiction not in ["CE", "CC", "CADF"]]

        if ce_entries:
            lines.append("### Conseil d'État")
            lines.append("")
            for entry in ce_entries:
                lines.extend(self._render_jurisprudence_entry(entry))

        if cc_entries:
            lines.append("### Conseil constitutionnel")
            lines.append("")
            for entry in cc_entries:
                lines.extend(self._render_jurisprudence_entry(entry))

        if cadf_entries:
            lines.append("### Comité de l'abus de droit fiscal (CADF)")
            lines.append("")
            for entry in cadf_entries:
                lines.extend(self._render_jurisprudence_entry(entry))

        if other_entries:
            lines.append("### Autres décisions")
            lines.append("")
            for entry in other_entries:
                lines.extend(self._render_jurisprudence_entry(entry))

        return "\n".join(lines)

    def _render_jurisprudence_entry(self, entry: JurisprudenceEntry) -> list[str]:
        """Render a single jurisprudence entry with Faits/Solution/Portée structure."""
        # Skip skeleton entries with no substantive content
        if not entry.titre and not entry.solution and not entry.faits:
            return []

        lines = [
            f"#### {entry.juridiction}, {entry.date}, {entry.reference}",
            "",
            f"**{entry.titre}**",
            "",
        ]

        if entry.faits:
            lines.extend([
                "**Faits**:",
                entry.faits,
                "",
            ])

        if entry.solution:
            lines.extend([
                "**Solution**:",
                entry.solution,
                "",
            ])

        if entry.portee:
            lines.extend([
                "**Portée**:",
                entry.portee,
                "",
            ])

        # If no detailed fields, just show solution as summary
        if not entry.faits and not entry.portee and entry.solution:
            lines = [
                f"#### {entry.juridiction}, {entry.date}, {entry.reference}",
                "",
                f"**{entry.titre}**",
                "",
                f"**Solution**: {entry.solution}",
                "",
            ]

        return lines

    def _render_doctrine(self, result: ArticleAnalysisResult) -> str:
        """Render doctrine/BOFIP section."""
        lines = [
            "## Doctrine administrative (BOFIP)",
            "",
            f"**Référence**: [{result.bofip_url}]({result.bofip_url})",
            "",
        ]

        if result.doctrine_bofip:
            for item in result.doctrine_bofip:
                lines.append(f"- {item}")

        return "\n".join(lines)

    def _render_methodologie(self, result: ArticleAnalysisResult) -> str:
        """Render methodology section with detailed archive references."""
        lines = [
            "## Méthodologie",
            "",
            "Cette analyse a été réalisée à partir de :",
            "",
            f"- **Base de données**: {result.total_debates_searched} débats analysés, {result.total_interventions_found} interventions trouvées",
            "- **Recherche hybride**: Combinaison de recherche par mots-clés et recherche sémantique (keyword_boost: 0.5)",
            "- **Filtrage strict**: Seuls les extraits mentionnant explicitement l'article ou ses mots-clés sont retenus",
            "- **Extraction d'intention**: LLM (Groq/Llama ou Anthropic/Claude)",
            "",
            "### Sources législatives",
            "",
            f"- [Légifrance - Article {result.article_ref}]({result.legifrance_url if result.legifrance_url else 'https://www.legifrance.gouv.fr/'})",
        ]

        if result.bofip_url:
            lines.append(f"- [BOFIP]({result.bofip_url})")

        # Add parliamentary rapport references if available
        if result.rapports_parlementaires:
            lines.extend([
                "",
                "### Rapports parlementaires",
                "",
            ])
            for rapport in result.rapports_parlementaires:
                rapport_type = rapport.get("type", "")
                numero = rapport.get("numero", "")
                year = rapport.get("year", "")
                title = rapport.get("title", "")
                url = rapport.get("url", "")
                rapporteur = rapport.get("rapporteur", "")

                if url and numero:
                    entry = f"- [{rapport_type} n° {numero} ({year})]({url})"
                elif url:
                    entry = f"- [{rapport_type} ({year})]({url})"
                elif numero:
                    entry = f"- {rapport_type} n° {numero} ({year})"
                else:
                    entry = f"- {rapport_type} ({year})"

                if title:
                    entry += f" - {title}"
                if rapporteur:
                    entry += f" (Rapporteur: {rapporteur})"

                lines.append(entry)

        # Add DILA sources if available
        if result.dila_sources:
            lines.extend([
                "",
                "### Sources DILA (archives parlementaires)",
                "",
            ])
            for source in result.dila_sources:
                lines.append(f"- {source}")

        # Add parliamentary debate sources if available
        if result.sources_parlementaires_an:
            lines.extend([
                "",
                "### Débats parlementaires (Assemblée Nationale)",
                "",
            ])
            for source in result.sources_parlementaires_an:
                lines.append(f"- {source}")

        if result.sources_parlementaires_senat:
            lines.extend([
                "",
                "### Débats parlementaires (Sénat)",
                "",
            ])
            for source in result.sources_parlementaires_senat:
                lines.append(f"- {source}")

        lines.extend([
            "",
            f"*Analyse générée le {result.generated_date} - Projet \"Intention du Législateur\"*",
        ])

        return "\n".join(lines)


def render_article_analysis(result: ArticleAnalysisResult) -> str:
    """Convenience function to render an analysis result."""
    renderer = MarkdownRenderer()
    return renderer.render(result)


class LawAnalysisRenderer:
    """Render LawAnalysisResult as comprehensive markdown document."""

    def render(self, result: LawAnalysisResult) -> str:
        """
        Render complete law analysis document as markdown.

        Args:
            result: LawAnalysisResult to render

        Returns:
            Markdown string
        """
        sections = []

        # Header
        sections.append(self._render_header(result))

        # Fiscal context
        if result.fiscal_context:
            sections.append(self._render_context(result))

        # Key themes
        if result.key_themes:
            sections.append(self._render_themes(result))

        # CGI modifications table
        sections.append(self._render_modifications_table(result))

        # Detailed modifications
        sections.append(self._render_modifications_detail(result))

        # Quality review section (if items need review)
        if result.needs_review:
            sections.append(self._render_quality_review(result))

        # Methodology
        sections.append(self._render_methodology(result))

        return "\n\n---\n\n".join(sections)

    def _render_header(self, result: LawAnalysisResult) -> str:
        """Render document header."""
        lines = [
            f"# Analyse de la {result.full_ref}",
            "",
            f"**{result.law_title}**",
            "",
            f"**Date de promulgation**: {result.date_promulgation}",
            "",
            f"**Modifications CGI identifiées**: {result.total_cgi_modifications}",
        ]

        if result.dossier_an_url:
            lines.append(f"**Dossier AN**: [{result.dossier_an_url}]({result.dossier_an_url})")
        if result.dossier_senat_url:
            lines.append(f"**Dossier Sénat**: [{result.dossier_senat_url}]({result.dossier_senat_url})")

        return "\n".join(lines)

    def _render_context(self, result: LawAnalysisResult) -> str:
        """Render fiscal context section."""
        return f"## Contexte fiscal\n\n{result.fiscal_context}"

    def _render_themes(self, result: LawAnalysisResult) -> str:
        """Render key themes section."""
        lines = ["## Thèmes principaux", ""]
        for i, theme in enumerate(result.key_themes, 1):
            lines.append(f"{i}. {theme}")
        return "\n".join(lines)

    def _render_modifications_table(self, result: LawAnalysisResult) -> str:
        """Render summary table of CGI modifications."""
        lines = [
            "## Synthèse des modifications CGI",
            "",
            "| Article CGI | Objet | Article de loi | Nature | Certitude |",
            "|-------------|-------|----------------|--------|-----------|",
        ]

        for mod in result.cgi_modifications:
            titre_short = mod.cgi_article_titre[:40] + "..." if len(mod.cgi_article_titre) > 40 else mod.cgi_article_titre
            lines.append(
                f"| {mod.cgi_article_ref} | {titre_short} | {mod.article_in_law} | {mod.nature.value} | {mod.confidence.value} |"
            )

        return "\n".join(lines)

    def _render_modifications_detail(self, result: LawAnalysisResult) -> str:
        """Render detailed analysis of each CGI modification."""
        lines = ["## Détail des modifications", ""]

        for i, mod in enumerate(result.cgi_modifications, 1):
            lines.extend([
                f"### {i}. Article {mod.cgi_article_ref} - {mod.nature.value}",
                "",
                f"**{mod.cgi_article_titre}**",
                "",
                f"**Article dans la loi**: {mod.article_in_law}",
                "",
                "#### Intention du législateur",
                "",
                mod.intention_summary if mod.intention_summary else "*Analyse non disponible*",
                "",
            ])

            if mod.key_changes:
                lines.append("#### Principales modifications")
                lines.append("")
                for change in mod.key_changes:
                    lines.append(f"- {change}")
                lines.append("")

            lines.append(f"*Niveau de certitude: {mod.confidence.value} ({mod.sources_count} sources)*")
            lines.append("")

        return "\n".join(lines)

    def _render_quality_review(self, result: LawAnalysisResult) -> str:
        """Render quality review section for items needing human review."""
        lines = [
            "## ⚠️ Éléments nécessitant une vérification",
            "",
            "Les éléments suivants ont un niveau de certitude faible et nécessitent une vérification humaine:",
            "",
        ]

        low_confidence = result.low_confidence_items
        if low_confidence:
            lines.append("### Modifications à faible certitude")
            lines.append("")
            for mod in low_confidence:
                lines.append(f"- **{mod.cgi_article_ref}** ({mod.article_in_law}): {mod.sources_count} sources trouvées")
                if mod.intention_summary:
                    lines.append(f"  - Analyse: {mod.intention_summary[:100]}...")
                else:
                    lines.append("  - ⚠️ Aucune analyse disponible")
            lines.append("")

        # Add quality score
        score_pct = result.quality_score * 100
        if score_pct < 30:
            quality_label = "❌ Faible"
        elif score_pct < 60:
            quality_label = "⚠️ Moyen"
        else:
            quality_label = "✓ Bon"

        lines.extend([
            "### Score de qualité global",
            "",
            f"**{quality_label}** ({score_pct:.0f}%)",
            "",
            "Ce score est basé sur:",
            "- Le niveau de certitude des analyses",
            "- Le nombre de sources parlementaires trouvées",
            "- La disponibilité des analyses d'intention",
        ])

        return "\n".join(lines)

    def _render_methodology(self, result: LawAnalysisResult) -> str:
        """Render methodology section."""
        lines = [
            "## Méthodologie",
            "",
            "Cette analyse a été réalisée à partir de :",
            "",
            f"- **Articles de loi analysés**: {result.articles_analyzed} / {result.total_articles}",
            f"- **Modifications CGI identifiées**: {result.total_cgi_modifications}",
            "- **Méthode**: Extraction automatique des références CGI + analyse LLM de l'intention législative",
            "",
            "### Sources",
            "",
            f"- [Légifrance - {result.law_type} {result.law_year}](https://www.legifrance.gouv.fr/)",
        ]

        if result.dossier_an_url:
            lines.append(f"- [Dossier législatif AN]({result.dossier_an_url})")
        if result.dossier_senat_url:
            lines.append(f"- [Dossier législatif Sénat]({result.dossier_senat_url})")

        lines.extend([
            "",
            f"*Analyse générée le {result.generated_date} - Projet \"Intention du Législateur\"*",
        ])

        return "\n".join(lines)


def render_law_analysis(result: LawAnalysisResult) -> str:
    """Convenience function to render a law analysis result."""
    renderer = LawAnalysisRenderer()
    return renderer.render(result)
