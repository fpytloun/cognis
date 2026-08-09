"""Explicit-recipient normalization, resolution, and durable admission."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.channels.route_admission import active_managed_binding_id, lock_channel_route
from cognis.channels.target_refs import ChannelTargetRefCodec
from cognis.models.channel import (
    ChannelCapabilities,
    ChannelRecipient,
    ChannelRecipientError,
    ChannelRecipientResult,
    ResolvedChannelTarget,
)
from cognis.store.models import ChannelAccountRow, ChannelRecipientIntentRow
from cognis.store.queries import (
    claim_channel_recipient_intent,
    create_or_get_channel_delivery_outbox,
    get_channel_recipient_intent,
)

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_SIGNAL_GROUP_ID = re.compile(r"^[A-Za-z0-9+/_-]{43}=?$")
_SNOWFLAKE = re.compile(r"^\d{15,22}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9._~+=/@#&:;,-]{1,255}$")
_TELEGRAM_CHAT = re.compile(r"^-?\d{1,20}$")
_SLACK_CONVERSATION = re.compile(r"^[CDG][A-Z0-9]{2,}$")
_SLACK_USER = re.compile(r"^U[A-Z0-9]{2,}$")
_MATRIX_ROOM_ID = re.compile(r"^![^: \t\r\n]+:[^: \t\r\n]+$")
_MATRIX_ALIAS = re.compile(r"^#[^: \t\r\n]+:[^: \t\r\n]+$")
_MATRIX_USER = re.compile(r"^@[^: \t\r\n]+:[^: \t\r\n]+$")
_GOOGLE_SPACE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")
_GOOGLE_USER = re.compile(r"^users/[A-Za-z0-9._~:-]+$")

ADDRESS_KINDS: dict[str, tuple[str, ...]] = {
    "signal": ("signal_e164", "signal_uuid", "signal_group_id"),
    "whatsapp": ("whatsapp_e164",),
    "telegram": ("telegram_chat_id", "telegram_public_username"),
    "discord": ("discord_channel_id", "discord_user_id"),
    "slack": ("slack_conversation_id", "slack_user_id"),
    "matrix": ("matrix_room_id", "matrix_room_alias", "matrix_user_id"),
    "irc": ("irc_nick", "irc_channel"),
    "google_chat": ("google_chat_space", "google_workspace_user"),
    "bluebubbles": ("bluebubbles_chat_guid", "imessage_handle"),
}
_CHAT_KINDS: dict[str, dict[str, tuple[str, ...]]] = {
    "signal": {
        "signal_e164": ("direct",),
        "signal_uuid": ("direct",),
        "signal_group_id": ("group",),
    },
    "whatsapp": {"whatsapp_e164": ("direct",)},
    "telegram": {"telegram_chat_id": ("direct", "group"), "telegram_public_username": ("group",)},
    "discord": {"discord_channel_id": ("direct", "group"), "discord_user_id": ("direct",)},
    "slack": {"slack_conversation_id": ("direct", "group"), "slack_user_id": ("direct",)},
    "matrix": {
        "matrix_room_id": ("direct", "group"),
        "matrix_room_alias": ("group",),
        "matrix_user_id": ("direct",),
    },
    "irc": {"irc_nick": ("direct",), "irc_channel": ("group",)},
    "google_chat": {"google_chat_space": ("direct", "group"), "google_workspace_user": ("direct",)},
    "bluebubbles": {"bluebubbles_chat_guid": ("direct", "group"), "imessage_handle": ("direct",)},
}


class RecipientNormalizationError(ValueError):
    """A safe recipient input error; never includes the supplied address."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_recipient(recipient: ChannelRecipient) -> ChannelRecipient:
    """Normalize and validate an explicit recipient without retaining raw PII in errors."""

    channel = recipient.channel_type.strip().lower()
    if channel not in ADDRESS_KINDS:
        raise RecipientNormalizationError("unsupported_channel", "Recipient channel is unsupported")
    raw_address = recipient.address
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_address):
        raise RecipientNormalizationError("invalid_address", "Recipient address is invalid")
    address = raw_address.strip()
    if not address:
        raise RecipientNormalizationError("invalid_address", "Recipient address is invalid")
    if "://" in address or address.lower().startswith(("mailto:", "tel:", "matrix:")):
        raise RecipientNormalizationError("uri_address", "Recipient address must not be URI-like")
    if not _OPAQUE.fullmatch(address):
        raise RecipientNormalizationError("invalid_address", "Recipient address is invalid")
    kind = recipient.address_kind
    if kind is None and channel == "signal" and _E164.fullmatch(address):
        kind = "signal_e164"
    if kind not in ADDRESS_KINDS[channel]:
        raise RecipientNormalizationError(
            "address_kind_required", "Recipient address kind is unsupported"
        )
    if not _address_matches(kind, address):
        raise RecipientNormalizationError("malformed_address", "Recipient address is malformed")
    if kind in {"signal_uuid", "telegram_public_username"} or (
        kind == "imessage_handle" and "@" in address
    ):
        address = address.lower()
    allowed_chat_kinds = _CHAT_KINDS[channel][kind]
    chat_kind = recipient.chat_kind
    if chat_kind is None:
        chat_kind = allowed_chat_kinds[0]
    if chat_kind not in allowed_chat_kinds:
        raise RecipientNormalizationError(
            "chat_kind_mismatch", "Recipient chat kind does not match address kind"
        )
    if kind == "telegram_chat_id":
        is_group_id = address.startswith("-")
        if (chat_kind == "group") != is_group_id:
            raise RecipientNormalizationError(
                "chat_kind_mismatch", "Recipient chat kind does not match address kind"
            )
    return recipient.model_copy(
        update={
            "channel_type": channel,
            "address": address,
            "address_kind": kind,
            "chat_kind": chat_kind,
        }
    )


def _address_matches(kind: str, address: str) -> bool:
    if kind in {"signal_e164", "whatsapp_e164"}:
        return bool(_E164.fullmatch(address))
    if kind == "signal_uuid":
        return bool(_UUID.fullmatch(address))
    if kind == "signal_group_id":
        return bool(_SIGNAL_GROUP_ID.fullmatch(address))
    if kind == "telegram_chat_id":
        return bool(_TELEGRAM_CHAT.fullmatch(address))
    if kind == "telegram_public_username":
        return bool(re.fullmatch(r"@[A-Za-z0-9_]{5,32}", address))
    if kind in {"discord_channel_id", "discord_user_id"}:
        return bool(_SNOWFLAKE.fullmatch(address))
    if kind == "slack_conversation_id":
        return bool(_SLACK_CONVERSATION.fullmatch(address))
    if kind == "slack_user_id":
        return bool(_SLACK_USER.fullmatch(address))
    if kind == "matrix_room_id":
        return bool(_MATRIX_ROOM_ID.fullmatch(address))
    if kind == "matrix_room_alias":
        return bool(_MATRIX_ALIAS.fullmatch(address))
    if kind == "matrix_user_id":
        return bool(_MATRIX_USER.fullmatch(address))
    if kind == "irc_nick":
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", address))
    if kind == "irc_channel":
        return bool(re.fullmatch(r"[#&][^\s,]{1,199}", address))
    if kind == "google_chat_space":
        return bool(_GOOGLE_SPACE.fullmatch(address))
    if kind == "google_workspace_user":
        return bool(_GOOGLE_USER.fullmatch(address))
    if kind == "bluebubbles_chat_guid":
        return bool(re.fullmatch(r"[A-Za-z0-9._~+=/@#&:;,-]{1,255}", address))
    if kind == "imessage_handle":
        return bool(_E164.fullmatch(address) or re.fullmatch(r"[^@\s]+@[^@\s]+", address))
    return False


def recipient_capabilities(channel_type: str) -> ChannelCapabilities:
    """Return static capabilities for recipient validation and tool responses."""

    kinds = list(ADDRESS_KINDS.get(channel_type, ()))
    return ChannelCapabilities(
        recipient_capabilities={
            "address_kinds": kinds,
            "supports_resolution": channel_type == "signal",
            "supports_creation": False,
        }
    )


@dataclass(frozen=True, slots=True)
class _Admission:
    recipient: ChannelRecipient
    account: ChannelAccountRow
    fingerprint: str
    intent_id: str


class RecipientResolutionService:
    """Persist explicit recipient intents and resolve them through fenced adapters."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        codec: ChannelTargetRefCodec,
        channel_manager_ref: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec
        self._channel_manager_ref = channel_manager_ref

    async def send(
        self,
        *,
        user_email: str,
        recipient: ChannelRecipient,
        content: str,
        artifact_metadata: list[dict[str, Any]],
        idempotency_key: str,
        conversation_id: str,
        idempotency_scope: str = "channel_recipient",
    ) -> ChannelRecipientResult:
        try:
            normalized = normalize_recipient(recipient)
        except RecipientNormalizationError as exc:
            return ChannelRecipientResult(
                status="failed", error=ChannelRecipientError(code=exc.code, message=str(exc))
            )
        admission = await self._admit(
            user_email=user_email,
            recipient=normalized,
            content=content,
            artifact_metadata=artifact_metadata,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
            idempotency_scope=idempotency_scope,
        )
        if isinstance(admission, ChannelRecipientResult):
            return admission
        intent = await self._resolve(admission, user_email=user_email)
        if isinstance(intent, ChannelRecipientResult):
            return intent
        target = intent
        async with self._session_factory() as session:
            row = await get_channel_recipient_intent(session, admission.intent_id)
            if row is None:
                return ChannelRecipientResult(
                    status="failed",
                    intent_id=admission.intent_id,
                    error=ChannelRecipientError(
                        code="intent_missing", message="Recipient intent is unavailable"
                    ),
                )
            account = await lock_channel_route(session, target.account_id)
            if (
                account.user_email != user_email
                or not account.enabled
                or not account.allow_new_conversations
                or await active_managed_binding_id(
                    session,
                    user_email=user_email,
                    account_id=target.account_id,
                    chat_id=target.chat_id,
                    thread_id=target.thread_id,
                )
                is not None
            ):
                row.resolution_state = "blocked"
                row.safe_error_json = {
                    "code": "managed_route",
                    "message": "Recipient route is managed",
                }
                await session.commit()
                return ChannelRecipientResult(
                    status="blocked",
                    intent_id=row.intent_id,
                    target_ref=None,
                    error=ChannelRecipientError(
                        code="managed_route", message="Recipient route is managed"
                    ),
                )
            delivery_id = f"cdel_recipient_{admission.intent_id}"
            outbox, _created = await create_or_get_channel_delivery_outbox(
                session,
                delivery_id=delivery_id,
                user_email=user_email,
                conversation_id=conversation_id,
                session_id=None,
                source_type="channel_recipient",
                source_id=admission.intent_id,
                channel_type=target.channel_type,
                account_id=target.account_id,
                chat_id=target.chat_id,
                thread_id=target.thread_id,
                fallback_text=content,
                attachments=artifact_metadata,
                deliverable_id=None,
                next_attempt_at=None,
            )
            row.resolution_state = "resolved"
            row.resolved_route_json = _target_data(target)
            row.safe_error_json = None
            await session.commit()
        return ChannelRecipientResult(
            status="sent" if outbox.status == "sent" else "queued",
            intent_id=admission.intent_id,
            delivery_id=outbox.delivery_id,
            target_ref=None,
            target=None,
        )

    async def _admit(self, **kwargs: Any) -> _Admission | ChannelRecipientResult:
        user_email = str(kwargs["user_email"])
        recipient: ChannelRecipient = kwargs["recipient"]
        idempotency_scope = str(kwargs.get("idempotency_scope") or "channel_recipient")
        idempotency_key = str(kwargs["idempotency_key"])
        fingerprint = _fingerprint(
            user_email,
            recipient,
            str(kwargs["content"]),
            kwargs["artifact_metadata"],
            idempotency_scope,
        )
        intent_id = _intent_id(user_email, idempotency_scope, idempotency_key)
        account_ref = recipient.account_ref
        async with self._session_factory() as session:
            account_token = None
            if account_ref:
                try:
                    account_token = self._codec.decode(
                        account_ref, user_email=user_email, expected_kind="account"
                    )
                except ValueError:
                    return ChannelRecipientResult(
                        status="failed",
                        error=ChannelRecipientError(
                            code="invalid_account_ref",
                            message="Recipient account reference is invalid",
                        ),
                    )
                if account_token.channel_type != recipient.channel_type:
                    return ChannelRecipientResult(
                        status="failed",
                        error=ChannelRecipientError(
                            code="account_channel_mismatch",
                            message="Recipient account does not match the channel",
                        ),
                    )
            existing = await get_channel_recipient_intent(session, intent_id)
            if existing is not None:
                if (
                    existing.user_email != user_email
                    or existing.fingerprint != fingerprint
                    or (
                        account_token is not None
                        and existing.account_id != account_token.account_id
                    )
                ):
                    return _idempotency_conflict(intent_id)
                account = await lock_channel_route(session, existing.account_id)
                if (
                    account.user_email != user_email
                    or not account.enabled
                    or account.channel_type != recipient.channel_type
                    or not account.allow_new_conversations
                ):
                    return ChannelRecipientResult(
                        status="blocked",
                        intent_id=intent_id,
                        error=ChannelRecipientError(
                            code="account_route_blocked",
                            message="Recipient account route is not available",
                        ),
                    )
                capability_error = await self._runtime_capability_error(
                    recipient, account.account_id
                )
                if capability_error is not None:
                    return capability_error
                target = _canonical_target(recipient, account.account_id)
                if isinstance(existing.resolved_route_json, dict):
                    try:
                        target = ResolvedChannelTarget.model_validate(existing.resolved_route_json)
                    except ValueError:
                        return ChannelRecipientResult(
                            status="failed",
                            intent_id=intent_id,
                            error=ChannelRecipientError(
                                code="intent_route_invalid",
                                message="Recipient route is unavailable",
                            ),
                        )
                if target is not None and (
                    target.account_id != account.account_id
                    or target.channel_type != account.channel_type
                    or target.chat_kind != recipient.chat_kind
                ):
                    return ChannelRecipientResult(
                        status="failed",
                        intent_id=intent_id,
                        error=ChannelRecipientError(
                            code="intent_route_invalid",
                            message="Recipient route is unavailable",
                        ),
                    )
                if target is not None and (
                    await active_managed_binding_id(
                        session,
                        user_email=user_email,
                        account_id=target.account_id,
                        chat_id=target.chat_id,
                        thread_id=target.thread_id,
                    )
                    is not None
                ):
                    return ChannelRecipientResult(
                        status="blocked",
                        intent_id=intent_id,
                        error=ChannelRecipientError(
                            code="managed_route",
                            message="Recipient route is managed",
                        ),
                    )
                if existing.resolution_state in {"failed", "blocked"}:
                    return _intent_result(existing)
                return _Admission(
                    recipient=recipient,
                    account=account,
                    fingerprint=fingerprint,
                    intent_id=intent_id,
                )
            accounts = list(
                (
                    await session.execute(
                        select(ChannelAccountRow).where(
                            ChannelAccountRow.user_email == user_email,
                            ChannelAccountRow.channel_type == recipient.channel_type,
                            ChannelAccountRow.enabled.is_(True),
                        )
                    )
                ).scalars()
            )
            if account_ref:
                accounts = [
                    account
                    for account in accounts
                    if account.account_id == account_token.account_id
                ]
            if len(accounts) != 1:
                code = "account_not_found" if not accounts else "account_ambiguous"
                return ChannelRecipientResult(
                    status="failed",
                    error=ChannelRecipientError(
                        code=code,
                        message="Recipient account selection is not unique",
                    ),
                )
            account = accounts[0]
            advisory_target = _canonical_target(recipient, account.account_id)
            if advisory_target is not None and (
                await active_managed_binding_id(
                    session,
                    user_email=user_email,
                    account_id=advisory_target.account_id,
                    chat_id=advisory_target.chat_id,
                    thread_id=advisory_target.thread_id,
                )
                is not None
            ):
                return ChannelRecipientResult(
                    status="blocked",
                    error=ChannelRecipientError(
                        code="managed_route",
                        message="Recipient route is managed",
                    ),
                )
            locked_account = await lock_channel_route(session, account.account_id)
            if (
                locked_account.user_email != user_email
                or not locked_account.enabled
                or locked_account.channel_type != recipient.channel_type
                or not locked_account.allow_new_conversations
            ):
                return ChannelRecipientResult(
                    status="blocked",
                    error=ChannelRecipientError(
                        code="account_route_blocked",
                        message="Recipient account route is not available",
                    ),
                )
            capability_error = await self._runtime_capability_error(recipient, account.account_id)
            if capability_error is not None:
                return capability_error
            row = ChannelRecipientIntentRow(
                intent_id=intent_id,
                user_email=user_email,
                account_id=account.account_id,
                channel_type=recipient.channel_type,
                address_kind=recipient.address_kind,
                normalized_address=recipient.address,
                chat_kind=recipient.chat_kind,
                allow_resolution=recipient.allow_resolution,
                allow_creation=recipient.allow_creation,
                provisional_route_key=_provisional_route_key(
                    self._codec,
                    user_email=user_email,
                    account_id=account.account_id,
                    recipient=recipient,
                ),
                fingerprint=fingerprint,
                content=str(kwargs["content"]),
                conversation_id=str(kwargs["conversation_id"]),
                idempotency_key=str(kwargs["idempotency_key"]),
                idempotency_scope=idempotency_scope,
                payload_json={
                    "recipient": recipient.model_dump(mode="json"),
                    "content": str(kwargs["content"]),
                    "conversation_id": str(kwargs["conversation_id"]),
                    "idempotency_key": str(kwargs["idempotency_key"]),
                    "idempotency_scope": idempotency_scope,
                    "artifacts": kwargs["artifact_metadata"],
                },
                authorized_artifacts_json=kwargs["artifact_metadata"],
                resolution_state="pending",
                attempt_count=0,
                side_effect_certainty="none",
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await get_channel_recipient_intent(session, intent_id)
                if existing is None:
                    raise
                if (
                    existing.user_email != user_email
                    or existing.fingerprint != fingerprint
                    or existing.account_id != account.account_id
                ):
                    return _idempotency_conflict(intent_id)
                if existing.resolution_state in {"failed", "blocked"}:
                    return _intent_result(existing)
                return _Admission(
                    recipient=recipient,
                    account=account,
                    fingerprint=fingerprint,
                    intent_id=intent_id,
                )
            return _Admission(
                recipient=recipient, account=account, fingerprint=fingerprint, intent_id=intent_id
            )

    async def recover_pending(self, *, limit: int = 100) -> int:
        """Reconstruct durable recipient payloads after a controller restart."""

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChannelRecipientIntentRow)
                        .where(
                            ChannelRecipientIntentRow.resolution_state.in_(
                                ["pending", "resolving", "resolved", "promoted"]
                            ),
                            (ChannelRecipientIntentRow.resolution_state != "resolving")
                            | (ChannelRecipientIntentRow.resolution_lease_expires_at <= now),
                        )
                        .order_by(ChannelRecipientIntentRow.created_at.asc())
                        .limit(limit)
                    )
                ).scalars()
            )
        recovered = 0
        for row in rows:
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            raw_recipient = payload.get("recipient")
            if not isinstance(raw_recipient, dict):
                continue
            try:
                recipient = ChannelRecipient.model_validate(raw_recipient)
                result = await self.send(
                    user_email=row.user_email,
                    recipient=recipient,
                    content=str(payload.get("content") or row.content),
                    artifact_metadata=(
                        payload.get("artifacts")
                        if isinstance(payload.get("artifacts"), list)
                        else row.authorized_artifacts_json or []
                    ),
                    idempotency_key=str(payload.get("idempotency_key") or row.idempotency_key),
                    idempotency_scope=str(
                        payload.get("idempotency_scope") or row.idempotency_scope
                    ),
                    conversation_id=str(payload.get("conversation_id") or row.conversation_id),
                )
            except Exception:
                continue
            if result.status in {"queued", "resolved"}:
                recovered += 1
        return recovered

    async def _resolve(
        self, admission: _Admission, *, user_email: str
    ) -> ResolvedChannelTarget | ChannelRecipientResult:
        recipient = admission.recipient
        async with self._session_factory() as session:
            existing = await get_channel_recipient_intent(session, admission.intent_id)
            route = existing.resolved_route_json if existing is not None else None
            if (
                existing is not None
                and existing.resolution_state in {"resolved", "promoted"}
                and isinstance(route, dict)
            ):
                return ResolvedChannelTarget.model_validate(route)
            claimed = await claim_channel_recipient_intent(
                session,
                intent_id=admission.intent_id,
                lease_token=f"rslv_{uuid.uuid4().hex}",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
                side_effect_certainty=("uncertain" if recipient.allow_creation else "none"),
            )
            if claimed is None:
                return ChannelRecipientResult(
                    status="resolving",
                    intent_id=admission.intent_id,
                    delivery_id=_recipient_delivery_id(admission.intent_id),
                )
            await session.commit()
        target = _canonical_target(recipient, admission.account.account_id)
        if target is not None:
            async with self._session_factory() as session:
                if (
                    await active_managed_binding_id(
                        session,
                        user_email=user_email,
                        account_id=target.account_id,
                        chat_id=target.chat_id,
                        thread_id=target.thread_id,
                    )
                    is not None
                ):
                    return await self._mark_error(
                        admission.intent_id,
                        "managed_route",
                        "Recipient route is managed",
                        state="blocked",
                    )
            await self._mark_resolved(admission.intent_id, target)
            return target
        manager = self._channel_manager_ref() if self._channel_manager_ref else None
        adapter = manager.get_adapter(admission.account.account_id) if manager else None
        if adapter is None:
            if manager is not None:
                return await self._mark_error(
                    admission.intent_id,
                    "account_unavailable",
                    "Recipient account is unavailable",
                )
            return await self._mark_error(
                admission.intent_id, "resolution_unavailable", "Recipient resolution is unavailable"
            )
        try:
            target = await adapter.resolve_recipient(recipient, resolution_key=admission.intent_id)
        except Exception as exc:
            raw_code = str(getattr(exc, "code", "resolution_failed"))
            code = raw_code if re.fullmatch(r"[a-z0-9_]{1,64}", raw_code) else "resolution_failed"
            side_effect_certainty = getattr(exc, "side_effect_certainty", None)
            if admission.recipient.allow_creation and side_effect_certainty != "none":
                return await self._mark_error(
                    admission.intent_id,
                    "resolution_uncertain",
                    "Recipient resolution outcome is uncertain",
                    state="uncertain",
                )
            return await self._mark_error(
                admission.intent_id, str(code), "Recipient resolution failed"
            )
        if (
            target.channel_type != recipient.channel_type
            or target.account_id != admission.account.account_id
            or target.chat_kind != recipient.chat_kind
        ):
            return await self._mark_error(
                admission.intent_id,
                "resolution_route_invalid",
                "Recipient resolution returned an invalid route",
            )
        await self._mark_resolved(admission.intent_id, target)
        return target

    async def _mark_resolved(self, intent_id: str, target: ResolvedChannelTarget) -> None:
        async with self._session_factory() as session:
            row = await get_channel_recipient_intent(session, intent_id)
            if row is not None:
                row.resolution_state = "resolved"
                row.resolved_route_json = _target_data(target)
                row.side_effect_certainty = "none"
                row.resolution_lease_token = None
                row.resolution_lease_expires_at = None
                await session.commit()

    async def _runtime_capability_error(
        self, recipient: ChannelRecipient, account_id: str
    ) -> ChannelRecipientResult | None:
        if _canonical_target(recipient, account_id) is not None:
            return None
        manager = self._channel_manager_ref() if self._channel_manager_ref else None
        if manager is None:
            return None
        adapter = manager.get_adapter(account_id)
        if adapter is None:
            return ChannelRecipientResult(
                status="failed",
                error=ChannelRecipientError(
                    code="account_unavailable",
                    message="Recipient account is unavailable",
                ),
            )
        capabilities = getattr(adapter, "capabilities", None)
        recipient_capabilities = getattr(capabilities, "recipient_capabilities", None)
        if recipient_capabilities is None:
            return None
        if recipient.address_kind not in set(recipient_capabilities.address_kinds):
            return ChannelRecipientResult(
                status="failed",
                error=ChannelRecipientError(
                    code="unsupported_address_kind",
                    message="Recipient address kind is not supported by this account",
                ),
            )
        if recipient.chat_kind not in set(recipient_capabilities.chat_kinds):
            return ChannelRecipientResult(
                status="failed",
                error=ChannelRecipientError(
                    code="unsupported_chat_kind",
                    message="Recipient chat kind is not supported by this account",
                ),
            )
        canonical = _canonical_target(recipient, account_id) is not None
        if not canonical and not recipient.allow_resolution and not recipient.allow_creation:
            return ChannelRecipientResult(
                status="failed",
                error=ChannelRecipientError(
                    code="resolution_not_authorized",
                    message="Recipient resolution is not authorized",
                ),
            )
        if recipient.allow_resolution and not recipient_capabilities.supports_resolution:
            return ChannelRecipientResult(
                status="failed",
                error=ChannelRecipientError(
                    code="resolution_unsupported",
                    message="Recipient resolution is not supported by this account",
                ),
            )
        if recipient.allow_creation and not recipient_capabilities.supports_creation:
            return ChannelRecipientResult(
                status="failed",
                error=ChannelRecipientError(
                    code="creation_unsupported",
                    message="Recipient creation is not supported by this account",
                ),
            )
        return None

    async def _mark_error(
        self,
        intent_id: str,
        code: str,
        message: str,
        *,
        state: str = "failed",
    ) -> ChannelRecipientResult:
        async with self._session_factory() as session:
            row = await get_channel_recipient_intent(session, intent_id)
            if row is not None:
                row.resolution_state = state
                row.safe_error_json = {"code": code, "message": message}
                row.resolution_lease_token = None
                row.resolution_lease_expires_at = None
                await session.commit()
        return ChannelRecipientResult(
            status={
                "blocked": "blocked",
                "uncertain": "uncertain",
            }.get(state, "failed"),
            intent_id=intent_id,
            error=ChannelRecipientError(code=code, message=message),
        )


def _fingerprint(
    user_email: str,
    recipient: ChannelRecipient,
    content: str,
    artifacts: list[dict[str, Any]],
    idempotency_scope: str,
) -> str:
    body = {
        "user": user_email,
        "channel": recipient.channel_type,
        "address_kind": recipient.address_kind,
        "address": recipient.address,
        "chat_kind": recipient.chat_kind,
        "content": content,
        "artifacts": _stable_artifact_identity(artifacts),
        "allow_resolution": recipient.allow_resolution,
        "allow_creation": recipient.allow_creation,
        "idempotency_scope": idempotency_scope,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _intent_id(user_email: str, idempotency_scope: str, idempotency_key: str) -> str:
    identity = "\x00".join((user_email, idempotency_scope, idempotency_key))
    return f"rint_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _stable_artifact_identity(artifacts: list[dict[str, Any]]) -> list[str]:
    identities: list[str] = []
    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            identities.append(f"artifact:{artifact_id}")
            continue
        inline = {
            "content_b64": artifact.get("content_b64"),
            "filename": artifact.get("filename"),
            "mime_type": artifact.get("mime_type"),
            "size_bytes": artifact.get("size_bytes"),
        }
        identities.append(
            "inline:"
            + hashlib.sha256(
                json.dumps(inline, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
    return identities


def _idempotency_conflict(intent_id: str) -> ChannelRecipientResult:
    return ChannelRecipientResult(
        status="conflict",
        intent_id=intent_id,
        error=ChannelRecipientError(
            code="idempotency_conflict",
            message="Recipient idempotency key conflicts with existing content",
        ),
    )


def _provisional_route_key(
    codec: ChannelTargetRefCodec,
    *,
    user_email: str,
    account_id: str,
    recipient: ChannelRecipient,
) -> str:
    material = "\x00".join(
        (
            user_email,
            account_id,
            recipient.channel_type,
            recipient.address_kind or "",
            recipient.address,
        )
    ).encode()
    return hmac.new(
        codec.derive_domain_key(b"cognis/channel-recipient-route/v1"),
        material,
        "sha256",
    ).hexdigest()


def _canonical_target(recipient: ChannelRecipient, account_id: str) -> ResolvedChannelTarget | None:
    """Construct routes whose address is already a provider chat identifier."""

    kind = recipient.address_kind
    address = recipient.address
    chat_id = address
    if kind == "whatsapp_e164":
        chat_id = address[1:]
    if kind in {
        "signal_e164",
        "signal_uuid",
        "signal_group_id",
        "whatsapp_e164",
        "telegram_chat_id",
        "discord_channel_id",
        "slack_conversation_id",
        "matrix_room_id",
        "irc_nick",
        "irc_channel",
        "google_chat_space",
        "bluebubbles_chat_guid",
    }:
        return ResolvedChannelTarget(
            channel_type=recipient.channel_type,
            account_id=account_id,
            chat_id=chat_id,
            chat_kind=recipient.chat_kind or "direct",
        )
    return None


def _intent_result(row: ChannelRecipientIntentRow) -> ChannelRecipientResult:
    error = row.safe_error_json
    status = {
        "resolved": "queued",
        "promoted": "queued",
        "uncertain": "uncertain",
        "blocked": "blocked",
    }.get(row.resolution_state, "failed" if error else "resolving")
    return ChannelRecipientResult(
        status=status,
        intent_id=row.intent_id,
        delivery_id=_recipient_delivery_id(row.intent_id),
        error=ChannelRecipientError.model_validate(error) if isinstance(error, dict) else None,
    )


def _recipient_delivery_id(intent_id: str) -> str:
    return f"cdel_recipient_{intent_id}"


def _target_data(target: ResolvedChannelTarget) -> dict[str, Any]:
    """Serialize an internal route for storage, never for a tool response."""

    return {
        "channel_type": target.channel_type,
        "account_id": target.account_id,
        "chat_id": target.chat_id,
        "chat_kind": target.chat_kind,
        "display_name": target.display_name,
        "thread_id": target.thread_id,
    }
