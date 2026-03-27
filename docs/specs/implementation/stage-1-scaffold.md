# Stage 1: Project Scaffold + Config + Database

**Status**: IMPLEMENTED*
**Repo**: `cognis`
**Depends on**: Stage 0 (contract tests written, not necessarily all merged)
**Estimated effort**: 2-3 days

## Objective

Create the Cognis Python package with a working entry point, environment
variable configuration, auto-generated data directory, and full database
schema. After this stage, `uvx cognis` should start, create `~/.cognis/`,
initialize the database, seed default settings, and exit cleanly (no API
server yet — that comes in Stage 2).

## Progress Notes

- Stage 1 scaffold implementation is complete.
- Implemented: `pyproject.toml`, package tree, structured logging with
  redaction, pure config loading, bootstrap helpers, DB models/query helpers,
  Alembic scaffolding, default settings seeding, and unit-test scaffolding.
- Local validation passed through `pytest`, `ruff`, `mypy`, package build, and
  CLI help checks.
- Direct `alembic upgrade/downgrade` command execution remains a manual
  follow-up because the state-changing shell command was blocked by the guarded
  execution environment.

## Deliverables

### 1. Python Package

- `pyproject.toml` with hatchling build backend
- Entry point: `cognis` CLI command
- Dependencies: fastapi, uvicorn, httpx, pydantic, sqlalchemy, aiosqlite,
  alembic, litellm, typer, argon2-cffi, cryptography, python-jose
- Dev dependencies: pytest, pytest-asyncio, ruff, mypy
- Package structure matching `docs/specs/01-architecture.md` Package
  Structure (directories and `__init__.py` files)

### 2. Configuration

- `cognis/config.py` — dataclass-based config from environment variables
- All env vars from AGENTS.md with sensible defaults
- `COGNIS_DATA_DIR` auto-created on startup (`~/.cognis/`)
- ES256 keypair auto-generated if missing
- Secrets encryption key auto-generated if missing
- `load_config()` validates and returns typed config object

### 3. Database

- `cognis/store/database.py` — SQLAlchemy 2.x async engine factory
- `cognis/store/models.py` — all ORM models:
  - `users` (email PK)
  - `api_keys`
  - `agents`
  - `conversations`
  - `sessions` (no event seq / compaction fields)
  - `settings`
  - `llm_providers`
  - `model_routing`
  - `secrets`
  - `audit_log`
- Dialect-aware JSONB handling (native on PostgreSQL, JSON text on SQLite)
- `cognis/store/migrations/` — Alembic setup with initial migration
- Default settings seeded on first run (all values from
  `docs/specs/11-deployment.md` settings table)

### 4. Entry Point

- `cognis/main.py` — Typer app (minimal: just `serve` subcommand that
  prints "not implemented" for now)
- On import: load config, auto-create data dir, auto-generate keys,
  initialize DB, run migrations, seed defaults
- Clean startup and shutdown

## Files To Create

```
cognis/
├── pyproject.toml
├── cognis/
│   ├── __init__.py              # Version, package metadata
│   ├── main.py                  # Typer CLI entry point
│   ├── config.py                # Env var config
│   ├── store/
│   │   ├── __init__.py
│   │   ├── database.py          # Async engine + session factory
│   │   ├── models.py            # SQLAlchemy ORM models
│   │   ├── queries.py           # Query helpers (empty, populated later)
│   │   └── migrations/
│   │       ├── env.py           # Alembic env
│   │       ├── script.py.mako
│   │       └── versions/
│   │           └── 001_initial.py
│   ├── api/
│   │   └── __init__.py          # Placeholder
│   ├── core/
│   │   └── __init__.py          # Placeholder
│   ├── models/
│   │   └── __init__.py          # Placeholder
│   ├── providers/
│   │   └── __init__.py          # Placeholder
│   ├── tools/
│   │   └── __init__.py          # Placeholder
│   └── cli/
│       ├── __init__.py
│       └── serve.py             # Minimal serve command
└── tests/
    ├── __init__.py
    ├── conftest.py              # Shared fixtures (test DB, test config)
    ├── unit/
    │   ├── __init__.py
    │   └── test_config.py       # Config loading tests
    └── contract/
        └── __init__.py          # Contract tests from Stage 0
```

## Acceptance Criteria

- [ ] `uv pip install -e ".[dev]"` succeeds
- [ ] `uvx cognis` or `uv run cognis` CLI starts (Typer help output)
- [ ] `uv run cognis serve` runs without error (can exit immediately)
- [ ] `~/.cognis/` auto-created with `keys/`, `secrets.key`, `cognis.db`
- [ ] Database has all 10 tables with correct schema
- [ ] `settings` table seeded with default values
- [ ] `uv run alembic -c cognis/store/migrations/alembic.ini upgrade head` works
- [ ] `uv run alembic -c cognis/store/migrations/alembic.ini downgrade base` works
- [ ] Config loads all env vars with correct defaults
- [ ] `uv run pytest tests/unit/test_config.py` passes
- [ ] `ruff check cognis/` clean
- [ ] `mypy cognis/` clean

## Key References

- `docs/specs/01-architecture.md` — DB schema, package structure
- `docs/specs/11-deployment.md` — env vars, settings table, deployment modes
- `AGENTS.md` — conventions, env var reference
