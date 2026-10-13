Voici un **rapport de correction orienté code**. Globalement, le PDF est **plutôt propre en mise en page** ; le vrai problème est le **pipeline d’analyse juridique**, pas le rendu. Les erreurs observées sont surtout des erreurs de **rattachement**, de **qualification**, de **statut procédural** et de **nettoyage textuel**.

## 1. Problème principal : mauvais rattachement entre la cible et les sources

Le générateur mélange encore plusieurs objets différents :

* **l’article 39 du CGI**,
* **l’article 39 d’une loi de finances**,
* des **articles voisins** comme 39 ter, 39 B, 39 decies, 39 sexies,
* et parfois de simples mentions procédurales de “l’article 39”.
  Le symptôme le plus net est que le document reconnaît lui-même, pour certaines années, que les débats trouvés ne portent **pas** sur l’article 39 CGI, tout en conservant malgré tout une fiche dans la chronologie. C’est visible pour la **LFR 2016**, où la source citée parle du **fonds d’urgence** et où la fiche admet que les extraits relèvent de “l’article 39 de la loi de finances, non du CGI”, ou d’articles voisins, mais la section reste présente. On voit le même glissement pour **LF 2024**, où la fiche est construite autour de **39 decies C** tout en étant présentée comme une modification de l’article 39 CGI, et pour **LF 2022**, où le document note lui-même la confusion avec **l’article 39 B**.

### Correction code

Il faut séparer le pipeline en deux étapes strictes :

1. **Identifier d’abord la modification normative exacte** dans le texte consolidé : article 39 lui-même, paragraphe, alinéa, 5°, 12 ter, etc.
2. **Ne chercher les débats qu’après**, avec une contrainte de pertinence forte : la source doit viser soit **“article 39 du CGI”**, soit la **subdivision exacte modifiée**.
   Tout ce qui relève de “article 39 du PLF”, “article 39 de la loi”, “39 ter”, “39 decies”, “39 sexies”, etc., doit être :

* soit exclu,
* soit rangé dans une section **connexe**, jamais fusionné avec la fiche principale.

## 2. Le moteur accepte des citations non pertinentes comme “sources principales”

Il y a des cas où la citation retenue ne parle manifestement pas du bon sujet, puis sert quand même à soutenir une intention législative. Le meilleur exemple est la citation de **Jean-Pierre Door** sur “la procédure de fixation des tarifs”, réutilisée dans les blocs **LF 2009 / LFR 2008** alors qu’elle ne documente pas l’article 39 CGI sur les charges déductibles. De même, des mentions procédurales comme la demande de seconde délibération sur les articles 39, 40, 41 et 71 ter du PLF ne devraient pas servir de base à une inférence sur l’intention du législateur concernant l’article 39 CGI.

### Correction code

Ajouter un **filtre de pertinence sémantique** entre extraction et rédaction :

* score élevé seulement si la citation mentionne **l’article cible** et un **motif fiscal cohérent** avec la modification ;
* score nul si la citation est seulement **procédurale** ;
* score nul si la citation mentionne un **autre article** ou un autre objet administratif.
  En pratique, la génération du bloc “Sources principales” ne devrait accepter qu’une citation classée **support direct**, pas une simple cooccurrence du nombre 39.

## 3. Le pipeline confond statut d’amendement et issue finale

Plusieurs fiches mélangent :

* le **sort de l’amendement**,
* le **sort de la mesure**,
* et le **sort final dans la loi**.
  Exemple très clair : l’amendement **I-19** est présenté comme “rejeté”, mais la raison indique qu’il **a été ADOPTÉ**. Même bug pour l’amendement **I-956**, qualifié de “rejeté” alors que la raison dit qu’il a été **retiré**. Même logique sur l’amendement **n°197** de 2021 : le texte dit qu’il n’a pas été adopté au Sénat, tout en expliquant qu’on retrouve la mesure dans la loi finale.

### Correction code

Le modèle de données doit distinguer au minimum :

* `amendment_status_in_debate`: adopté / rejeté / retiré / non soutenu / tombé
* `measure_status_in_final_text`: présente / absente / réintroduite / reprise par autre amendement / reprise par CMP ou seconde lecture

Et la rédaction ne doit jamais produire une phrase du type :

* “amendement rejeté” + “raison : adopté”
  Il faut un **contrôle de cohérence automatique** avant rendu.

## 4. Le moteur infère trop quand la preuve est faible

Plusieurs fiches sont marquées **“Confiance faible”** ou admettent qu’il n’y a **pas de débat substantiel**, mais elles produisent quand même un contexte, une évolution, des “points de vigilance”, voire une intention assez affirmative. C’est le cas de **LF 2011**, **LF 2012**, **LFR 2021**, et d’autres sections où le document reconnaît lui-même que les extraits ne permettent pas vraiment d’identifier le “pourquoi”.

### Correction code

Instaurer une règle dure :

* si pas de **citation directement explicative** ou pas de **convergence minimale** entre plusieurs sources, alors la fiche doit sortir en mode
  **“Intention non déterminable à partir des débats disponibles”**
  et **ne pas** générer :
* “Le législateur souhaite…”
* “Évolution des débats…”
* “Implication : …”
  Autrement dit, quand la preuve est faible, il faut **assumer le vide**, pas le remplir.

## 5. Les “points de vigilance” absorbent trop de bruit hors sujet

Le pipeline verse encore dans la fiche des amendements qui ne sont pas vraiment utiles pour comprendre la modification de l’article 39 CGI. On le voit avec des références à **France Télévisions**, aux **éoliennes**, à des formes sociétaires outre-mer, à des mesures Pinel très périphériques, etc. Certaines peuvent être mentionnées dans le corpus, mais elles ne devraient pas remonter dans le résumé analytique d’une fiche centrée sur l’article 39 CGI.

### Correction code

Ajouter un filtre “**amendement pertinent pour la fiche**” :

* garder seulement les amendements qui modifient directement l’article 39 ou sa subdivision ciblée ;
* ou ceux explicitement invoqués dans les débats pour justifier la mesure ;
* exclure les amendements simplement voisins dans la séance.

## 6. Le texte final n’est pas assez nettoyé

On voit plusieurs artefacts de génération ou d’extraction :

* **“N°N°”** répété dans les amendements,
* **“n’porte”**,
* **“5bisde”**,
* citations tronquées avec **“fo…”**,
* insertions comme **“[charges]”** au milieu d’une citation.
  Ce n’est pas seulement cosmétique : ce type de bruit fragilise aussi l’analyse downstream.

### Correction code

Prévoir un post-traitement systématique :

* normalisation des références d’amendements,
* correction des collages de tokens,
* suppression des crochets éditoriaux non justifiés,
* refus des citations tronquées comme “fo…” dans les “sources principales”.

## 7. La synthèse agrège des fiches de qualité hétérogène comme si elles se valaient

La partie “Objectif de l’article” est globalement juste, mais la **chronologie de synthèse** mélange des entrées solides avec des entrées faibles ou hors périmètre. Elle conserve aussi des problèmes d’ordre et de normalisation, avec par exemple des entrées 2014/2015 qui ne sont pas ordonnées de façon nette dans la liste consolidée.

### Correction code

La synthèse ne doit agréger que des fiches ayant passé un seuil minimal de qualité :

* cible correctement identifiée,
* au moins une source directe,
* pas de contradiction sur le statut,
* pas de marqueur d’incertitude bloquant.

## 8. Les sections vides ou semi-vides doivent être traitées comme un état valide, pas comme un brouillon

On voit encore des fiches avec **“Intention du législateur”** laissé vide ou quasi vide, puis une relance automatique sur l’“Évolution des débats”. Ce comportement donne une impression de document inachevé. On le voit notamment autour de **LF 2004**, **LF 2011** ou **LF 2012**.

### Correction code

Si l’intention est vide après filtrage, il faut choisir un des deux modes :

* soit **masquer totalement la section**,
* soit afficher un bloc standardisé :
  **“Les débats retrouvés ne permettent pas d’identifier de manière fiable l’intention du législateur pour cette modification.”**

## 9. La partie jurisprudence a été enrichie, mais certaines fiches sont incomplètes

L’ajout de jurisprudence est une bonne direction, mais au moins certaines fiches apparaissent encore avec **Faits / Solution / Portée** laissés vides, ce qui donne un rendu inachevé. C’est visible pour des décisions comme **CE 2016-07-13** et **CE 2018-12-21** dans la section finale. 

### Correction code

Même logique que pour les fiches législatives :

* soit la jurisprudence est assez renseignée et on l’affiche ;
* soit elle est masquée ;
* jamais de squelette vide.

## 10. Ce qui fonctionne déjà et qu’il faut garder comme “bon cas de test”

Certaines parties sont nettement meilleures et peuvent servir de **références positives** pour vos tests :

* **LFR 2012** sur les aides et abandons de créance : cible claire, intention claire, citations cohérentes.
* **LF 2019** sur le 12 ter et les redevances de propriété intellectuelle : fiche plus structurée et juridiquement mieux tenue.
* **LF 2022 / LF 2023** sur l’amortissement des fonds commerciaux et la clause anti-abus : logique législative identifiable et bien résumée.

---

## Plan de correction priorisé

### Priorité 1

Corriger le **rattachement cible-source** :

* distinguer article 39 CGI / article 39 d’une loi / articles voisins ;
* interdire l’inférence si la citation n’est pas directement pertinente.

### Priorité 2

Corriger le **modèle de statut procédural** :

* adopté / rejeté / retiré / tombé / non soutenu ;
* distinct de la présence finale dans la loi.

### Priorité 3

Réduire les **hallucinations d’intention** :

* pas de “Le législateur souhaite…” sans preuve directe suffisante.

### Priorité 4

Nettoyer le **texte généré** :

* N°N°, tokens collés, citations tronquées, placeholders.

### Priorité 5

Durcir la **synthèse finale** :

* n’agréger que les fiches validées.

---

## Tests automatiques que je mettrais en place

1. **Test de périmètre**
   Échec si une fiche “article 39 CGI” contient une source parlant seulement de :

* “article 39 de la loi”,
* “39 ter”,
* “39 decies”,
* “39 sexies”,
* ou d’un autre article sans rattachement explicite.

2. **Test de cohérence procédurale**
   Échec si une même ligne contient :

* `status = rejeté` et une raison contenant “adopté” ;
* `status = rejeté` et une raison contenant “retiré”.

3. **Test d’inférence minimale**
   Échec si `confidence = faible` et que la fiche génère quand même une intention affirmative sans source directe.

4. **Test de bruit textuel**
   Échec si le rendu contient :

* “N°N°”,
* “n’porte”,
* “5bisde”,
* “fo…”,
* crochets éditoriaux dans une citation.

5. **Test de section vide**
   Échec si une rubrique “Intention du législateur”, “Solution”, “Portée” ou “Faits” est affichée vide.
