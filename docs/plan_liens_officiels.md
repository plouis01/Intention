# Liens vers les documents parlementaires officiels

## Context

Les analyses extraient des sources (citations AN/Sénat, amendements) mais l'utilisateur ne peut pas les retrouver dans les documents officiels. On veut ajouter des liens cliquables vers assemblee-nationale.fr et senat.fr.

---

## 1. Liens Dossiers Législatifs (déjà en DB)

**État** : `DossierLegislatif.url_source` est déjà rempli pour AN et Sénat.

**Action** : Propager l'URL du dossier dans la réponse API de chaque section d'analyse.

**Fichiers** :

- `src/analysis/article_analyzer.py` - dans `_analyze_modification()`, récupérer `dossier.url_source` via la relation `loi → dossier` et l'ajouter au résultat de section
- `src/api/cgi_routes.py` - s'assurer que `url_dossier` est inclus dans le stream JSON

**Patterns URL existants** :

- AN : `https://www.assemblee-nationale.fr/dyn/{legislature}/dossiers/{uid}`
- Sénat : `https://www.senat.fr/dossier-legislatif/{signet}.html`

---

## 2. Liens Amendements

### 2a. Sénat (pattern clair et constructible)

**Pattern** :

```
https://www.senat.fr/amendements/{SESSION}/{TEXT_NUMBER}/Amdt_{PARTIE}-{NUMERO}.html
```

- SESSION : `2024-2025`, `2023-2024`, etc. (dérivé de `amendement.date_depot`)
- TEXT_NUMBER : numéro du texte au Sénat (stocké dans `dossier.reference_externe` ou à extraire)
- PARTIE-NUMERO : `I-2038`, `II-707`, etc. (déjà dans `amendement.numero`)

**Exemple** : `https://www.senat.fr/amendements/2024-2025/143/Amdt_I-2038.html`

### 2b. AN (pattern plus complexe)

**Pattern** :

```
https://www.assemblee-nationale.fr/dyn/{LEGISLATURE}/amendements/{BILL_CODE}/{SOURCE}/{AMENDMENT_NUMBER}
```

- LEGISLATURE : 14, 15, 16, 17
- BILL_CODE : `{TEXT_NUMBER_PADDED}{READING_LETTER}` (ex: `0324C`)
- SOURCE : `AN` (séance) ou `CION_FIN` (commission finances)
- AMENDMENT_NUMBER : numéro brut

**Exemple** : `https://www.assemblee-nationale.fr/dyn/17/amendements/0324C/AN/3656`

**Difficulté** : Le `BILL_CODE` et la `SOURCE` ne sont pas toujours stockés dans notre DB. Il faudra soit :

- Stocker `url_source` au moment de l'import (modifier les collectors)
- Construire l'URL à partir des métadonnées existantes (fragile)

### 2c. Implémentation

**Approche recommandée** : Construire un helper `build_amendment_url()` dans `src/utils/url_builder.py` (nouveau fichier) :

```python
def build_senat_amendment_url(session: str, text_number: str, numero: str) -> str | None:
    """Construit l'URL Sénat. Fiable car pattern stable."""
    return f"https://www.senat.fr/amendements/{session}/{text_number}/Amdt_{numero}.html"

def build_an_amendment_url(legislature: int, bill_code: str, numero: str) -> str | None:
    """Construit l'URL AN. Peut retourner None si données insuffisantes."""
    if not bill_code:
        return None
    return f"https://www.assemblee-nationale.fr/dyn/{legislature}/amendements/{bill_code}/AN/{numero}"
```

**Stockage** : Ajouter `url_source` lors de l'import des amendements dans `src/db/import_data.py` (pour AN et Sénat). Pour les amendements déjà en DB, un script de migration reconstruit les URLs.

---

## 3. Liens Débats (Comptes Rendus)

### 3a. Sénat

**Pattern** :

```
https://www.senat.fr/seances/s{YYYYMM}/s{YYYYMMDD}/s{YYYYMMDD}{NNN}.html
```

- `NNN` = numéro de session dans la journée (ex: `019`, `020`)

**Problème** : Le `NNN` n'est pas stocké dans notre DB.

**Solutions** :

1. Stocker `url_source` dans `Debat` au moment du parsing CRI (`src/parsers/senat_debats.py`) - le XML CRI contient le nom du fichier source qui correspond au `NNN`
2. Alternative : lien vers la page journalière (sans le `NNN`) : `https://www.senat.fr/seances/s{YYYYMM}/s{YYYYMMDD}/` → liste toutes les sessions du jour

### 3b. AN

**Pattern moderne** :

```
https://www.assemblee-nationale.fr/dyn/{LEGISLATURE}/comptes-rendus/seance/session-ordinaire-de-{YEAR}-{YEAR+1}/seance-du-{DATE_FR}
```

**Problème** : Le format `DATE_FR` est en toutes lettres ("mardi-14-octobre-2025"). Trop fragile à construire.

**Solution alternative** : Lien vers le dossier législatif (déjà disponible, niveau 1) qui contient les liens vers tous les comptes rendus associés.

### 3c. Implémentation

**Approche pragmatique** :

1. **Sénat** : Stocker `url_source` dans `Debat` pendant le parsing CRI. Pour les débats existants, construire `https://www.senat.fr/seances/s{YYYYMM}/s{YYYYMMDD}/` (page journalière)
2. **AN** : Utiliser le lien vers le dossier législatif (fiable, déjà en DB). Ne pas tenter de construire l'URL du CR spécifique (trop fragile)

**Fichiers** :

- `src/parsers/senat_debats.py` - capturer `url_source` lors du parsing
- `src/utils/url_builder.py` - helpers pour construire les URLs de fallback
- `src/db/models.py` - vérifier que `Debat.url_source` existe (déjà le cas)

---

## 4. Propagation dans les chunks et l'API

### 4a. Enrichir les métadonnées des chunks

**Fichier** : `src/indexing/chunking.py`

Ajouter dans les métadonnées de chunk :

- `url_source` : URL directe si disponible
- `debate_id` / `amendment_id` : ID DB pour lookup

### 4b. Exposer dans la réponse API

**Fichier** : `src/analysis/article_analyzer.py`

Dans le résultat de chaque section, ajouter :

```json
{
  "url_dossier": "https://www.senat.fr/dossier-legislatif/...",
  "sources_an": [
    {
      "citation": "...",
      "url_source": "https://www.assemblee-nationale.fr/..."
    }
  ],
  "amendements_rejetes_detailles": [
    { "numero": "I-792", "url_source": "https://www.senat.fr/amendements/..." }
  ]
}
```

---

## Fichiers modifiés

| Fichier                            | Changement                                                |
| ---------------------------------- | --------------------------------------------------------- |
| `src/utils/url_builder.py`         | **Nouveau** - helpers pour construire les URLs AN/Sénat   |
| `src/analysis/article_analyzer.py` | Propager `url_dossier` + `url_source` dans les résultats  |
| `src/db/import_data.py`            | Stocker `url_source` pour amendements AN/Sénat            |
| `src/parsers/senat_debats.py`      | Capturer `url_source` pour débats Sénat                   |
| `src/indexing/chunking.py`         | Ajouter `url_source` et IDs dans les métadonnées de chunk |
| `src/api/cgi_routes.py`            | Inclure les URLs dans le stream JSON                      |

## Ordre d'implémentation

1. `url_builder.py` - les helpers URL (pas de dépendance)
2. `article_analyzer.py` - propager `url_dossier` (déjà en DB)
3. `import_data.py` - stocker `url_source` pour les futurs imports
4. Script migration - reconstruire les URLs pour les données existantes
5. `chunking.py` + `cgi_routes.py` - enrichir chunks et API

## Vérification

1. `pytest tests/ -q`
2. Vérifier manuellement que les URLs construites sont valides (ouvrir dans le navigateur)
3. Benchmark 150-0 B ter : vérifier que `url_dossier` apparaît dans chaque section
4. Spot-check : ouvrir 3-4 liens amendements Sénat pour confirmer qu'ils pointent vers la bonne page
