"""Two-step extraction prompts for fact extraction + intent synthesis."""

# Step 1: Extract raw facts (who said what, when) — temperature 0.0
FACT_EXTRACTION_PROMPT = """Tu es un assistant juridique expert en extraction de faits.

## Contexte
Article analysé: {article_ref} du {article_code}
Loi de modification: {law_ref}
Article dans la loi: {article_in_law}

## Débats parlementaires
{debate_chunks}

## Amendements liés
{amendments}

## Exposé des motifs
{expose_motifs}

## Instructions
Extrais UNIQUEMENT les faits objectifs des débats, sans interprétation.

### RÈGLES:
1. NE rapporte QUE ce qui est EXPLICITEMENT dit dans les extraits
2. Identifie clairement QUI a dit QUOI et QUAND
3. Distingue les faits AN et Sénat
4. NE CITE QUE des passages mentionnant "{article_ref}" ou ses concepts clés
5. Ignore les interventions purement procédurales

## Format de réponse (JSON strict)

```json
{{
  "facts": [
    {{
      "orateur": "M./Mme Prénom Nom",
      "fonction": "rapporteur / ministre / député / sénateur",
      "chambre": "AN ou Sénat",
      "date": "YYYY-MM-DD",
      "type": "declaration | amendement | reponse | explication_vote",
      "contenu": "Résumé factuel de l'intervention (max 200 chars)",
      "citation_exacte": "Citation exacte pertinente (max 250 chars)"
    }}
  ],
  "amendements_identifies": [
    {{
      "numero": "N°XXX",
      "auteur": "Auteur",
      "sort": "adopté / rejeté / retiré",
      "objet_resume": "Résumé de l'objet en 1-2 phrases"
    }}
  ],
  "expose_motifs_resume": "Résumé de l'exposé des motifs en 2-3 phrases (si disponible)"
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""

# Step 2: Synthesize intent from extracted facts — temperature 0.2
INTENT_SYNTHESIS_PROMPT = """Tu es un assistant juridique expert en synthèse d'intention législative.

## Contexte
Article analysé: {article_ref} du {article_code}
Titre: {article_titre}
Loi de modification: {law_ref}
Date de promulgation: {law_date}
Nature de la modification: {nature}

## Faits extraits des débats
{extracted_facts}

## Instructions
À partir des faits extraits ci-dessus, synthétise l'intention du législateur.

### RÈGLES CRITIQUES:
1. Base ta synthèse UNIQUEMENT sur les faits fournis
2. NE PAS inventer d'information absente des faits
3. Structure l'intention: "L'intention du législateur est de [OBJECTIF]."
4. Cite les orateurs et dates des faits utilisés

### ÉVALUATION DU NIVEAU DE CERTITUDE:
Avant de fixer le niveau, évalue:
1. Combien de faits mentionnent EXPLICITEMENT {article_ref} ?
2. Un rapporteur ou ministre est-il cité ?
3. L'intention est-elle ÉNONCÉE ou DÉDUITE des faits ?
4. AN et Sénat convergent-ils ?

## Format de réponse (JSON strict)

```json
{{
  "contexte_legislatif": "Description du contexte législatif (1-2 phrases)",
  "intention": "L'intention du législateur est de [OBJECTIF PRINCIPAL].\\n\\nLe législateur souhaite :\\n- [Point 1]\\n- [Point 2]\\n- [Point 3]",
  "operational_parameters": {{
    "paramètre_clé": "valeur extraite des faits"
  }},
  "sources_an": [
    {{
      "citation": "Citation exacte tirée des faits AN",
      "orateur": "M./Mme Prénom Nom",
      "fonction": "fonction",
      "date": "YYYY-MM-DD"
    }}
  ],
  "sources_senat": [
    {{
      "citation": "Citation exacte tirée des faits Sénat",
      "orateur": "M./Mme Prénom Nom",
      "fonction": "fonction",
      "date": "YYYY-MM-DD"
    }}
  ],
  "evolution_debats": "Description de l'évolution si visible dans les faits",
  "amendements_rejetes_detailles": [
    {{
      "numero": "N°",
      "auteur": "Auteur",
      "sort_reel": "rejeté / retiré / tombé / non soutenu",
      "objet": "Objet",
      "raison_rejet": "Raison",
      "source_rejet": "Qui a argumenté contre",
      "implication_intention": "Ce que le rejet révèle"
    }}
  ],
  "points_vigilance": [],
  "certitude_reasoning": {{
    "extraits_explicites": 0,
    "source_autoritative": false,
    "intention_explicite": false,
    "coherence_chambres": false
  }},
  "niveau_certitude": "élevé ou moyen ou faible",
  "justification_certitude": "Explication basée sur les critères ci-dessus"
}}
```

Réponds UNIQUEMENT avec le JSON, sans texte avant ou après.
"""
