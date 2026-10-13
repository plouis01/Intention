# Intention du Législateur

Outil d'analyse de l'intention législative des lois de finances françaises (LF, LFR, LFSS) de 2012 à 2025, destiné aux avocats fiscalistes.

## Fonctionnalités

- **Collecte automatisée** des textes de loi, débats parlementaires et amendements
- **Recherche sémantique** dans les débats par article de loi
- **Extraction de l'intention législative** via IA (Claude/Groq)
- **Sources multiples** : Légifrance, Assemblée nationale, Sénat

## Installation

```bash
# Cloner le repository
git clone <repo-url>
cd legal-project

# Créer un environnement virtuel
python -m venv venv
source venv/bin/activate  # Linux/Mac
# ou: venv\Scripts\activate  # Windows

# Installer les dépendances
pip install -e .
```

## Configuration

Copier `.env.example` vers `.env` et configurer les variables :

```bash
cp .env.example .env
```

### Variables requises

| Variable | Description |
|----------|-------------|
| `PISTE_CLIENT_ID` | Identifiant OAuth Légifrance ([piste.gouv.fr](https://piste.gouv.fr)) |
| `PISTE_CLIENT_SECRET` | Secret OAuth Légifrance |
| `DATABASE_URL` | URL de la base de données |

### Configuration IA (optionnel)

**Embeddings** (recherche sémantique) :
- `EMBEDDING_PROVIDER=local` : Gratuit, utilise sentence-transformers
- `EMBEDDING_PROVIDER=openai` : API payante, meilleure qualité

**LLM** (extraction d'intention) :
- `LLM_PROVIDER=groq` : Gratuit (tier limité), Llama 3.3
- `LLM_PROVIDER=anthropic` : API payante, Claude

## Utilisation

### Pipeline complet

```bash
# Exécuter toutes les étapes (collecte + import + validation)
python scripts/run_pipeline.py --all
```

### Étapes individuelles

```bash
# Collecter les données des sources
python scripts/run_pipeline.py --collect

# Importer dans la base de données
python scripts/run_pipeline.py --import

# Valider la complétude des données
python scripts/run_pipeline.py --validate

# Réinitialiser la base avant import
python scripts/run_pipeline.py --reset
```

### Collecteurs individuels

```bash
python -m src.collectors.legifrance  # Textes de loi
python -m src.collectors.assemblee   # AN : dossiers, amendements, débats
python -m src.collectors.senat       # Sénat : DOSLEG, AMELI
```

### Tests

```bash
pytest tests/
```

## Architecture

```
src/
├── collectors/     # Collecte des données (Légifrance, AN, Sénat)
├── parsers/        # Parsing des débats et amendements
├── indexing/       # Embeddings, chunking, recherche, extraction IA
├── db/             # Modèles SQLAlchemy et import
└── utils/          # Configuration

data/
├── lois_finances_2012_2025.json  # Référentiel des 35 lois
├── raw/                          # Données brutes (gitignored)
└── embeddings_cache/             # Cache des embeddings (gitignored)
```

### Pipeline de données

1. **Collecte** : Récupération des textes, débats et amendements
2. **Parsing** : Structuration en interventions et amendements normalisés
3. **Indexation** : Génération des embeddings par chunk
4. **Recherche** : Recherche sémantique + extraction de l'intention

### Schéma de données

```
lois → articles → dossiers_legislatifs → debats → interventions → amendements
```

## Sources de données

| Source | Type | Contenu |
|--------|------|---------|
| [Légifrance](https://piste.gouv.fr) | API OAuth | Textes de loi (JORF) |
| [data.assemblee-nationale.fr](https://data.assemblee-nationale.fr) | Open Data | Dossiers, amendements, débats |
| [data.senat.fr](https://data.senat.fr) | Open Data | DOSLEG, AMELI |

## Stack technique

- **Python** 3.10+
- **FastAPI** pour l'API REST
- **SQLAlchemy 2.0** pour l'ORM
- **SQLite** (dev) / **PostgreSQL** (prod)
- **Sentence-Transformers** ou **OpenAI** pour les embeddings
- **Groq** ou **Anthropic Claude** pour l'extraction d'intention

## Licence

[À définir]
