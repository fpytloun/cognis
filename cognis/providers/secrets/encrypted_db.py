"""AES-256-GCM encrypted secrets provider."""

from __future__ import annotations

import base64
import os
import uuid
from typing import Any, cast

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.config import ProviderHealth
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.models import Secret

logger = get_logger(__name__)


class EncryptedDBSecretsProvider:
    """Secrets stored encrypted in Cognis DB."""

    def __init__(self, session_factory: async_sessionmaker[Any], key_path: str) -> None:
        self.session_factory = session_factory
        with open(key_path, "rb") as key_file:
            self.key = base64.urlsafe_b64decode(key_file.read())

    def _encrypt(self, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        cipher = AESGCM(self.key)
        return nonce + cipher.encrypt(nonce, plaintext.encode(), None)

    def _decrypt(self, data: bytes) -> str:
        nonce, ciphertext = data[:12], data[12:]
        cipher = AESGCM(self.key)
        return cipher.decrypt(nonce, ciphertext, None).decode()

    async def get_secret(self, name: str, user_id: str, agent_id: str | None = None) -> str:
        async with self.session_factory() as session:
            scopes: list[tuple[str, str, str | None]] = [
                (user_id, "agent", agent_id),
                (user_id, "user", None),
                (SYSTEM_USER_EMAIL, "system", None),
                (user_id, "global", None),
            ]
            for scope_user_id, scope, scope_agent_id in scopes:
                stmt = select(Secret).where(
                    Secret.user_email == scope_user_id, Secret.name == name, Secret.scope == scope
                )
                if scope_agent_id is not None:
                    stmt = stmt.where(Secret.agent_id == scope_agent_id)
                else:
                    stmt = stmt.where(Secret.agent_id.is_(None))
                result = await session.execute(stmt)
                secret = result.scalar_one_or_none()
                if secret is not None:
                    return self._decrypt(secret.encrypted_value)
        raise KeyError(f"Secret not found: {name}")

    async def set_secret(
        self,
        name: str,
        value: str,
        user_id: str,
        scope: str = "user",
        agent_id: str | None = None,
        description: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            stmt = select(Secret).where(
                Secret.user_email == user_id, Secret.name == name, Secret.scope == scope
            )
            if agent_id is not None:
                stmt = stmt.where(Secret.agent_id == agent_id)
            else:
                stmt = stmt.where(Secret.agent_id.is_(None))
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            encrypted = self._encrypt(value)
            if existing is not None:
                existing.encrypted_value = encrypted
                existing.description = description
            else:
                session.add(
                    Secret(
                        secret_id=f"secret_{uuid.uuid4().hex[:12]}",
                        user_email=user_id,
                        name=name,
                        scope=scope,
                        agent_id=agent_id,
                        encrypted_value=encrypted,
                        description=description,
                    )
                )
            await session.commit()

    async def delete_secret(
        self, name: str, user_id: str, scope: str = "user", agent_id: str | None = None
    ) -> bool:
        async with self.session_factory() as session:
            stmt = delete(Secret).where(
                Secret.user_email == user_id, Secret.name == name, Secret.scope == scope
            )
            if agent_id is not None:
                stmt = stmt.where(Secret.agent_id == agent_id)
            else:
                stmt = stmt.where(Secret.agent_id.is_(None))
            result = await session.execute(stmt)
            await session.commit()
            return cast(int, result.rowcount or 0) > 0

    async def list_secrets(self, user_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(select(Secret).where(Secret.user_email == user_id))
            return [
                {
                    "name": item.name,
                    "scope": item.scope,
                    "agent_id": item.agent_id,
                    "description": item.description,
                }
                for item in result.scalars().all()
            ]

    async def resolve_for_execution(self, agent: AgentDefinition, user_id: str) -> dict[str, str]:
        resolved: dict[str, str] = {}
        if agent.permissions is None:
            return resolved
        for name in agent.permissions.allowed_secrets:
            resolved[name] = await self.get_secret(name, user_id, agent.agent_id)
        return resolved

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name="secrets", status="healthy")
