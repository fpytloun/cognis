"""Claude subscription OAuth and direct Anthropic Messages transport.

This module mirrors the controller-managed Codex subscription model: Cognis
stores refreshable OAuth records in encrypted secrets and sends inference
requests directly from the controller.  No Claude credentials are stored in
provider config or on the executor.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from cognis.ownership import SYSTEM_USER_EMAIL

ANTHROPIC_SUBSCRIPTION_SECRET_PREFIX = "llm_oauth_anthropic"
ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_CODE_CALLBACK_URL = "https://platform.claude.com/oauth/code/callback"
ANTHROPIC_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_OAUTH_SCOPES = (
    "org:create_api_key",
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)
ANTHROPIC_REQUIRED_BETAS = (
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
)
ANTHROPIC_EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"
CLAUDE_CODE_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
CLAUDE_CODE_IDENTITY_BRIDGE = "The operative agent identity follows."
CLAUDE_CODE_VERSION = "2.1.87"
CLAUDE_CODE_ENTRYPOINT = "sdk-cli"
CLAUDE_CODE_USER_AGENT = f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)"
CCH_SALT = "59cf53e54c78"
CCH_POSITIONS = (4, 7, 20)
TOOL_PREFIX = "mcp_"


@dataclass(frozen=True, slots=True)
class AnthropicSubscriptionAuth:
    """Fresh Claude subscription access token."""

    access_token: str
    expires_at: float | None = None


def oauth_token_secret_name(provider: Any) -> str:
    config = dict(provider.config or {})
    auth_config = config.get("auth_config")
    if isinstance(auth_config, dict):
        for key in ("token_secret_name", "secret_name"):
            value = auth_config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"{ANTHROPIC_SUBSCRIPTION_SECRET_PREFIX}_{provider.provider_id}"


def oauth_pending_secret_name(provider: Any) -> str:
    return f"{oauth_token_secret_name(provider)}_pending"


def oauth_secret_description(provider: Any) -> str:
    return f"Claude subscription OAuth token cache for LLM provider {provider.provider_id}"


def oauth_secret_owner(provider: Any) -> str:
    return provider.owner_email or SYSTEM_USER_EMAIL


def parse_authorized_record(raw: str) -> dict[str, str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Claude subscription OAuth token cache is invalid; restart OAuth"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Claude subscription OAuth token cache is invalid; restart OAuth")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Claude subscription OAuth is not authorized; complete OAuth first")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("Claude subscription OAuth is not authorized; complete OAuth first")
    result = {"access_token": access_token, "refresh_token": refresh_token}
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, int | float):
        result["expires_at"] = str(float(expires_at))
    elif isinstance(expires_at, str) and expires_at:
        result["expires_at"] = expires_at
    return result


def generate_authorization_state() -> dict[str, Any]:
    verifier = _base64_url(secrets.token_bytes(64))
    challenge = _base64_url(hashlib.sha256(verifier.encode("utf-8")).digest())
    state = secrets.token_hex(16)
    params = {
        "code": "true",
        "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_CODE_CALLBACK_URL,
        "scope": " ".join(ANTHROPIC_OAUTH_SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {
        "status": "pending",
        "authorization_url": f"{ANTHROPIC_AUTHORIZE_URL}?{urlencode(params)}",
        "redirect_uri": ANTHROPIC_CODE_CALLBACK_URL,
        "state": state,
        "code_verifier": verifier,
        "expires_at": time.time() + 15 * 60,
        "created_at": time.time(),
    }


def parse_callback_input(raw: str) -> tuple[str, str] | None:
    value = raw.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        query = parse_qs(parsed.query)
        code = _first_query_value(query, "code")
        state = _first_query_value(query, "state")
        if code and state:
            return code, state
    if "#" in value:
        code, state = value.split("#", 1)
        if code and state:
            return code, state
    query = parse_qs(value)
    code = _first_query_value(query, "code")
    state = _first_query_value(query, "state")
    if code and state:
        return code, state
    return None


async def exchange_authorization_code(
    *,
    callback_input: str,
    pending: dict[str, Any],
) -> dict[str, Any]:
    parsed = parse_callback_input(callback_input)
    if parsed is None:
        raise RuntimeError("Claude subscription OAuth callback is invalid")
    code, state = parsed
    expected_state = pending.get("state")
    if not isinstance(expected_state, str) or state != expected_state:
        raise RuntimeError("Claude subscription OAuth callback state does not match")
    verifier = pending.get("code_verifier")
    redirect_uri = pending.get("redirect_uri") or ANTHROPIC_CODE_CALLBACK_URL
    if not isinstance(verifier, str) or not verifier:
        raise RuntimeError("Claude subscription OAuth pending state is incomplete")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            ANTHROPIC_OAUTH_TOKEN_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": "axios/1.13.6",
            },
            json={
                "code": code,
                "state": state,
                "grant_type": "authorization_code",
                "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
    if response.status_code >= 400:
        raise RuntimeError("Claude subscription OAuth exchange failed")
    data = response.json()
    return _auth_record_from_token_response(data, require_refresh_token=True)


async def refresh_authorized_record(auth_record: dict[str, str]) -> dict[str, Any]:
    refresh_token = auth_record["refresh_token"]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            ANTHROPIC_OAUTH_TOKEN_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": "axios/1.13.6",
            },
            json={
                "grant_type": "refresh_token",
                "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    if response.status_code >= 400:
        raise RuntimeError("Claude subscription OAuth expired; re-authorize the provider")
    data = response.json()
    refreshed = _auth_record_from_token_response(data, require_refresh_token=False)
    if "refresh_token" not in refreshed:
        refreshed["refresh_token"] = refresh_token
    return refreshed


class AnthropicSubscriptionTransport:
    """Direct controller-side Anthropic Messages transport using Claude OAuth."""

    def __init__(
        self,
        auth: AnthropicSubscriptionAuth,
        *,
        timeout: float | None = None,
    ) -> None:
        self._auth = auth
        self._base_url = ANTHROPIC_MESSAGES_URL
        self._timeout = timeout or 600.0

    async def completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        payload = _build_anthropic_payload(
            model=model, messages=messages, stream=stream, kwargs=kwargs
        )
        headers = _oauth_headers(
            self._auth.access_token,
            extra_headers=kwargs.get("extra_headers")
            if isinstance(kwargs.get("extra_headers"), dict)
            else None,
        )
        if _payload_uses_extended_cache_ttl(payload):
            headers["anthropic-beta"] = _with_extended_cache_ttl_beta(headers["anthropic-beta"])
        url = self._base_url
        if "?" not in url:
            url = f"{url}?beta=true"
        elif "beta=" not in url:
            url = f"{url}&beta=true"
        client = httpx.AsyncClient(timeout=self._timeout)
        if not stream:
            try:
                response = await client.post(url, headers=headers, json=payload)
                await _raise_for_anthropic_error(response)
                return _anthropic_response_to_chat(response.json(), model=model)
            finally:
                await client.aclose()
        request = client.build_request("POST", url, headers=headers, json=payload)
        response = await client.send(request, stream=True)
        try:
            await _raise_for_anthropic_error(response)
        except Exception:
            await response.aclose()
            await client.aclose()
            raise
        return _stream_chat_chunks(response, client, model=model)


def bundled_anthropic_model_entries() -> list[dict[str, Any]]:
    """Return current Claude model metadata used when remote discovery is unavailable.

    Keep this list intentionally small and current.  It is a fallback/enrichment
    catalog, not the source of truth when the Models API is reachable.
    """

    common = {
        "supports_tools": True,
        "supports_streaming": True,
        "supports_vision": True,
        "supports_pdf_input": True,
        "supports_file_input": True,
        "supports_reasoning": True,
        "supports_prompt_caching": True,
        "supports_defer_loading": True,
        "supports_extended_thinking": True,
        "max_tools": 128,
    }
    return [
        {
            **common,
            "model_id": "claude-fable-5",
            "name": "Claude Fable 5",
            "display_name": "Claude Fable 5",
            "context_window": 1_000_000,
            "max_context_window": 1_000_000,
            "max_input_tokens": 872_000,
            "max_output_tokens": 128_000,
            "input_cost_per_mtok": 10.0,
            "output_cost_per_mtok": 50.0,
        },
        {
            **common,
            "model_id": "claude-mythos-5",
            "name": "Claude Mythos 5",
            "display_name": "Claude Mythos 5",
            "context_window": 1_000_000,
            "max_context_window": 1_000_000,
            "max_input_tokens": 872_000,
            "max_output_tokens": 128_000,
            "input_cost_per_mtok": 10.0,
            "output_cost_per_mtok": 50.0,
            "tier": "limited",
        },
        {
            **common,
            "model_id": "claude-opus-4-8",
            "name": "Claude Opus 4.8",
            "display_name": "Claude Opus 4.8",
            "context_window": 1_000_000,
            "max_context_window": 1_000_000,
            "max_input_tokens": 872_000,
            "max_output_tokens": 128_000,
            "input_cost_per_mtok": 5.0,
            "output_cost_per_mtok": 25.0,
        },
        {
            **common,
            "model_id": "claude-sonnet-5",
            "name": "Claude Sonnet 5",
            "display_name": "Claude Sonnet 5",
            "context_window": 1_000_000,
            "max_context_window": 1_000_000,
            "max_input_tokens": 872_000,
            "max_output_tokens": 128_000,
            "input_cost_per_mtok": 3.0,
            "output_cost_per_mtok": 15.0,
        },
        {
            **common,
            "model_id": "claude-haiku-4-5",
            "name": "Claude Haiku 4.5",
            "display_name": "Claude Haiku 4.5",
            "context_window": 200_000,
            "max_context_window": 200_000,
            "max_input_tokens": 172_000,
            "max_output_tokens": 32_000,
            "input_cost_per_mtok": 1.0,
            "output_cost_per_mtok": 5.0,
        },
        {
            **common,
            "model_id": "claude-sonnet-4-6",
            "name": "Claude Sonnet 4.6",
            "display_name": "Claude Sonnet 4.6",
            "context_window": 1_000_000,
            "max_context_window": 1_000_000,
            "max_input_tokens": 872_000,
            "max_output_tokens": 128_000,
            "input_cost_per_mtok": 3.0,
            "output_cost_per_mtok": 15.0,
        },
    ]


async def fetch_subscription_models(auth: AnthropicSubscriptionAuth) -> list[dict[str, Any]]:
    """Fetch Claude models visible to the subscription OAuth token."""

    headers = _oauth_headers(auth.access_token)
    headers["anthropic-version"] = "2023-06-01"
    entries: list[dict[str, Any]] = []
    params: dict[str, str] = {"limit": "100"}
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(20):
            response = await client.get(ANTHROPIC_MODELS_URL, headers=headers, params=params)
            await _raise_for_anthropic_error(response)
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Claude subscription model response is invalid")
            raw_models = payload.get("data")
            if not isinstance(raw_models, list):
                raise RuntimeError("Claude subscription model response is invalid")
            for item in raw_models:
                entry = _model_entry_from_api_item(item)
                if entry is not None:
                    entries.append(entry)
            if not payload.get("has_more"):
                break
            last_id = payload.get("last_id")
            if not isinstance(last_id, str) or not last_id:
                break
            params["after_id"] = last_id
    return entries


def _base64_url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if values:
        return values[0]
    return None


def _model_entry_from_api_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    display_name = item.get("display_name") or item.get("name") or model_id
    entry: dict[str, Any] = {
        "model_id": model_id.strip(),
        "name": str(display_name),
        "display_name": str(display_name),
        "supports_tools": True,
        "supports_streaming": True,
        "supports_vision": True,
        "supports_pdf_input": True,
        "supports_file_input": True,
        "supports_reasoning": True,
        "supports_prompt_caching": True,
        "supports_defer_loading": True,
        "supports_extended_thinking": True,
        "max_tools": 128,
    }
    created_at = item.get("created_at")
    if isinstance(created_at, str) and created_at:
        entry["created_at"] = created_at
    return entry


def _auth_record_from_token_response(data: Any, *, require_refresh_token: bool) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuntimeError("Claude subscription OAuth token response is invalid")
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Claude subscription OAuth token response missing access_token")
    if require_refresh_token and (not isinstance(refresh_token, str) or not refresh_token):
        raise RuntimeError("Claude subscription OAuth token response missing refresh_token")
    record: dict[str, Any] = {"access_token": access_token}
    if isinstance(refresh_token, str) and refresh_token:
        record["refresh_token"] = refresh_token
    expires_in = data.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        record["expires_at"] = time.time() + float(expires_in)
    return record


def _oauth_headers(
    access_token: str,
    *,
    extra_headers: dict[str, Any] | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": CLAUDE_CODE_USER_AGENT,
        "anthropic-beta": ",".join(ANTHROPIC_REQUIRED_BETAS),
    }
    if extra_headers:
        for key, value in extra_headers.items():
            if str(key).lower() in {"authorization", "x-api-key", "user-agent"}:
                continue
            if str(key).lower() == "anthropic-beta":
                headers["anthropic-beta"] = _merge_beta_headers(str(value))
            else:
                headers[str(key)] = str(value)
    return headers


def _merge_beta_headers(incoming: str) -> str:
    values = [*ANTHROPIC_REQUIRED_BETAS]
    values.extend(item.strip() for item in incoming.split(",") if item.strip())
    return ",".join(dict.fromkeys(values))


def _with_extended_cache_ttl_beta(beta_header: str) -> str:
    values = [item.strip() for item in beta_header.split(",") if item.strip()]
    values.append(ANTHROPIC_EXTENDED_CACHE_TTL_BETA)
    return ",".join(dict.fromkeys(values))


def _default_max_tokens_for_model(model: str) -> int:
    normalized = model.lower()
    if "opus-4" in normalized or "sonnet-4" in normalized or "haiku-4" in normalized:
        return 64_000
    if "claude-3-7" in normalized or "claude-3.7" in normalized:
        return 64_000
    if "claude-3-5" in normalized or "claude-3.5" in normalized:
        return 8_192
    return 8_192


def _payload_uses_extended_cache_ttl(value: Any) -> bool:
    if isinstance(value, dict):
        cache_control = value.get("cache_control")
        if (
            isinstance(cache_control, dict)
            and str(cache_control.get("ttl") or "").strip().lower() == "1h"
        ):
            return True
        return any(_payload_uses_extended_cache_ttl(item) for item in value.values())
    if isinstance(value, list):
        return any(_payload_uses_extended_cache_ttl(item) for item in value)
    return False


def _build_anthropic_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    system_blocks, anthropic_messages = _convert_messages(messages)
    first_user_text = _first_user_text(anthropic_messages)
    system = [
        {"type": "text", "text": _billing_header(first_user_text)},
        {"type": "text", "text": CLAUDE_CODE_IDENTITY},
        {"type": "text", "text": CLAUDE_CODE_IDENTITY_BRIDGE},
        *system_blocks,
    ]
    payload: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "system": system,
        "stream": stream,
        "max_tokens": int(
            kwargs.get("max_tokens")
            or kwargs.get("max_completion_tokens")
            or _default_max_tokens_for_model(model)
        ),
    }
    for key in ("temperature", "top_p", "top_k", "stop_sequences", "metadata", "thinking"):
        value = kwargs.get(key)
        if value is not None:
            payload[key] = value
    tools = _convert_tools(kwargs.get("tools"))
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = {"type": "auto"}
    return payload


def _convert_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_blocks: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role in {"system", "developer"}:
            blocks = _content_to_blocks(message.get("content"))
            for block in blocks:
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        block["text"] = _sanitize_system_text(text)
                    system_blocks.append(block)
            continue
        if role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or ""),
                    "content": _content_to_text(message.get("content")),
                    **({"is_error": True} if message.get("_tool_is_error") else {}),
                }
            ]
            _append_anthropic_message(output, "user", blocks)
            continue
        if role not in {"user", "assistant"}:
            continue
        blocks = _content_to_blocks(message.get("content"))
        if role == "assistant":
            blocks = [
                *_content_to_anthropic_thinking_blocks(message.get("_anthropic_thinking_blocks")),
                *blocks,
            ]
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    continue
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or ""),
                        "name": _prefix_tool_name(name),
                        "input": _parse_tool_arguments(function.get("arguments")),
                    }
                )
        if blocks:
            _append_anthropic_message(output, role, blocks)
    if not output:
        output.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    return system_blocks, output


def _append_anthropic_message(
    messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    if messages and messages[-1].get("role") == role:
        existing = messages[-1].setdefault("content", [])
        if isinstance(existing, list):
            existing.extend(blocks)
            return
    messages.append({"role": role, "content": blocks})


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                block_type = item.get("type")
                if block_type == "text" and isinstance(item.get("text"), str):
                    block = {"type": "text", "text": item["text"]}
                    _copy_cache_control(item, block)
                    blocks.append(block)
                elif block_type == "image":
                    blocks.append(dict(item))
                elif block_type == "image_url":
                    image_block = _image_url_to_anthropic_block(item.get("image_url"))
                    if image_block is not None:
                        blocks.append(image_block)
                elif block_type in {"thinking", "redacted_thinking"}:
                    blocks.extend(_content_to_anthropic_thinking_blocks([item]))
                else:
                    text = _content_to_text(item)
                    if text:
                        blocks.append({"type": "text", "text": text})
        return blocks
    text = _content_to_text(content)
    return [{"type": "text", "text": text}] if text else []


def _copy_cache_control(source: dict[str, Any], target: dict[str, Any]) -> None:
    cache_control = source.get("cache_control")
    if isinstance(cache_control, dict) and cache_control.get("type") == "ephemeral":
        copied = {"type": "ephemeral"}
        ttl = cache_control.get("ttl")
        if isinstance(ttl, str) and ttl.strip():
            copied["ttl"] = ttl.strip().lower()
        target["cache_control"] = copied


def _image_url_to_anthropic_block(raw: Any) -> dict[str, Any] | None:
    url = raw.get("url") if isinstance(raw, dict) else raw
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.startswith("data:"):
        header, separator, payload = url.partition(",")
        if not separator:
            return None
        media_type = header[5:].split(";", 1)[0] or "image/png"
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": payload,
            },
        }
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_to_anthropic_thinking_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if block_type == "thinking":
            thinking = item.get("thinking")
            if not isinstance(thinking, str) or not thinking:
                continue
            block = {"type": "thinking", "thinking": thinking}
            signature = item.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = signature
            blocks.append(block)
        elif block_type == "redacted_thinking":
            data = item.get("data")
            if isinstance(data, str) and data:
                blocks.append({"type": "redacted_thinking", "data": data})
    return blocks


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part for item in content if (part := _content_to_text(item)))
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if isinstance(text, str):
            return text
    return str(content)


def _convert_tools(raw_tools: Any) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if not isinstance(raw_tools, list):
        return tools
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            continue
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        tool = {
            "name": _prefix_tool_name(name),
            "description": str(function.get("description") or ""),
            "input_schema": function.get("parameters")
            if isinstance(function.get("parameters"), dict)
            else {"type": "object", "properties": {}},
        }
        for key in ("cache_control", "defer_loading"):
            if key in function:
                tool[key] = function[key]
        tools.append(tool)
    return tools


def _prefix_tool_name(name: str) -> str:
    if name.startswith(TOOL_PREFIX):
        return name
    return f"{TOOL_PREFIX}{name[:1].upper()}{name[1:]}"


def _unprefix_tool_name(name: str) -> str:
    if not name.startswith(TOOL_PREFIX):
        return name
    raw = name[len(TOOL_PREFIX) :]
    if raw == "StructuredOutput":
        return raw
    return f"{raw[:1].lower()}{raw[1:]}"


def _parse_tool_arguments(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        return text
        return _content_to_text(content)
    return ""


def _billing_header(message_text: str) -> str:
    chars = "".join(
        message_text[index] if index < len(message_text) else "0" for index in CCH_POSITIONS
    )
    suffix = hashlib.sha256(f"{CCH_SALT}{chars}{CLAUDE_CODE_VERSION}".encode()).hexdigest()[:3]
    cch = hashlib.sha256(message_text.encode()).hexdigest()[:5]
    return (
        "x-anthropic-billing-header: "
        f"cc_version={CLAUDE_CODE_VERSION}.{suffix}; "
        f"cc_entrypoint={CLAUDE_CODE_ENTRYPOINT}; "
        f"cch={cch};"
    )


def _sanitize_system_text(text: str) -> str:
    return text.replace(
        "Here is some useful information about the environment you are running in:",
        "Environment context you are running in:",
    ).strip()


class AnthropicSubscriptionError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        super().__init__(f"Anthropic subscription request failed ({status_code}): {detail}")


async def _raise_for_anthropic_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    detail = response.text[:500]
    raise AnthropicSubscriptionError(response.status_code, detail)


def _anthropic_response_to_chat(response: dict[str, Any], *, model: str) -> dict[str, Any]:
    message = _anthropic_content_to_chat_message(response.get("content"))
    return {
        "id": response.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _map_stop_reason(response.get("stop_reason")),
            }
        ],
        "usage": _normalize_usage(response.get("usage")),
    }


def _anthropic_content_to_chat_message(content: Any) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    thinking_blocks: list[dict[str, Any]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") in {"thinking", "redacted_thinking"}:
                thinking_blocks.extend(_content_to_anthropic_thinking_blocks([block]))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": _unprefix_tool_name(str(block.get("name") or "")),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if thinking_blocks:
        message["thinking_blocks"] = thinking_blocks
    return message


def _normalize_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


def _map_stop_reason(reason: Any) -> str:
    if reason == "tool_use":
        return "tool_calls"
    if reason == "max_tokens":
        return "length"
    return "stop"


async def _stream_chat_chunks(
    response: httpx.Response,
    client: httpx.AsyncClient,
    *,
    model: str,
) -> AsyncIterator[dict[str, Any]]:
    usage: dict[str, int] = {}
    tool_indices: dict[int, int] = {}
    thinking_blocks: dict[int, dict[str, Any]] = {}
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            event_type = event.get("type")
            if event_type == "message_start":
                usage.update(_normalize_usage((event.get("message") or {}).get("usage")))
                continue
            if event_type == "content_block_start":
                index = int(event.get("index") or 0)
                block = event.get("content_block") or {}
                if isinstance(block, dict) and block.get("type") in {
                    "thinking",
                    "redacted_thinking",
                }:
                    normalized_blocks = _content_to_anthropic_thinking_blocks([block])
                    thinking_blocks[index] = (
                        normalized_blocks[0]
                        if normalized_blocks
                        else {"type": str(block.get("type") or ""), "thinking": ""}
                    )
                elif isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_indices[index] = len(tool_indices)
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": tool_indices[index],
                                            "id": str(block.get("id") or ""),
                                            "type": "function",
                                            "function": {
                                                "name": _unprefix_tool_name(
                                                    str(block.get("name") or "")
                                                ),
                                                "arguments": "",
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                continue
            if event_type == "content_block_delta":
                index = int(event.get("index") or 0)
                delta = event.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                    yield {"choices": [{"index": 0, "delta": {"content": delta["text"]}}]}
                elif delta.get("type") == "thinking_delta" and isinstance(
                    delta.get("thinking"), str
                ):
                    block = thinking_blocks.setdefault(index, {"type": "thinking", "thinking": ""})
                    block["thinking"] = str(block.get("thinking") or "") + delta["thinking"]
                    yield {
                        "choices": [{"index": 0, "delta": {"reasoning_content": delta["thinking"]}}]
                    }
                elif delta.get("type") == "signature_delta" and isinstance(
                    delta.get("signature"), str
                ):
                    block = thinking_blocks.setdefault(index, {"type": "thinking", "thinking": ""})
                    block["signature"] = delta["signature"]
                elif delta.get("type") == "redacted_thinking_delta" and isinstance(
                    delta.get("data"), str
                ):
                    block = thinking_blocks.setdefault(
                        index, {"type": "redacted_thinking", "data": ""}
                    )
                    block["data"] = str(block.get("data") or "") + delta["data"]
                elif delta.get("type") == "input_json_delta":
                    partial = str(delta.get("partial_json") or "")
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": tool_indices.get(index, index),
                                            "function": {"arguments": partial},
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                continue
            if event_type == "content_block_stop":
                index = int(event.get("index") or 0)
                block = thinking_blocks.pop(index, None)
                normalized_blocks = _content_to_anthropic_thinking_blocks([block])
                if normalized_blocks:
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"provider_thinking_blocks": normalized_blocks},
                            }
                        ],
                    }
                continue
            if event_type == "message_delta":
                delta = event.get("delta") or {}
                if isinstance(event.get("usage"), dict):
                    usage.update(_normalize_usage(event["usage"]))
                if isinstance(delta, dict) and delta.get("stop_reason"):
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": _map_stop_reason(delta.get("stop_reason")),
                            }
                        ]
                    }
            elif event_type == "message_stop" and usage:
                yield {"usage": usage}
    finally:
        await response.aclose()
        await client.aclose()
