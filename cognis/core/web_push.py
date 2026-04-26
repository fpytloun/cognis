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
from cognis.store.models import PushSubscriptionRow
from cognis.store.queries import get_conversation

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


def _generate_vapid_private_key(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
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
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Push endpoint must be an HTTPS URL")
    host = parsed.hostname.rstrip(".").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        raise ValueError("Push endpoint must use a trusted push service hostname")
    if host in _ALLOWED_PUSH_ENDPOINT_HOSTS:
        return
    if any(host.endswith(suffix) for suffix in _ALLOWED_PUSH_ENDPOINT_SUFFIXES):
        return
    raise ValueError("Push endpoint host is not a trusted browser push service")


def load_web_push_config(config: CognisConfig) -> WebPushRuntimeConfig:
    """Load VAPID keys from env/file, generating a dev key when appropriate."""

    subject = config.vapid_subject or "mailto:admin@localhost"
    env_private = _normalize_private_key(config.vapid_private_key)
    env_public = config.vapid_public_key.strip()
    if env_private:
        public_key = env_public or _public_key_from_pem(env_private) or ""
        if public_key:
            return WebPushRuntimeConfig(
                enabled=True,
                public_key=public_key,
                private_key=env_private,
                subject=subject,
            )
        return WebPushRuntimeConfig(
            enabled=False,
            public_key="",
            private_key="",
            subject=subject,
            reason="COGNIS_VAPID_PUBLIC_KEY is required when the private key is not PEM",
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
        public_key = env_public or _public_key_from_pem(private_key) or ""
        return WebPushRuntimeConfig(
            enabled=bool(public_key),
            public_key=public_key,
            private_key=private_key if public_key else "",
            subject=subject,
            reason=None if public_key else "Unable to derive VAPID public key",
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
    public_key = _public_key_from_pem(private_key) or ""
    return WebPushRuntimeConfig(
        enabled=bool(public_key),
        public_key=public_key,
        private_key=private_key if public_key else "",
        subject=subject,
        reason=None if public_key else "Unable to generate VAPID public key",
    )


class WebPushService:
    """Stores browser subscriptions and sends privacy-safe Web Push payloads."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
        config: WebPushRuntimeConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._pending_tasks: set[asyncio.Task[None]] = set()
        event_bus.subscribe(EventType.TURN_COMPLETED, self._handle_event)
        event_bus.subscribe(EventType.TASK_COMPLETED, self._handle_event)
        event_bus.subscribe(EventType.TASK_FAILED, self._handle_event)
        event_bus.subscribe(EventType.TASK_CANCELLED, self._handle_event)
        event_bus.subscribe(EventType.NOTIFICATION_CREATED, self._handle_event)

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
            if row is None:
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
    ) -> None:
        """Send a push payload to all enabled browser subscriptions for a user."""

        if not self._config.enabled:
            return

        async with self._session_factory() as session:
            result = await session.execute(
                select(PushSubscriptionRow).where(
                    PushSubscriptionRow.user_email == user_email,
                    PushSubscriptionRow.enabled.is_(True),
                )
            )
            rows = list(result.scalars().all())

        if not rows:
            return

        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "url": url,
                "tag": tag,
                "kind": kind,
            },
            separators=(",", ":"),
        )
        await asyncio.gather(
            *(self._send_one(row, payload) for row in rows), return_exceptions=True
        )

    async def _handle_event(self, event: Event) -> None:
        payload = await self._event_payload(event)
        if payload is None:
            return
        task = asyncio.create_task(self.send_to_user(**payload))
        self._pending_tasks.add(task)
        task.add_done_callback(self._on_delivery_done)

    def _on_delivery_done(self, task: asyncio.Task[None]) -> None:
        self._pending_tasks.discard(task)
        try:
            task.result()
        except Exception:
            logger.exception("web_push: background delivery failed")

    async def _event_payload(self, event: Event) -> dict[str, str] | None:
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            return None
        async with self._session_factory() as session:
            conversation = await get_conversation(session, conversation_id)
        if conversation is None or conversation.context_type != "web":
            return None

        url = f"/chat/{conversation_id}"
        tag = conversation_id
        user_email = conversation.user_email

        if event.type == EventType.TURN_COMPLETED:
            if event.data.get("task_id") or event.data.get("channel_deliverable"):
                return None
            return {
                "user_email": user_email,
                "title": "Cognis",
                "body": "New web chat message.",
                "url": url,
                "tag": tag,
                "kind": "message",
            }

        if event.type in {
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.TASK_CANCELLED,
        }:
            status = {
                EventType.TASK_COMPLETED: "Task completed.",
                EventType.TASK_FAILED: "Task failed.",
                EventType.TASK_CANCELLED: "Task was cancelled.",
            }[event.type]
            return {
                "user_email": user_email,
                "title": "Cognis",
                "body": status,
                "url": url,
                "tag": f"task:{event.data.get('task_id') or conversation_id}",
                "kind": "task",
            }

        if event.type == EventType.NOTIFICATION_CREATED:
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
                "title": "Cognis",
                "body": body,
                "url": url,
                "tag": f"notification:{event.data.get('notification_id') or conversation_id}",
                "kind": notification_type,
            }
        return None

    async def _send_one(self, row: PushSubscriptionRow, payload: str) -> None:
        status, error = await asyncio.to_thread(self._send_sync, row, payload)
        if status == "sent":
            return
        async with self._session_factory() as session:
            values: dict[str, Any] = {
                "last_error": error[:500] if error else status,
                "updated_at": datetime.now(UTC),
            }
            if status == "gone":
                values["enabled"] = False
            await session.execute(
                update(PushSubscriptionRow)
                .where(PushSubscriptionRow.subscription_id == row.subscription_id)
                .values(**values)
            )
            await session.commit()

    def _send_sync(self, row: PushSubscriptionRow, payload: str) -> tuple[str, str | None]:
        try:
            from pywebpush import WebPushException, webpush
        except Exception as exc:
            return "error", f"pywebpush unavailable: {type(exc).__name__}"

        try:
            webpush(
                subscription_info={
                    "endpoint": row.endpoint,
                    "keys": {"p256dh": row.p256dh, "auth": row.auth},
                },
                data=payload,
                vapid_private_key=self._config.private_key,
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
