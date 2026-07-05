#!/usr/bin/env python3
"""Seed a deterministic E2E test environment.

Creates:
- A capability-off agent (memory_backend=none, guardrails_backend=none)
- An LLM provider pointing at the mock-llm server
- Pre-seeded conversations for the refresh-mid-and-post-turn scenario
- Model routing for the mock provider

Idempotent — safe to re-run.
"""

from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.logging import setup_logging
from cognis.models.agent import AgentCapabilities
from cognis.models.session import SessionEvent
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.security import create_password_hasher
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_llm_provider,
    create_session,
    create_user,
    get_agent,
    get_conversation,
    get_llm_provider,
    get_user,
    update_agent,
    update_conversation_active_session,
    update_llm_provider,
    update_user,
    upsert_model_routing,
)

E2E_PROVIDER_ID = "e2e-mock-llm"
E2E_AGENT_ID = "e2e-test-agent"
E2E_CONVERSATION_ID = "conv_e2e_refresh_scenario"
E2E_SESSION_ID = "sess_e2e_refresh_scenario"

TEXT_ROUTE_TYPES = ("default", "classifier", "compaction", "evaluator")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


async def _seed_user(session: AsyncSession, password_hasher: object) -> None:
    email = _env("COGNIS_LOCAL_ADMIN_EMAIL", "admin@cognis-e2e.localdev.me")
    password = _env("COGNIS_LOCAL_ADMIN_PASSWORD", "cognis-local-admin")
    name = _env("COGNIS_LOCAL_ADMIN_NAME", "Local Admin")

    existing = await get_user(session, email)
    if existing is None:
        pw_hash = password_hasher.hash(password)  # type: ignore[attr-defined]
        await create_user(session, email=email, name=name, password_hash=pw_hash, role="admin")
        print(f"Seeded e2e admin user: {email}")
    else:
        await update_user(session, email, name=name, role="admin")
        print(f"Updated e2e admin user: {email}")


async def _seed_provider(session: AsyncSession) -> None:
    llm_base_url = _env("COGNIS_LOCAL_LLM_BASE_URL", "http://mock-llm:8090/v1")
    chat_model = _env("COGNIS_LOCAL_CHAT_MODEL", "mock-model")

    provider_config = {
        "scope": "system",
        "preset": "litellm_proxy",
        "api_base": llm_base_url,
        "default_model": chat_model,
        "models": [
            {
                "model_id": chat_model,
                "display_name": "Mock LLM (e2e)",
                "supports_tools": True,
                "supports_streaming": True,
                "supports_reasoning": True,
                "context_window": 131072,
                "max_output_tokens": 8192,
                "tier": "cheap",
            }
        ],
        "auth_config": {"mode": "env", "env_var": "COGNIS_LOCAL_LLM_API_KEY"},
    }

    existing = await get_llm_provider(session, E2E_PROVIDER_ID)
    if existing is None:
        await create_llm_provider(
            session,
            provider_id=E2E_PROVIDER_ID,
            display_name="Mock LLM (e2e testing)",
            location="controller",
            backend="litellm",
            owner_email=SYSTEM_USER_EMAIL,
            config=provider_config,
            status="active",
        )
        print(f"Seeded e2e provider: {E2E_PROVIDER_ID}")
    else:
        await update_llm_provider(
            session,
            E2E_PROVIDER_ID,
            display_name="Mock LLM (e2e testing)",
            location="controller",
            backend="litellm",
            owner_email=SYSTEM_USER_EMAIL,
            config=provider_config,
            status="active",
        )
        print(f"Updated e2e provider: {E2E_PROVIDER_ID}")


async def _seed_model_routing(session: AsyncSession) -> None:
    chat_model = _env("COGNIS_LOCAL_CHAT_MODEL", "mock-model")
    for task_type in TEXT_ROUTE_TYPES:
        await upsert_model_routing(
            session,
            task_type=task_type,
            provider_id=E2E_PROVIDER_ID,
            model=chat_model,
            owner_email=SYSTEM_USER_EMAIL,
            config=None,
        )
    print(f"Seeded e2e model routing for: {', '.join(TEXT_ROUTE_TYPES)}")


async def _seed_agent(session: AsyncSession) -> None:
    admin_email = _env("COGNIS_LOCAL_ADMIN_EMAIL", "admin@cognis-e2e.localdev.me")
    chat_model = _env("COGNIS_LOCAL_CHAT_MODEL", "mock-model")

    capabilities = AgentCapabilities(
        memory_backend="none",
        guardrails_backend="none",
    )

    payload = {
        "name": "E2E Test Agent",
        "display_name": "E2E Test Agent",
        "description": (
            "Deterministic e2e test agent. Uses mock-llm provider with "
            "memory and guardrails disabled for fully deterministic testing."
        ),
        "system_prompt": (
            "You are a deterministic test assistant. "
            "Respond exactly as scripted by the mock LLM provider."
        ),
        "personality": {
            "purpose": "E2E testing of the Cognis streaming chat timeline.",
            "tone": "direct, technical",
            "temperament": "deterministic",
        },
        "capabilities": capabilities.model_dump(),
        "skills": {"loaded": []},
        "tools": {"builtin_tools": [], "tool_groups": []},
        "permissions": {
            "allowed_tools": ["*"],
            "denied_tools": [],
            "tool_permissions": {"*": "allow"},
            "can_delegate": False,
        },
        "execution": {"executor_id": "local-compose-executor"},
        "llm_config": {"provider_id": E2E_PROVIDER_ID, "model": chat_model},
        "agent_type": "primary",
        "status": "active",
    }

    existing = await get_agent(session, E2E_AGENT_ID)
    if existing is None:
        await create_agent(
            session,
            agent_id=E2E_AGENT_ID,
            owner_email=admin_email,
            **payload,
        )
        print(f"Seeded e2e agent: {E2E_AGENT_ID}")
    else:
        await update_agent(
            session,
            E2E_AGENT_ID,
            updates={
                **payload,
                "owner_email": admin_email,
            },
        )
        print(f"Updated e2e agent: {E2E_AGENT_ID}")


async def _seed_refresh_conversation(
    session: AsyncSession,
    guardrails_provider: IntarisProvider,
) -> None:
    """Pre-seed a conversation for the refresh-mid-and-post-turn scenario.

    This gives the history projector events to project, so we can test
    that runtime item ids match history item ids on refresh.
    """
    admin_email = _env("COGNIS_LOCAL_ADMIN_EMAIL", "admin@cognis-e2e.localdev.me")

    existing = await get_conversation(session, E2E_CONVERSATION_ID)
    if existing is not None:
        print(f"E2E refresh conversation already exists: {E2E_CONVERSATION_ID}")
        return

    await create_conversation(
        session,
        admin_email,
        E2E_AGENT_ID,
        "web",
        conversation_id=E2E_CONVERSATION_ID,
        title="E2E refresh scenario conversation",
        title_source="manual",
        context_data={"seeded_by": "seed_e2e", "scenario": "refresh-mid-and-post-turn"},
    )
    session_row = await create_session(
        session,
        E2E_CONVERSATION_ID,
        admin_email,
        E2E_AGENT_ID,
        session_id=E2E_SESSION_ID,
        intaris_session_id=E2E_SESSION_ID,
        mnemory_session_id=E2E_SESSION_ID,
        status="idle",
    )
    await update_conversation_active_session(session, E2E_CONVERSATION_ID, E2E_SESSION_ID)
    await session.flush()

    try:
        await guardrails_provider.create_session(
            session_row.intaris_session_id or session_row.session_id,
            "E2E refresh scenario test session.",
            E2E_AGENT_ID,
            user_id=admin_email,
        )
        await guardrails_provider.record_events(
            session_row.intaris_session_id or session_row.session_id,
            [
                SessionEvent(
                    type="user_message",
                    data={
                        "role": "user",
                        "content": "Hello, this is the first message in the e2e refresh test.",
                        "content_type": "text",
                        "turn_id": "turn_e2e_1",
                    },
                ),
                SessionEvent(
                    type="assistant_thinking",
                    data={
                        "role": "assistant",
                        "message_id": "msg_e2e_1",
                        "turn_id": "turn_e2e_1",
                        "block_id": "blk_e2e_1",
                        "title": "Thinking",
                        "content": "Processing the initial message.",
                        "assistant_phase_index": 0,
                    },
                ),
                SessionEvent(
                    type="assistant_message",
                    data={
                        "role": "assistant",
                        "content": "Hello! I'm the e2e test agent. This is a pre-seeded response.",
                        "content_type": "text",
                        "turn_id": "turn_e2e_1",
                        "message_id": "msg_e2e_1",
                        "assistant_phase_index": 0,
                    },
                ),
            ],
            source="seed_e2e",
            idempotency_key="e2e-refresh-seed-v1",
            user_email=admin_email,
            agent_id=E2E_AGENT_ID,
        )
        print(f"Seeded e2e refresh conversation: {E2E_CONVERSATION_ID}")
    except Exception as exc:
        print(f"Skipped Intaris event seed for refresh conversation: {exc}")


async def _seed() -> None:
    setup_logging(_env("COGNIS_LOG_LEVEL", "info"), _env("COGNIS_LOG_FORMAT", "text"))
    password_hasher = create_password_hasher()
    runtime_config, bootstrap_engine, _, _ = await bootstrap_runtime(
        load_config(),
        password_hasher,
    )
    await bootstrap_engine.dispose()

    engine: AsyncEngine = create_engine(runtime_config.database_url)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    auth_provider = JWTAuthProvider(
        runtime_config.jwt_private_key_path,
        runtime_config.jwt_public_key_path,
    )
    guardrails_provider = IntarisProvider(runtime_config.intaris_url, auth_provider)

    try:
        async with session_factory() as session:
            await _seed_user(session, password_hasher)
            await _seed_provider(session)
            await _seed_model_routing(session)
            await _seed_agent(session)
            await _seed_refresh_conversation(session, guardrails_provider)
            await session.commit()

        print(
            json.dumps(
                {
                    "e2e_provider_id": E2E_PROVIDER_ID,
                    "e2e_agent_id": E2E_AGENT_ID,
                    "e2e_conversation_id": E2E_CONVERSATION_ID,
                    "mock_llm_url": _env("COGNIS_LOCAL_LLM_BASE_URL", "http://mock-llm:8090/v1"),
                    "admin_email": _env("COGNIS_LOCAL_ADMIN_EMAIL", "admin@cognis-e2e.localdev.me"),
                },
                indent=2,
            )
        )
    finally:
        await guardrails_provider.client.aclose()
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(_seed())
    except Exception as exc:
        print(f"E2E seed failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
