# Backend Pointage Fortico

API construite avec **Python 3.12, FastAPI, SQLAlchemy 2 et PostgreSQL 16** pour le frontend React/TypeScript et le cahier des charges fourni.

## Couverture fonctionnelle

Les **103 opérations OpenAPI** couvrent les services du frontend :

- authentification, session JWT/refresh-cookie, profil et révocation de session ;
- salariés, lieux de travail, géolocalisation, entrée/sortie et historique ;
- calculs de paie, soldes, rapports, paiements et export CSV ;
- paramètres d’entreprise, TVA, clients, devis, factures et conversions ;
- chantiers, équipes, étapes, notes et statistiques ;
- demandes de congé, décisions, annulation et accusés de lecture ;
- matériel, prêts, retours et disponibilités ;
- conversations, messages, réactions, membres, lectures et WebSocket ;
- rondes de présence, contrôles GPS et journal d’audit ;
- réinitialisation administrateur sélective prévue par l’interface.

Les payloads et erreurs sont sérialisés en camelCase et `{ "error": { "code", "message", "details" } }` comme l’attend `src/services/api.ts`. Swagger : `/docs`, OpenAPI : `/openapi.json`.

## Démarrage local avec Docker

Depuis ce dossier (`backend/`) :

```bash
cp .env.example .env
# Modifiez au minimum INVITE_CODE, JWT_ACCESS_SECRET et POSTGRES_PASSWORD.
docker compose up --build -d
```

- API : `http://localhost:4000/api/v1`
- Documentation interactive : `http://localhost:4000/docs`
- Readiness PostgreSQL : `http://localhost:4000/ready`
- Liveness : `http://localhost:4000/health`

Compose est configuré pour le **développement local** : PostgreSQL est publié uniquement sur `127.0.0.1` et le conteneur applique `alembic upgrade head` avant Uvicorn (`RUN_MIGRATIONS=true`). Ainsi, une base initiale reçoit le schéma et une base déjà versionnée applique les migrations de reprise sans masquer d’anciennes données JSONB. `AUTO_CREATE_SCHEMA` reste désactivé.

Si vous réutilisez une base créée par une ancienne version avec `create_all` mais sans `alembic_version`, **arrêtez l’API et faites une sauvegarde**. Ne lancez pas `stamp` à l’aveugle : vérifiez d’abord que toutes les tables correspondent exactement à la révision précédente `b652075ff233`; seulement dans ce cas, marquez cette révision (`alembic stamp b652075ff233`) puis lancez `alembic upgrade head`. Pour un schéma partiel ou différent, faites vérifier le schéma avant toute migration.

### Créer le premier administrateur

Méthode recommandée après le démarrage :

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/create_admin.py
```

Le script ne crée qu’un premier compte, demande le mot de passe sans l’afficher et inscrit l’opération dans l’audit. Il utilise `DATABASE_URL` du fichier `.env`. L’endpoint contractuel `POST /api/v1/auth/register-admin` est également disponible et protégé par `INVITE_CODE`.

## Démarrage local sans Docker

PostgreSQL 16 doit déjà être accessible. Dans `backend/` :

```bash
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
python scripts/create_admin.py
uvicorn app.main:app --reload --host 0.0.0.0 --port 4000
```

## Frontend local

Le client HTTP lit `VITE_API_URL` et `VITE_MOCK` dans `src/services/api.ts`. Le proxy de `vite.config.ts` pointe déjà `/api` et le WebSocket vers `localhost:4000`. À la racine du frontend, créer `.env.local` :

```dotenv
VITE_API_URL=/api/v1
VITE_MOCK=false
```

Puis exécuter `npm run dev`. **Ne lancez pas `npm run dev:mock`** pour tester l’API : ce script utilise les données fictives du navigateur. Le WebSocket local est dérivé automatiquement (`/api/v1/ws`) et transmis par le proxy Vite.

## Migrations PostgreSQL

- Baseline : `migrations/versions/c12c6765d713_initial_postgresql_schema.py` ; matériel/prêts : `migrations/versions/b652075ff233_normalize_equipment_and_loans.py` ; chantiers, clients/documents et messagerie : `migrations/versions/0be719ba5a41_normalize_sites_documents_and_chat.py`.
- Appliquer : `alembic upgrade head`.
- État : `alembic current`.
- Comparer la métadonnée ORM : `alembic check`.
- Revenir à la version précédente : `alembic downgrade -1`.

En production, exécuter `alembic upgrade head` comme étape de déploiement, **avant** de lancer plusieurs instances de l’API, et garder `RUN_MIGRATIONS=false` pour éviter que plusieurs réplicas migrent simultanément. `AUTO_CREATE_SCHEMA` doit rester désactivé.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

La suite livrée vérifie les routes critiques, leur présence dans OpenAPI, les structures relationnelles métier et les index d’intégrité matériel/prêts. Les parcours HTTP client→chantier→devis/version et groupe→message→réaction→lecture, la conservation des snapshots client à la suppression, ainsi que le downgrade→JSONB→upgrade des données de chaque domaine ont été testés sur PostgreSQL local.

## Configuration de production

Définir les secrets via le gestionnaire de secrets de l’hébergeur, jamais dans Git :

- `ENVIRONMENT=production` ;
- `DATABASE_URL=postgresql+psycopg://...` avec TLS selon l’hébergeur PostgreSQL ;
- `JWT_ACCESS_SECRET` : au moins 32 octets aléatoires ;
- `INVITE_CODE` : au moins 16 caractères aléatoires ;
- `FRONTEND_ORIGINS` : origines exactes séparées par des virgules, sans `*` ;
- `COOKIE_SECURE=true`, `COOKIE_SAMESITE=lax` ou `none` selon le déploiement (None exige Secure) ;
- `TIMEZONE_NAME=Indian/Antananarivo` ou le fuseau métier convenu ;
- `AUTO_CREATE_SCHEMA=false`.

Les cookies de refresh sont `HttpOnly`, rotatifs et liés à `/api/v1/auth`. Pour un frontend et une API sur des sites différents, configurer HTTPS, `COOKIE_SECURE=true`, `COOKIE_SAMESITE=none`, CORS avec l’origine exacte et cookies `credentials` autorisés.

## Notes d’architecture et migration de données

Les tables PostgreSQL relationnelles couvrent les comptes, salariés, lieux, pointages, paie, paiements, paramètres, journal, limites de connexion, équipement/prêts, clients, chantiers et membres/étapes/notes, devis/factures avec lignes/versions/compteurs, ainsi que conversations/membres/messages/réactions/lectures. La migration `b652075ff233` transfère le matériel/prêts et la migration `0be719ba5a41` migre les domaines clients/sites/documents/chat depuis `domain_records` JSONB en conservant les snapshots et historiques. Les deux downgrades restaurent les structures JSONB correspondantes. Les domaines applicatifs restant dans `domain_records` sont notamment les congés et les contrôles/rondes de présence.

Aucun export Firestore/Firebase ni accès au projet source n’était joint. La création d’un importeur de données réelles ne peut donc pas être validée ici. En particulier, les mots de passe Firebase ne sont pas récupérables en clair : la migration des comptes doit prévoir une réinitialisation ou une procédure d’invitation. Avant l’import, fournir un export autorisé, les noms de collections et la correspondance des champs, puis vérifier un échantillon en préproduction.

Le WebSocket et la file d’événements sont en mémoire du processus : lancer le backend en **instance unique** tant qu’un broker partagé (par ex. Redis) n’est pas ajouté. Les notifications temps réel ne remplacent pas la lecture REST ; le frontend conserve son mécanisme de resynchronisation/polling.
"# Backend" 
