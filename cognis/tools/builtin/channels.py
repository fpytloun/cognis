"""Built-in tools for observed channel targets and durable one-shot delivery."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, and_, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.channels.bindings import ManagedChannelBindingLookup, NoManagedChannelBindingLookup
from cognis.channels.constants import (
    CHANNEL_RECIPIENT_MESSAGE_SOURCE,
    CHANNEL_TOOL_CONVERSATION_PREFIX,
    CHANNEL_TOOL_MESSAGE_SOURCE,
)
from cognis.channels.recipients import ADDRESS_KINDS, RecipientResolutionService
from cognis.channels.route_admission import (
    active_managed_binding_id,
    lock_channel_route,
)
from cognis.channels.target_refs import ChannelTargetRef, ChannelTargetRefCodec
from cognis.core.artifact_inputs import (
    authorize_outbound_artifact_refs,
    resolve_owned_artifact_refs,
    safe_attachment_metadata,
)
from cognis.models.channel import ChannelRecipient
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolResult, ToolSource
from cognis.store.models import (
    ChannelAccountRow,
    ChannelDeliveryOutboxRow,
    ChannelDeliveryReceiptRow,
    ChannelInboundLedgerRow,
    ChannelRecipientIntentRow,
    Conversation,
    ManagedChannelBinding,
    ManagedConversationLink,
)
from cognis.store.queries import (
    create_or_get_channel_delivery_outbox,
    get_channel_account,
    get_channel_delivery_outbox,
    get_channel_observed_target,
    list_channel_accounts,
    list_channel_observed_targets,
)
from cognis.tools.registry import ToolExecutionContext

_SOURCE = ToolSource(type="builtin")
_MAX_SEARCH_LIMIT = 100
_MAX_CONTENT_LENGTH = 100_000
_RECIPIENT_ADDRESS_KINDS = sorted(
    address_kind for address_kinds in ADDRESS_KINDS.values() for address_kind in address_kinds
)


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    read_only: bool,
    schema_extensions: dict[str, Any] | None = None,
    examples: list[dict[str, Any]] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
            **(schema_extensions or {}),
        },
        source=_SOURCE,
        category="channels",
        read_only=read_only,
        examples=examples or [],
    )


LIST_CHANNEL_ACCOUNTS_TOOL = _tool(
    "list_channel_accounts",
    (
        "List channel accounts owned by the current user without exposing provider identifiers. "
        "Results identify the shared transport account; outbound identity does not follow the "
        "initiating agent."
    ),
    {"enabled_only": {"type": "boolean", "default": True}},
    read_only=True,
)
SEARCH_CHANNEL_TARGETS_TOOL = _tool(
    "search_channel_targets",
    (
        "Search direct chats and groups already observed or successfully contacted by Cognis. "
        "Provider directory discovery is not available. Use read_channel_messages on a "
        "returned target_ref for the observed external transcript."
    ),
    {
        "query": {"type": "string", "default": ""},
        "account_ref": {"type": "string"},
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": ["direct", "group"]},
            "uniqueItems": True,
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_SEARCH_LIMIT, "default": 25},
    },
    read_only=True,
)
SEND_CHANNEL_MESSAGE_TOOL = _tool(
    "send_channel_message",
    (
        "Queue one durable text and/or artifact message to an observed target or an explicit "
        "recipient. "
        "This creates no Cognis conversation and does not wait for a reply. The provider uses "
        "the shared channel account identity, not the initiating agent identity."
    ),
    {
        "target_ref": {"type": "string"},
        "recipient": {
            "type": "object",
            "description": (
                "Explicit provider recipient. Omit account_ref only when exactly one enabled "
                "owned account exists for channel_type. Provider-side lookup or conversation "
                "creation occurs only when the corresponding allow flag is true. After delivery, "
                "use get_channel_delivery to obtain the reusable target_ref."
            ),
            "properties": {
                "channel_type": {"type": "string", "enum": sorted(ADDRESS_KINDS)},
                "address": {
                    "type": "string",
                    "description": "Provider address accepted by the selected address_kind.",
                },
                "account_ref": {
                    "type": "string",
                    "description": "Opaque account reference from list_channel_accounts.",
                },
                "address_kind": {"type": "string", "enum": _RECIPIENT_ADDRESS_KINDS},
                "chat_kind": {"type": "string", "enum": ["direct", "group"]},
                "allow_resolution": {
                    "type": "boolean",
                    "default": False,
                    "description": "Permit provider lookup of an existing route.",
                },
                "allow_creation": {
                    "type": "boolean",
                    "default": False,
                    "description": "Permit provider creation of a direct chat or room.",
                },
            },
            "required": ["channel_type", "address"],
            "additionalProperties": False,
        },
        "content": {"type": "string", "maxLength": _MAX_CONTENT_LENGTH},
        "artifact_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    required=["idempotency_key"],
    read_only=False,
    schema_extensions={
        "oneOf": [
            {"required": ["target_ref"], "not": {"required": ["recipient"]}},
            {"required": ["recipient"], "not": {"required": ["target_ref"]}},
        ]
    },
    examples=[
        {
            "recipient": {
                "channel_type": "signal",
                "address": "+12025550123",
            },
            "content": "Hello.",
            "idempotency_key": "first-contact-2026-08-04",
        }
    ],
)
READ_CHANNEL_MESSAGES_TOOL = _tool(
    "read_channel_messages",
    (
        "Read the Cognis-observed external transcript for an observed or successfully contacted "
        "authorized target. Returns "
        "inbound participant messages and authoritative outbound delivery status, never "
        "private controller instructions. Coverage is cognis_observed, not provider backfill."
    ),
    {
        "target_ref": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        "cursor": {"type": "string"},
        "since": {"type": "string"},
        "until": {"type": "string"},
        "direction": {"type": "string", "enum": ["inbound", "outbound"]},
        "status": {"type": "string"},
        "anchor": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["latest", "from_start", "before", "after", "around"],
                },
                "message_ref": {"type": "string"},
                "before": {"type": "integer", "minimum": 0, "maximum": 50},
                "after": {"type": "integer", "minimum": 0, "maximum": 50},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    required=["target_ref"],
    read_only=True,
)
GET_CHANNEL_DELIVERY_TOOL = _tool(
    "get_channel_delivery",
    "Inspect one channel delivery owned by the current user.",
    {"delivery_id": {"type": "string"}},
    required=["delivery_id"],
    read_only=True,
)


def channel_tools() -> list[ToolDefinition]:
    return [
        LIST_CHANNEL_ACCOUNTS_TOOL,
        SEARCH_CHANNEL_TARGETS_TOOL,
        SEND_CHANNEL_MESSAGE_TOOL,
        GET_CHANNEL_DELIVERY_TOOL,
        READ_CHANNEL_MESSAGES_TOOL,
    ]


def build_channel_tool_handlers(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    application_secret: str | bytes,
    binding_lookup: ManagedChannelBindingLookup | None = None,
    channel_manager_ref: Any | None = None,
    recipient_service: RecipientResolutionService | None = None,
) -> dict[str, Any]:
    codec = ChannelTargetRefCodec(application_secret)
    bindings = binding_lookup or NoManagedChannelBindingLookup()
    recipient_service = recipient_service or RecipientResolutionService(
        session_factory, codec=codec, channel_manager_ref=channel_manager_ref
    )

    async def list_accounts_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, Any]]:
        user_email = _user(context)
        async with session_factory() as session:
            rows = await list_channel_accounts(
                session,
                enabled_only=bool(arguments.get("enabled_only", True)),
                user_email=user_email,
            )
        return [_account_response(codec, row) for row in rows]

    async def search_targets_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        user_email = _user(context)
        query = _normalize_search(str(arguments.get("query") or ""))
        kinds = _normalize_kinds(arguments.get("kinds"))
        limit = min(max(int(arguments.get("limit") or 25), 1), _MAX_SEARCH_LIMIT)
        account_filter: ChannelAccountRow | None = None
        async with session_factory() as session:
            if arguments.get("account_ref"):
                account_ref = codec.decode(
                    str(arguments["account_ref"]),
                    user_email=user_email,
                    expected_kind="account",
                )
                account_filter = await _owned_account(session, account_ref, user_email)
                accounts = [account_filter]
            else:
                accounts = await list_channel_accounts(
                    session, enabled_only=True, user_email=user_email
                )
            targets = await _observed_targets(
                session,
                codec=codec,
                user_email=user_email,
                accounts=accounts,
                query=query,
                kinds=kinds,
                limit=limit,
            )
            observed_target_count = len(
                await list_channel_observed_targets(
                    session,
                    user_email=user_email,
                    account_ids=[account.account_id for account in accounts],
                )
            )
            for result in targets:
                decoded = codec.decode(
                    str(result["target_ref"]),
                    user_email=user_email,
                    expected_kind="target",
                )
                managed = (
                    await session.execute(
                        select(ManagedChannelBinding, ManagedConversationLink)
                        .join(
                            ManagedConversationLink,
                            ManagedConversationLink.link_id == ManagedChannelBinding.link_id,
                        )
                        .where(
                            ManagedChannelBinding.user_email == user_email,
                            ManagedChannelBinding.account_id == decoded.account_id,
                            ManagedChannelBinding.chat_id == decoded.chat_id,
                            ManagedChannelBinding.thread_key == (decoded.thread_id or ""),
                            ManagedChannelBinding.active_route_key.is_not(None),
                        )
                        .order_by(ManagedChannelBinding.updated_at.desc())
                        .limit(1)
                    )
                ).one_or_none()
                latest = (
                    await session.execute(
                        select(ChannelDeliveryOutboxRow)
                        .where(
                            ChannelDeliveryOutboxRow.user_email == user_email,
                            ChannelDeliveryOutboxRow.account_id == decoded.account_id,
                            ChannelDeliveryOutboxRow.chat_id == decoded.chat_id,
                            ChannelDeliveryOutboxRow.thread_id == decoded.thread_id,
                            ChannelDeliveryOutboxRow.source_type == CHANNEL_TOOL_MESSAGE_SOURCE,
                        )
                        .order_by(ChannelDeliveryOutboxRow.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                context_ref = f"{decoded.channel_type}:{decoded.account_id}:{decoded.chat_id}" + (
                    f":{decoded.thread_id}" if decoded.thread_id else ""
                )
                ordinary = (
                    await session.execute(
                        select(Conversation)
                        .where(
                            Conversation.user_email == user_email,
                            Conversation.context_ref == context_ref,
                            Conversation.status == "active",
                        )
                        .order_by(
                            Conversation.last_message_at.desc().nullslast(),
                            Conversation.created_at.desc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                result["latest_owned_conversation_id"] = (
                    ordinary.conversation_id if ordinary is not None else None
                )
                result["active_managed_conversation"] = (
                    {
                        "conversation_id": managed[1].target_conversation_id,
                        "status": managed[0].state,
                    }
                    if managed is not None
                    else None
                )
                result["latest_one_shot_delivery"] = (
                    _delivery_response(latest) if latest is not None else None
                )
        return {
            "targets": targets,
            "capabilities": {
                "observed_chats": True,
                "provider_directory": False,
                "detail": (
                    "No enabled channel accounts are available for observation."
                    if not accounts
                    else "Only chats already observed by Cognis are searchable."
                ),
                "account_count": len(accounts),
                "observed_target_count": observed_target_count,
                "observation_status": (
                    "no_enabled_accounts"
                    if not accounts
                    else (
                        "observed_targets_available"
                        if observed_target_count
                        else "no_observed_traffic"
                    )
                ),
            },
        }

    async def send_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any] | ToolResult:
        user_email = _user(context)
        content = str(arguments.get("content") or "")
        requested_artifact_ids = _requested_artifact_ids(arguments.get("artifact_ids"))
        if not content.strip() and not requested_artifact_ids:
            raise ValueError("Channel message requires content or at least one artifact")
        idempotency_key = str(arguments["idempotency_key"]).strip()
        if not idempotency_key:
            raise ValueError("Channel message idempotency key must not be empty")
        if ("target_ref" in arguments) == ("recipient" in arguments):
            raise ValueError("Exactly one of target_ref or recipient is required")
        if "recipient" in arguments:
            raw_recipient = arguments["recipient"]
            if not isinstance(raw_recipient, dict):
                raise ValueError("Recipient must be an object")
            try:
                recipient = ChannelRecipient.model_validate(raw_recipient)
            except ValueError as exc:
                raise ValueError("Recipient is invalid") from exc
            attachments = await resolve_owned_artifact_refs(
                session_factory,
                requested_artifact_ids,
                user_email=user_email,
                conversation_id=_runtime_access_string(context, "conversation_id"),
                agent_id=_runtime_access_string(context, "agent_id") or None,
            )
            authorized_attachments = await authorize_outbound_artifact_refs(
                session_factory,
                attachments,
                user_email=user_email,
                conversation_id=_runtime_access_string(context, "conversation_id"),
                agent_id=_runtime_access_string(context, "agent_id") or None,
            )
            result = await recipient_service.send(
                user_email=user_email,
                recipient=recipient,
                content=content,
                artifact_metadata=authorized_attachments,
                idempotency_key=idempotency_key,
                conversation_id=_synthetic_conversation_id(user_email),
            )
            if result.error is not None:
                return ToolResult(
                    output=json.dumps(result.model_dump(mode="json", exclude={"target"})),
                    is_error=True,
                )
            return result.model_dump(mode="json", exclude={"target"})
        target = codec.decode(
            str(arguments["target_ref"]),
            user_email=user_email,
            expected_kind="target",
        )
        assert target.chat_id is not None
        async with session_factory() as session:
            account = await _owned_account(session, target, user_email)
        advertised_binding = await bindings.find_active_binding(
            user_email=user_email,
            account_id=target.account_id,
            chat_id=target.chat_id,
            thread_id=target.thread_id,
        )
        if advertised_binding is not None:
            return _active_binding_refusal(
                advertised_binding=advertised_binding,
                transport_identity=_transport_identity(account),
            )
        async with session_factory() as session:
            account = await _owned_account(session, target, user_email)
            await lock_channel_route(session, account.account_id)
            if not await _target_is_observed(session, target, user_email):
                raise ValueError("Channel target is no longer available")
            binding_row = await active_managed_binding_id(
                session,
                user_email=user_email,
                account_id=target.account_id,
                chat_id=target.chat_id,
                thread_id=target.thread_id,
            )
            binding = (
                await bindings.find_active_binding(
                    user_email=user_email,
                    account_id=target.account_id,
                    chat_id=target.chat_id,
                    thread_id=target.thread_id,
                )
                if binding_row is not None
                else None
            )
            if binding_row is not None:
                if binding is None:
                    return _active_binding_refusal(
                        advertised_binding=None,
                        transport_identity=_transport_identity(account),
                    )
                return _active_binding_refusal(
                    advertised_binding=binding,
                    transport_identity=_transport_identity(account),
                )
            delivery_id = _delivery_id(user_email, idempotency_key)
            conversation_id = _synthetic_conversation_id(user_email)
            if await session.get(Conversation, conversation_id) is not None:
                raise RuntimeError("Reserved channel delivery namespace collision")
            existing = await get_channel_delivery_outbox(session, delivery_id)
            if existing is not None:
                _validate_idempotent_reuse(
                    existing,
                    target=target,
                    user_email=user_email,
                    content=content,
                    artifact_ids=requested_artifact_ids,
                )
                response = _delivery_response(existing)
                response["created"] = False
                response["active_binding"] = None
                response["transport_identity"] = _transport_identity(account)
                return response
            attachments = await resolve_owned_artifact_refs(
                session_factory,
                requested_artifact_ids,
                user_email=user_email,
                conversation_id=_runtime_access_string(context, "conversation_id"),
                agent_id=_runtime_access_string(context, "agent_id") or None,
            )
            authorized_attachments = await authorize_outbound_artifact_refs(
                session_factory,
                attachments,
                user_email=user_email,
                conversation_id=_runtime_access_string(context, "conversation_id"),
                agent_id=_runtime_access_string(context, "agent_id") or None,
            )
            row, created = await create_or_get_channel_delivery_outbox(
                session,
                delivery_id=delivery_id,
                user_email=user_email,
                conversation_id=conversation_id,
                session_id=None,
                source_type=CHANNEL_TOOL_MESSAGE_SOURCE,
                source_id=idempotency_key,
                channel_type=target.channel_type,
                account_id=target.account_id,
                chat_id=target.chat_id,
                thread_id=target.thread_id,
                fallback_text=content,
                attachments=authorized_attachments,
                deliverable_id=None,
                next_attempt_at=datetime.now(UTC),
            )
            _validate_idempotent_reuse(
                row,
                target=target,
                user_email=user_email,
                content=content,
                artifact_ids=requested_artifact_ids,
            )
            await session.commit()
            response = _delivery_response(row)
        response["created"] = created
        response["active_binding"] = None
        response["transport_identity"] = _transport_identity(account)
        return response

    async def get_delivery_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        async with session_factory() as session:
            delivery_id = str(arguments["delivery_id"])
            row = await get_channel_delivery_outbox(session, delivery_id)
            if row is None and delivery_id.startswith("cdel_recipient_"):
                intent_id = delivery_id.removeprefix("cdel_recipient_")
                intent = await session.get(ChannelRecipientIntentRow, intent_id)
                if intent is None or intent.user_email != _user(context):
                    raise ValueError("Channel delivery not found")
                return {
                    "delivery_id": delivery_id,
                    "status": intent.resolution_state,
                    "error": (
                        intent.safe_error_json if isinstance(intent.safe_error_json, dict) else None
                    ),
                }
            if (
                row is None
                or row.user_email != _user(context)
                or row.source_type
                not in {CHANNEL_TOOL_MESSAGE_SOURCE, CHANNEL_RECIPIENT_MESSAGE_SOURCE}
            ):
                raise ValueError("Channel delivery not found")
            response = _delivery_response(row)
            if (
                row.source_type == CHANNEL_RECIPIENT_MESSAGE_SOURCE
                and row.completed_chunk_count > 0
            ):
                observed = await get_channel_observed_target(
                    session,
                    user_email=row.user_email,
                    account_id=row.account_id,
                    chat_id=row.chat_id,
                )
                if observed is not None:
                    response["target_ref"] = codec.encode(
                        ChannelTargetRef(
                            kind="target",
                            user_email=row.user_email,
                            account_id=row.account_id,
                            channel_type=row.channel_type,
                            chat_id=observed.chat_id,
                            chat_kind=observed.chat_kind,
                            thread_id=observed.thread_id,
                            sender_id=observed.sender_id,
                        )
                    )
            return response

    async def read_messages_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        user_email = _user(context)
        caller_conversation_id = _runtime_access_string(context, "conversation_id")
        async with session_factory() as session:
            caller = await session.get(Conversation, caller_conversation_id)
        managed_channel_caller = bool(
            caller is not None
            and caller.user_email == user_email
            and caller.context_type == "agent_work"
            and isinstance(caller.context_data, dict)
            and caller.context_data.get("managed_conversation_kind") == "channel"
        )
        if managed_channel_caller:
            target = codec.decode(
                str(arguments["target_ref"]),
                user_email=user_email,
                expected_kind="transcript",
                scope_conversation_id=caller_conversation_id,
            )
        else:
            target = codec.decode(
                str(arguments["target_ref"]),
                user_email=user_email,
                expected_kind="target",
            )
        assert target.chat_id is not None
        since = _parse_datetime(arguments.get("since"))
        until = _parse_datetime(arguments.get("until"))
        if since and until and since > until:
            raise ValueError("since must not be later than until")
        direction = arguments.get("direction")
        status_filter = arguments.get("status")
        context_key = hashlib.sha256(
            json.dumps(
                {
                    "user": user_email,
                    "account": target.account_id,
                    "chat": target.chat_id,
                    "thread": target.thread_id or "",
                    "since": since.isoformat() if since else None,
                    "until": until.isoformat() if until else None,
                    "direction": direction,
                    "status": status_filter,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        anchor = arguments.get("anchor") or {"kind": "latest"}
        if arguments.get("cursor"):
            cursor = codec.decode_cursor(str(arguments["cursor"]))
            if cursor.get("context") != context_key:
                raise ValueError("Channel message cursor is not available for this query")
            anchor = {
                "kind": cursor.get("kind"),
                "message_ref": cursor.get("message_ref"),
                "_timestamp": cursor.get("timestamp"),
            }
        limit = min(max(int(arguments.get("limit") or 25), 1), 100)
        async with session_factory() as session:
            await _owned_account(session, target, user_email)
            if not await _target_is_observed(session, target, user_email):
                raise ValueError("Channel target is no longer available")
            messages = await _query_channel_message_candidates(
                session,
                target=target,
                user_email=user_email,
                since=since,
                until=until,
                direction=direction,
                status_filter=status_filter,
                anchor=anchor,
                limit=limit,
            )
        messages.sort(key=lambda item: (item["timestamp"], item["message_ref"]))
        page, anchor_used = _channel_message_window(messages, anchor, limit)
        next_cursor = (
            codec.encode_cursor(
                {
                    "context": context_key,
                    "kind": "after",
                    "message_ref": page[-1]["message_ref"],
                    "timestamp": page[-1]["timestamp"],
                }
            )
            if page and page[-1] != messages[-1]
            else None
        )
        prev_cursor = (
            codec.encode_cursor(
                {
                    "context": context_key,
                    "kind": "before",
                    "message_ref": page[0]["message_ref"],
                    "timestamp": page[0]["timestamp"],
                }
            )
            if page and page[0] != messages[0]
            else None
        )
        stored_output, output_anchors = _channel_message_stored_output(page)
        preview = {
            "coverage": "cognis_observed",
            "messages": [_channel_message_preview(item) for item in page[:10]],
            "stored_message_count": len(page),
            "page": {"next_cursor": next_cursor, "prev_cursor": prev_cursor},
            "anchor_used": anchor_used,
        }
        return ToolResult(
            output=json.dumps(preview, default=str),
            metadata={
                "stored_output": stored_output,
                "output_anchors": output_anchors,
                "producer_truncated": len(page) > 10,
            },
        )

    return {
        LIST_CHANNEL_ACCOUNTS_TOOL.name: list_accounts_handler,
        SEARCH_CHANNEL_TARGETS_TOOL.name: search_targets_handler,
        SEND_CHANNEL_MESSAGE_TOOL.name: send_handler,
        GET_CHANNEL_DELIVERY_TOOL.name: get_delivery_handler,
        READ_CHANNEL_MESSAGES_TOOL.name: read_messages_handler,
    }


def _user(context: ToolExecutionContext) -> str:
    candidates = [context.runtime_metadata, context.shared_runtime_metadata or {}]
    for metadata in candidates:
        access = metadata.get("runtime_access")
        if isinstance(access, dict):
            user_email = access.get("user_email")
            if isinstance(user_email, str) and user_email:
                return user_email
    raise ValueError("User context is unavailable")


def _runtime_access_string(context: ToolExecutionContext, key: str) -> str:
    candidates = [context.runtime_metadata, context.shared_runtime_metadata or {}]
    for metadata in candidates:
        runtime_access = metadata.get("runtime_access")
        value = runtime_access.get(key) if isinstance(runtime_access, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"Channel tool runtime is missing {key}")


def _account_response(codec: ChannelTargetRefCodec, row: ChannelAccountRow) -> dict[str, Any]:
    return {
        "account_ref": codec.encode(
            ChannelTargetRef(
                kind="account",
                user_email=row.user_email,
                account_id=row.account_id,
                channel_type=row.channel_type,
            )
        ),
        "channel_type": row.channel_type,
        "display_name": row.display_name,
        "enabled": row.enabled,
        "agent_id": row.agent_id,
        "transport_identity": _transport_identity(row),
        "target_discovery": {"observed_chats": True, "provider_directory": False},
    }


async def _owned_account(
    session: AsyncSession, ref: ChannelTargetRef, user_email: str
) -> ChannelAccountRow:
    row = await get_channel_account(session, ref.account_id)
    if (
        row is None
        or row.user_email != user_email
        or row.channel_type != ref.channel_type
        or not row.enabled
    ):
        raise ValueError("Channel account not found")
    return row


async def _observed_targets(
    session: AsyncSession,
    *,
    codec: ChannelTargetRefCodec,
    user_email: str,
    accounts: list[ChannelAccountRow],
    query: str,
    kinds: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    by_id = {row.account_id: row for row in accounts}
    if not by_id:
        return []
    rows = await list_channel_observed_targets(
        session, user_email=user_email, account_ids=list(by_id)
    )
    targets: list[dict[str, Any]] = []
    for row in rows:
        account = by_id.get(row.account_id)
        if account is None or row.chat_kind not in kinds:
            continue
        display_name = row.display_name or "Observed chat"
        if query and query not in _normalize_search(
            f"{display_name} {account.display_name} {row.sender_id or ''}"
        ):
            continue
        targets.append(
            {
                "target_ref": codec.encode(
                    ChannelTargetRef(
                        kind="target",
                        user_email=user_email,
                        account_id=row.account_id,
                        channel_type=account.channel_type,
                        chat_id=row.chat_id,
                        chat_kind=row.chat_kind,
                        thread_id=row.thread_id,
                        sender_id=row.sender_id,
                    )
                ),
                "account_ref": codec.encode(
                    ChannelTargetRef(
                        kind="account",
                        user_email=user_email,
                        account_id=row.account_id,
                        channel_type=account.channel_type,
                    )
                ),
                "channel_type": account.channel_type,
                "kind": row.chat_kind,
                "display_name": display_name,
                "source": "observed_chat",
                "last_active_at": row.last_observed_at.isoformat(),
                "transport_identity": _transport_identity(account),
            }
        )
        if len(targets) >= limit:
            break
    return targets


async def _target_is_observed(
    session: AsyncSession, target: ChannelTargetRef, user_email: str
) -> bool:
    row = await get_channel_observed_target(
        session,
        user_email=user_email,
        account_id=target.account_id,
        chat_id=target.chat_id or "",
    )
    return bool(row is not None and row.chat_kind == target.chat_kind)


def _normalize_kinds(value: Any) -> set[str]:
    if value is None:
        return {"direct", "group"}
    if not isinstance(value, list) or not value:
        raise ValueError("Channel target kinds must be a non-empty list")
    kinds = {str(item) for item in value}
    if not kinds <= {"direct", "group"}:
        raise ValueError("Unsupported channel target kind")
    return kinds


def _normalize_search(value: str) -> str:
    return " ".join(value.casefold().split())


def _requested_artifact_ids(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("artifact_ids must contain at most 10 IDs")
    artifact_ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("artifact_ids must contain persisted Cognis artifact IDs")
        artifact_ids.append(item.strip())
    return artifact_ids


def _delivery_id(user_email: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"channel-tool-delivery:v1:{user_email}:{idempotency_key}".encode()
    ).hexdigest()[:32]
    return f"cdel_{digest}"


def _synthetic_conversation_id(user_email: str) -> str:
    digest = hashlib.sha256(f"channel-tool-conversation:v1:{user_email}".encode()).hexdigest()
    return f"{CHANNEL_TOOL_CONVERSATION_PREFIX}{digest}"


def _validate_idempotent_reuse(
    row: ChannelDeliveryOutboxRow,
    *,
    target: ChannelTargetRef,
    user_email: str,
    content: str,
    artifact_ids: list[str],
) -> None:
    if (
        row.user_email != user_email
        or row.source_type != CHANNEL_TOOL_MESSAGE_SOURCE
        or row.channel_type != target.channel_type
        or row.account_id != target.account_id
        or row.chat_id != target.chat_id
        or row.thread_id != target.thread_id
        or row.fallback_text != content
        or [str(item["artifact_id"]) for item in safe_attachment_metadata(row.attachments_json)]
        != artifact_ids
    ):
        raise ValueError("Channel message idempotency key conflicts with an existing delivery")


def _delivery_response(row: ChannelDeliveryOutboxRow) -> dict[str, Any]:
    return {
        "delivery_id": row.delivery_id,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "completed_chunk_count": row.completed_chunk_count,
        "projected_chunk_count": row.projected_chunk_count,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "last_error": row.last_error,
        "attachments": safe_attachment_metadata(row.attachments_json),
    }


def _transport_identity(row: ChannelAccountRow) -> dict[str, Any]:
    return {
        "scope": "channel_account",
        "account_display_name": row.display_name,
        "configured_agent_id": row.agent_id,
        "per_message_agent_identity": False,
        "detail": (
            "The provider displays the shared channel account identity. "
            "It does not follow the agent that initiated this message."
        ),
    }


def _active_binding_refusal(
    *,
    advertised_binding: Any,
    transport_identity: dict[str, Any],
) -> ToolResult:
    active_binding = (
        {
            "conversation_id": advertised_binding.conversation_id,
            "agent_id": advertised_binding.agent_id,
            "title": advertised_binding.title,
            "status": advertised_binding.status,
        }
        if advertised_binding is not None
        else None
    )
    return ToolResult(
        output=json.dumps(
            {
                "status": "active_binding",
                "code": "channel_route_managed",
                "message": (
                    "One-shot delivery is refused while this route has an active managed "
                    "conversation. The tool did not submit or deliver the requested content. "
                    "Inspect or close that conversation before sending."
                ),
                "delivery_id": None,
                "created": False,
                "content_submitted": False,
                "externally_delivered": False,
                "active_binding": active_binding,
                "transport_identity": transport_identity,
            },
            sort_keys=True,
        ),
        is_error=True,
    )


def _parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("Datetime must include a timezone")
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _truncate_channel_content(content: str) -> tuple[str, dict[str, object]]:
    limit = 4000
    if len(content) <= limit:
        return content, {"truncated": False, "original_chars": len(content), "limit": limit}
    return (
        content[:limit],
        {"truncated": True, "original_chars": len(content), "limit": limit},
    )


def _channel_message_preview(message: dict[str, Any]) -> dict[str, Any]:
    preview = dict(message)
    content = str(preview.get("content") or "")
    preview["content"] = content[:500]
    preview["preview_truncated"] = len(content) > 500
    return preview


async def _query_channel_message_candidates(
    session: AsyncSession,
    *,
    target: ChannelTargetRef,
    user_email: str,
    since: datetime | None,
    until: datetime | None,
    direction: object,
    status_filter: object,
    anchor: object,
    limit: int,
) -> list[dict[str, Any]]:
    raw_anchor = anchor if isinstance(anchor, dict) else {"kind": "latest"}
    kind = str(raw_anchor.get("kind") or "latest")
    anchor_ref = str(raw_anchor.get("message_ref") or "")
    anchor_time = _parse_datetime(raw_anchor.get("_timestamp"))
    if anchor_time is None and kind in {"before", "after", "around"}:
        anchor_time = await _channel_anchor_time(
            session,
            str(raw_anchor.get("message_ref") or ""),
            user_email=user_email,
        )
        if anchor_time is None:
            raise ValueError("Channel message anchor is not available")
    row_limit = min(limit * 2 + 2, 202)
    messages: list[dict[str, Any]] = []
    if direction != "outbound" and status_filter is None:
        stmt = select(ChannelInboundLedgerRow).where(
            ChannelInboundLedgerRow.user_email == user_email,
            ChannelInboundLedgerRow.account_id == target.account_id,
            ChannelInboundLedgerRow.chat_id == target.chat_id,
            ChannelInboundLedgerRow.thread_key == (target.thread_id or ""),
            ChannelInboundLedgerRow.is_bot_output.is_(False),
            or_(
                ChannelInboundLedgerRow.retain_until.is_(None),
                ChannelInboundLedgerRow.retain_until > datetime.now(UTC),
            ),
        )
        if since is not None:
            stmt = stmt.where(ChannelInboundLedgerRow.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(ChannelInboundLedgerRow.occurred_at <= until)
        rows = await _bounded_channel_rows(
            session,
            stmt,
            time_column=ChannelInboundLedgerRow.occurred_at,
            ref_column=literal("inbound:") + ChannelInboundLedgerRow.inbound_id,
            kind=kind,
            anchor_time=anchor_time,
            anchor_ref=anchor_ref,
            limit=row_limit,
        )
        messages.extend(_inbound_message(row) for row in rows)
    if direction != "inbound":
        if status_filter is None:
            receipt_ref = (
                literal("delivery:")
                + ChannelDeliveryReceiptRow.delivery_id
                + literal(":chunk:")
                + cast(ChannelDeliveryReceiptRow.chunk_index, String)
            )
            stmt = (
                select(ChannelDeliveryReceiptRow, ChannelDeliveryOutboxRow)
                .join(
                    ChannelDeliveryOutboxRow,
                    ChannelDeliveryOutboxRow.delivery_id == ChannelDeliveryReceiptRow.delivery_id,
                )
                .where(
                    ChannelDeliveryOutboxRow.user_email == user_email,
                    ChannelDeliveryOutboxRow.account_id == target.account_id,
                    ChannelDeliveryOutboxRow.chat_id == target.chat_id,
                    ChannelDeliveryOutboxRow.thread_id == target.thread_id,
                    ChannelDeliveryOutboxRow.source_type.in_(
                        {
                            CHANNEL_TOOL_MESSAGE_SOURCE,
                            "managed_channel_final",
                            "direct_turn_result",
                        }
                    ),
                )
            )
            if since is not None:
                stmt = stmt.where(ChannelDeliveryReceiptRow.sent_at >= since)
            if until is not None:
                stmt = stmt.where(ChannelDeliveryReceiptRow.sent_at <= until)
            rows = await _bounded_channel_rows(
                session,
                stmt,
                time_column=ChannelDeliveryReceiptRow.sent_at,
                ref_column=receipt_ref,
                kind=kind,
                anchor_time=anchor_time,
                anchor_ref=anchor_ref,
                limit=row_limit,
                scalar=False,
            )
            messages.extend(_delivered_chunk_message(receipt, row) for receipt, row in rows)
        else:
            timestamp_column = func.coalesce(
                ChannelDeliveryOutboxRow.last_delivered_at,
                ChannelDeliveryOutboxRow.sent_at,
                ChannelDeliveryOutboxRow.updated_at,
                ChannelDeliveryOutboxRow.created_at,
            )
            stmt = select(ChannelDeliveryOutboxRow).where(
                ChannelDeliveryOutboxRow.user_email == user_email,
                ChannelDeliveryOutboxRow.account_id == target.account_id,
                ChannelDeliveryOutboxRow.chat_id == target.chat_id,
                ChannelDeliveryOutboxRow.thread_id == target.thread_id,
                ChannelDeliveryOutboxRow.source_type.in_(
                    {
                        CHANNEL_TOOL_MESSAGE_SOURCE,
                        "managed_channel_final",
                        "direct_turn_result",
                    }
                ),
            )
            stmt = stmt.where(ChannelDeliveryOutboxRow.status == str(status_filter))
            if since is not None:
                stmt = stmt.where(timestamp_column >= since)
            if until is not None:
                stmt = stmt.where(timestamp_column <= until)
            rows = await _bounded_channel_rows(
                session,
                stmt,
                time_column=timestamp_column,
                ref_column=literal("attempt:") + ChannelDeliveryOutboxRow.delivery_id,
                kind=kind,
                anchor_time=anchor_time,
                anchor_ref=anchor_ref,
                limit=row_limit,
            )
            messages.extend(_delivery_attempt(row) for row in rows)
    return messages


async def _bounded_channel_rows(
    session: AsyncSession,
    statement: Any,
    *,
    time_column: Any,
    ref_column: Any,
    kind: str,
    anchor_time: datetime | None,
    anchor_ref: str,
    limit: int,
    scalar: bool = True,
) -> list[Any]:
    def _rows(result: Any) -> list[Any]:
        return list(result.scalars().all()) if scalar else list(result.all())

    if kind == "around" and anchor_time is not None:
        before_bound = or_(
            time_column < anchor_time,
            and_(time_column == anchor_time, ref_column <= anchor_ref),
        )
        before = _rows(
            await session.execute(
                statement.where(before_bound)
                .order_by(time_column.desc(), ref_column.desc())
                .limit(limit)
            )
        )
        after = _rows(
            await session.execute(
                statement.where(
                    or_(
                        time_column > anchor_time,
                        and_(time_column == anchor_time, ref_column > anchor_ref),
                    )
                )
                .order_by(time_column.asc(), ref_column.asc())
                .limit(limit)
            )
        )
        return [*reversed(before), *after]
    descending = kind in {"latest", "before"}
    if anchor_time is not None:
        before_bound = or_(
            time_column < anchor_time,
            and_(time_column == anchor_time, ref_column <= anchor_ref),
        )
        after_bound = or_(
            time_column > anchor_time,
            and_(time_column == anchor_time, ref_column >= anchor_ref),
        )
        statement = statement.where(before_bound if kind == "before" else after_bound)
    ordering = (
        (time_column.desc(), ref_column.desc())
        if descending
        else (time_column.asc(), ref_column.asc())
    )
    rows = _rows(await session.execute(statement.order_by(*ordering).limit(limit)))
    return list(reversed(rows)) if descending else rows


async def _channel_anchor_time(
    session: AsyncSession,
    message_ref: str,
    *,
    user_email: str,
) -> datetime | None:
    if message_ref.startswith("inbound:"):
        row = await session.get(ChannelInboundLedgerRow, message_ref.removeprefix("inbound:"))
        return (
            _as_utc(row.occurred_at) if row is not None and row.user_email == user_email else None
        )
    if message_ref.startswith("attempt:"):
        delivery_id = message_ref.removeprefix("attempt:")
    elif message_ref.startswith("delivery:"):
        delivery_id = message_ref.split(":", 2)[1]
    else:
        return None
    row = await session.get(ChannelDeliveryOutboxRow, delivery_id)
    if row is None or row.user_email != user_email:
        return None
    if ":chunk:" in message_ref:
        chunk_index = int(message_ref.rsplit(":", 1)[1])
        receipt = await session.get(
            ChannelDeliveryReceiptRow,
            {"delivery_id": delivery_id, "chunk_index": chunk_index},
        )
        return _as_utc(receipt.sent_at) if receipt is not None else None
    return _as_utc(row.last_delivered_at or row.sent_at or row.updated_at or row.created_at)


def _inbound_message(row: ChannelInboundLedgerRow) -> dict[str, Any]:
    content, truncation = _truncate_channel_content(row.content)
    raw_attachments = (
        row.platform_data.get("safe_attachments") if isinstance(row.platform_data, dict) else None
    )
    return {
        "message_ref": f"inbound:{row.inbound_id}",
        "anchor": f"message:inbound:{row.inbound_id}",
        "direction": "inbound",
        "timestamp": _as_utc(row.occurred_at).isoformat(),
        "sender": row.sender_name or row.sender_id,
        "content": content,
        "content_truncation": truncation,
        "attachments": safe_attachment_metadata(raw_attachments),
        "delivery_status": None,
        "source_conversation_id": None,
        "source_delivery_id": None,
    }


def _delivered_chunk_message(
    receipt: ChannelDeliveryReceiptRow,
    row: ChannelDeliveryOutboxRow,
) -> dict[str, Any]:
    partial = row.status != "sent" or row.completed_chunk_count != row.projected_chunk_count
    content, truncation = _truncate_channel_content(receipt.content)
    return {
        "message_ref": f"delivery:{row.delivery_id}:chunk:{receipt.chunk_index}",
        "anchor": f"message:delivery:{row.delivery_id}:chunk:{receipt.chunk_index}",
        "record_type": "external_message",
        "externally_observed": True,
        "direction": "outbound",
        "timestamp": _as_utc(receipt.sent_at).isoformat(),
        "sender": "Cognis",
        "content": content,
        "content_truncation": truncation,
        "attachments": safe_attachment_metadata(receipt.attachments_json),
        "delivery_status": row.status,
        "partial_delivery": partial,
        "external_message_id": receipt.external_message_id,
        "source_conversation_id": (
            None
            if row.conversation_id.startswith(CHANNEL_TOOL_CONVERSATION_PREFIX)
            else row.conversation_id
        ),
        "source_delivery_id": row.delivery_id,
    }


def _delivery_attempt(row: ChannelDeliveryOutboxRow) -> dict[str, Any]:
    content, truncation = _truncate_channel_content(row.fallback_text)
    timestamp = row.last_delivered_at or row.sent_at or row.updated_at or row.created_at
    return {
        "message_ref": f"attempt:{row.delivery_id}",
        "anchor": f"message:attempt:{row.delivery_id}",
        "record_type": "delivery_attempt",
        "externally_observed": False,
        "direction": "outbound",
        "timestamp": _as_utc(timestamp).isoformat(),
        "sender": "Cognis",
        "content": content,
        "content_truncation": truncation,
        "attachments": safe_attachment_metadata(row.attachments_json),
        "delivery_status": row.status,
        "delivered_chunk_count": len(row.delivery_receipts_json or []),
        "source_conversation_id": (
            None
            if row.conversation_id.startswith(CHANNEL_TOOL_CONVERSATION_PREFIX)
            else row.conversation_id
        ),
        "source_delivery_id": row.delivery_id,
    }


def _channel_message_window(
    messages: list[dict[str, Any]],
    anchor: object,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = anchor if isinstance(anchor, dict) else {}
    kind = str(raw.get("kind") or "latest")
    ref = raw.get("message_ref")
    index = next(
        (idx for idx, item in enumerate(messages) if item["message_ref"] == ref),
        None,
    )
    if kind in {"before", "after", "around"} and index is None:
        raise ValueError("Channel message anchor was not found")
    if kind == "from_start":
        start, end = 0, min(limit, len(messages))
    elif kind == "before":
        end = index or 0
        start = max(0, end - limit)
    elif kind == "after":
        start = (index or 0) + 1
        end = min(len(messages), start + limit)
    elif kind == "around":
        before_value = raw.get("before")
        before = min(
            max(int(before_value) if before_value is not None else limit // 2, 0),
            50,
            limit - 1,
        )
        after_value = raw.get("after")
        after = min(
            max(
                int(after_value) if after_value is not None else limit - before - 1,
                0,
            ),
            50,
            limit - before - 1,
        )
        start = max(0, (index or 0) - before)
        end = min(len(messages), (index or 0) + after + 1)
    elif kind == "latest":
        end = len(messages)
        start = max(0, end - limit)
    else:
        raise ValueError("Unsupported channel message anchor kind")
    return messages[start:end], {
        "kind": kind,
        "message_ref": ref,
        "before": raw.get("before"),
        "after": raw.get("after"),
    }


def _channel_message_stored_output(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, object]]]:
    lines: list[str] = []
    anchors: list[dict[str, object]] = []
    for message in messages:
        start = len(lines) + 1
        lines.extend(
            [
                f"[[{message['anchor']}]]",
                f"### {message['direction']} · {message['timestamp']} · {message['message_ref']}",
                "Sender: " + json.dumps(message["sender"]),
                f"Delivery status: {message.get('delivery_status') or 'n/a'}",
                "Content: " + json.dumps(message["content"]),
                "Content truncation: " + json.dumps(message["content_truncation"], sort_keys=True),
                "Attachments: " + json.dumps(message["attachments"], sort_keys=True),
                "Source: "
                + json.dumps(
                    {
                        "conversation_id": message.get("source_conversation_id"),
                        "delivery_id": message.get("source_delivery_id"),
                    },
                    sort_keys=True,
                ),
            ]
        )
        end = len(lines)
        anchors.append(
            {
                "anchor": message["anchor"],
                "label": f"{message['direction']} {message['timestamp']}",
                "summary": str(message["content"])[:120],
                "kind": "channel_message",
                "format": "markdown",
                "start_line": start,
                "end_line": end,
            }
        )
        lines.append("")
    return "\n".join(lines), anchors
