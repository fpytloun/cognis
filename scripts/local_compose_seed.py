#!/usr/bin/env python3
"""Seed a Local Compose Cognis deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.logging import setup_logging
from cognis.models.agent import AgentDefinition
from cognis.models.session import SessionEvent
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.providers.memory.mnemory import MnemoryProvider
from cognis.security import create_password_hasher
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_executor,
    create_llm_provider,
    create_session,
    create_user,
    get_agent,
    get_conversation,
    get_executor_row,
    get_llm_provider,
    get_user,
    update_agent,
    update_conversation_active_session,
    update_executor,
    update_executor_runtime_state,
    update_llm_provider,
    update_user,
    upsert_model_routing,
)

PROVIDER_ID = "local-openai-compatible"
EXECUTOR_ID = "local-compose-executor"
AGENT_ID = "local-implement-test-agent"
CONVERSATION_ID = "conv_local_compose_welcome"
SESSION_ID = "sess_local_compose_welcome"

TEXT_ROUTE_TYPES = ("default", "classifier", "compaction", "evaluator")


@dataclass(frozen=True)
class LocalSeedConfig:
    admin_email: str
    admin_password: str
    admin_name: str
    provider_name: str
    llm_base_url: str
    llm_api_key_env: str
    chat_model: str
    executor_token_dir: Path
    controller_ws_url: str
    host_controller_ws_url: str
    seed_sample_conversation: bool


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _local_config() -> LocalSeedConfig:
    llm_base_url = _env("COGNIS_LOCAL_LLM_BASE_URL")
    if not llm_base_url:
        raise RuntimeError("COGNIS_LOCAL_LLM_BASE_URL is required for local provider seeding")
    llm_api_key_env = _env("COGNIS_LOCAL_LLM_API_KEY_ENV", "COGNIS_LOCAL_LLM_API_KEY")
    if not _env(llm_api_key_env):
        raise RuntimeError(f"{llm_api_key_env} is required for local provider seeding")
    return LocalSeedConfig(
        admin_email=_env("COGNIS_LOCAL_ADMIN_EMAIL", "admin@localhost"),
        admin_password=_env("COGNIS_LOCAL_ADMIN_PASSWORD", "cognis-local-admin"),
        admin_name=_env("COGNIS_LOCAL_ADMIN_NAME", "Local Admin"),
        provider_name=_env("COGNIS_LOCAL_PROVIDER_NAME", "Local OpenAI-Compatible"),
        llm_base_url=llm_base_url,
        llm_api_key_env=llm_api_key_env,
        chat_model=_env("COGNIS_LOCAL_CHAT_MODEL", "gpt-oss-120b"),
        executor_token_dir=Path(_env("COGNIS_LOCAL_EXECUTOR_TOKEN_DIR", "/run/cognis-local")),
        controller_ws_url=_env(
            "COGNIS_LOCAL_EXECUTOR_CONTROLLER_URL",
            "ws://cognis:8080/api/executor/ws",
        ),
        host_controller_ws_url=_env(
            "COGNIS_LOCAL_HOST_EXECUTOR_CONTROLLER_URL",
            "ws://localhost:8080/api/executor/ws",
        ),
        seed_sample_conversation=_env_bool("COGNIS_LOCAL_SEED_SAMPLE_CONVERSATION", True),
    )


def _provider_config(config: LocalSeedConfig) -> dict[str, Any]:
    model = {
        "model_id": config.chat_model,
        "display_name": config.chat_model,
        "supports_tools": True,
        "supports_streaming": True,
        "supports_reasoning": True,
        "reasoning_efforts": ["low", "medium", "high"],
        "context_window": 131072,
        "max_output_tokens": 8192,
        "tier": "cheap",
    }
    return {
        "scope": "system",
        "preset": "litellm_proxy",
        "api_base": config.llm_base_url,
        "default_model": config.chat_model,
        "models": [model],
        "auth_config": {"mode": "env", "env_var": config.llm_api_key_env},
    }


def _agent_payload(config: LocalSeedConfig) -> dict[str, Any]:
    return {
        "name": "Local Implement Test Agent",
        "display_name": "Local Implement Test Agent",
        "description": (
            "Seeded local agent for validating Cognis, executor tools, and the "
            "implement-test feedback loop."
        ),
        "system_prompt": (
            "You are a local Cognis implementation/testing agent. Prefer concise, "
            "technical answers. Use tools only when needed and report failures clearly."
        ),
        "personality": {
            "purpose": "Exercise and validate a local Cognis deployment.",
            "tone": "direct, technical, concise",
            "temperament": "calm and methodical",
            "behavioral_rules": [
                "Use browser, shell, and filesystem tools only for local validation tasks.",
                "Never expose API keys or executor tokens.",
                "Report concrete failures with reproduction steps.",
            ],
        },
        "skills": {"loaded": []},
        "tools": {
            "builtin_tools": [],
            "tool_groups": ["filesystem", "shell", "web", "browser", "development"],
        },
        "permissions": {
            "allowed_tools": [],
            "denied_tools": [],
            "tool_permissions": {},
            "allowed_secrets": [],
            "allowed_credentials": [],
            "allowed_knowledgebases": [],
            "max_delegation_depth": 1,
            "can_delegate": False,
        },
        "llm_config": {"provider_id": PROVIDER_ID, "model": config.chat_model},
        "execution": {"executor_id": EXECUTOR_ID},
        "agent_type": "primary",
        "status": "active",
    }


async def _seed_user(session: AsyncSession, config: LocalSeedConfig, password_hasher: Any) -> None:
    existing = await get_user(session, config.admin_email)
    if existing is None:
        password_hash = password_hasher.hash(config.admin_password)
        await create_user(
            session,
            email=config.admin_email,
            name=config.admin_name,
            password_hash=password_hash,
            role="admin",
        )
        print(f"Seeded local admin user: {config.admin_email}")
        return
    await update_user(
        session,
        config.admin_email,
        name=config.admin_name,
        role="admin",
    )
    print(f"Updated local admin user: {config.admin_email}")


async def _seed_provider(session: AsyncSession, config: LocalSeedConfig) -> None:
    provider_config = _provider_config(config)
    existing = await get_llm_provider(session, PROVIDER_ID)
    if existing is None:
        await create_llm_provider(
            session,
            provider_id=PROVIDER_ID,
            display_name=config.provider_name,
            location="controller",
            backend="litellm",
            owner_email=SYSTEM_USER_EMAIL,
            config=provider_config,
            status="active",
        )
        print(f"Seeded provider: {PROVIDER_ID}")
    else:
        await update_llm_provider(
            session,
            PROVIDER_ID,
            display_name=config.provider_name,
            location="controller",
            backend="litellm",
            owner_email=SYSTEM_USER_EMAIL,
            config=provider_config,
            status="active",
        )
        print(f"Updated provider: {PROVIDER_ID}")


async def _seed_model_routing(session: AsyncSession, config: LocalSeedConfig) -> None:
    for task_type in TEXT_ROUTE_TYPES:
        await upsert_model_routing(
            session,
            task_type=task_type,
            provider_id=PROVIDER_ID,
            model=config.chat_model,
            owner_email=SYSTEM_USER_EMAIL,
            config=None,
        )
    print(f"Seeded model routing for: {', '.join(TEXT_ROUTE_TYPES)}")


def _executor_config() -> dict[str, Any]:
    return {
        "browser": {
            "enabled": True,
            "auto_install": True,
            "headed_allowed": False,
            "persistent_profiles_enabled": True,
            "profile_mode_default": "persistent_local",
            "realistic_launch": True,
            "xvfb_auto": True,
            "engine": "chromium",
            "runtime": "playwright",
            "stealth_enabled": True,
            "realistic_user_agent": True,
            "default_timezone_id": "UTC",
            "default_accept_language": "en-US,en;q=0.9",
        },
        "lsp": {"enabled": True, "auto_install": True},
        "officecli": {"enabled": True, "auto_install": True},
    }


async def _seed_executor(
    session: AsyncSession,
    config: LocalSeedConfig,
    auth_provider: JWTAuthProvider,
) -> str:
    labels = {"deployment": "local-compose", "role": "primary"}
    enabled_tool_groups = ["filesystem", "shell", "web", "browser", "development", "office"]
    existing = await get_executor_row(session, EXECUTOR_ID)
    if existing is None:
        row = await create_executor(
            session,
            executor_id=EXECUTOR_ID,
            name="Local Compose Executor",
            executor_type="websocket",
            labels=labels,
            enabled_tools=[],
            enabled_tool_groups=enabled_tool_groups,
            config=_executor_config(),
            is_default=True,
            owner_email=config.admin_email,
            shared=False,
        )
        row.desired_config_version = 1
        row.runtime_state = "stale"
        token_version = int(row.token_version or 0)
        print(f"Seeded executor: {EXECUTOR_ID}")
    else:
        next_version = max(int(existing.desired_config_version or 0), 1) + 1
        await update_executor(
            session,
            EXECUTOR_ID,
            owner_email=config.admin_email,
            include_shared=True,
            name="Local Compose Executor",
            executor_type="websocket",
            labels=labels,
            enabled_tools=[],
            enabled_tool_groups=enabled_tool_groups,
            config=_executor_config(),
            is_default=True,
            shared=False,
        )
        await update_executor_runtime_state(
            session,
            EXECUTOR_ID,
            desired_config_version=next_version,
            runtime_state="stale",
        )
        token_version = int(existing.token_version or 0)
        print(f"Updated executor: {EXECUTOR_ID}")
    return auth_provider.sign_executor_token(EXECUTOR_ID, token_version=token_version)


async def _seed_agent(
    session: AsyncSession,
    config: LocalSeedConfig,
    memory_provider: MnemoryProvider,
) -> None:
    payload = _agent_payload(config)
    existing = await get_agent(session, AGENT_ID)
    if existing is None:
        await create_agent(
            session,
            agent_id=AGENT_ID,
            owner_email=config.admin_email,
            **payload,
        )
        print(f"Seeded agent: {AGENT_ID}")
    else:
        await update_agent(session, AGENT_ID, updates=payload)
        print(f"Updated agent: {AGENT_ID}")
    await session.flush()
    agent = await get_agent(session, AGENT_ID)
    if agent is not None:
        try:
            definition = AgentDefinition.model_validate(
                {
                    "agent_id": agent.agent_id,
                    "owner_email": agent.owner_email,
                    "name": agent.name,
                    "display_name": agent.display_name,
                    "description": agent.description,
                    "system_prompt": agent.system_prompt,
                    "personality": agent.personality,
                    "skills": agent.skills,
                    "tools": agent.tools,
                    "permissions": agent.permissions,
                    "llm_config": agent.llm_config,
                    "execution": agent.execution,
                    "agent_type": agent.agent_type,
                    "status": agent.status,
                }
            )
            await memory_provider.replace_bootstrap_identity(
                definition,
                previous_content=None,
                allow_legacy_cleanup=True,
            )
        except Exception as exc:
            print(f"Skipped Mnemory agent identity bootstrap: {exc}")


async def _seed_sample_conversation(
    session: AsyncSession,
    config: LocalSeedConfig,
    guardrails_provider: IntarisProvider,
) -> None:
    if not config.seed_sample_conversation:
        return
    conversation = await get_conversation(session, CONVERSATION_ID)
    if conversation is not None:
        print(f"Sample conversation already exists: {conversation.conversation_id}")
        return
    conversation = await create_conversation(
        session,
        config.admin_email,
        AGENT_ID,
        "web",
        conversation_id=CONVERSATION_ID,
        title="Local Compose welcome",
        title_source="manual",
        context_data={"seeded_by": "local_compose_seed"},
        memory_labels={"deployment": "local-compose"},
    )
    session_row = await create_session(
        session,
        CONVERSATION_ID,
        config.admin_email,
        AGENT_ID,
        session_id=SESSION_ID,
        intaris_session_id=SESSION_ID,
        mnemory_session_id=SESSION_ID,
        status="idle",
    )
    await update_conversation_active_session(session, CONVERSATION_ID, SESSION_ID)
    await session.flush()
    try:
        await guardrails_provider.create_session(
            session_row.intaris_session_id or session_row.session_id,
            "Seeded local compose welcome conversation.",
            AGENT_ID,
            user_id=config.admin_email,
        )
        await guardrails_provider.record_events(
            session_row.intaris_session_id or session_row.session_id,
            [
                SessionEvent(
                    type="user_message",
                    data={
                        "role": "user",
                        "content": "This is a seeded local compose conversation.",
                        "content_type": "text",
                    },
                ),
                SessionEvent(
                    type="assistant_message",
                    data={
                        "role": "assistant",
                        "content": (
                            "Local Compose is seeded. Use this agent to verify Cognis, "
                            "Mnemory, Intaris, and the executor."
                        ),
                        "content_type": "text",
                    },
                ),
            ],
            source="local_compose_seed",
            idempotency_key="local-compose-welcome-v1",
            user_email=config.admin_email,
            agent_id=AGENT_ID,
        )
    except Exception as exc:
        print(f"Skipped Intaris sample event seed: {exc}")
    print(f"Seeded sample conversation: {conversation.conversation_id}")


def _write_executor_env(config: LocalSeedConfig, token: str) -> None:
    config.executor_token_dir.mkdir(parents=True, exist_ok=True)
    token_file = config.executor_token_dir / "executor.env"
    content = "\n".join(
        [
            f"COGNIS_CONTROLLER_URL={config.controller_ws_url}",
            f"COGNIS_EXECUTOR_TOKEN={token}",
            "COGNIS_EXECUTOR_WORKDIR=/home/cognis/workspace",
            "COGNIS_EXECUTOR_ALLOW_INSECURE_WS=1",
            "",
        ]
    )
    tmp_path = token_file.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    tmp_path.replace(token_file)
    print(f"Wrote Docker executor env file: {token_file}")

    host_env_path = config.executor_token_dir / "host-executor.env"
    host_content = "\n".join(
        [
            f"COGNIS_CONTROLLER_URL={config.host_controller_ws_url}",
            f"COGNIS_EXECUTOR_TOKEN={token}",
            "COGNIS_EXECUTOR_WORKDIR=$HOME",
            "",
        ]
    )
    host_tmp = host_env_path.with_suffix(".tmp")
    host_tmp.write_text(host_content, encoding="utf-8")
    host_tmp.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    host_tmp.replace(host_env_path)
    print(f"Wrote host executor env file: {host_env_path}")


def _print_summary(config: LocalSeedConfig) -> None:
    print(
        json.dumps(
            {
                "admin_email": config.admin_email,
                "provider_id": PROVIDER_ID,
                "chat_model": config.chat_model,
                "executor_id": EXECUTOR_ID,
                "agent_id": AGENT_ID,
                "conversation_id": CONVERSATION_ID if config.seed_sample_conversation else None,
                "host_executor_command": (
                    "set -a; source .local/cognis-compose/executor-token/"
                    "host-executor.env; set +a; uvx cognis-executor"
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


async def _seed() -> None:
    setup_logging(_env("COGNIS_LOG_LEVEL", "info"), _env("COGNIS_LOG_FORMAT", "text"))
    seed_config = _local_config()
    password_hasher = create_password_hasher()
    runtime_config, bootstrap_engine, _bootstrap_session_factory, _ = await bootstrap_runtime(
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
    memory_provider = MnemoryProvider(runtime_config.mnemory_url, auth_provider)
    guardrails_provider = IntarisProvider(runtime_config.intaris_url, auth_provider)
    try:
        async with session_factory() as session:
            await _seed_user(session, seed_config, password_hasher)
            await _seed_provider(session, seed_config)
            await _seed_model_routing(session, seed_config)
            token = await _seed_executor(session, seed_config, auth_provider)
            await _seed_agent(session, seed_config, memory_provider)
            await _seed_sample_conversation(session, seed_config, guardrails_provider)
            await session.commit()
        _write_executor_env(seed_config, token)
        _print_summary(seed_config)
    finally:
        await memory_provider.client.aclose()
        await guardrails_provider.client.aclose()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Seed a Local Compose Cognis deployment with a local admin user, "
            "OpenAI-compatible provider, model routing, executor token, "
            "dummy agent, and sample data."
        )
    )
    parser.parse_args()
    try:
        asyncio.run(_seed())
    except Exception as exc:
        print(f"Local Compose seed failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
