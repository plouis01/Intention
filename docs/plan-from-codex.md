# Refonte du sélecteur d’interventions du stream API

**Résumé**

- Audit vérifié de bout en bout: le `stream` groupe aujourd’hui les modifications par `law_numero`, fait une seule recherche par loi, puis redistribue/déduplique les citations entre sections. Cela casse la granularité modification par modification.
- Cas confirmé sur `150-0 B ter`: 10 modifications brutes donnent 8 sections dans le benchmark actuel; seulement 2 sections sont bicamérales, 6 sont Sénat-only.
- Le principal défaut de sélection est en amont du LLM: la recherche est surtout `law_numero + keywords article + objet`, pas `modification ciblée + article dans la loi`; en plus, le “rescue” AN/Sénat va chercher hors de la loi cible.
- Les données permettent un design plus strict: sur 5 350 modifications finance depuis 2012, seulement 10 n’ont pas `article_in_law`; toutes les lois finances ont AN + Sénat en base; en revanche les chunks d’interventions manquent d’ancres loi/article, et 71 118 chunks d’amendements AN n’ont pas de date.

**Changements d’implémentation**

- L’unité d’analyse devient une `LawModification` brute, jamais un groupe par loi. Le nombre de sections du `stream` doit égaler le nombre de modifications retenues.
- Introduire un `modification_id` et une `modification_signature` calculés à partir de: `law_numero`, `article_in_law` normalisé, `nature`, `objet` normalisé, discriminants extraits (`%`, montants, dates, seuils, acteurs, conditions), et termes négatifs dérivés des modifications sœurs de la même loi.
- Générer les candidats séparément pour AN et Sénat, par ordre de précision strict:

1. amendements liés à la bonne loi et au bon `article_in_law` / `article_id`
2. débats de la bonne loi mentionnant le bon `article_in_law` ou un amendement lié
3. débats de la bonne loi mentionnant l’article CGI et la signature de modification
4. rapports/exposés de la bonne loi uniquement en secours

- Supprimer tout “rescue” de chambre hors de la loi cible. Aucune source ne doit entrer uniquement pour remplir la chambre manquante.
- Mettre tous les candidats en compétition contre toutes les modifications sœurs du même `law_numero + article_in_law`: un chunk n’est gardé que s’il gagne nettement pour une seule signature; sinon il est rejeté comme ambigu.
- La bicaméralité devient “maximisée sous contrainte de précision”: on fusionne AN + Sénat seulement si les deux côtés ont des preuves fortes pour la même signature. Sinon on assume un résultat mono-chambre explicite, sans pollution.
- Supprimer la réaffectation post-LLM “par date la plus proche”; l’appartenance d’une source à une modification doit être décidée avant LLM, par compétition de signatures.
- Pour les 10 cas modernes sans `article_in_law`, utiliser un fallback dédié: même loi, article CGI explicite, signature plus stricte, aucune bicaméralité forcée.

**Interfaces / Types**

- Étendre la sortie interne du sélecteur avec: `modification_id`, `modification_signature`, `anchor_type`, `evidence_class`, `law_scope_match`, `article_in_law_match`, `modification_match_score`, `runner_up_score`, `rejection_reason`.
- Garder le schéma public du `stream` stable par défaut, mais ajouter un mode debug qui expose diagnostics de sélection et de rejet.
- Réindexer les chunks d’interventions avec des métadonnées minimales obligatoires: `law_numero`, `dossier_id`, `debat_id`, mentions d’articles normalisées, références d’amendements détectées. Sans cela, le sélecteur restera trop inférentiel.

**Plan de test**

- `150-0 B ter`: 10 modifications brutes doivent produire 10 sections; le cluster `2017-1837` ne doit plus partager de citations entre ses 3 modifications.
- Tests de non-contamination sur articles avec multiples modifications dans la même loi: aucune source sélectionnée ne peut être réutilisée par une modification sœur, sauf égalité exacte de `modification_id` si ce comportement est explicitement autorisé plus tard.
- Tests de chambre:
  - deux chambres quand les deux atteignent le seuil de précision
  - mono-chambre assumé quand l’autre chambre n’a que des hits faibles
  - zéro source provenant d’une autre loi pour “compléter” une chambre
- Tests de diagnostics: chaque chunk rejeté doit avoir une raison déterministe (`wrong_law`, `wrong_article_in_law`, `lost_sibling_competition`, `generic_only`, `ambiguous`, `too_far_in_time`).
- Validation pipeline: métriques systématiques sur couverture AN/Sénat, taux de lien amendement→article, dates manquantes, et part des sections réellement bicamérales.

**Hypothèses / choix verrouillés**

- Politique retenue: la spécificité prime sur la bicaméralité forcée.
- Pool primaire retenu: débats + amendements; rapports/exposés seulement en secours ciblé.
- Les citations curées ne doivent pas piloter la sélection principale; elles ne restent admissibles qu’en complément si elles sont rattachables à la bonne `modification_id`.
- Les améliorations de données à prioriser sont: enrichissement des chunks d’interventions, amélioration du lien `amendement.article_id` (124 031/224 163 aujourd’hui), et complétion des dates d’amendements AN.
