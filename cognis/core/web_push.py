"""Web Push delivery for installed PWAs.

The payloads intentionally avoid assistant/user message content. Push is a
wake-up signal that opens the relevant Cognis conversation; full content stays
behind the authenticated app session.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.config import CognisConfig
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.store.models import Agent, Conversation, PushSubscriptionRow
from cognis.store.queries import get_agent, get_conversation

logger = get_logger(__name__)

_PUSH_SEND_TIMEOUT_SECONDS = 5.0
_ALLOWED_PUSH_ENDPOINT_HOSTS = {
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
}
_ALLOWED_PUSH_ENDPOINT_SUFFIXES = (
    ".push.apple.com",
    ".push.services.mozilla.com",
    ".notify.windows.com",
)
_MAX_PUSH_LABEL_LENGTH = 80


@dataclass(frozen=True, slots=True)
class WebPushRuntimeConfig:
    """Runtime VAPID configuration for Web Push."""

    enabled: bool
    public_key: str
    private_key: str
    subject: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PushSubscription:
    """Stored browser push subscription."""

    subscription_id: str
    endpoint: str
    enabled: bool


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _normalize_private_key(value: str) -> str:
    return value.replace("\\n", "\n").strip()


def _public_key_from_pem(private_key_pem: str) -> str | None:
    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
    except Exception:
        return None
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        return None
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(public_bytes)


def _to_sec1_pem(private_key_pem: str) -> str | None:
    """Normalize VAPID private keys to SEC1 PEM for py_vapid compatibility."""

    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
    except Exception:
        return None
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        return None
    sec1 = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return sec1.decode("utf-8").strip()


def _validate_py_vapid_key(private_key_pem: str) -> str | None:
    """Return a reason when py_vapid cannot load the resolved private key."""

    try:
        _load_py_vapid_key(private_key_pem)
    except Exception as exc:
        return f"VAPID key cannot be loaded by py_vapid: {type(exc).__name__}: {exc}"
    return None


def _load_py_vapid_key(private_key_pem: str) -> Any:
    """Load a VAPID object for pywebpush; PEM strings are not accepted by webpush()."""

    try:
        from py_vapid import Vapid
    except Exception as exc:
        raise RuntimeError(f"py_vapid unavailable: {type(exc).__name__}") from exc
    return Vapid.from_pem(private_key_pem.encode("utf-8"))


def _endpoint_host(endpoint: str) -> str:
    try:
        return urlparse(endpoint).hostname or "unknown"
    except Exception:
        return "unknown"


def _warn_on_default_subject(subject: str) -> None:
    if subject == "mailto:admin@localhost":
        logger.warning(
            "web_push: using development VAPID subject; set COGNIS_VAPID_SUBJECT "
            "to a real mailto: or https: contact because Apple Web Push and FCM may reject it"
        )


def _push_label(value: str | None) -> str:
    label = " ".join((value or "").split())
    if len(label) <= _MAX_PUSH_LABEL_LENGTH:
        return label
    return f"{label[: _MAX_PUSH_LABEL_LENGTH - 3].rstrip()}..."


def _agent_notification_title(agent: Agent | None) -> str:
    return (
        _push_label(agent.display_name if agent else None)
        or _push_label(agent.name if agent else None)
        or "Cognis"
    )


def _is_same_origin_relative_icon(value: str) -> bool:
    if not value.startswith("/") or value.startswith("//"):
        return False
    if any(ord(char) < 32 for char in value):
        return False
    parsed = urlparse(value)
    return not parsed.scheme and not parsed.netloc


def _conversation_notification_body(action: str, conversation: Conversation) -> str:
    title = _push_label(conversation.title)
    if title:
        return f"{action} in {title}."
    if action == "New reply":
        return "New reply in this chat."
    return f"{action}."


def _runtime_config_from_key(
    *,
    private_key: str,
    public_key_override: str,
    subject: str,
    public_key_error: str,
) -> WebPushRuntimeConfig:
    derived_public_key = _public_key_from_pem(private_key) or ""
    if public_key_override and derived_public_key and public_key_override != derived_public_key:
        return WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject=subject,
            reason="COGNIS_VAPID_PUBLIC_KEY does not match the resolved private key",
        )
    public_key = public_key_override or derived_public_key
    sec1_private_key = _to_sec1_pem(private_key) or ""
    if not public_key or not sec1_private_key:
        return WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject=subject,
            reason=public_key_error,
        )

    invalid_reason = _validate_py_vapid_key(sec1_private_key)
    if invalid_reason:
        return WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject=subject,
            reason=invalid_reason,
        )

    return WebPushRuntimeConfig(
        enabled=True,
        public_key=public_key,
        private_key=sec1_private_key,
        subject=subject,
    )


def _generate_vapid_private_key(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(pem)
    try:
        tmp_path.chmod(0o600)
    except OSError:
        logger.warning("Could not set VAPID key file permissions")
    tmp_path.replace(path)
    return pem.decode("utf-8")


def validate_push_endpoint(endpoint: str) -> None:
    """Validate a browser push endpoint before the controller ever calls it."""

    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not parsed.hostname:
        logger.warning("web_push: rejected push endpoint with invalid scheme or host")
        raise ValueError("Push endpoint must be an HTTPS URL")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        logger.warning("web_push: rejected push endpoint with IP host", extra={"host": host})
        raise ValueError("Push endpoint must use a trusted push service hostname")
    if host in _ALLOWED_PUSH_ENDPOINT_HOSTS:
        return
    if any(host.endswith(suffix) for suffix in _ALLOWED_PUSH_ENDPOINT_SUFFIXES):
        return
    logger.warning("web_push: rejected untrusted push endpoint", extra={"host": host})
    raise ValueError("Push endpoint host is not a trusted browser push service")


def load_web_push_config(config: CognisConfig) -> WebPushRuntimeConfig:
    """Load VAPID keys from env/file, generating a dev key when appropriate."""

    subject = config.vapid_subject or "mailto:admin@localhost"
    _warn_on_default_subject(subject)
    env_private = _normalize_private_key(config.vapid_private_key)
    env_public = config.vapid_public_key.strip()
    if env_private:
        return _runtime_config_from_key(
            private_key=env_private,
            public_key_override=env_public,
            subject=subject,
            public_key_error="COGNIS_VAPID_PUBLIC_KEY is required when the private key is not PEM",
        )

    path = config.vapid_private_key_path
    try:
        path_exists = path.exists()
    except OSError:
        path_exists = False
    if path_exists:
        try:
            private_key = _normalize_private_key(path.read_text(encoding="utf-8"))
        except OSError:
            return WebPushRuntimeConfig(
                enabled=False,
                public_key="",
                private_key="",
                subject=subject,
                reason="Unable to read VAPID private key file",
            )
        return _runtime_config_from_key(
            private_key=private_key,
            public_key_override=env_public,
            subject=subject,
            public_key_error="Unable to derive VAPID public key",
        )

    if config.require_external_crypto:
        return WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject=subject,
            reason="VAPID key is not configured",
        )

    try:
        private_key = _generate_vapid_private_key(path)
    except OSError:
        return WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject=subject,
            reason="Unable to create VAPID private key file",
        )
    return _runtime_config_from_key(
        private_key=private_key,
        public_key_override=env_public,
        subject=subject,
        public_key_error="Unable to generate VAPID public key",
    )


class WebPushService:
    """Stores browser subscriptions and sends privacy-safe Web Push payloads."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
        config: WebPushRuntimeConfig,
        artifact_store: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._artifact_store = artifact_store
        self._vapid_key: Any | None = None
        if config.enabled:
            try:
                self._vapid_key = _load_py_vapid_key(config.private_key)
            except Exception:
                logger.exception("web_push: failed to load VAPID key for delivery")
        self._pending_tasks: set[asyncio.Task[dict[str, int]]] = set()
        event_bus.subscribe(EventType.TURN_COMPLETED, self._handle_event)
        event_bus.subscribe(EventType.TASK_COMPLETED, self._handle_event)
        event_bus.subscribe(EventType.TASK_FAILED, self._handle_event)
        event_bus.subscribe(EventType.TASK_CANCELLED, self._handle_event)
        event_bus.subscribe(EventType.NOTIFICATION_CREATED, self._handle_event)
        if config.enabled:
            logger.info("web_push: service enabled")
        else:
            logger.info("web_push: service disabled", extra={"reason": config.reason})

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def public_key(self) -> str:
        return self._config.public_key

    @property
    def disabled_reason(self) -> str | None:
        return self._config.reason

    async def upsert_subscription(
        self,
        *,
        user_email: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str | None,
        platform: str | None,
    ) -> PushSubscription:
        """Create or refresh a browser subscription for a user."""

        validate_push_endpoint(endpoint)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                select(PushSubscriptionRow).where(PushSubscriptionRow.endpoint == endpoint)
            )
            row = result.scalar_one_or_none()
            action = "updated"
            if row is None:
                action = "created"
                row = PushSubscriptionRow(
                    subscription_id=f"push_{uuid.uuid4().hex[:16]}",
                    user_email=user_email,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    user_agent=user_agent,
                    platform=platform,
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.user_email = user_email
                row.p256dh = p256dh
                row.auth = auth
                row.user_agent = user_agent
                row.platform = platform
                row.enabled = True
                row.last_error = None
                row.updated_at = now
            await session.commit()
            logger.info(
                "web_push: subscription %s",
                action,
                extra={"endpoint_host": _endpoint_host(endpoint), "platform": platform},
            )
            return PushSubscription(
                subscription_id=row.subscription_id,
                endpoint=row.endpoint,
                enabled=row.enabled,
            )

    async def unsubscribe(self, *, user_email: str, endpoint: str) -> bool:
        """Disable a browser subscription owned by the current user."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(PushSubscriptionRow).where(
                    PushSubscriptionRow.user_email == user_email,
                    PushSubscriptionRow.endpoint == endpoint,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            row.enabled = False
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return True

    async def send_to_user(
        self,
        *,
        user_email: str,
        title: str,
        body: str,
        url: str,
        tag: str,
        kind: str,
        icon: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, int]:
        """Send a push payload to all enabled browser subscriptions for a user."""

        if not self._config.enabled:
            logger.debug("web_push: send skipped because service is disabled", extra={"kind": kind})
            return {"sent_to": 0, "errors": 0}

        async with self._session_factory() as session:
            result = await session.execute(
                select(PushSubscriptionRow).where(
                    PushSubscriptionRow.user_email == user_email,
                    PushSubscriptionRow.enabled.is_(True),
                )
            )
            rows = list(result.scalars().all())

        if not rows:
            logger.debug(
                "web_push: send skipped with no enabled subscriptions", extra={"kind": kind}
            )
            return {"sent_to": 0, "errors": 0}

        logger.info(
            "web_push: sending notification",
            extra={"kind": kind, "subscription_count": len(rows)},
        )

        payload_data = {
            "title": title,
            "body": body,
            "url": url,
            "tag": tag,
            "kind": kind,
        }
        if icon:
            payload_data["icon"] = icon
        if conversation_id:
            payload_data["conversation_id"] = conversation_id
        payload = json.dumps(payload_data, separators=(",", ":"))
        statuses = await asyncio.gather(
            *(self._send_one(row, payload) for row in rows), return_exceptions=True
        )
        errors = 0
        sent = 0
        for status in statuses:
            if status == "sent":
                sent += 1
            else:
                errors += 1
        return {"sent_to": sent, "errors": errors}

    async def _handle_event(self, event: Event) -> None:
        payload = await self._event_payload(event)
        if payload is None:
            logger.debug(
                "web_push: event produced no push payload", extra={"event_type": event.type.value}
            )
            return
        logger.debug("web_push: dispatching event push", extra={"event_type": event.type.value})
        task = asyncio.create_task(self.send_to_user(**payload))
        self._pending_tasks.add(task)
        task.add_done_callback(self._on_delivery_done)

    def _on_delivery_done(self, task: asyncio.Task[dict[str, int]]) -> None:
        self._pending_tasks.discard(task)
        try:
            task.result()
        except Exception:
            logger.exception("web_push: background delivery failed")

    async def _event_payload(self, event: Event) -> dict[str, str] | None:
        if event.type == EventType.TURN_COMPLETED:
            conversation_id = event.data.get("conversation_id")
            if not isinstance(conversation_id, str):
                return None
            async with self._session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                agent = await get_agent(session, conversation.agent_id) if conversation else None
            if conversation is None or conversation.context_type != "web":
                return None
            url = f"/chat/{conversation_id}"
            tag = conversation_id
            user_email = conversation.user_email
            title = _agent_notification_title(agent)
            icon = await self._agent_notification_icon(agent)
            if event.data.get("task_id") or event.data.get("channel_deliverable"):
                return None
            return {
                "user_email": user_email,
                "title": title,
                "body": _conversation_notification_body("New reply", conversation),
                "url": url,
                "tag": tag,
                "kind": "message",
                "conversation_id": conversation_id,
                **({"icon": icon} if icon else {}),
            }

        if event.type in {
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.TASK_CANCELLED,
        }:
            conversation_id = event.data.get("conversation_id")
            if not isinstance(conversation_id, str):
                return None
            async with self._session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                agent = await get_agent(session, conversation.agent_id) if conversation else None
            if conversation is None or conversation.context_type != "web":
                return None
            url = f"/chat/{conversation_id}"
            user_email = conversation.user_email
            title = _agent_notification_title(agent)
            icon = await self._agent_notification_icon(agent)
            status = {
                EventType.TASK_COMPLETED: "Task completed.",
                EventType.TASK_FAILED: "Task failed.",
                EventType.TASK_CANCELLED: "Task was cancelled.",
            }[event.type]
            return {
                "user_email": user_email,
                "title": title,
                "body": _conversation_notification_body(status.rstrip("."), conversation),
                "url": url,
                "tag": f"task:{event.data.get('task_id') or conversation_id}",
                "kind": "task",
                "conversation_id": conversation_id,
                **({"icon": icon} if icon else {}),
            }

        if event.type in {EventType.SCHEDULE_ERROR, EventType.SCHEDULE_DISABLED}:
            user_email = event.data.get("created_by")
            if not isinstance(user_email, str) or not user_email:
                return None
            agent_id = event.data.get("agent_id")
            agent = None
            if isinstance(agent_id, str) and agent_id:
                async with self._session_factory() as session:
                    agent = await get_agent(session, agent_id)
            title = _agent_notification_title(agent)
            icon = await self._agent_notification_icon(agent)
            schedule_id = str(event.data.get("schedule_id") or "schedule")
            schedule_name = str(event.data.get("schedule_name") or "Scheduled task")
            if event.type == EventType.SCHEDULE_DISABLED:
                body = f'Schedule "{schedule_name}" was disabled after repeated failures.'
            else:
                body = f'Schedule "{schedule_name}" failed to start.'
            return {
                "user_email": user_email,
                "title": title,
                "body": body,
                "url": "/tasks",
                "tag": f"schedule:{schedule_id}",
                "kind": "schedule",
                "conversation_id": "",
                **({"icon": icon} if icon else {}),
            }

        if event.type == EventType.NOTIFICATION_CREATED:
            conversation_id = event.data.get("conversation_id")
            if not isinstance(conversation_id, str):
                return None
            async with self._session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                agent = await get_agent(session, conversation.agent_id) if conversation else None
            if conversation is None or conversation.context_type != "web":
                return None
            url = f"/chat/{conversation_id}"
            user_email = conversation.user_email
            title = _agent_notification_title(agent)
            icon = await self._agent_notification_icon(agent)
            notification_type = str(event.data.get("notification_type") or "notification")
            body = {
                "escalation": "Tool approval required.",
                "gate": "Workflow review required.",
                "step_question": "The assistant needs your input.",
                "credential_request": "Credential input required.",
                "auth_challenge": "Authentication challenge requires attention.",
            }.get(notification_type, "Cognis needs your attention.")
            return {
                "user_email": user_email,
                "title": title,
                "body": _conversation_notification_body(body.rstrip("."), conversation),
                "url": url,
                "tag": f"notification:{event.data.get('notification_id') or conversation_id}",
                "kind": notification_type,
                "conversation_id": conversation_id,
                **({"icon": icon} if icon else {}),
            }
        return None

    async def _agent_notification_icon(self, agent: Agent | None) -> str | None:
        if agent is None:
            return None
        if agent.avatar_image_id and self._artifact_store is not None:
            try:
                meta = await self._artifact_store.async_load_metadata(
                    "avatars",
                    agent.avatar_image_id,
                    "image",
                )
                if meta is None or meta.owner_email != agent.owner_email:
                    return None
                if not str(getattr(meta, "content_type", "")).startswith("image/"):
                    return None
                return await self._artifact_store.async_get_public_url(
                    "avatars",
                    agent.avatar_image_id,
                    "image",
                    ttl_seconds=3600,
                )
            except Exception:
                logger.debug(
                    "web_push: unable to sign agent avatar for notification", exc_info=True
                )
        if agent.avatar_url and _is_same_origin_relative_icon(agent.avatar_url):
            return agent.avatar_url
        return None

    async def _send_one(self, row: PushSubscriptionRow, payload: str) -> str:
        status, error = await asyncio.to_thread(self._send_sync, row, payload)
        if status == "sent":
            async with self._session_factory() as session:
                await session.execute(
                    update(PushSubscriptionRow)
                    .where(PushSubscriptionRow.subscription_id == row.subscription_id)
                    .values(last_error=None, updated_at=datetime.now(UTC))
                )
                await session.commit()
            logger.debug(
                "web_push: delivery succeeded",
                extra={"endpoint_host": _endpoint_host(row.endpoint)},
            )
            return status
        async with self._session_factory() as session:
            values: dict[str, Any] = {
                "last_error": error[:500] if error else status,
                "updated_at": datetime.now(UTC),
            }
            if status == "gone":
                values["enabled"] = False
                logger.info(
                    "web_push: subscription disabled after push service returned gone",
                    extra={"endpoint_host": _endpoint_host(row.endpoint)},
                )
            else:
                logger.warning(
                    "web_push: delivery failed",
                    extra={
                        "endpoint_host": _endpoint_host(row.endpoint),
                        "error_class": (error or status).split(":", 1)[0],
                    },
                )
            await session.execute(
                update(PushSubscriptionRow)
                .where(PushSubscriptionRow.subscription_id == row.subscription_id)
                .values(**values)
            )
            await session.commit()
        return status

    def _send_sync(self, row: PushSubscriptionRow, payload: str) -> tuple[str, str | None]:
        try:
            from pywebpush import WebPushException, webpush
        except Exception as exc:
            return "error", f"pywebpush unavailable: {type(exc).__name__}"

        if self._vapid_key is None:
            return "error", "VAPID key unavailable"

        try:
            webpush(
                subscription_info={
                    "endpoint": row.endpoint,
                    "keys": {"p256dh": row.p256dh, "auth": row.auth},
                },
                data=payload,
                vapid_private_key=self._vapid_key,
                vapid_claims={"sub": self._config.subject},
                timeout=_PUSH_SEND_TIMEOUT_SECONDS,
                ttl=60,
            )
            return "sent", None
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in {404, 410}:
                return "gone", str(exc)
            return "error", str(exc)
        except Exception as exc:
            return "error", f"{type(exc).__name__}: {exc}"
