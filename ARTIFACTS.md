# ai-km-service — Artifact Inventory

A file-by-file account of the codebase: **what each artifact does**, whether it
is **completely new** or **inherited** from a sibling service, and — for
inherited files — **what was changed**.

**Sibling sources referenced** (all under `D:\NNP\AI\`, read-only — none were modified):
- `ai-data-ingestion-service` — RAG ingestion (FastAPI). Source of the request-ID middleware + error envelope + Dockerfile.
- `ai-data-query-service` — hybrid RAG query (FastAPI).
- `ai-sql-query-observability-service` — NL→SQL (FastAPI). Source of the config/settings, logger, app bootstrap, run_api, k8s manifests, CI.

**Legend:** 🆕 New · ♻️ Inherited (adapted) · 📋 Inherited (near-verbatim)

---

## 1. Summary table

| File | Status | Source | One-line purpose |
|---|---|---|---|
| `src/config/settings.py` | ♻️ Inherited | sql-service `src/config/settings.py` | Env-driven settings (CoreComp PG + external-service flags) |
| `src/utils/logger.py` | 📋 Inherited | sql-service `src/utils/logger.py` | Stdlib logger + /health log suppression |
| `src/utils/validators.py` | 🆕 New | — | Milvus-name / status / inline-credential validation helpers |
| `src/db/pool.py` | 🆕 New | — | psycopg2 pool + `get_cursor()` with `search_path=nnp-rag` |
| `src/db/__init__.py` | 🆕 New | — | package marker |
| `src/models/common.py` | 🆕 New | — | Error envelope model |
| `src/models/bucket.py` | 🆕 New | — | manageBucket request models + validators |
| `src/models/bucket_detail.py` | 🆕 New | — | manageBucketDetails request models + validators |
| `src/models/question.py` | 🆕 New | — | manageQuestions request models + validators |
| `src/models/data_sql.py` | 🆕 New | — | manageDataSql request models + validators |
| `src/repositories/_common.py` | 🆕 New | — | SET-clause builder for partial updates |
| `src/repositories/bucket_repo.py` | 🆕 New | — | SQL for buckets + account mapping |
| `src/repositories/bucket_detail_repo.py` | 🆕 New | — | SQL for documents |
| `src/repositories/question_repo.py` | 🆕 New | — | SQL for curated Q&A |
| `src/repositories/data_sql_repo.py` | 🆕 New | — | SQL for databases + saved queries |
| `src/services/provision_client.py` | 🆕 New | — | Async client → Milvus-provision service (stubbed) |
| `src/services/ingestion_client.py` | 🆕 New | — | Async client → ingestion service (stubbed) |
| `src/api/deps.py` | 🆕 New | — | `current_user()` from PORTAL header |
| `src/api/routers/manage_bucket.py` | 🆕 New | — | manageBucket endpoints |
| `src/api/routers/manage_bucket_details.py` | 🆕 New | — | manageBucketDetails endpoints |
| `src/api/routers/manage_questions.py` | 🆕 New | — | manageQuestions endpoints |
| `src/api/routers/manage_data_sql.py` | 🆕 New | — | manageDataSql endpoints |
| `src/api/main.py` | ♻️ Inherited | ingestion `main.py` + sql `api/main.py` | App bootstrap, request-ID middleware, health, router includes |
| `run_api.py` | 📋 Inherited | sql-service `run_api.py` | uvicorn launcher |
| `requirements-api.txt` | ♻️ Inherited | ingestion `requirements-api.txt` | Python deps (trimmed + psycopg2/httpx added) |
| `Dockerfile.api` | ♻️ Inherited | ingestion `Dockerfile.api` | Container image |
| `.github/workflows/ci-cd.yml` | 🆕 New | — | GitHub Actions CI/CD (validation, pytest, SAST, SBOM, GHCR container push) |
| `k8s-manifest/deployment-api.yaml` | ♻️ Inherited | sql-service same file | API Deployment |
| `k8s-manifest/service-api.yaml` | ♻️ Inherited | sql-service same file | ClusterIP Service |
| `k8s-manifest/configmap.yaml` | ♻️ Inherited | sql-service same file | Non-secret config |
| `k8s-manifest/secret.yaml` | ♻️ Inherited | sql-service same file | Secret (PG password, placeholder) |
| `compose.yml` | 🆕 New | — | Local Postgres + API (schema auto-load) |
| `.env.sample` | 🆕 New | — | Sample environment file |
| `.gitignore` | 🆕 New | — | Python ignores |
| `README.md` | 🆕 New | — | Usage + endpoint list |
| `db/schema.sql` | 🆕 Canonical | Consolidated locally | Complete idempotent schema applied by GitLab before backend deployment |
| `ARTIFACTS.md` | 🆕 New | — | This document |
| `src/**/__init__.py` | 🆕 New | — | Package markers |

**Totals:** 26 new, 9 inherited (4 near-verbatim, 5 adapted).

---

## 2. Inherited artifacts — detail & changes

### `src/config/settings.py` ♻️
- **From:** `ai-sql-query-observability-service/src/config/settings.py`.
- **Kept:** the `pydantic-settings BaseSettings` + `Field(alias=...)` + inner `Config(env_file, populate_by_name, case_sensitive)` pattern; the `BaseAppSettings`/`ApiSettings` split.
- **Changed:** removed all OpenAI/Milvus/ClickHouse/Vanna/per-database settings (not relevant to CRUD). Added CoreComp Postgres settings (`PG_*`, incl. `PG_SCHEMA=nnp-rag` and pool sizes), identity header config (`USER_HEADER`, `DEFAULT_USER`), and external-service wiring/flags (`PROVISION_*`, `INGESTION_*`, `CALLBACK_BASE_URL`, `HTTP_TIMEOUT`). Set `extra="ignore"` and gave every field a default so the app boots without a `.env` (the sql-service version raised on missing `OPENAI_API_KEY`).

### `src/utils/logger.py` 📋
- **From:** `ai-sql-query-observability-service/src/utils/logger.py`.
- **Changed:** only the settings import path (`src.config.settings`). Added a safe `getattr(..., logging.INFO)` fallback for an unknown `LOG_LEVEL`. Logic otherwise identical.

### `src/api/main.py` ♻️
- **From:** the app-bootstrap shape of `ai-sql-query-observability-service/src/api/main.py` (FastAPI + `lifespan` + CORS + `/health` + `uvicorn.run`) **plus** the `request_trace_middleware` lifted from `ai-data-ingestion-service/main.py`.
- **Changed:** lifespan now initialises/closes the **Postgres pool** (the sql-service initialised a `QueryService`). The middleware now also stores `request_id` on `request.state`. Added `/health/deep` that pings the DB. Mounts the four CoreComp routers instead of a `/query` endpoint. No Milvus/Vanna/OpenAI references.

### `run_api.py` 📋
- **From:** `ai-sql-query-observability-service/run_api.py`.
- **Changed:** nothing functional — default `API_APP_PATH` already resolves to `src.api.main:app`. Copied for a consistent launch story.

### `requirements-api.txt` ♻️
- **From:** `ai-data-ingestion-service/requirements-api.txt`.
- **Changed:** dropped everything RAG/LLM/loader-specific (langchain, pymilvus, openai, google-*, gitpython, loguru, …). Kept `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `python-dotenv`. **Added** `psycopg2-binary` (CoreComp DB) and `httpx` (outbound async calls).

### `Dockerfile.api` ♻️
- **From:** `ai-data-ingestion-service/Dockerfile.api`.
- **Changed:** `CMD` runs `src.api.main:app` (sibling ran `main:app`). Removed `git` from system deps and the `data/git_repos` mkdir (not needed). Same `python:3.13-slim` base and layer-caching layout.

### `.github/workflows/ci-cd.yml` 🆕
- **Standardized CI/CD**: Replaced `.gitlab-ci.yml` with GitHub Actions workflow aligned with NNP platform conventions (`ghcr.io` registry, OCI metadata, Buildx layer caching).
- **Two Stages**: `validate-and-test` (Python 3.13, compileall, ruff lint, pytest, bandit SAST, pip-audit, CycloneDX SBOM) followed by `docker` container build and push to GHCR on `main`/`master` pushes and semver tags.

### `k8s-manifest/*.yaml` ♻️
- **From:** the sql-service `k8s-manifest/` set.
- **Changed:** renamed all objects to `ai-km-service*`; **dropped UI manifests**. `deployment-api.yaml` env block trimmed to CoreComp PG + external-service config (removed OpenAI/Milvus/ClickHouse/per-DB vars), `PG_PASSWORD` from secret, readiness probe points at `/health/deep`, smaller resource requests. `secret.yaml` contains only `PG_PASSWORD` as a **placeholder** — the sibling's committed OpenAI key and DB passwords were deliberately **not** copied.

---

## 3. New artifacts — detail & functionality

### Data access (net-new — no sibling owns a CRUD-over-its-own-Postgres store)
- **`src/db/pool.py`** — `ThreadedConnectionPool` (psycopg2) created in the FastAPI lifespan. `get_cursor()` context manager checks out a `RealDictCursor`, runs `SET search_path TO "nnp-rag", public`, yields, then commits (rollback on error) and returns the connection. Multiple statements in one `with` block share a transaction (used by `createBucket`). `ping()` backs `/health/deep`.
- **`src/repositories/_common.py`** — `build_set_clause(data, allowed)` turns the provided (exclude_unset) fields into a parameterised SQL `SET` clause for partial updates.
- **`src/repositories/bucket_repo.py`** — `get_by_account` (join through the M:N map), `create` (insert bucket as `PROVISIONING` + insert mapping, one transaction), `update` (attributes + optional full re-sync of `account_ids`), `set_provision_result` (callback), `get_by_id`.
- **`src/repositories/bucket_detail_repo.py`** — `get_by_bucket` (+ category/status filters), `create` (insert as `PENDING`), `update`, `apply_ingestion_result` (callback; `COALESCE`s the milvus_* fields), `get_by_id`.
- **`src/repositories/question_repo.py`** — `get_by_buckets` (`bucket_id = ANY(...)`), `create`, `update`, `get_by_id`.
- **`src/repositories/data_sql_repo.py`** — `get_by_buckets` (databases + nested `queries[]`), `create_database`/`update_database`, `create_sql`/`update_sql`, plus get-by-id helpers.

### Validation & models (net-new — DB is permissive, service is the gatekeeper)
- **`src/utils/validators.py`** — Milvus name regex, the three status vocabularies, and inline-credential detection.
- **`src/models/bucket.py`** — `BucketCreate` (validates Milvus name), `BucketUpdate` (no `bucket_name`; optional `account_ids`; status check), `ProvisionCallback`.
- **`src/models/bucket_detail.py`** — `BucketDetailCreate`, `BucketDetailUpdate` (status check; `reingest` flag), `IngestionCallback`.
- **`src/models/question.py`** — `QuestionCreate`/`QuestionUpdate` (rank 1–5, threshold 0–1, status check).
- **`src/models/data_sql.py`** — `DatabaseCreate`/`DatabaseUpdate` (reject inline-credential `connection_url`), `SqlQueryCreate`/`SqlQueryUpdate` (rank 1–5, quality 0–1, status check).
- **`src/models/common.py`** — `ErrorResponse` envelope.

### Routers (net-new — implement the four services' functions)
- **`manage_bucket.py`** — `getDetails/{account_id}`, `createBucket` (202 + background provision), `updateBucket/{id}` (attrs + account remap), `provisionCallback`.
- **`manage_bucket_details.py`** — `getDetails/{bucket_id}`, `createBucketDetails` (202 + background ingest), `updateBucketDetails/{id}` (re-ingest / soft-delete → delete_by_source), `ingestionCallback`.
- **`manage_questions.py`** — `getDetails?bucketIds=`, `addQDetails`, `updateQDetails/{id}`.
- **`manage_data_sql.py`** — `getDetails?bucketIds=`, `addDBDetails`/`updateDBDetails/{id}`, `addSQLDetails`/`updateSQLDetails/{id}`.

### External clients (net-new — stubbed behind feature flags)
- **`src/services/provision_client.py`** — `request_provision(bucket_id, collection_name)`. With `PROVISION_ENABLED=false` it only logs (bucket stays `PROVISIONING`); enabled, it POSTs to the provision service with a callback URL.
- **`src/services/ingestion_client.py`** — `request_ingestion(detail_id, payload)` and `request_delete_by_source(source_id)`. Same flag-gated stub behaviour (`INGESTION_ENABLED`).

### Misc new
- **`src/api/deps.py`** — `current_user(request)` reads the PORTAL `User` / `X-User-Name` header (default `system`).
- **`compose.yml`** — local Postgres initializes new volumes from `db/schema.sql` + the API.
- **`.env.sample`**, **`.gitignore`**, **`README.md`**, package `__init__.py` files.
- **`db/schema.sql`** — the sole schema source of truth, consolidating the former base DDL and migrations.

---

## 4. Behaviour with external services OFF (default)

The service is fully runnable today against just the `nnp-rag` Postgres schema:
- `createBucket` → row created as **`PROVISIONING`**; provision client logs and returns. Flip `PROVISION_ENABLED=true` (+ URL) to make it real; the provision service then calls `/manageBucket/provisionCallback` to set `ACTIVE`/`FAILED`.
- `createBucketDetails` → row created as **`PENDING`**; ingestion client logs and returns. Flip `INGESTION_ENABLED=true` to make it real; the ingestion service calls `/manageBucketDetails/ingestionCallback`.
- All reads/updates/validation work normally regardless of the flags.

## 5. Known dependencies on other teams (not in this repo)

- A **Milvus-provision service** + its callback contract (for `createBucket`).
- The ingestion service to expose an **async ingest + callback** path and an HTTP **`delete_by_source`** endpoint.
- A **Vanna service** that reads `nnp_km_database.training_script`.
- A **separate process** to populate `nnp_km_qa.question_embedding_id`.
