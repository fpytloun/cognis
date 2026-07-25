"""Claude subscription OAuth and native Anthropic Messages compatibility helpers.

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
        "supports_tool_search": True,
        "supports_native_tool_search": True,
        "supports_defer_loading": True,
        "supports_extended_thinking": True,
        "supports_strict_tools": True,
        "supports_pause_turn": True,
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
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ",".join(ANTHROPIC_REQUIRED_BETAS),
    }
    if extra_headers:
        for key, value in extra_headers.items():
            if str(key).lower() in {
                "authorization",
                "anthropic-version",
                "x-api-key",
                "user-agent",
            }:
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
            native_blocks = message.get("_anthropic_native_blocks")
            if isinstance(native_blocks, list) and all(
                isinstance(block, dict) for block in native_blocks
            ):
                blocks = [dict(block) for block in native_blocks]
            else:
                blocks = [
                    *_content_to_anthropic_thinking_blocks(
                        message.get("_anthropic_thinking_blocks")
                    ),
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
                            "name": name,
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
    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        response: httpx.Response | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.response = response
        self.body = body
        super().__init__(f"Anthropic subscription request failed ({status_code}): {detail}")


async def _raise_for_anthropic_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    await response.aread()
    detail = response.text[:500]
    try:
        parsed_body = response.json()
    except json.JSONDecodeError:
        parsed_body = None
    body = parsed_body if isinstance(parsed_body, dict) else None
    raise AnthropicSubscriptionError(response.status_code, detail, response=response, body=body)
