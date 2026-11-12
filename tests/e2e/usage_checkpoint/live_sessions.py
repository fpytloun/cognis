"""Real event-store identities for the isolated usage fixture's chat graph."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.store.models import Session


async def register_live_sessions(
    database: Path, facts: dict[str, Any], session_ids: tuple[str, ...]
) -> None:
    """Register persisted graph identities through authenticated Intaris creation."""
    endpoint = urlsplit(facts["intaris_url"])
    assert endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
    assert endpoint.port and not endpoint.username and not endpoint.password
    assert not endpoint.query and not endpoint.fragment and endpoint.path in {"", "/"}
    signer = JWTAuthProvider(
        database.parent / "keys/private.pem", database.parent / "keys/public.pem"
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            rows = {
                row.session_id: row
                for row in await db.scalars(
                    select(Session).where(Session.session_id.in_(session_ids))
                )
            }
        assert set(rows) == set(session_ids)
    finally:
        await engine.dispose()

    # Use the sealed 0.14.1 metadata API, not the production provider's newer
    # flag-acknowledgment contract. Default guardrails remain untouched.
    async with httpx.AsyncClient(
        base_url=facts["intaris_url"], timeout=30, trust_env=False
    ) as client:
        # Retained streams and inclusive work refer to the whole seeded graph,
        # not just its active stream. Preserve its parent ordering and ownership.
        for session_id in session_ids:
            row = rows[session_id]
            assert row.intaris_session_id in {None, session_id}
            headers = {
                "Authorization": "Bearer "
                + signer.sign_service_jwt(
                    row.user_email, row.agent_id, ["intaris"], agent_owner_email=row.user_email
                ),
                "X-Agent-Id": row.agent_id,
                "X-Agent-Owner": row.user_email,
            }
            created = await client.post(
                "/api/v1/intention",
                headers=headers,
                json={
                    "session_id": session_id,
                    "intention": "Read-only usage browser fixture",
                    "agent_id": row.agent_id,
                    "parent_session_id": row.parent_session_id,
                },
            )
            created.raise_for_status()
            response = await client.get(f"/api/v1/session/{session_id}", headers=headers)
            response.raise_for_status()
            stored = response.json()
            assert tuple(
                stored.get(key)
                for key in ("session_id", "user_id", "agent_id", "parent_session_id")
            ) == (session_id, row.user_email, row.agent_id, row.parent_session_id)
