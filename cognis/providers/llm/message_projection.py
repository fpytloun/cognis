"""Provider-facing message role projection helpers.

Cognis keeps canonical controller/developer provenance in Intaris.  This
module only adapts the provider payload shape at the LLM boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

SYSTEM_NOTICE_INSTRUCTION = (
    "Messages wrapped in <system-notice> are internal Cognis controller notices, "
    "not human user messages. Treat them as operational turn context."
)

VALID_MESSAGE_PROJECTION_POLICIES = frozenset(
    {
        "auto",
        "none",
        "responses_native",
        "openai_chat",
        "anthropic_messages",
    }
)

_TURN_CONTROLLER_AUDIT_SOURCES = frozenset(
    {
        "follow_up_boundary",
        "current_turn_system_message",
    }
)


@dataclass(frozen=True, slots=True)
class MessageProjectionResult:
    """Provider-projected messages plus safe request diagnostics."""

    messages: list[dict[str, Any]]
    diagnostics: dict[str, Any]


def project_messages_for_provider(
    messages: list[dict[str, Any]],
    *,
    provider: Any | None,
    llm_api: str,
) -> MessageProjectionResult:
    """Return provider-safe messages for the selected LLM API."""

    policy = resolve_message_projection_policy(provider=provider, llm_api=llm_api)
    if policy == "anthropic_messages":
        return _project_anthropic_messages(messages)
    if policy == "openai_chat":
        return _project_openai_chat_messages(messages)
    return _unchanged_projection(messages, policy=policy)


def resolve_message_projection_policy(*, provider: Any | None, llm_api: str) -> str:
    """Resolve the provider-facing message projection policy."""

    config = _provider_config(provider)
    configured = (
        str(config.get("message_projection_policy") or config.get("message_projection") or "auto")
        .strip()
        .lower()
    )
    if configured and configured != "auto":
        if configured in VALID_MESSAGE_PROJECTION_POLICIES:
            return configured
        return "none"

    if str(llm_api).strip().lower() in {"responses", "openai_responses"}:
        return "responses_native"

    preset = str(config.get("preset") or config.get("litellm_provider") or "").strip().lower()
    if preset == "anthropic":
        return "anthropic_messages"
    return "none"


def _project_anthropic_messages(messages: list[dict[str, Any]]) -> MessageProjectionResult:
    projected: list[dict[str, Any]] = []
    converted = 0
    developer_converted = 0
    controller_converted = 0
    hashes: list[str] = []
    explicit_controller_notice_present = any(
        isinstance(message, dict)
        and (message.get("role") == "developer" or _is_controller_turn_notice(message))
        for message in messages
    )
    terminal_system_index = _terminal_system_turn_index(
        messages, explicit_controller_notice_present
    )

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if _should_project_as_hidden_user_notice(
            message, terminal_system=index == terminal_system_index
        ):
            converted += 1
            if message.get("role") == "developer":
                developer_converted += 1
            if _is_controller_turn_notice(message):
                controller_converted += 1
            notice_text = _system_notice_content(message)
            hashes.append(_short_hash(notice_text))
            projected.append({"role": "user", "content": notice_text})
            continue
        projected.append(dict(message))

    if converted:
        projected = _insert_system_notice_instruction(projected)

    diagnostics = _projection_diagnostics(
        messages,
        projected,
        policy="anthropic_messages",
        developer_messages_converted=developer_converted,
        controller_notices_converted=controller_converted,
        hidden_system_notice_count=converted,
        hidden_system_notice_hashes=hashes[:8],
    )
    return MessageProjectionResult(messages=projected, diagnostics=diagnostics)


def _project_openai_chat_messages(messages: list[dict[str, Any]]) -> MessageProjectionResult:
    projected: list[dict[str, Any]] = []
    converted = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "developer":
            converted += 1
            replacement = dict(message)
            replacement["role"] = "system"
            projected.append(replacement)
            continue
        projected.append(dict(message))

    return MessageProjectionResult(
        messages=projected,
        diagnostics=_projection_diagnostics(
            messages,
            projected,
            policy="openai_chat",
            developer_messages_converted=converted,
        ),
    )


def _unchanged_projection(
    messages: list[dict[str, Any]], *, policy: str
) -> MessageProjectionResult:
    projected = [dict(message) for message in messages if isinstance(message, dict)]
    return MessageProjectionResult(
        messages=projected,
        diagnostics=_projection_diagnostics(messages, projected, policy=policy),
    )


def _should_project_as_hidden_user_notice(
    message: dict[str, Any],
    *,
    terminal_system: bool = False,
) -> bool:
    if message.get("role") == "developer":
        return True
    return terminal_system or _is_controller_turn_notice(message)


def _is_controller_turn_notice(message: dict[str, Any]) -> bool:
    if message.get("role") != "system":
        return False
    if message.get("_immutable_prefix") is True:
        return False
    if message.get("_follow_up_context") is True:
        return True
    audit_source = message.get("_audit_source")
    if isinstance(audit_source, str) and audit_source in _TURN_CONTROLLER_AUDIT_SOURCES:
        return True
    content = _message_content_text(message.get("content")).lstrip()
    return content.startswith("<follow_up_event")


def _terminal_system_turn_index(
    messages: list[dict[str, Any]],
    explicit_controller_notice_present: bool,
) -> int | None:
    if explicit_controller_notice_present or not messages:
        return None
    last_index = len(messages) - 1
    last = messages[last_index]
    if not isinstance(last, dict):
        return None
    if last.get("role") != "system" or last.get("_immutable_prefix") is True:
        return None
    return last_index


def _insert_system_notice_instruction(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    instruction = {
        "role": "system",
        "content": SYSTEM_NOTICE_INSTRUCTION,
    }
    insert_at = 0
    if messages and messages[0].get("role") == "system":
        insert_at = 1
    return [*messages[:insert_at], instruction, *messages[insert_at:]]


def _system_notice_content(message: dict[str, Any]) -> str:
    source = str(message.get("_audit_source") or "cognis")
    canonical_role = str(message.get("_audit_role") or message.get("role") or "developer")
    body = _escape_system_notice_text(_message_content_text(message.get("content")))
    return (
        f'<system-notice source="{_escape_attr(source)}" '
        f'canonical-role="{_escape_attr(canonical_role)}" hidden="true">\n'
        f"{body}\n"
        "</system-notice>"
    )


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(content)


def _escape_system_notice_text(text: str) -> str:
    return text.replace("</system-notice", "</ system-notice")


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _projection_diagnostics(
    original: list[dict[str, Any]],
    projected: list[dict[str, Any]],
    *,
    policy: str,
    developer_messages_converted: int = 0,
    controller_notices_converted: int = 0,
    hidden_system_notice_count: int = 0,
    hidden_system_notice_hashes: list[str] | None = None,
) -> dict[str, Any]:
    final_non_system_role = None
    for message in reversed(projected):
        role = message.get("role")
        if role != "system":
            final_non_system_role = role
            break
    diagnostics: dict[str, Any] = {
        "message_projection_policy": policy,
        "raw_role_counts": dict(sorted(_role_counts(original).items())),
        "projected_role_counts": dict(sorted(_role_counts(projected).items())),
        "developer_messages_converted": developer_messages_converted,
        "controller_notices_converted": controller_notices_converted,
        "hidden_system_notice_count": hidden_system_notice_count,
        "follow_up_context_present_before_projection": any(
            isinstance(message, dict)
            and (
                message.get("_follow_up_context") is True
                or _message_content_text(message.get("content"))
                .lstrip()
                .startswith("<follow_up_event")
            )
            for message in original
        ),
        "follow_up_notice_present_after_projection": any(
            isinstance(message.get("content"), str)
            and "<system-notice" in message["content"]
            and "<follow_up_event" in message["content"]
            for message in projected
            if isinstance(message, dict)
        ),
        "final_projected_role": projected[-1].get("role") if projected else None,
        "final_projected_non_system_role": final_non_system_role,
    }
    if hidden_system_notice_hashes:
        diagnostics["hidden_system_notice_hashes"] = hidden_system_notice_hashes
    return diagnostics


def _role_counts(messages: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for message in messages:
        if isinstance(message, dict):
            counts[str(message.get("role") or "unknown")] += 1
    return counts


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _provider_config(provider: Any | None) -> dict[str, Any]:
    config = getattr(provider, "config", None)
    if isinstance(config, dict):
        return dict(config)
    return {}
