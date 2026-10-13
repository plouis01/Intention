"""LLM prompts for structured extraction in article analysis."""

import re

# Prompt for extracting legislative intent from debates for a specific law modification
SECTION_EXTRACTION_PROMPT = """Tu es un assistant juridique expert en analyse des travaux parlementaires français.

## Contexte
Article analysé: {article_ref} du {article_code}
Titre: {article_titre}
Loi de modification: {law_ref}
Date de promulgation: {law_date}
Article dans la loi: {article_in_law}
Nature de la modification: {nature}

## Mots-clés de l'article
{article_keywords}

## Débats parlementaires à analyser
{debate_chunks}

## Amendements liés
{amendments}

## Exposé des motifs / Rapports parlementaires
{expose_motifs}

## Instructions
Analyse ces extraits pour produire une section de rapport sur l'intention du législateur pour cette modification spécifique.

### ATTENTION AUX CONFUSIONS FRÉQUENTES:
- Cet article est l'article {article_ref} du {article_code} ({article_titre}).
- NE PAS confondre avec "l'article {article_ref}" d'une loi de finances (numéro d'article dans le texte de loi, ex: "l'article 39 du projet de loi" ≠ "l'article 39 du CGI").
- NE PAS confondre avec des articles voisins du {article_code} ({article_ref} bis, {article_ref} ter, {article_ref} decies, etc.).
- Si un extrait parle EXCLUSIVEMENT d'un article voisin sans impact sur {article_ref} lui-même, IGNORE-LE.
- Si la nature est "Création" et que l'article {article_ref} existait déjà avant cette loi, il s'agit de la création d'un ALINÉA, d'un PARAGRAPHE ou d'une EXCEPTION au sein de l'article, pas de l'article entier.

### RÈGLES CRITIQUES DE VALIDATION:
1. CITE les passages qui mentionnent "{article_ref}" OU qui traitent du sujet de l'article ({article_titre}) dans le contexte de cette loi
2. Si un extrait parle d'un AUTRE sujet (logement, TVA, etc.) sans lien avec le thème de {article_ref}, IGNORE-LE
3. Les extraits qui traitent du fond de l'article (mots-clés ci-dessus) SANS le mentionner par numéro sont ACCEPTABLES — le débat parlementaire discute souvent du sujet sans citer le numéro d'article
4. Vérifie que chaque citation est PERTINENTE pour cet article spécifique
5. Si AUCUN extrait ne concerne le sujet de l'article {article_ref}, retourne:
   - contexte_legislatif: "Modification apportée par {law_ref}"
   - intention: ""
   - sources_an: []
   - sources_senat: []
   - niveau_certitude: "faible"
   - justification_certitude: "Aucun débat pertinent identifié pour cet article spécifique"
   NE JAMAIS inventer d'intention à partir d'extraits qui parlent d'un AUTRE sujet.

### CITATIONS INTERDITES (rejeter automatiquement):
- Phrases procédurales: "L'article est ainsi modifié", "La parole est à...", "Je mets aux voix"
- Descriptions du vote: "L'amendement est adopté/rejeté", "Adopté", "Il en est ainsi décidé"
- Lectures de texte sans explication: "L'article 150-0 B ter du CGI est ainsi modifié:"
- Interventions sans contenu substantiel: remerciements, rappels au règlement, suspensions

### CITATIONS REQUISES (critères obligatoires):
- DOIT contenir une explication du POURQUOI (objectif, justification, raison d'être)
- DOIT mentionner explicitement l'article {article_ref} OU un concept clé directement lié à cet article
- DOIT apporter un éclairage sur l'intention du législateur, pas simplement décrire le dispositif
- Préférer les interventions de: rapporteurs généraux, ministres, auteurs d'amendements

### FORMAT DE L'INTENTION:
Structure l'intention ainsi:
- Commence par: "L'intention du législateur est de [OBJECTIF PRINCIPAL]."
- Puis liste: "Le législateur souhaite :" suivi de 3-5 points précis

### PARAMÈTRES OPÉRATIONNELS:
- Identifie les paramètres quantitatifs SPÉCIFIQUES à CET article discutés dans les débats (seuils, délais, taux, plafonds, quotas, conditions chiffrées)
- Utilise des clés descriptives propres à l'article (ex: "seuil_reinvestissement", "delai_cession", "taux_reduction", "plafond_deduction")
- Si l'article ne comporte aucun paramètre quantitatif discuté, retourne un objet vide {{}}
- Ne PAS inventer de paramètres génériques ou non mentionnés dans les débats

### SOURCES PAR CHAMBRE:
- Sépare les citations de l'Assemblée Nationale (AN) et du Sénat
- Fournis 2-3 citations par chambre si disponibles
- Si une chambre n'a pas de sources pertinentes, laisse le tableau vide

### PONDÉRATION DES AUTEURS D'AMENDEMENTS:
Les amendements sont annotés avec le rôle de leur auteur entre crochets [gouvernement], [rapporteur], [commission], [parlementaire].
- Les amendements du **gouvernement** et du **rapporteur** sont les plus significatifs pour déterminer l'intention du législateur
- Les amendements de **commission** reflètent un consensus institutionnel
- Les amendements **parlementaires** individuels sont informatifs mais moins déterminants

### AMENDEMENTS REJETÉS:
Si des amendements rejetés sont présents dans les extraits, analyse pourquoi ils ont été rejetés et ce que cela révèle sur l'intention du législateur.
Remplis le champ `amendements_rejetes_detailles` pour chaque amendement rejeté notable.

### ATTENTION AU STATUT DES AMENDEMENTS:
Chaque amendement dans les extraits est annoté avec son sort réel: "Sort: adopté", "Sort: rejeté", "Sort: retiré", "Sort: tombé", "Sort: non soutenu".
- Un amendement marqué "*** ADOPTÉ ***" ou "Sort: adopté" ne doit JAMAIS apparaître dans `amendements_rejetes_detailles`
- Un amendement "retiré" ou "tombé" n'est PAS un amendement "rejeté" — utilise le terme exact
- Ne confonds PAS le sort d'un amendement avec la présence de la mesure dans le texte final:
  - Un amendement peut être "rejeté" mais la mesure peut figurer dans la loi finale (reprise par un autre amendement ou en CMP)
  - Un amendement peut être "adopté" dans une chambre mais supprimé en CMP
- Dans `raison_rejet`, utilise le sort exact (rejeté/retiré/tombé/non soutenu), jamais "rejeté" si l'amendement a été adopté

### FILTRAGE DES AMENDEMENTS HORS SUJET:
N'inclure dans `amendements_rejetes_detailles` et `points_vigilance` QUE les amendements qui:
- Modifient directement l'article {article_ref} du {article_code} ou une de ses subdivisions (alinéa, paragraphe, 1°, 1 bis, etc.)
- OU sont explicitement invoqués dans les débats pour justifier ou contester la modification de l'article {article_ref}
EXCLURE les amendements qui portent sur un autre article ou un autre sujet, même s'ils figurent dans la même séance parlementaire.
Exemples d'amendements à EXCLURE: amendements sur l'audiovisuel public, les éoliennes, les formes sociétaires outre-mer, le dispositif Pinel — sauf s'ils modifient directement {article_ref}.

### ÉVOLUTION DES DÉBATS:
Si les extraits montrent une évolution (projet initial → version adoptée), décris-la

### ÉVALUATION DU NIVEAU DE CERTITUDE:
Avant de fixer le niveau, réponds à ces questions dans le JSON:
1. extraits_explicites: Combien d'extraits mentionnent EXPLICITEMENT {article_ref} ? (nombre)
2. source_autoritative: Un rapporteur ou ministre est-il cité ? (true/false)
3. intention_explicite: L'intention est-elle ÉNONCÉE directement ou DÉDUITE ? (true/false)
4. coherence_chambres: AN et Sénat convergent-ils ? (true/false)

Règles:
- "élevé" REQUIERT extraits_explicites >= 2 ET source_autoritative == true
- "moyen" si extraits_explicites >= 1 OU source_autoritative == true
- "faible" sinon

## Exemples de réponse attendue

### Exemple 1: Article 150-0 B ter — LF 2019
Extrait de débat: "[Orateur: M. Giraud (rapporteur général) | AN | Commission | 2018-10-15]
L'article 150-0 B ter encadre le report d'imposition lors de l'apport de titres. L'objectif est de porter le quota de réinvestissement de 50% à 60% et de le concentrer dans des PME opérationnelles."

Réponse attendue:
```json
{{"contexte_legislatif": "La LF 2019 renforce le dispositif de l'apport-cession.", "intention": "L'intention du législateur est de renforcer l'obligation de réinvestissement dans l'économie réelle.\\n\\nLe législateur souhaite :\\n- Porter le quota de réinvestissement de 50% à 60%\\n- Concentrer le réinvestissement dans des PME opérationnelles", "operational_parameters": {{"quota_reinvestissement": "60% (contre 50% précédemment)"}}, "sources_an": [{{"citation": "L'article 150-0 B ter encadre le report d'imposition lors de l'apport de titres. L'objectif est de porter le quota de réinvestissement de 50% à 60%", "orateur": "M. Giraud", "fonction": "rapporteur général", "date": "2018-10-15"}}], "sources_senat": [], "evolution_debats": "", "amendements_rejetes_detailles": [], "points_vigilance": [], "certitude_reasoning": {{"extraits_explicites": 1, "source_autoritative": true, "intention_explicite": true, "coherence_chambres": false}}, "niveau_certitude": "moyen", "justification_certitude": "Un extrait de rapporteur mentionne explicitement l'article mais une seule chambre est représentée."}}
```

### Exemple 2: Article 787 B — LF 2019
Extrait: "[Orateur: Mme de Montchalin (rapporteure) | AN | Séance publique | 2018-12-14]
L'article 787 B relatif au Pacte Dutreil est un outil fondamental pour la transmission des entreprises familiales. Le gouvernement propose d'assouplir les conditions de conservation."

Réponse attendue:
```json
{{"contexte_legislatif": "La LF 2019 assouplit le régime Dutreil de transmission d'entreprises.", "intention": "L'intention du législateur est de faciliter la transmission d'entreprises familiales via le Pacte Dutreil.\\n\\nLe législateur souhaite :\\n- Assouplir les conditions de conservation des titres\\n- Simplifier le dispositif pour le rendre plus attractif", "operational_parameters": {{}}, "sources_an": [{{"citation": "L'article 787 B relatif au Pacte Dutreil est un outil fondamental pour la transmission des entreprises familiales. Le gouvernement propose d'assouplir les conditions de conservation.", "orateur": "Mme de Montchalin", "fonction": "rapporteure", "date": "2018-12-14"}}], "sources_senat": [], "evolution_debats": "", "amendements_rejetes_detailles": [], "points_vigilance": [], "certitude_reasoning": {{"extraits_explicites": 1, "source_autoritative": true, "intention_explicite": true, "coherence_chambres": false}}, "niveau_certitude": "moyen", "justification_certitude": "Source autoritative avec mention explicite mais une seule chambre."}}
```

## Format de réponse (JSON strict)

```json
{{
  "contexte_legislatif": "Description du contexte législatif (1-2 phrases)",
  "intention": "L'intention du législateur est de [OBJECTIF PRINCIPAL].\\n\\nLe législateur souhaite :\\n- [Point 1]\\n- [Point 2]\\n- [Point 3]",
  "operational_parameters": {{
    "paramètre_clé_1": "valeur extraite des débats (ex: seuil, délai, taux, plafond...)",
    "paramètre_clé_2": "valeur extraite des débats"
  }},
  "sources_an": [
    {{
      "citation": "Citation exacte de l'AN entre guillemets, max 250 caractères",
      "orateur": "M./Mme Prénom Nom",
      "fonction": "rapporteur général / ministre / député",
      "date": "YYYY-MM-DD"
    }}
  ],
  "sources_senat": [
    {{
      "citation": "Citation exacte du Sénat entre guillemets, max 250 caractères",
      "orateur": "M./Mme Prénom Nom",
      "fonction": "rapporteur général / ministre / sénateur",
      "date": "YYYY-MM-DD"
    }}
  ],
  "evolution_debats": "Description de l'évolution du texte pendant les débats (projet initial → version adoptée) ou vide si non applicable",
  "amendements_rejetes_detailles": [
    {{
      "numero": "N° de l'amendement",
      "auteur": "Auteur de l'amendement",
      "sort_reel": "rejeté / retiré / tombé / non soutenu (reprendre le sort exact indiqué dans l'en-tête de l'extrait)",
      "objet": "Objet de l'amendement (ce qu'il proposait)",
      "raison_rejet": "Pourquoi il a été rejeté/retiré/tombé",
      "source_rejet": "Qui a argumenté contre (rapporteur, gouvernement...)",
      "implication_intention": "Ce que le rejet révèle sur l'intention du législateur"
    }}
  ],
  "points_vigilance": [
    "Amendement rejeté ou objection notable (si présent dans les extraits)"
  ],
  "certitude_reasoning": {{
    "extraits_explicites": 0,
    "source_autoritative": false,
    "intention_explicite": false,
    "coherence_chambres": false
  }},
  "niveau_certitude": "élevé ou moyen ou faible",
  "justification_certitude": "Explication du niveau de certitude en 1-2 phrases"
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""

# Prompt for synthesizing the evolution of legislative intent across multiple modifications
SYNTHESIS_PROMPT = """Tu es un assistant juridique expert en analyse des travaux parlementaires français.

## Article analysé
{article_ref} du {article_code}: {article_titre}

## Sections analysées
{sections_summary}

## Évolution des paramètres opérationnels
{parameter_timeline}

## Instructions
Synthétise l'évolution de l'intention législative à travers les différentes modifications de cet article.

IMPORTANT:
- Base ta synthèse UNIQUEMENT sur les informations des sections analysées.
- Utilise l'évolution des paramètres opérationnels ci-dessus pour montrer comment les valeurs clés (seuils, délais, taux, plafonds) ont changé au fil du temps.
- Identifie les 3-5 paramètres clés spécifiques à CET article et suis leur évolution.
- Les paramètres doivent être ceux qui sont effectivement discutés dans les débats, pas des paramètres génériques.

## Format de réponse (JSON strict)

```json
{{
  "objectif_constant": "L'objectif principal et constant du législateur (2-3 phrases)",
  "parametres_cles": ["nom_param_1", "nom_param_2", "nom_param_3"],
  "evolution_table": [
    {{
      "periode": "2012-2018",
      "nom_param_1": "valeur du paramètre pour cette période",
      "nom_param_2": "valeur du paramètre pour cette période",
      "nom_param_3": "valeur du paramètre pour cette période"
    }}
  ],
  "preoccupations_recurrentes": [
    "Préoccupation 1 citée dans les débats",
    "Préoccupation 2 citée dans les débats"
  ]
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""


# Prompt for consolidation periods (years between modifications)
CONSOLIDATION_PROMPT = """Tu es un assistant juridique expert en analyse des travaux parlementaires français.

## Contexte
Article analysé: {article_ref} du {article_code}
Période de consolidation: {start_year} à {end_year}

## Débats de la période
{debate_chunks}

## Instructions
Analyse ces extraits pour décrire comment le dispositif a été discuté et consolidé pendant cette période sans modification législative.

IMPORTANT:
- Cite UNIQUEMENT des passages qui mentionnent explicitement "{article_ref}" ou ses mots-clés
- Concentre-toi sur : la doctrine parlementaire, les amendements rejetés, les discussions prospectives

## Format de réponse (JSON strict)

```json
{{
  "titre": "Consolidation du dispositif ({start_year}-{end_year})",
  "doctrine_evolution": "Description de l'évolution de la doctrine et des pratiques pendant cette période",
  "amendements_rejetes": [
    "Description d'amendement rejeté visant à modifier l'article"
  ],
  "citations": [
    {{
      "citation": "Citation pertinente max 250 caractères",
      "orateur": "M./Mme Prénom Nom",
      "fonction": "fonction",
      "date": "YYYY-MM-DD",
      "chambre": "AN ou Sénat"
    }}
  ]
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""

# Prompt for forward-looking analysis (PLF discussions in progress)
FORWARD_LOOKING_PROMPT = """Tu es un assistant juridique expert en analyse des travaux parlementaires français.

## Contexte
Article analysé: {article_ref} du {article_code}
Année PLF: {plf_year}

## Débats parlementaires à analyser
{debate_chunks}

## Instructions
Analyse ces extraits pour identifier les projets de modification en cours de discussion pour l'article {article_ref}.

### RÈGLES CRITIQUES:
1. NE CITE QUE des passages qui mentionnent EXPLICITEMENT "{article_ref}" ou un concept directement lié à cet article
2. Identifie les propositions de modification concrètes (seuils, délais, quotas)
3. Indique clairement le statut du projet (en cours, débattu, adopté, rejeté)

## Format de réponse (JSON strict)

```json
{{
  "plf_year": {plf_year},
  "status": "en cours ou projet ou adopté",
  "propositions": [
    "Description concise de chaque proposition de modification"
  ],
  "citations": [
    {{
      "citation": "Citation exacte max 250 caractères",
      "orateur": "M./Mme Prénom Nom",
      "fonction": "fonction",
      "date": "YYYY-MM-DD",
      "chambre": "AN ou Sénat"
    }}
  ],
  "niveau_certitude": "élevé ou moyen ou faible"
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""

# Prompt for when no relevant debates are found
NO_DEBATES_PROMPT = """Tu es un assistant juridique expert.

## Contexte
Article: {article_ref}
Loi: {law_ref} ({law_date})
Nature: {nature}

Aucun débat parlementaire pertinent n'a été trouvé pour cette modification dans la base de données.

## Instructions
Génère une section minimale indiquant l'absence de sources.

## Format de réponse (JSON strict)

```json
{{
  "contexte_legislatif": "Modification apportée par {law_ref}",
  "intention": "Les débats parlementaires relatifs à cette modification n'ont pas été identifiés dans la base de données. Une recherche complémentaire dans les archives parlementaires serait nécessaire.",
  "sources": [],
  "points_vigilance": [],
  "niveau_certitude": "faible",
  "justification_certitude": "Aucun débat parlementaire trouvé dans la base de données pour cette modification."
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""


# Prompt for enrichment: generate synthesis for an article
SYNTHESE_ENRICHMENT_PROMPT = """Tu es un assistant juridique expert en droit fiscal français.

## Article analysé
Article {ref} du CGI: {titre}

## Historique des modifications
{modification_history}

## Citations parlementaires
{best_quotes}

## Instructions
À partir de l'historique des modifications et des citations parlementaires, génère une synthèse de l'intention du législateur pour cet article.

### RÈGLES:
1. Base ta synthèse UNIQUEMENT sur les informations fournies
2. Si les citations sont insuffisantes, indique-le dans l'objectif
3. Les préoccupations doivent refléter les débats réels, pas des généralités

## Format de réponse (JSON strict)

```json
{{
  "objectif_constant": "L'objectif principal et constant du législateur pour cet article (2-3 phrases précises)",
  "preoccupations_recurrentes": [
    "Préoccupation 1 identifiée dans les débats",
    "Préoccupation 2 identifiée dans les débats"
  ]
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""

# Prompt for enrichment: generate jurisprudence for an article
JURISPRUDENCE_ENRICHMENT_PROMPT = """Tu es un assistant juridique expert en droit fiscal français.

## Article analysé
Article {ref} du CGI: {titre}

## Instructions
Liste les décisions de jurisprudence les plus significatives concernant l'article {ref} du CGI.

### RÈGLES CRITIQUES:
1. N'inclus QUE des décisions RÉELLES et VÉRIFIABLES dont tu es certain
2. Inclus la juridiction exacte, la date exacte, et le numéro de pourvoi/arrêt exact
3. Si tu n'es pas certain d'une décision, NE L'INCLUS PAS
4. Si aucune jurisprudence notable n'existe pour cet article, retourne un tableau vide []
5. Privilégie les décisions du Conseil d'État, du Conseil constitutionnel, et de la Cour de cassation
6. Les décisions des CAA ne doivent être incluses que si elles sont particulièrement significatives

## Format de réponse (JSON strict)

```json
[
  {{
    "juridiction": "CE|CC|Cass. com.|CAA",
    "date": "YYYY-MM-DD",
    "reference": "n° XXXXXX",
    "titre": "Titre descriptif court de la décision",
    "faits": "Résumé des faits en 1-2 phrases",
    "solution": "Solution retenue par la juridiction",
    "portee": "Portée et implications pour l'interprétation de l'article"
  }}
]
```

Réponds UNIQUEMENT avec le JSON (tableau), sans texte avant ou après.
"""


def format_parameter_timeline(timeline: dict[str, list[tuple[str, str]]]) -> str:
    """Format parameter evolution timeline for inclusion in synthesis prompt."""
    if not timeline:
        return "[Aucun paramètre opérationnel extrait des débats]"

    parts = []
    for param_name, entries in timeline.items():
        values = ", ".join(f"{period}: {value}" for period, value in entries)
        parts.append(f"- {param_name}: {values}")
    return "\n".join(parts)


def classify_amendment_author(auteur: str | None) -> str:
    """Classify an amendment author into a role category.

    Returns one of: "gouvernement", "rapporteur", "commission", "parlementaire", "inconnu".
    """
    if not auteur:
        return "inconnu"
    auteur_lower = auteur.lower()
    if (
        "gouvernement" in auteur_lower
        or "ministre" in auteur_lower
        or "secrétaire d'état" in auteur_lower
    ):
        return "gouvernement"
    if "rapporteur" in auteur_lower:
        return "rapporteur"
    if "commission" in auteur_lower:
        return "commission"
    # If there's an actual name (non-empty after stripping), it's a parlementaire
    if auteur.strip():
        return "parlementaire"
    return "inconnu"


def _deduplicate_chunks(chunks: list, threshold: float = 0.6) -> list:
    """Remove near-duplicate chunks using word-set Jaccard similarity.

    Keeps the higher-scored chunk when duplicates are detected.
    O(n²) but n≤30 so negligible.
    """
    if len(chunks) <= 1:
        return chunks

    def _word_set(text: str) -> set[str]:
        return set(re.findall(r'\w+', text.lower()))

    kept = []
    for i, (chunk_i, score_i) in enumerate(chunks):
        is_dup = False
        words_i = _word_set(chunk_i.content)
        for j, (chunk_j, score_j) in enumerate(kept):
            words_j = _word_set(chunk_j.content)
            union = words_i | words_j
            if not union:
                continue
            jaccard = len(words_i & words_j) / len(union)
            if jaccard >= threshold:
                # Keep the higher-scored one (already in kept)
                is_dup = True
                break
        if not is_dup:
            kept.append((chunk_i, score_i))
    return kept


def _shorten_role(fonction: str) -> str:
    """Shorten long role descriptions for prompt headers."""
    if not fonction:
        return ""
    f = fonction.lower()
    if "rapporteur" in f and ("général" in f or "genera" in f):
        return "rapporteur général"
    if "rapporteur" in f:
        return "rapporteur"
    if "ministre" in f:
        return "ministre"
    if "secrétaire d" in f:
        return "secrétaire d'État"
    if "président" in f and "commission" in f:
        return "président de commission"
    # Truncate if too long
    if len(fonction) > 40:
        return fonction[:40] + "…"
    return fonction


def format_debate_chunks_for_prompt(chunks: list, max_chars: int = 15000) -> str:
    """Format debate chunks for inclusion in prompt.

    Improvements:
    - Deduplicates near-identical chunks (Jaccard > 0.6)
    - Sorts by score descending (defensive)
    - Groups by etape (commission first, then seance_publique)
    - Adds etape and score to metadata headers
    - Shortens verbose role descriptions
    """
    if not chunks:
        return "[Aucun débat parlementaire trouvé]"

    # Deduplicate and sort by score descending
    chunks = _deduplicate_chunks(chunks)
    chunks = sorted(chunks, key=lambda x: x[1], reverse=True)

    # Group by etape
    commission_chunks = []
    seance_chunks = []
    other_chunks = []
    for chunk, score in chunks:
        etape = (chunk.metadata.get("etape") or "").lower()
        if "commission" in etape:
            commission_chunks.append((chunk, score))
        elif "seance" in etape or "séance" in etape:
            seance_chunks.append((chunk, score))
        else:
            other_chunks.append((chunk, score))

    sections = []
    current_chars = 0

    def _format_chunk(chunk, score):
        metadata = chunk.metadata
        header_parts = []
        if metadata.get("orateur"):
            role = _shorten_role(metadata.get("fonction", ""))
            if role:
                header_parts.append(f"Orateur: {metadata['orateur']} ({role})")
            else:
                header_parts.append(f"Orateur: {metadata['orateur']}")
        if metadata.get("chambre"):
            header_parts.append(metadata["chambre"])
        etape = metadata.get("etape", "")
        if etape:
            header_parts.append(etape.replace("_", " ").title())
        if metadata.get("date"):
            header_parts.append(f"Date: {metadata['date']}")
        header_parts.append(f"Score: {score:.2f}")
        header = " | ".join(header_parts)
        return f"[{header}]\n{chunk.content}"

    for label, group in [
        ("## Travaux en commission", commission_chunks),
        ("## Séance publique", seance_chunks),
        ("## Autres débats", other_chunks),
    ]:
        if not group:
            continue
        group_parts = []
        for chunk, score in group:
            chunk_text = _format_chunk(chunk, score)
            if current_chars + len(chunk_text) + len(label) > max_chars:
                break
            group_parts.append(chunk_text)
            current_chars += len(chunk_text) + 10
        if group_parts:
            sections.append(label + "\n\n" + "\n\n---\n\n".join(group_parts))

    return "\n\n".join(sections) if sections else "[Aucun débat parlementaire trouvé]"


def format_amendment_chunks_for_prompt(
    chunks: list,
    max_chars: int = 5000,
    debate_chunks: list | None = None,
) -> str:
    """Format amendment chunks for inclusion in prompt.

    Improvements:
    - Deduplicates near-identical chunks
    - Sorts: adopted first, rejected second, others last
    - Marks adopted amendments with *** ADOPTÉ ***
    - Allocates 70% of max_chars to adopted, 30% to rejected
    - Adds linked debate discussion when available
    - Adds score and role to headers
    """
    if not chunks:
        return "[Aucun amendement trouvé]"

    # Deduplicate
    chunks = _deduplicate_chunks(chunks)

    # Sort by adoption status: adopted first, rejected second, others last
    sort_order = {"adopte": 0, "adopté": 0, "rejete": 1, "rejeté": 1}

    def _sort_key(item):
        chunk, score = item
        sort_val = (chunk.metadata.get("sort") or "").lower()
        return (sort_order.get(sort_val, 2), -score)

    chunks = sorted(chunks, key=_sort_key)

    # Split into adopted and other
    adopted = []
    other = []
    for chunk, score in chunks:
        sort_val = (chunk.metadata.get("sort") or "").lower()
        if sort_val in ("adopte", "adopté"):
            adopted.append((chunk, score))
        else:
            other.append((chunk, score))

    adopted_budget = int(max_chars * 0.7)
    other_budget = max_chars - adopted_budget

    # Build debate lookup for amendment-debate linking
    debate_lookup: dict[str, str] = {}
    if debate_chunks:
        for dchunk, dscore in debate_chunks:
            debate_lookup[dchunk.content[:100]] = dchunk.content

    parts = []
    current_chars = 0

    def _format_amendment(chunk, score, budget_remaining):
        metadata = chunk.metadata
        header_parts = []

        sort_val = (metadata.get("sort") or "").lower()
        is_adopted = sort_val in ("adopte", "adopté")

        if is_adopted:
            header_parts.append("*** ADOPTÉ ***")

        if metadata.get("numero"):
            num = metadata["numero"].lstrip("N°").strip()
            header_parts.append(f"Amendement {num}")
        if metadata.get("auteur"):
            role = classify_amendment_author(metadata["auteur"])
            header_parts.append(f"Auteur: {metadata['auteur']} [{role}]")
        if metadata.get("sort"):
            header_parts.append(f"Sort: {metadata['sort']}")
        header_parts.append(f"Score: {score:.2f}")

        header = " | ".join(header_parts)
        text = f"[{header}]\n{chunk.content}"

        # Look for linked debate discussion about this amendment
        if debate_chunks and metadata.get("numero"):
            amend_num = metadata["numero"]
            for dchunk, dscore in debate_chunks:
                dcontent = dchunk.content.lower()
                # Check for amendment number patterns
                if (
                    f"amendement n°{amend_num.lower()}" in dcontent
                    or f"n° {amend_num.lower()}" in dcontent
                    or f"amendement {amend_num.lower()}" in dcontent
                ):
                    discussion = dchunk.content[:500]
                    text += f"\n> Discussion en séance: {discussion}"
                    break

        return text

    # Format adopted amendments
    for chunk, score in adopted:
        text = _format_amendment(chunk, score, adopted_budget - current_chars)
        if current_chars + len(text) > adopted_budget:
            break
        parts.append(text)
        current_chars += len(text) + 10

    # Format other amendments
    other_start = current_chars
    for chunk, score in other:
        text = _format_amendment(chunk, score, other_budget - (current_chars - other_start))
        if current_chars + len(text) > max_chars:
            break
        parts.append(text)
        current_chars += len(text) + 10

    return "\n\n---\n\n".join(parts)
