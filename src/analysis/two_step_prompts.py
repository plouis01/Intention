"""Two-step extraction prompts optimized for Haiku 4.5.

Step 1: Factual extraction (T=0.0) - simple, deterministic
Step 2: Intent synthesis (T=0.2) - priority-ordered, all business rules
"""

# Step 1: Extract raw facts - temperature 0.0, max_tokens 3000
# Minimal instructions, Haiku just extracts who said what
FACT_EXTRACTION_PROMPT = """Article analysé : {article_ref} du {article_code}
Loi : {law_ref} (n°{law_numero}) | Article dans la loi : {article_in_law}
Mots-clés : {article_keywords}

## Débats parlementaires
{debate_chunks}

## Amendements
{amendments}

## Exposé des motifs
{expose_motifs}

## Tâche
Extrais les faits des débats concernant {article_ref}. Réponds en JSON.

Règles :
- MAXIMUM 10 faits au total : AU MOINS 2 faits AN et AU MOINS 2 faits Sénat (si la chambre est présente dans les extraits). Complète avec les plus pertinents.
- NE rapporte QUE ce qui est dit dans les extraits, SANS interpréter
- Traite la section AN EN PREMIER, puis la section Sénat
- CITE les passages mentionnant {article_ref} ou ses mécanismes clés : {article_keywords}
- Inclus aussi les interventions expliquant le POURQUOI de la modification même sans citer le numéro d'article
- Utilise l'identifiant de l'extrait (ex: "EXTRAIT-3" ou "AMEND-1") et copie les 10-15 premiers mots du passage pertinent dans passage_cle
- Inclus les amendements avec leur sort exact (adopté/rejeté/retiré/tombé/non soutenu)
- Ignore la procédure pure (mise aux voix, suspension, rappels au règlement)
- Les citations procédurales mentionnant {article_ref} (lecture d'amendement, renvois) SONT pertinentes

JSON :
{{
  "facts": [
    {{"extrait_id": "EXTRAIT-3", "passage_cle": "premiers mots du passage",
     "type": "declaration|amendement|reponse"}}
  ],
  "amendements_identifies": [
    {{"numero": "N°XXX", "auteur": "Auteur", "sort": "adopté/rejeté/retiré/tombé/non soutenu",
     "objet_resume": "Objet en 1 phrase"}}
  ]
}}

JSON uniquement."""

# Step 2: Synthesize intent - temperature 0.2, max_tokens 1500
# Priority-ordered instructions optimized for Haiku's sequential generation
INTENT_SYNTHESIS_PROMPT = """Article : {article_ref} du {article_code} - {article_titre}
Loi : {law_ref} ({law_date}) | Nature : {nature}
Mots-clés : {article_keywords}

## Faits extraits
{extracted_facts}

## Synthétise l'intention du législateur. Réponds en JSON.

PRIORITÉ 1 - SOURCES (remplis EN PREMIER)
- NE cite QUE des extrait_id présents dans les faits ci-dessus. N'invente PAS de références.
- 1 à 3 sources par chambre présente dans les faits (0 si aucune chambre n'a de contenu pertinent)
- Cherche activement dans CHAQUE chambre. Si des faits AN existent, cite AU MOINS 1 source AN.
- Préfère : rapporteur général > rapporteur/ministre > commission > parlementaire
- Le POURQUOI (objectif), pas le QUOI (description technique)
- Citations procédurales mentionnant {article_ref} sont valides
- Format : {{"extrait_id": "EXTRAIT-N", "passage_cle": "10-15 premiers mots du passage pertinent"}}

PRIORITÉ 2 - INTENTION (basée sur les sources que tu viens de citer)
- 1 phrase introductive + 2-3 bullet points (1 phrase chacun)
- Commence par "L'intention du législateur est de [OBJECTIF]."
- Vide "" uniquement si sources est vide
- Exemple :
  "L'intention du législateur est de mieux orienter l'épargne vers l'économie réelle.\n\nLe législateur souhaite :\n- Porter le seuil de réinvestissement de 50% à 60%\n- Concentrer les investissements dans les PME opérationnelles"

PRIORITÉ 3 - CERTITUDE
Remplis certitude_reasoning :
- extraits_explicites : nombre de faits mentionnant {article_ref}
- source_autoritative : rapporteur ou ministre cité ? (true/false)
- intention_explicite : intention énoncée directement ? (true/false)
- coherence_chambres : AN et Sénat convergent ? (true/false)
Puis niveau_certitude : "élevé" (≥2 extraits + autoritative), "moyen" (≥1 OU autoritative), "faible" (insuffisant)

PRIORITÉ 4 - RESTE
- amendements_rejetes_detailles : amendements rejetés/retirés/tombés [{{"numero": "N°", "auteur": "...", "sort_reel": "rejeté", "objet": "...", "raison_rejet": "...", "source_rejet": "...", "implication_intention": "..."}}]
- points_vigilance : ["1 ligne par amendement rejeté pertinent"]
- evolution_debats : max 2 phrases
- contexte_legislatif : 1 phrase
- operational_parameters : valeurs chiffrées (seuils, délais, taux) ou {{}}

Si 0 fait pertinent → {{"sources":[],"amendements_rejetes_detailles":[],"evolution_debats":"","points_vigilance":[],"operational_parameters":{{}},"certitude_reasoning":{{"extraits_explicites":0,"source_autoritative":false,"intention_explicite":false,"coherence_chambres":false}},"niveau_certitude":"faible","contexte_legislatif":"Modification par {law_ref}","intention":""}}

JSON uniquement."""
