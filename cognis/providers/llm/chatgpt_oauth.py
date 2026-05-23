"""ChatGPT OAuth helper functions shared by LiteLLM and direct Codex paths."""

from __future__ import annotations

import base64
import json
from typing import Any

from cognis.ownership import SYSTEM_USER_EMAIL

CHATGPT_OAUTH_AUTH_FILE = "auth.json"
CHATGPT_OAUTH_SECRET_PREFIX = "llm_oauth_chatgpt"


def decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def chatgpt_access_token_expires_at(token: str) -> int | None:
    exp = decode_jwt_claims(token).get("exp")
    if isinstance(exp, int | float):
        return int(exp)
    return None


def chatgpt_account_id_from_tokens(*tokens: str | None) -> str | None:
    for token in tokens:
        if not token:
            continue
        auth_claims = decode_jwt_claims(token).get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            account_id = auth_claims.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
    return None


def parse_chatgpt_authorized_record(raw: str) -> dict[str, str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ChatGPT OAuth token cache is invalid; restart OAuth") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ChatGPT OAuth token cache is invalid; restart OAuth")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("ChatGPT OAuth is not authorized; complete the device flow first")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("ChatGPT OAuth is not authorized; complete the device flow first")
    result = {"access_token": access_token, "refresh_token": refresh_token}
    for key in ("id_token", "account_id", "expires_at"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            result[key] = value
        elif isinstance(value, int | float):
            result[key] = str(value)
    return result


def oauth_token_secret_name(provider: Any) -> str:
    config = dict(provider.config or {})
    auth_config = config.get("auth_config")
    if isinstance(auth_config, dict):
        for key in ("token_secret_name", "secret_name"):
            value = auth_config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"{CHATGPT_OAUTH_SECRET_PREFIX}_{provider.provider_id}"


def oauth_pending_secret_name(provider: Any) -> str:
    return f"{oauth_token_secret_name(provider)}_pending"


def oauth_secret_description(provider: Any) -> str:
    return f"OAuth token cache for LLM provider {provider.provider_id}"


def oauth_secret_owner(provider: Any) -> str:
    return provider.owner_email or SYSTEM_USER_EMAIL
