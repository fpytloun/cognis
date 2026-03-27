"""Environment variable configuration.

Pure config loading — no side effects. All filesystem/DB operations
are in the bootstrap module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _expand_path(path: str) -> Path:
    """Expand ~ and env vars in a path."""
    return Path(os.path.expandvars(os.path.expanduser(path)))


@dataclass(frozen=True)
class CognisConfig:
    """Typed configuration loaded from environment variables."""

    # Core
    data_dir: Path
    host: str
    port: int

    # Service URLs
    mnemory_url: str
    intaris_url: str

    # Database
    database_url: str

    # Keys
    jwt_private_key_path: Path
    jwt_public_key_path: Path
    secrets_key_path: Path

    # Logging
    log_level: str
    log_format: str

    # CORS
    cors_origins: list[str]

    # Initial admin (container/CI bootstrap)
    initial_admin_email: str | None
    initial_admin_password: str | None


def load_config() -> CognisConfig:
    """Load configuration from environment variables.

    All values have sensible defaults for local development.
    This function is pure — no side effects.
    """
    data_dir = _expand_path(os.environ.get("COGNIS_DATA_DIR", "~/.cognis"))

    # Derive default database URL from data dir
    default_db_url = f"sqlite+aiosqlite:///{data_dir / 'cognis.db'}"

    cors_raw = os.environ.get("COGNIS_CORS_ORIGINS", "http://localhost:5173")
    cors_origins = [origin.strip() for origin in cors_raw.split(",") if origin.strip()]

    return CognisConfig(
        data_dir=data_dir,
        host=os.environ.get("COGNIS_HOST", "0.0.0.0"),
        port=int(os.environ.get("COGNIS_PORT", "8080")),
        mnemory_url=os.environ.get("COGNIS_MNEMORY_URL", "http://localhost:8050"),
        intaris_url=os.environ.get("COGNIS_INTARIS_URL", "http://localhost:8060"),
        database_url=os.environ.get("DATABASE_URL", default_db_url),
        jwt_private_key_path=_expand_path(
            os.environ.get(
                "COGNIS_JWT_PRIVATE_KEY_PATH",
                str(data_dir / "keys" / "private.pem"),
            )
        ),
        jwt_public_key_path=_expand_path(
            os.environ.get(
                "COGNIS_JWT_PUBLIC_KEY_PATH",
                str(data_dir / "keys" / "public.pem"),
            )
        ),
        secrets_key_path=_expand_path(
            os.environ.get(
                "COGNIS_SECRETS_KEY_PATH",
                str(data_dir / "secrets.key"),
            )
        ),
        log_level=os.environ.get("COGNIS_LOG_LEVEL", "info"),
        log_format=os.environ.get("COGNIS_LOG_FORMAT", "json"),
        cors_origins=cors_origins,
        initial_admin_email=os.environ.get("COGNIS_INITIAL_ADMIN_EMAIL"),
        initial_admin_password=os.environ.get("COGNIS_INITIAL_ADMIN_PASSWORD"),
    )


# Environment variable template for `cognis config init`
ENV_TEMPLATE = """\
# Cognis Configuration
# All values shown are defaults. Uncomment and modify as needed.

# Core
# COGNIS_DATA_DIR=~/.cognis
# COGNIS_HOST=0.0.0.0
# COGNIS_PORT=8080

# Service URLs
# COGNIS_MNEMORY_URL=http://localhost:8050
# COGNIS_INTARIS_URL=http://localhost:8060

# Database (default: SQLite in data dir)
# DATABASE_URL=sqlite+aiosqlite:///~/.cognis/cognis.db
# DATABASE_URL=postgresql+asyncpg://cognis:password@localhost:5432/cognis

# Keys (auto-generated if missing)
# COGNIS_JWT_PRIVATE_KEY_PATH=~/.cognis/keys/private.pem
# COGNIS_JWT_PUBLIC_KEY_PATH=~/.cognis/keys/public.pem
# COGNIS_SECRETS_KEY_PATH=~/.cognis/secrets.key

# Logging
# COGNIS_LOG_LEVEL=info
# COGNIS_LOG_FORMAT=json

# CORS
# COGNIS_CORS_ORIGINS=http://localhost:5173

# Container/CI: auto-create admin on first start
# COGNIS_INITIAL_ADMIN_EMAIL=admin@example.com
# COGNIS_INITIAL_ADMIN_PASSWORD=changeme
"""
