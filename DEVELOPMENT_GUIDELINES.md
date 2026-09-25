# Development Guidelines and Contribution Standards: `PICC-AIKM-AIKM-backend`

This document defines the architectural standards, development workflows, coding conventions, and security requirements for contributors to **`PICC-AIKM-AIKM-backend`**.

---

## Table of Contents

1. [Architecture and Design Principles](#1-architecture-and-design-principles)
2. [Development Environment Setup](#2-development-environment-setup)
3. [Package Structure and Code Navigation](#3-package-structure-and-code-navigation)
4. [Coding Standards and Best Practices](#4-coding-standards-and-best-practices)
   - [FastAPI Router Boundaries](#fastapi-router-boundaries)
   - [Configuration Loading and Placeholders](#configuration-loading-and-placeholders)
   - [PostgreSQL Repository Practices](#postgresql-repository-practices)
   - [External Service Integrations](#external-service-integrations)
   - [Exception Handling Conventions](#exception-handling-conventions)
   - [Logging and Sensitive Data Masking](#logging-and-sensitive-data-masking)
5. [Security, Code Quality, and Compliance Tooling](#5-security-code-quality-and-compliance-tooling)
   - [Tests](#tests)
   - [Dependency Review](#dependency-review)
   - [Container Verification](#container-verification)
   - [Configuration Hygiene](#configuration-hygiene)
6. [Git Workflow and Branching Strategy](#6-git-workflow-and-branching-strategy)
   - [Branch Naming Conventions](#branch-naming-conventions)
   - [Conventional Commits](#conventional-commits)
7. [Pull Request Checklist](#7-pull-request-checklist)
8. [Release Lifecycle and Versioning](#8-release-lifecycle-and-versioning)

---

## 1. Architecture and Design Principles

`PICC-AIKM-AIKM-backend` serves as the FastAPI backend for AI Knowledge Management metadata and knowledge-search asset proxying. All modifications must comply with these core tenets:

1. **Zero-Trust Configuration**: Never commit private IP addresses, internal hostnames, real tokens, production passwords, or environment-specific URLs. Use placeholders in documentation and sample files.
2. **Layered Separation of Concerns**: API routers should validate HTTP inputs and delegate work. Business orchestration belongs in services. SQL persistence belongs in repositories.
3. **Explicit Runtime Configuration**: Add new runtime settings through `src/config/settings.py`, document them, and validate required values at startup when the service cannot operate without them.
4. **Database Safety**: Use parameterized SQL and repository helpers. Do not concatenate user-provided values into SQL statements.
5. **Resilient Integrations**: Provisioning, ingestion, RAG query, MinIO, Milvus, Ollama, and LLM provider calls must use explicit timeouts and clear failure behavior.
6. **Local Development Isolation**: Local runs should be possible with Docker Compose or a local PostgreSQL database. Optional downstream services must remain disabled unless explicitly configured.
7. **Operational Traceability**: Preserve request tracing and health endpoints. Changes should not remove `X-Request-ID`, `/health`, or `/health/deep` behavior without an approved replacement.

---

## 2. Development Environment Setup

### Required Tools

- **Python 3.13** recommended for parity with `Dockerfile.api`.
- **Docker Desktop** or Docker Engine with Docker Compose.
- **PostgreSQL 16** for local database parity with `compose.yml`.
- **PostgreSQL client tools** if manually applying `db/schema.sql`.
- **IDE**: VS Code, PyCharm, or another Python-aware editor with linting and formatting support.

### Local Setup

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements-api.txt
```

On Linux or macOS:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-api.txt
```

Create local configuration:

```bash
copy .env.sample .env
```

Use placeholders or local-only values. Do not commit `.env`.

Run the full local stack:

```bash
docker compose up --build
```

Run against an existing database:

```bash
python run_api.py
```

---

## 3. Package Structure and Code Navigation

```text
src/
├── api/
│   ├── main.py                  # FastAPI app, middleware, health routes, router mounting
│   ├── deps.py                  # Shared API dependencies
│   └── routers/                 # HTTP route modules grouped by feature
├── config/
│   ├── config_server.py         # Config-server bootstrap and local override loading
│   └── settings.py              # Pydantic settings, required-key validation
├── db/
│   └── pool.py                  # psycopg2 threaded pool and cursor helper
├── models/                      # Pydantic request/response/domain models
├── repositories/                # SQL persistence functions and repository helpers
├── services/                    # Business logic and downstream integrations
└── utils/                       # Logging, validators, and shared utilities

db/
└── schema.sql                   # PostgreSQL schema used by local and deployment flows

tests/                           # pytest API, model, and validation tests
```

---

## 4. Coding Standards and Best Practices

### FastAPI Router Boundaries

- Keep route functions focused on HTTP concerns: path/query/body parameters, response status, and dependency wiring.
- Move reusable orchestration to `src/services/`.
- Move SQL reads and writes to `src/repositories/`.
- Use Pydantic models from `src/models/` for request and response structure.
- For blocking database operations inside async routes, use FastAPI's threadpool helpers or keep the blocking work isolated behind service/repository boundaries already designed for that usage.

### Configuration Loading and Placeholders

Runtime settings are resolved through config-server bootstrap values and allowed local overrides.

- Add new settings to `ApiSettings` in `src/config/settings.py`.
- Add required settings to `REQUIRED_CONFIG_KEYS` only when startup should fail without them.
- Add conditional settings to `CONDITIONAL_REQUIRED_CONFIG_KEYS` when they depend on a feature flag.
- Add sensitive settings to `SECRET_CONFIG_KEYS` so they are treated consistently.
- Keep `.env.sample` sanitized. Use placeholders such as `<CONFIG_SERVER_URL>`, `<POSTGRES_PASSWORD>`, and `<RAG_QUERY_SERVICE_URL>`.

Correct documentation example:

```env
PG_HOST=<POSTGRES_HOST>
PG_PASSWORD=<POSTGRES_PASSWORD>
MINIO_ROOT_PASSWORD=<MINIO_ROOT_PASSWORD>
```

Avoid:

```env
PG_PASSWORD=<REAL_PASSWORD>
CONFIG_SERVER_URL=<REAL_PRIVATE_CONFIG_SERVER_URL>
```

### PostgreSQL Repository Practices

- Use the connection pool in `src/db/pool.py`; do not create ad hoc database connections in routers.
- Keep schema ownership in deployment and `db/schema.sql`. Application startup must not silently create or mutate schema objects.
- Use parameterized queries with named parameters.
- Reuse helpers such as `build_set_clause` for update statements.
- Keep multiple statements in the same `with get_cursor()` block when they must be committed or rolled back together.
- Do not include untrusted values in SQL identifiers. If a new dynamic identifier is unavoidable, use a strict allowlist.

### External Service Integrations

- Keep provisioning-service calls in `src/services/provision_client.py`.
- Keep ingestion-service calls in `src/services/ingestion_client.py`.
- Keep MinIO access in `src/services/minio_storage.py`.
- Keep Milvus access in `src/services/milvus_client.py`.
- Keep RAG query service behavior in the knowledge/search service path.
- Use `settings.http_timeout` or another documented timeout for outbound requests.
- Feature-flag optional integrations. Local CRUD flows should work with `PROVISION_ENABLED=false` and `INGESTION_ENABLED=false`.

### Exception Handling Conventions

- Return clear HTTP errors for validation and not-found cases.
- Do not expose database connection strings, credentials, stack traces, or downstream service secrets in API responses.
- Preserve the request tracing middleware in `src/api/main.py` so unhandled failures include a request id.
- Prefer specific exception handling near the integration boundary so logs explain which dependency failed.

### Logging and Sensitive Data Masking

- Use `src/utils/logger.py` for consistent logger setup.
- Never log passwords, API keys, bearer tokens, config-server credentials, full connection strings, or MinIO credentials.
- Avoid logging full request bodies for endpoints that may contain database connection details, SQL text, user content, or document metadata.
- Logging configuration values is acceptable only for non-sensitive operational fields such as hostnames that are safe local placeholders or approved public values.

---

## 5. Security, Code Quality, and Compliance Tooling

### Tests

Run the test suite before opening a pull request:

```bash
pytest
```

The current tests patch the FastAPI lifespan database pool so API and model tests do not require a live PostgreSQL instance.

### Dependency Review

- Add runtime dependencies only to `requirements-api.txt`.
- Add development-only dependencies only to `requirements-dev.txt`.
- Check new dependencies for license compatibility and known security issues before merging.
- Keep version pins where compatibility matters, especially for database, vector, LLM, or parsing libraries.

### Container Verification

For changes to `Dockerfile.api`, `compose.yml`, dependencies, startup settings, or database initialization, verify:

```bash
docker compose up --build
curl http://localhost:8000/health
curl http://localhost:8000/health/deep
```

### Configuration Hygiene

Before committing, scan docs and sample files for real values:

```bash
rg -n "<REAL_|PRIVATE_|SECRET_|TOKEN_|PASSWORD_>" .
```

Investigate every match. Some local-only examples such as `localhost` are acceptable; real internal values are not.

### Continuous Integration and Deployment (CI/CD)

The repository uses GitHub Actions defined in `.github/workflows/ci-cd.yml` with two automated stages:

1. **`validate-and-test`** (runs on all PRs and pushes to `main`, `master`, `develop`, `v*.*.*`):
   - Sets up Python 3.13 with pip dependency caching.
   - Verifies bytecode compilation (`python -m compileall src tests`).
   - Runs linting and style checks (`ruff check src tests`).
   - Executes the full test suite with mocked environment isolation (`pytest`).
   - Runs SAST security scanning (`bandit`) and dependency vulnerability checks (`pip-audit`).
   - Generates and uploads a CycloneDX Software Bill of Materials (`bom.json`).

2. **`docker`** (runs on pushes to `main`/`master` and release tags `v*.*.*` after validation passes):
   - Authenticates to GitHub Container Registry (`ghcr.io`) using `GITHUB_TOKEN` (`packages: write` permission).
   - Generates OCI metadata tags (`latest`, `semver`, short SHA).
   - Builds and publishes the container image from `Dockerfile.api` using Docker Buildx and GitHub Actions layer caching.

---

## 6. Git Workflow and Branching Strategy

### Branch Naming Conventions

- `feature/<short-description>`
- `fix/<short-description>`
- `docs/<short-description>`
- `refactor/<short-description>`
- `chore/<short-description>`

Examples:

```text
feature/bucket-provision-callback
fix/deep-health-db-timeout
docs/local-setup-guide
refactor/repository-update-helper
```

### Conventional Commits

Use clear, scoped commit messages:

```text
feat(bucket): add provisioning callback validation
fix(config): reject missing rag query service url
docs(readme): document docker compose startup
test(models): cover bucket detail status validation
refactor(db): reuse cursor helper for SQL metadata updates
```

---

## 7. Pull Request Checklist

Before submitting a pull request, ensure:

- [ ] `pytest` passes with zero failures.
- [ ] The service starts locally through the relevant path for your change.
- [ ] API changes are reflected in README or API workflow documentation when needed.
- [ ] New or changed configuration keys are documented.
- [ ] No real secrets, private URLs, internal hostnames, or production `.env` values are committed.
- [ ] Database schema changes are explicit in `db/schema.sql` and called out in the PR.
- [ ] Optional downstream integrations remain disabled by default for local development.
- [ ] Dependency changes are justified and reviewed.
- [ ] Logs and API responses do not expose sensitive values.

---

## 8. Release Lifecycle and Versioning

- Build artifacts are produced from `Dockerfile.api`.
- The API image should be built from the committed source state and tagged by the CI/CD pipeline.
- Database schema application must be coordinated with service deployment when `db/schema.sql` changes.
- Kubernetes changes under `k8s-manifest/` should be reviewed with the same care as application changes.
- Release notes should mention API changes, schema changes, new configuration keys, changed dependency behavior, and operational follow-up.
- Do not promote a release if the image cannot start, `/health` fails, or `/health/deep` fails in the target environment.
