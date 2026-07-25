"""Environment variable configuration.

Pure config loading — no side effects. All filesystem/DB operations
are in the bootstrap module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path


def _expand_path(path: str) -> Path:
    """Expand ~ and env vars in a path."""
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    value = default if raw is None else float(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _trusted_proxy_cidrs(raw: str) -> tuple[str, ...]:
    cidrs = tuple(value.strip() for value in raw.split(",") if value.strip())
    for cidr in cidrs:
        ip_network(cidr, strict=False)
    return cidrs


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
    public_mnemory_ui_url: str
    public_intaris_ui_url: str
    public_base_url: str

    # Database
    database_url: str

    # Keys
    jwt_private_key_path: Path
    jwt_public_key_path: Path
    secrets_key_path: Path

    # Logging
    log_level: str
    log_format: str

    # UI serving
    serve_ui: bool

    # CORS
    cors_origins: list[str]
    trusted_proxy_cidrs: tuple[str, ...]

    # Browser session cookies
    browser_session_ttl_seconds: int
    session_cookie_domain: str
    session_cookie_samesite: str

    # LSP diagnostics
    lsp_enabled: bool
    lsp_auto_install: bool
    lsp_diagnostics_timeout_ms: int
    lsp_idle_timeout_seconds: int
    lsp_max_concurrent_servers: int

    # Artifact store
    artifact_backend: str
    artifact_path: Path
    artifact_s3_endpoint: str
    artifact_s3_access_key: str
    artifact_s3_secret_key: str
    artifact_s3_bucket: str
    artifact_s3_region: str
    artifact_max_size_bytes: int
    artifact_signed_url_ttl_seconds: int
    artifact_signing_secret: str
    deliverable_share_link_ttl_seconds: int

    # Knowledgebase (optional)
    knowledgebase_vector_backend: str
    knowledgebase_qdrant_url: str
    knowledgebase_qdrant_api_key: str
    knowledgebase_qdrant_collection: str
    knowledgebase_index_poll_interval_seconds: float
    knowledgebase_max_artifact_size_bytes: int
    knowledgebase_max_chunks_per_artifact: int
    knowledgebase_chunk_target_tokens: int
    knowledgebase_chunk_overlap_tokens: int
    knowledgebase_embedding_batch_size: int

    # Production crypto
    require_external_crypto: bool

    # Web Push (VAPID)
    vapid_private_key: str
    vapid_public_key: str
    vapid_private_key_path: Path
    vapid_subject: str

    # Redis (session cache L2)
    redis_url: str

    # Tool output storage
    tool_output_backend: str
    tool_output_s3_endpoint: str
    tool_output_s3_access_key: str
    tool_output_s3_secret_key: str
    tool_output_s3_bucket: str
    tool_output_s3_region: str
    tool_output_ttl_hours: int
    tool_output_max_size_mb: int

    # Initial admin (container/CI bootstrap)
    initial_admin_email: str | None
    initial_admin_password: str | None

    # Default backend selection (overridden per-agent via AgentCapabilities)
    default_memory_backend: str
    default_guardrails_backend: str

    # Controller-owned MCP OAuth lifecycle
    mcp_oauth_refresh_timeout_seconds: float

    # Test-only control-plane routes
    e2e_mode: bool = False


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

    serve_ui_raw = os.environ.get("COGNIS_SERVE_UI", "true").strip().lower()
    serve_ui = serve_ui_raw not in {"0", "false", "no", "off"}
    e2e_mode = os.environ.get("COGNIS_E2E_MODE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    lsp_enabled_raw = os.environ.get("COGNIS_LSP_ENABLED", "true").strip().lower()
    lsp_auto_install_raw = os.environ.get("COGNIS_LSP_AUTO_INSTALL", "true").strip().lower()

    mnemory_url = os.environ.get("COGNIS_MNEMORY_URL", "http://localhost:8050").rstrip("/")
    intaris_url = os.environ.get("COGNIS_INTARIS_URL", "http://localhost:8060").rstrip("/")
    public_mnemory_ui_url = os.environ.get("PUBLIC_MNEMORY_UI_URL", mnemory_url).rstrip("/")
    public_intaris_ui_url = os.environ.get("PUBLIC_INTARIS_UI_URL", intaris_url).rstrip("/")

    return CognisConfig(
        data_dir=data_dir,
        host=os.environ.get("COGNIS_HOST", "0.0.0.0"),
        port=int(os.environ.get("COGNIS_PORT", "8080")),
        mnemory_url=mnemory_url,
        intaris_url=intaris_url,
        public_mnemory_ui_url=public_mnemory_ui_url,
        public_intaris_ui_url=public_intaris_ui_url,
        public_base_url=os.environ.get("COGNIS_PUBLIC_BASE_URL", "").rstrip("/"),
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
        serve_ui=serve_ui,
        e2e_mode=e2e_mode,
        cors_origins=cors_origins,
        trusted_proxy_cidrs=_trusted_proxy_cidrs(os.environ.get("COGNIS_TRUSTED_PROXY_CIDRS", "")),
        browser_session_ttl_seconds=int(
            os.environ.get("COGNIS_BROWSER_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60))
        ),
        session_cookie_domain=os.environ.get("COGNIS_SESSION_COOKIE_DOMAIN", "").strip(),
        session_cookie_samesite=os.environ.get("COGNIS_SESSION_COOKIE_SAMESITE", "lax")
        .strip()
        .lower(),
        lsp_enabled=lsp_enabled_raw not in {"0", "false", "no", "off"},
        lsp_auto_install=lsp_auto_install_raw not in {"0", "false", "no", "off"},
        lsp_diagnostics_timeout_ms=int(
            os.environ.get("COGNIS_LSP_DIAGNOSTICS_TIMEOUT_MS", "10000")
        ),
        lsp_idle_timeout_seconds=int(os.environ.get("COGNIS_LSP_IDLE_TIMEOUT_SECONDS", "600")),
        lsp_max_concurrent_servers=int(os.environ.get("COGNIS_LSP_MAX_CONCURRENT_SERVERS", "8")),
        artifact_backend=os.environ.get("COGNIS_ARTIFACT_BACKEND", "filesystem"),
        artifact_path=_expand_path(
            os.environ.get("COGNIS_ARTIFACT_PATH", str(data_dir / "artifacts"))
        ),
        artifact_s3_endpoint=os.environ.get("COGNIS_ARTIFACT_S3_ENDPOINT", "http://localhost:9000"),
        artifact_s3_access_key=os.environ.get("COGNIS_ARTIFACT_S3_ACCESS_KEY", ""),
        artifact_s3_secret_key=os.environ.get("COGNIS_ARTIFACT_S3_SECRET_KEY", ""),
        artifact_s3_bucket=os.environ.get("COGNIS_ARTIFACT_S3_BUCKET", "cognis-artifacts"),
        artifact_s3_region=os.environ.get("COGNIS_ARTIFACT_S3_REGION", ""),
        artifact_max_size_bytes=int(os.environ.get("COGNIS_ARTIFACT_MAX_SIZE_MB", "50"))
        * 1024
        * 1024,
        artifact_signed_url_ttl_seconds=int(
            os.environ.get("COGNIS_ARTIFACT_SIGNED_URL_TTL_SECONDS", "3600")
        ),
        artifact_signing_secret=os.environ.get("COGNIS_ARTIFACT_SIGNING_SECRET", ""),
        deliverable_share_link_ttl_seconds=int(
            os.environ.get("COGNIS_DELIVERABLE_SHARE_LINK_TTL_SECONDS", str(7 * 24 * 60 * 60))
        ),
        knowledgebase_vector_backend=os.environ.get(
            "COGNIS_KNOWLEDGEBASE_VECTOR_BACKEND", "disabled"
        )
        .strip()
        .lower(),
        knowledgebase_qdrant_url=os.environ.get(
            "COGNIS_KNOWLEDGEBASE_QDRANT_URL", "http://localhost:6333"
        ).strip(),
        knowledgebase_qdrant_api_key=os.environ.get(
            "COGNIS_KNOWLEDGEBASE_QDRANT_API_KEY", ""
        ).strip(),
        knowledgebase_qdrant_collection=os.environ.get(
            "COGNIS_KNOWLEDGEBASE_QDRANT_COLLECTION", "cognis_knowledgebase_chunks"
        ).strip(),
        knowledgebase_index_poll_interval_seconds=float(
            os.environ.get("COGNIS_KNOWLEDGEBASE_INDEX_POLL_INTERVAL_SECONDS", "5")
        ),
        knowledgebase_max_artifact_size_bytes=int(
            os.environ.get("COGNIS_KNOWLEDGEBASE_MAX_ARTIFACT_SIZE_MB", "50")
        )
        * 1024
        * 1024,
        knowledgebase_max_chunks_per_artifact=int(
            os.environ.get("COGNIS_KNOWLEDGEBASE_MAX_CHUNKS_PER_ARTIFACT", "2000")
        ),
        knowledgebase_chunk_target_tokens=int(
            os.environ.get("COGNIS_KNOWLEDGEBASE_CHUNK_TARGET_TOKENS", "800")
        ),
        knowledgebase_chunk_overlap_tokens=int(
            os.environ.get("COGNIS_KNOWLEDGEBASE_CHUNK_OVERLAP_TOKENS", "100")
        ),
        knowledgebase_embedding_batch_size=int(
            os.environ.get("COGNIS_KNOWLEDGEBASE_EMBEDDING_BATCH_SIZE", "32")
        ),
        require_external_crypto=os.environ.get("COGNIS_REQUIRE_EXTERNAL_CRYPTO", "false")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
        vapid_private_key=os.environ.get("COGNIS_VAPID_PRIVATE_KEY", "").strip(),
        vapid_public_key=os.environ.get("COGNIS_VAPID_PUBLIC_KEY", "").strip(),
        vapid_private_key_path=_expand_path(
            os.environ.get(
                "COGNIS_VAPID_PRIVATE_KEY_PATH",
                str(data_dir / "keys" / "vapid_private.pem"),
            )
        ),
        vapid_subject=os.environ.get("COGNIS_VAPID_SUBJECT", "mailto:admin@localhost").strip(),
        redis_url=os.environ.get("COGNIS_REDIS_URL", ""),
        tool_output_backend=os.environ.get("COGNIS_TOOL_OUTPUT_BACKEND", "filesystem"),
        tool_output_s3_endpoint=os.environ.get(
            "COGNIS_TOOL_OUTPUT_S3_ENDPOINT", "http://localhost:9000"
        ),
        tool_output_s3_access_key=os.environ.get("COGNIS_TOOL_OUTPUT_S3_ACCESS_KEY", ""),
        tool_output_s3_secret_key=os.environ.get("COGNIS_TOOL_OUTPUT_S3_SECRET_KEY", ""),
        tool_output_s3_bucket=os.environ.get("COGNIS_TOOL_OUTPUT_S3_BUCKET", "cognis-tool-outputs"),
        tool_output_s3_region=os.environ.get("COGNIS_TOOL_OUTPUT_S3_REGION", ""),
        tool_output_ttl_hours=int(os.environ.get("COGNIS_TOOL_OUTPUT_TTL_HOURS", "168")),
        tool_output_max_size_mb=int(os.environ.get("COGNIS_TOOL_OUTPUT_MAX_SIZE_MB", "500")),
        initial_admin_email=os.environ.get("COGNIS_INITIAL_ADMIN_EMAIL"),
        initial_admin_password=os.environ.get("COGNIS_INITIAL_ADMIN_PASSWORD"),
        default_memory_backend=os.environ.get("COGNIS_DEFAULT_MEMORY_BACKEND", "mnemory"),
        default_guardrails_backend=os.environ.get("COGNIS_DEFAULT_GUARDRAILS_BACKEND", "intaris"),
        mcp_oauth_refresh_timeout_seconds=_bounded_float_env(
            "COGNIS_MCP_OAUTH_REFRESH_TIMEOUT_SECONDS",
            30.0,
            minimum=5.0,
            maximum=120.0,
        ),
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
# COGNIS_REQUIRE_EXTERNAL_CRYPTO=false

# Web Push / PWA notifications (mount from Kubernetes Secret in stateless deployments)
# COGNIS_VAPID_PRIVATE_KEY=
# COGNIS_VAPID_PUBLIC_KEY=
# COGNIS_VAPID_PRIVATE_KEY_PATH=~/.cognis/keys/vapid_private.pem
# Must be a real operator contact URI (RFC 8292), for example mailto:ops@example.com
# or https://example.com/contact. Some push services reject localhost subjects.
# COGNIS_VAPID_SUBJECT=mailto:admin@localhost

# Logging
# COGNIS_LOG_LEVEL=info
# COGNIS_LOG_FORMAT=json

# UI serving
# COGNIS_SERVE_UI=true

# CORS
# COGNIS_CORS_ORIGINS=http://localhost:5173

# Browser session cookies
# COGNIS_BROWSER_SESSION_TTL_SECONDS=2592000
# COGNIS_SESSION_COOKIE_DOMAIN=
# COGNIS_SESSION_COOKIE_SAMESITE=lax

# MCP OAuth refresh (controller-owned, wall-clock bounded)
# COGNIS_MCP_OAUTH_REFRESH_TIMEOUT_SECONDS=30

# LSP diagnostics (auto-detect language servers for edit feedback)
# COGNIS_LSP_ENABLED=true
# COGNIS_LSP_AUTO_INSTALL=true
# COGNIS_LSP_DIAGNOSTICS_TIMEOUT_MS=10000
# COGNIS_LSP_IDLE_TIMEOUT_SECONDS=600
# COGNIS_LSP_MAX_CONCURRENT_SERVERS=8

# Artifact store (images, generated content)
# COGNIS_ARTIFACT_BACKEND=filesystem
# COGNIS_ARTIFACT_PATH=~/.cognis/artifacts
# COGNIS_ARTIFACT_S3_ENDPOINT=http://localhost:9000
# COGNIS_ARTIFACT_S3_ACCESS_KEY=
# COGNIS_ARTIFACT_S3_SECRET_KEY=
# COGNIS_ARTIFACT_S3_BUCKET=cognis-artifacts
# COGNIS_ARTIFACT_S3_REGION=
# COGNIS_ARTIFACT_MAX_SIZE_MB=50
# COGNIS_ARTIFACT_SIGNED_URL_TTL_SECONDS=3600
# COGNIS_ARTIFACT_SIGNING_SECRET=
# COGNIS_DELIVERABLE_SHARE_LINK_TTL_SECONDS=604800

# Knowledgebase (optional; hidden unless backend and embedding route are configured)
# COGNIS_KNOWLEDGEBASE_VECTOR_BACKEND=disabled
# COGNIS_KNOWLEDGEBASE_QDRANT_URL=http://localhost:6333
# COGNIS_KNOWLEDGEBASE_QDRANT_API_KEY=
# COGNIS_KNOWLEDGEBASE_QDRANT_COLLECTION=cognis_knowledgebase_chunks
# COGNIS_KNOWLEDGEBASE_INDEX_POLL_INTERVAL_SECONDS=5
# COGNIS_KNOWLEDGEBASE_MAX_ARTIFACT_SIZE_MB=50
# COGNIS_KNOWLEDGEBASE_MAX_CHUNKS_PER_ARTIFACT=2000
# COGNIS_KNOWLEDGEBASE_EMBEDDING_BATCH_SIZE=32

# Redis (session cache L2 — empty = L1-only)
# COGNIS_REDIS_URL=redis://localhost:6379/0

# Tool output storage
# COGNIS_TOOL_OUTPUT_BACKEND=filesystem
# COGNIS_TOOL_OUTPUT_S3_ENDPOINT=http://localhost:9000
# COGNIS_TOOL_OUTPUT_S3_ACCESS_KEY=
# COGNIS_TOOL_OUTPUT_S3_SECRET_KEY=
# COGNIS_TOOL_OUTPUT_S3_BUCKET=cognis-tool-outputs
# COGNIS_TOOL_OUTPUT_S3_REGION=
# COGNIS_TOOL_OUTPUT_TTL_HOURS=168
# COGNIS_TOOL_OUTPUT_MAX_SIZE_MB=500

# Container/CI: auto-create admin on first start
# COGNIS_INITIAL_ADMIN_EMAIL=admin@example.com
# COGNIS_INITIAL_ADMIN_PASSWORD=changeme
"""
