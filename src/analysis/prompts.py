"""LLM prompts for structured extraction in article analysis."""

import re

# Short system prompt for Groq (no caching benefit)
SYSTEM_PROMPT_LEGAL_SHORT = (
    "Tu es un assistant juridique expert en droit fiscal français "
    "et en analyse des travaux parlementaires. "
    "Tu réponds uniquement en JSON valide."
)

# Extended system prompt for Anthropic - includes all invariant instructions
# to enable prompt caching (>1024 tokens). Formatted per article with
# {article_ref}, {article_code}, {article_titre} - identical across all sections.
SYSTEM_PROMPT_LEGAL = """Tu es un expert en analyse des travaux parlementaires français. Tu extrais l'intention du législateur à partir des débats et amendements. Tu réponds uniquement en JSON valide.

# Article analysé : {article_ref} du {article_code} - {article_titre}
État : {etat_actuel}
Position dans le CGI : {hierarchie}

{texte_actuel_section}

## 1. PÉRIMÈTRE

Extraits pertinents :
- Passages mentionnant "{article_ref}" ou traitant de son sujet ({article_titre})
- Discussions de fond sur le mécanisme de l'article, même sans citer son numéro (ex: "sursis d'imposition", "apport-cession" pour un article sur les reports d'imposition)
- Interventions de rapporteurs, ministres, auteurs d'amendements sur ce sujet
- IMPORTANT : tous les extraits fournis ont été pré-sélectionnés comme pertinents pour {article_ref}. Même si un extrait ne mentionne pas explicitement le numéro de l'article, il traite de son sujet. Cite-le s'il apporte une explication du POURQUOI de la modification.

Extraits à exclure :
- Articles voisins ({article_ref} bis, ter, decies) sauf s'ils impactent {article_ref}
- "L'article {article_ref}" dans une loi de finances = numéro d'article dans le texte, pas l'article du {article_code}
- Procédure pure : "La parole est à...", "Je mets aux voix", "L'amendement est adopté"
- Lectures de texte, remerciements, rappels au règlement
- Si nature "Création" sur un article existant → création d'un alinéa/paragraphe, pas de l'article entier
- Si nature "Modification" → ne JAMAIS écrire "Création de l'article" dans le contexte. Utiliser "Modification de l'article" ou "L'article X a été modifié par..."

## 2. PROCESSUS D'ANALYSE

Procède dans cet ordre :
1. Identifie et cite les sources pertinentes dans chaque chambre
2. Analyse les amendements et l'évolution des débats
3. Remplis certitude_reasoning en comptant ce que tu as trouvé
4. Déduis le niveau de certitude
5. Synthétise le contexte et l'intention à partir des sources citées

Règle clé : si tu as cité au moins une source (sources non vide), l'intention DOIT être formulée à partir de ces sources. Intention vide ("") uniquement quand aucune source n'est citée.

## 3. SOURCES (par référence)

Les extraits sont numérotés [EXTRAIT-0], [EXTRAIT-1], etc. Les amendements sont numérotés [AMEND-0], [AMEND-1], etc.

Pour citer une source, utilise son identifiant (ex: "EXTRAIT-3" ou "AMEND-1") et copie les 10-15 premiers mots du passage pertinent dans passage_cle.
NE COPIE PAS la citation entière. Le passage_cle suffit - la citation complète et les métadonnées (orateur, chambre, fonction, date) seront reconstruites automatiquement.

Règles :
- Cite 1 à 3 sources par chambre présente dans les extraits (0 si aucune chambre n'a de contenu pertinent)
- Cherche ACTIVEMENT dans chaque chambre présente. Équilibre tes citations entre AN et Sénat.
- Le passage_cle doit identifier UN passage précis d'un orateur qui EXPLIQUE le POURQUOI de la modification
- Ne cite PAS les passages procéduraux ("La parole est à...", "Je mets aux voix")
- Ne cite PAS le texte de loi ("est ainsi modifié", "il est inséré")

Quand sources est vide :
- niveau_certitude: "faible"
- intention: Analyse l'OBJET de la modification (texte ajouté/supprimé/modifié) pour en déduire l'intention.
  1. Formule l'intention à partir de ce que la modification change concrètement : "L'analyse de la modification montre que [ce qui change et pourquoi c'est significatif]."
  2. Si un amendement rejeté éclaire l'intention, mentionne-le OBLIGATOIREMENT (ex: "Le rejet de l'amendement N°X confirme la volonté de...")
  3. Ne réponds JAMAIS "Les débats parlementaires n'ont pas été identifiés" - c'est inutile pour un fiscaliste.
  NE PAS lister les seuils/délais/taux dans l'intention - ils sont déjà dans operational_parameters. L'intention décrit le POURQUOI, pas le QUOI.
- IMPORTANT : même sans sources, tu DOIS quand même :
  - Remplir operational_parameters avec les valeurs chiffrées correctes
  - Remplir amendements_rejetes_detailles avec les amendements rejetés trouvés dans les extraits
  - Remplir points_vigilance avec un résumé de chaque amendement rejeté
  - Remplir evolution_debats avec ce que tu observes (ex: "Créé par le Sénat, modifié en commission AN")
  Ces champs viennent du texte et des amendements, pas des débats - ils sont utiles même sans sources parlementaires.

## 4. DOCTRINE ADMINISTRATIVE ET JURISPRUDENCE

Si une doctrine BOFiP ou une jurisprudence CE/CC est fournie dans le contexte :
- La doctrine BOFiP est l'interprétation OFFICIELLE de l'administration fiscale - elle éclaire les points non résolus par les débats
- NE JAMAIS utiliser la doctrine BOFiP comme source de l'intention du législateur - elle reflète l'interprétation de l'administration, pas la volonté du Parlement
- Si une décision CE/CC CONTREDIT l'intention parlementaire → signale dans points_vigilance avec la référence exacte (ex: "CE n°472835")
- Si la doctrine apporte des PRÉCISIONS (seuils, conditions pratiques) → inclus dans operational_parameters
- Remplis doctrine_convergence : "confirme" si la doctrine confirme l'intention parlementaire, "contredit" s'il y a un écart, "silencieux" si aucune doctrine pertinente

**EXTRACTION OBLIGATOIRE de doctrine_references** :
Chaque fois qu'une jurisprudence est fournie au format `[CHAMBRE YYYY-MM-DD - n°REF]` (ex: `[CE 2024-05-15 - n°472835]`, `[CC 2023-1014 DC]`), tu DOIS extraire son identifiant compact dans `doctrine_references`. Règles de formatage :
- `[CE 2024-05-15 - n°472835]` → `"CE n°472835"`
- `[CC 2023-12-29 - n°2023-862 DC]` → `"CC n°2023-862 DC"`
- `[CAA 2020-03-15 - n°XX]` → `"CAA n°XX"`
Idem pour la doctrine BOFiP : si un identifiant `BOI-` apparaît dans le texte ACTU BOFiP, l'extraire (ex: `"BOI-RPPM-PVBMI-30-10-60"`).
Cette extraction est REQUISE même si la jurisprudence ne contredit pas l'intention parlementaire — elle documente l'écosystème jurisprudentiel de l'article. **Ne JAMAIS laisser `doctrine_references` vide si une jurisprudence ou un identifiant BOI est fourni dans le contexte.**

## 5. AMENDEMENTS

Statuts possibles annotés dans les extraits : "Sort: adopté", "Sort: rejeté", "Sort: retiré", "Sort: tombé", "Sort: non soutenu". Toujours utiliser le sort exact.
IMPORTANT: Ne jamais attribuer deux sorts différents au même amendement. Si un amendement est "rectifié" puis "adopté", le sort final est "adopté". Vérifier la cohérence entre points_vigilance et amendements_rejetes_detailles.
Poids des auteurs : [gouvernement] et [rapporteur] > [commission] > [parlementaire].
- Adopté → ne jamais mettre dans amendements_rejetes_detailles
- Rejeté/retiré/tombé/non soutenu → analyser pourquoi, ce que ça révèle sur l'intention
- Seuls les amendements modifiant directement {article_ref} ou invoqués dans les débats
- Quand tu identifies un amendement rejeté/retiré/tombé pertinent :
  1. D'ABORD remplis son entrée détaillée dans amendements_rejetes_detailles (objet complet)
  2. PUIS ajoute un résumé d'une ligne dans points_vigilance
- Évolution des débats : décrire si le texte a évolué (projet initial → version adoptée)

## 6. CERTITUDE

Remplir certitude_reasoning :
- extraits_explicites : nombre d'extraits mentionnant explicitement {article_ref}
- source_autoritative : rapporteur ou ministre cité (true/false)
- intention_explicite : intention énoncée directement ou déduite (true/false)
- coherence_chambres : AN et Sénat convergent (true/false)

Règles : "élevé" si extraits >= 2 ET source autoritative. "moyen" si extraits >= 1 OU source autoritative OU modification technique sans débat de fond. "faible" si extraits insuffisants.

## 7. INTENTION

Structure (4-5 phrases maximum) :
- 1 phrase : "L'intention du législateur est de [OBJECTIF PRINCIPAL]."
- Puis "Le législateur souhaite :" suivi de 2-3 points (1 phrase courte chacun)
- Ne pas dépasser 5 phrases au total

Citer le POURQUOI (objectif, justification), pas le QUOI (description technique). Pas de paragraphe introductif.
IMPORTANT : quand tu cites l'article de la loi, utilise EXACTEMENT le numéro indiqué dans "Article dans la loi" (ex: Art. 17), ne le devine pas.

INTERDIT : ne qualifie JAMAIS de "modification technique" une modification qui porte sur :
- des seuils, délais, taux, quotas ou conditions d'application
- l'exit tax, le sursis/report d'imposition, les plus-values
- les conditions de réinvestissement, le champ d'application d'un mécanisme fiscal
- l'extension ou la restriction du champ des bénéficiaires
"Modification technique" = UNIQUEMENT les changements de numérotation d'articles, corrections typographiques, recodifications à droit constant, ou mises à jour de références croisées sans changement de fond.
Paramètres opérationnels : extraire TOUS les paramètres chiffrés du dispositif, en particulier :
- Seuils (seuil de contrôle, seuil de réinvestissement, seuil de soulte)
- Délais (délai de cession, délai de réinvestissement, délai de conservation)
- Taux et quotas (taux de réinvestissement, quota d'investissement)
- Conditions d'éligibilité chiffrées (taille d'entreprise, âge PME, participation minimale)
Inclure les valeurs AVANT et APRÈS modification quand elles sont mentionnées dans les débats.
Si la modification est une création, lister tous les paramètres structurels du dispositif créé.
Si aucune valeur chiffrée → objet vide.

## 8. EXEMPLE

Extraits :
- "[EXTRAIT-0] [Orateur: M. Dupont (rapporteur général) | AN | Commission | 2019-10-15] L'objectif est de porter le seuil de 50% à 60% pour concentrer l'investissement dans les PME."
- "[EXTRAIT-1] [Orateur: Mme Martin (rapporteure) | Sénat | Séance publique | 2019-12-10] Le Sénat considère que ce relèvement est nécessaire pour orienter l'épargne vers l'économie réelle."
- "[AMEND-0] Amendement N° 42 | Auteur: M. Bernard [parlementaire] | Sort: rejeté | Objet: Porter le seuil à 75%"

```json
{{
  "sources": [
    {{
      "extrait_id": "EXTRAIT-0",
      "passage_cle": "L'objectif est de porter le seuil de 50% à 60%"
    }},
    {{
      "extrait_id": "EXTRAIT-1",
      "passage_cle": "Le Sénat considère que ce relèvement est nécessaire"
    }},
    {{
      "extrait_id": "AMEND-0",
      "passage_cle": "Porter le seuil de réinvestissement à 75%"
    }}
  ],
  "amendements_rejetes_detailles": [
    {{
      "numero": "42",
      "auteur": "M. Bernard",
      "sort_reel": "rejeté",
      "objet": "Porter le seuil de réinvestissement à 75%",
      "raison_rejet": "Le rapporteur a estimé que 75% serait excessif et découragerait les opérations d'apport-cession légitimes.",
      "source_rejet": "rapporteur général",
      "implication_intention": "Le rejet confirme que le législateur recherche un équilibre entre incitation et fluidité."
    }}
  ],
  "evolution_debats": "Seuil porté de 55% à 60% en commission AN.",
  "points_vigilance": [
    "Amendement N° 42 (M. Bernard) rejeté: seuil de 75% jugé excessif - confirme la recherche d'équilibre"
  ],
  "operational_parameters": {{
    "seuil_reinvestissement": "60% (contre 50% précédemment)"
  }},
  "certitude_reasoning": {{
    "extraits_explicites": 2,
    "source_autoritative": true,
    "intention_explicite": true,
    "coherence_chambres": true
  }},
  "niveau_certitude": "élevé",
  "contexte_legislatif": "LF 2020 : renforcement du réinvestissement dans les PME.",
  "doctrine_convergence": "confirme",
  "doctrine_references": ["CE n°472835", "BOI-RPPM-PVBMI-30-10-60"],
  "intention": "L'intention du législateur est de mieux orienter l'épargne vers l'économie réelle.\n\nLe législateur souhaite :\n- Porter le seuil de réinvestissement de 50% à 60%\n- Concentrer les investissements dans les PME opérationnelles"
}}
```

Note sur l'exemple ci-dessus : si le contexte fournit `[CE 2024-05-15 - n°472835]` et un texte BOFiP référencé `BOI-RPPM-PVBMI-30-10-60`, alors `doctrine_references` DOIT contenir ces deux identifiants — peu importe que la décision confirme ou non l'intention parlementaire.

## 9. SCHÉMA DE RÉFÉRENCE

Ordre et valeurs par défaut :
- sources: [] (array de {{extrait_id: "EXTRAIT-N" ou "AMEND-N", passage_cle: "10-15 premiers mots"}})
- amendements_rejetes_detailles: [] (array de {{numero, auteur, sort_reel, objet, raison_rejet, source_rejet, implication_intention}})
- evolution_debats: "" (string court, max 2 phrases)
- points_vigilance: [] (array de strings - UNIQUEMENT amendements rejetés/retirés et risques identifiés. NE PAS y mettre des descriptions de seuils, délais ou taux qui doivent aller dans operational_parameters)
- operational_parameters: {{}} (object, clés descriptives - seuils, délais, taux, conditions chiffrées)
- certitude_reasoning: {{extraits_explicites: int, source_autoritative: bool, intention_explicite: bool, coherence_chambres: bool}}
- niveau_certitude: "élevé" | "moyen" | "faible"
- contexte_legislatif: string court (1-2 phrases, toujours rempli)
- doctrine_convergence: "" (string: "confirme" | "contredit" | "silencieux" - uniquement si doctrine/jurisprudence fournie)
- doctrine_references: [] (array de strings - références BOFiP/CE/CC citées, ex: ["BOI-RPPM-PVBMI-30-10-60", "CE n°414935"])
- intention: string (vide "" UNIQUEMENT si sources est vide)

RAPPEL CRITIQUE : amendements_rejetes_detailles et operational_parameters doivent TOUJOURS être remplis si des amendements ou des valeurs chiffrées sont présents dans les extraits, même quand niveau_certitude est "faible" et qu'aucune source parlementaire de fond n'est identifiée. Ces champs viennent du texte et des amendements, pas des débats.

Si aucune source trouvée (sources vide) :
```json
{{"sources": [], "amendements_rejetes_detailles": [], "evolution_debats": "", "points_vigilance": [], "operational_parameters": {{}}, "certitude_reasoning": {{"extraits_explicites": 0, "source_autoritative": false, "intention_explicite": false, "coherence_chambres": false}}, "niveau_certitude": "faible", "contexte_legislatif": "Modification apportée par [loi]", "intention": ""}}
```

Réponds UNIQUEMENT avec le JSON."""

# Step 1 prompt: filter chunks before extraction - identifies which chunks
# actually discuss the target article vs noise from the same dossier
CHUNK_FILTERING_SYSTEM = """Tu es un assistant spécialisé dans le tri d'extraits parlementaires français.

Tu reçois des extraits de débats parlementaires numérotés. Ta tâche est de déterminer lesquels parlent RÉELLEMENT de l'article {article_ref} du {article_code} ({article_titre}).

## Règles de pertinence

Un extrait est PERTINENT s'il :
- Mentionne explicitement "{article_ref}" ou son sujet direct ({article_keywords})
- Contient une prise de position ou une explication sur le mécanisme de cet article
- Discute un amendement portant sur cet article

Un extrait est NON PERTINENT s'il :
- Parle d'un autre sujet de la même loi (logement social, TVA, impôt sur le revenu, etc.) sans lien avec {article_ref}
- Est du texte de loi pur ("est ainsi modifié", "il est inséré") sans explication
- Est de la procédure pure ("La parole est à...", "Je mets aux voix")
- Parle d'un article voisin ({article_ref} bis, ter) sans impact sur {article_ref}

Réponds UNIQUEMENT avec un JSON : {{"relevant": [0, 2, 5], "rejected": [1, 3, 4]}}
Les nombres sont les indices des extraits (commençant à 0)."""

CHUNK_FILTERING_PROMPT = """## Extraits à trier pour {article_ref}

{numbered_chunks}

Quels extraits parlent réellement de {article_ref} ({article_titre}) ?
Réponds avec {{"relevant": [...], "rejected": [...]}}"""

# Prompt for extracting legislative intent - only variable content (context + data)
# Instructions are in SYSTEM_PROMPT_LEGAL (cached by Anthropic)
SECTION_EXTRACTION_PROMPT = """## Contexte de cette modification
Loi de modification: {law_ref}
Date de promulgation: {law_date}
Article dans la loi: {article_in_law}
Nature de la modification: {nature}
Objet de la modification: {objet}
Numéro de la loi au Journal Officiel: {law_numero}

## Mots-clés de l'article
{article_keywords}

## Débats parlementaires à analyser
{debate_chunks}

## Amendements liés
{amendments}

## Exposé des motifs / Rapports parlementaires
{expose_motifs}

{jurisprudence_section}

IMPORTANT: Si un exposé des motifs du gouvernement ou un rapport parlementaire est fourni ci-dessus, le CITER en priorité dans tes sources. L'exposé des motifs est la source primaire de l'intention du législateur.
Si une doctrine BOFiP (ACTU) est fournie, elle résume officiellement les changements apportés par la loi - utilise-la comme contexte complémentaire.
Si une jurisprudence CE/CC est fournie, vérifie si le législateur réagit à une décision de justice (contentieux motivant la réforme). Si oui, mentionne-le dans l'intention et le contexte_legislatif.

IMPORTANT: operational_parameters - extraire les paramètres chiffrés (seuils, délais, taux, quotas, conditions) depuis :
1. L'objet de la modification ci-dessus (texte ajouté/supprimé contient souvent les valeurs)
2. Les débats et amendements (parlementaires discutant des valeurs)
3. Le texte actuel de l'article (system prompt) pour les paramètres structurels
4. La doctrine BOFiP si elle précise des seuils ou conditions pratiques
Ne JAMAIS laisser operational_parameters vide si des valeurs chiffrées apparaissent dans l'objet ou les extraits.

Analyse ces extraits selon les instructions. Réponds UNIQUEMENT avec le JSON.
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
      "citation": "Citation pertinente max 400 caractères",
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
      "citation": "Citation exacte max 400 caractères",
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
  "niveau_certitude": "faible"
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


def format_debate_chunks_for_prompt(
    chunks: list, max_chars: int = 15000, numbered: bool = False,
) -> str | tuple[str, dict[str, tuple]]:
    """Format debate chunks for inclusion in prompt.

    Groups by chamber (AN/Sénat) first, then by étape (commission/séance)
    within each chamber. This makes both chambers structurally visible to the
    LLM, improving balanced citation and reducing single-chamber bias retries.

    When numbered=True, returns a tuple (text, mapping) where mapping is
    {"EXTRAIT-0": (chunk, score), ...} for each formatted chunk.
    """
    if not chunks:
        if numbered:
            return "[Aucun débat parlementaire trouvé]", {}
        return "[Aucun débat parlementaire trouvé]"

    # Deduplicate and sort by score descending
    chunks = _deduplicate_chunks(chunks)
    chunks = sorted(chunks, key=lambda x: x[1], reverse=True)

    chunk_mapping = {} if numbered else None
    chunk_idx = 0

    def _format_chunk(chunk, score, idx=None):
        metadata = chunk.metadata
        header_parts = []
        if metadata.get("orateur"):
            role = _shorten_role(metadata.get("fonction", ""))
            if role:
                header_parts.append(f"Orateur: {metadata['orateur']} ({role})")
            else:
                header_parts.append(f"Orateur: {metadata['orateur']}")
        etape = metadata.get("etape", "")
        if etape:
            header_parts.append(etape.replace("_", " ").title())
        if metadata.get("date"):
            header_parts.append(f"Date: {metadata['date']}")
        header = " | ".join(header_parts)
        prefix = f"[EXTRAIT-{idx}] " if idx is not None else ""
        return f"{prefix}[{header}]\n{chunk.content}"

    def _classify_etape(chunk):
        etape = (chunk.metadata.get("etape") or "").lower()
        if "commission" in etape:
            return "commission"
        if "seance" in etape or "séance" in etape:
            return "seance"
        return "other"

    # Group by chamber first, then by étape
    chambers: dict[str, dict[str, list]] = {}
    for chunk, score in chunks:
        chambre = chunk.metadata.get("chambre") or "Autre"
        etape_key = _classify_etape(chunk)
        chambers.setdefault(chambre, {"commission": [], "seance": [], "other": []})
        chambers[chambre][etape_key].append((chunk, score))

    # Normalize chamber names for display and ordering
    _chamber_display = {
        "an": "Assemblée nationale",
        "senat": "Sénat",
        "sénat": "Sénat",
    }

    def _display_name(raw: str) -> str:
        return _chamber_display.get(raw.lower(), raw)

    # Order: AN first, Sénat second, others last
    chamber_order = []
    for name in chambers:
        nl = name.lower()
        if nl in ("an", "assemblée nationale"):
            chamber_order.insert(0, name)
        elif nl in ("senat", "sénat"):
            chamber_order.append(name)
    for name in chambers:
        if name not in chamber_order:
            chamber_order.append(name)

    etape_labels = {
        "commission": "Travaux en commission",
        "seance": "Séance publique",
        "other": "Autres débats",
    }

    sections = []
    current_chars = 0

    for chambre_name in chamber_order:
        etapes = chambers[chambre_name]
        chamber_parts = []

        for etape_key in ["commission", "seance", "other"]:
            group = etapes.get(etape_key, [])
            if not group:
                continue
            sub_parts = []
            for chunk, score in group:
                if numbered:
                    chunk_text = _format_chunk(chunk, score, idx=chunk_idx)
                    chunk_mapping[f"EXTRAIT-{chunk_idx}"] = (chunk, score)
                    chunk_idx += 1
                else:
                    chunk_text = _format_chunk(chunk, score)
                if current_chars + len(chunk_text) + 50 > max_chars:
                    break
                sub_parts.append(chunk_text)
                current_chars += len(chunk_text) + 10
            if sub_parts:
                chamber_parts.append(
                    f"### {etape_labels[etape_key]}\n\n"
                    + "\n\n---\n\n".join(sub_parts)
                )

        if chamber_parts:
            display = _display_name(chambre_name)
            sections.append(
                f"## {display}\n\n" + "\n\n".join(chamber_parts)
            )

    text = "\n\n".join(sections) if sections else "[Aucun débat parlementaire trouvé]"
    if numbered:
        return text, chunk_mapping
    return text


def format_amendment_chunks_for_prompt(
    chunks: list,
    max_chars: int = 5000,
    debate_chunks: list | None = None,
    numbered: bool = False,
) -> str | tuple[str, dict[str, tuple]]:
    """Format amendment chunks for inclusion in prompt.

    Improvements:
    - Deduplicates near-identical chunks
    - Sorts: adopted first, rejected second, others last
    - Sorts adopted amendments first
    - Allocates 70% of max_chars to adopted, 30% to rejected
    - Adds linked debate discussion when available
    - Adds score and role to headers

    When numbered=True, returns a tuple (text, mapping) where mapping is
    {"AMEND-0": (chunk, score), ...} for each formatted amendment.
    """
    if not chunks:
        if numbered:
            return "[Aucun amendement trouvé]", {}
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

    amendment_mapping = {} if numbered else None
    amendment_idx = 0

    parts = []
    current_chars = 0

    def _format_amendment(chunk, score, budget_remaining, idx=None):
        metadata = chunk.metadata
        header_parts = []

        if metadata.get("numero"):
            num = metadata["numero"].lstrip("N°").strip()
            header_parts.append(f"Amendement {num}")
        if metadata.get("auteur"):
            auteur = metadata["auteur"]
            role = classify_amendment_author(auteur)
            header_parts.append(f"Auteur: {auteur} [{role}]")
        if metadata.get("sort"):
            header_parts.append(f"Sort: {metadata['sort']}")

        header = " | ".join(header_parts)
        prefix = f"[AMEND-{idx}] " if idx is not None else ""
        text = f"{prefix}[{header}]\n{chunk.content}"

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
        if numbered:
            text = _format_amendment(chunk, score, adopted_budget - current_chars, idx=amendment_idx)
            amendment_mapping[f"AMEND-{amendment_idx}"] = (chunk, score)
            amendment_idx += 1
        else:
            text = _format_amendment(chunk, score, adopted_budget - current_chars)
        if current_chars + len(text) > adopted_budget:
            break
        parts.append(text)
        current_chars += len(text) + 10

    # Format other amendments
    other_start = current_chars
    for chunk, score in other:
        if numbered:
            text = _format_amendment(chunk, score, other_budget - (current_chars - other_start), idx=amendment_idx)
            amendment_mapping[f"AMEND-{amendment_idx}"] = (chunk, score)
            amendment_idx += 1
        else:
            text = _format_amendment(chunk, score, other_budget - (current_chars - other_start))
        if current_chars + len(text) > max_chars:
            break
        parts.append(text)
        current_chars += len(text) + 10

    text = "\n\n---\n\n".join(parts)
    if numbered:
        return text, amendment_mapping
    return text
