"""Encrypted credential provider backed by the Cognis DB."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.config import ProviderHealth
from cognis.models.credential import CredentialAccessError, CredentialRecord, CredentialResolution
from cognis.store.models import CredentialRow

logger = get_logger(__name__)

_TOTP_ALGORITHMS = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
}


class EncryptedDBCredentialsProvider:
    """Credential storage and resolution using AES-256-GCM."""

    def __init__(self, session_factory: async_sessionmaker[Any], key_path: str) -> None:
        self.session_factory = session_factory
        with open(key_path, "rb") as key_file:
            self.key = base64.urlsafe_b64decode(key_file.read())

    def _encrypt_payload(self, payload: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        cipher = AESGCM(self.key)
        plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return nonce + cipher.encrypt(nonce, plaintext, None)

    def _decrypt_payload(self, data: bytes) -> dict[str, Any]:
        nonce, ciphertext = data[:12], data[12:]
        cipher = AESGCM(self.key)
        plaintext = cipher.decrypt(nonce, ciphertext, None)
        payload = json.loads(plaintext.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _field_names_from_payload(self, payload: dict[str, Any]) -> list[str]:
        return sorted(str(key) for key in payload)

    def _safe_field_names(self, row: CredentialRow) -> list[str]:
        try:
            return self._field_names_from_payload(self._decrypt_payload(row.encrypted_payload))
        except Exception:
            logger.warning(
                "credential: field-name extraction failed",
                extra={
                    "extra_data": {"credential_id": row.credential_id, "user_email": row.user_email}
                },
                exc_info=True,
            )
            return []

    async def list_credentials(self, user_email: str) -> list[CredentialRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(CredentialRow)
                .where(CredentialRow.user_email == user_email)
                .order_by(CredentialRow.label, CredentialRow.credential_id)
            )
            rows = result.scalars().all()
            return [
                self._row_to_record(row, field_names=self._safe_field_names(row)) for row in rows
            ]

    async def get_credential(self, credential_id: str, user_email: str) -> CredentialRecord | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(CredentialRow).where(
                    CredentialRow.user_email == user_email,
                    CredentialRow.credential_id == credential_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_record(row, field_names=self._safe_field_names(row))

    async def upsert_credential(
        self,
        *,
        credential_id: str,
        user_email: str,
        kind: str,
        label: str,
        payload: dict[str, Any],
        scope: str = "user",
        agent_id: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "active",
        expires_at: datetime | None = None,
        last_verified_at: datetime | None = None,
    ) -> CredentialRecord:
        async with self.session_factory() as session:
            result = await session.execute(
                select(CredentialRow).where(
                    CredentialRow.user_email == user_email,
                    CredentialRow.credential_id == credential_id,
                )
            )
            row = result.scalar_one_or_none()
            encrypted_payload = self._encrypt_payload(payload)
            if row is None:
                row = CredentialRow(
                    row_id=f"credrow_{uuid.uuid4().hex[:12]}",
                    credential_id=credential_id,
                    user_email=user_email,
                    scope=scope,
                    agent_id=agent_id,
                    kind=kind,
                    label=label,
                    description=description,
                    metadata_json=metadata or {},
                    encrypted_payload=encrypted_payload,
                    version=1,
                    status=status,
                    expires_at=expires_at,
                    last_verified_at=last_verified_at,
                )
                session.add(row)
            else:
                row.scope = scope
                row.agent_id = agent_id
                row.kind = kind
                row.label = label
                row.description = description
                row.metadata_json = metadata or {}
                row.encrypted_payload = encrypted_payload
                row.version = int(row.version or 0) + 1
                row.status = status
                row.expires_at = expires_at
                row.last_verified_at = last_verified_at
                if status != "revoked":
                    row.revoked_at = None
            await session.commit()
            await session.refresh(row)
            return self._row_to_record(row, field_names=self._field_names_from_payload(payload))

    async def revoke_credential(self, credential_id: str, user_email: str) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                select(CredentialRow).where(
                    CredentialRow.user_email == user_email,
                    CredentialRow.credential_id == credential_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            row.status = "revoked"
            row.revoked_at = datetime.now(UTC)
            row.version = int(row.version or 0) + 1
            await session.commit()
            return True

    async def delete_credential(self, credential_id: str, user_email: str) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                delete(CredentialRow).where(
                    CredentialRow.user_email == user_email,
                    CredentialRow.credential_id == credential_id,
                )
            )
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def resolve_ref(
        self,
        ref: str,
        *,
        agent: AgentDefinition,
        user_email: str,
    ) -> CredentialResolution:
        if not ref.startswith("$credential:"):
            raise ValueError("Invalid credential ref")
        raw = ref[len("$credential:") :]
        credential_id, field = raw, None
        if "." in raw:
            credential_id, field = raw.split(".", 1)
        if not credential_id:
            raise ValueError("Credential ref is missing credential_id")
        async with self.session_factory() as session:
            result = await session.execute(
                select(CredentialRow).where(
                    CredentialRow.user_email == user_email,
                    CredentialRow.credential_id == credential_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise CredentialAccessError(
                    "credential_not_found",
                    f"Credential not found: {credential_id}",
                    credential_id=credential_id,
                )
            allowed = set(agent.permissions.allowed_credentials if agent.permissions else [])
            is_ephemeral = bool((row.metadata_json or {}).get("ephemeral"))
            if credential_id not in allowed and not is_ephemeral:
                raise CredentialAccessError(
                    "credential_not_allowed",
                    f"Credential not allowed for agent: {credential_id}",
                    credential_id=credential_id,
                )
            if row.agent_id is not None and row.agent_id != agent.agent_id:
                raise CredentialAccessError(
                    "credential_not_allowed",
                    f"Credential not scoped to agent: {credential_id}",
                    credential_id=credential_id,
                )
            if row.status != "active":
                raise CredentialAccessError(
                    "credential_inactive",
                    f"Credential is not active: {credential_id}",
                    credential_id=credential_id,
                )
            if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
                raise CredentialAccessError(
                    "credential_expired",
                    f"Credential expired: {credential_id}",
                    credential_id=credential_id,
                )
            kind = row.kind
            payload = self._decrypt_payload(row.encrypted_payload)
        if kind == "totp_seed" and field == "otp":
            value = _generate_totp(payload)
        else:
            value = payload if field is None else payload.get(field)
        if field is not None and field not in payload and not (kind == "totp_seed" and field == "otp"):
            raise CredentialAccessError(
                "credential_field_not_found",
                f"Credential field not found: {credential_id}.{field}",
                credential_id=credential_id,
                field=field,
            )
        return CredentialResolution(credential_id=credential_id, field=field, value=value)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name="credentials", status="healthy")

    def _row_to_record(
        self, row: CredentialRow, *, field_names: list[str] | None = None
    ) -> CredentialRecord:
        return CredentialRecord(
            credential_id=row.credential_id,
            user_email=row.user_email,
            scope=row.scope,
            agent_id=row.agent_id,
            kind=row.kind,
            label=row.label,
            description=row.description,
            metadata=row.metadata_json or {},
            field_names=list(field_names or []),
            version=row.version,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_verified_at=row.last_verified_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )


def _generate_totp(payload: dict[str, Any]) -> str:
    """Generate a TOTP code from an encrypted totp_seed credential payload."""

    secret = str(payload.get("secret") or payload.get("seed") or "").strip().replace(" ", "")
    if not secret:
        raise CredentialAccessError(
            "credential_field_not_found",
            "TOTP seed credential is missing secret",
            field="secret",
        )
    try:
        digits = int(payload.get("digits") or 6)
        period = int(payload.get("period") or 30)
    except (TypeError, ValueError) as exc:
        raise CredentialAccessError(
            "credential_invalid",
            "TOTP digits and period must be integers",
        ) from exc
    if digits < 6 or digits > 10:
        raise CredentialAccessError(
            "credential_invalid",
            "TOTP digits must be between 6 and 10",
            field="digits",
        )
    if period <= 0:
        raise CredentialAccessError(
            "credential_invalid",
            "TOTP period must be positive",
            field="period",
        )
    algorithm = str(payload.get("algorithm") or "sha1").lower().replace("-", "")
    digest = _TOTP_ALGORITHMS.get(algorithm)
    if digest is None:
        raise CredentialAccessError(
            "credential_invalid",
            f"Unsupported TOTP algorithm: {algorithm}",
            field="algorithm",
        )
    try:
        padded_secret = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(padded_secret, casefold=True)
    except Exception as exc:
        raise CredentialAccessError(
            "credential_invalid",
            "TOTP seed is not valid base32",
            field="secret",
        ) from exc
    counter = int(time.time() // period)
    hmac_digest = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = hmac_digest[-1] & 0x0F
    code_int = struct.unpack(">I", hmac_digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)
