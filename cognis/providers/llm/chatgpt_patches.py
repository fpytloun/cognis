"""Runtime patches for LiteLLM's ChatGPT Responses transport.

LiteLLM's ``ChatGPTResponsesAPIConfig.transform_responses_api_request`` strips
Cognis-supplied ``prompt_cache_key`` and ``prompt_cache_retention`` from the
outgoing request because they are not in its ``allowed_keys`` whitelist.  It
also unconditionally prepends a large (~5 KB) Codex CLI default instructions
block to any ``instructions`` field Cognis sends, causing:

1. ``cached_tokens = 0`` — the cache key never reaches the backend.
2. Hosted-instruction drift warnings — the server echoes the combined
   instructions (Codex defaults + Cognis prefix) while Cognis only sent its
   own prefix, so the two strings never match.
3. ~5 KB of wasted tokens per request.

This module installs two idempotent fixes at process startup:

* ``install_chatgpt_responses_cache_passthrough()`` — wraps
  ``transform_responses_api_request`` so that ``prompt_cache_key`` and
  ``prompt_cache_retention`` survive the upstream key-whitelist filter.

* ``suppress_chatgpt_default_instructions()`` — prevents the upstream helper
  ``get_chatgpt_default_instructions()`` from returning LiteLLM's large Codex
  CLI prompt.  LiteLLM uses ``os.getenv(...) or DEFAULT``, so setting the env var
  to an empty string is not enough; Cognis sets a harmless single-space override
  and patches the imported helper references to return ``""``.

Both functions are safe to call multiple times; they check for a sentinel
attribute before patching.
"""

from __future__ import annotations

import os
from typing import Any

from cognis.logging import get_logger

logger = get_logger(__name__)

_CACHE_PASSTHROUGH_SENTINEL = "_cognis_cache_passthrough_installed"
_SUPPRESS_INSTRUCTIONS_SENTINEL = "_cognis_suppress_instructions_installed"

# Keys that Cognis may attach for explicit prompt-cache affinity.
_COGNIS_CACHE_KEYS = frozenset({"prompt_cache_key", "prompt_cache_retention"})


def install_chatgpt_responses_cache_passthrough() -> bool:
    """Wrap ChatGPTResponsesAPIConfig to preserve Cognis cache params.

    After the upstream transform strips ``prompt_cache_key`` and
    ``prompt_cache_retention`` via its ``allowed_keys`` whitelist, this wrapper
    re-inserts them from the original ``response_api_optional_request_params``
    dict so they reach ``litellm.aresponses``.

    Returns ``True`` if the patch was newly installed, ``False`` if it was
    already present (idempotent).
    """
    try:
        from litellm.llms.chatgpt.responses.transformation import (
            ChatGPTResponsesAPIConfig,
        )
    except ImportError:
        logger.debug(
            "chatgpt_patches: ChatGPTResponsesAPIConfig not importable — skipping cache passthrough"
        )
        return False

    if getattr(ChatGPTResponsesAPIConfig, _CACHE_PASSTHROUGH_SENTINEL, False):
        return False

    original_transform = ChatGPTResponsesAPIConfig.transform_responses_api_request

    def _patched_transform(
        self: Any,
        model: str,
        input: Any,
        response_api_optional_request_params: dict[str, Any],
        litellm_params: Any,
        headers: dict[str, Any],
    ) -> dict[str, Any]:
        result = original_transform(
            self,
            model,
            input,
            response_api_optional_request_params,
            litellm_params,
            headers,
        )
        # Re-insert cache params that the upstream whitelist stripped.
        for key in _COGNIS_CACHE_KEYS:
            value = response_api_optional_request_params.get(key)
            if value is not None and key not in result:
                result[key] = value
        return result

    ChatGPTResponsesAPIConfig.transform_responses_api_request = _patched_transform  # type: ignore[method-assign]
    setattr(ChatGPTResponsesAPIConfig, _CACHE_PASSTHROUGH_SENTINEL, True)
    logger.debug("chatgpt_patches: cache passthrough installed on ChatGPTResponsesAPIConfig")
    return True


def suppress_chatgpt_default_instructions() -> bool:
    """Prevent LiteLLM from prepending Codex CLI default instructions.

    LiteLLM's helper uses ``os.getenv("CHATGPT_DEFAULT_INSTRUCTIONS") or
    CHATGPT_DEFAULT_INSTRUCTIONS``, so an empty-string env override would fall
    back to the built-in prompt.  If the operator has not supplied an override,
    Cognis sets a single-space env value for subprocess visibility and patches
    LiteLLM's helper references in-process to return ``""``.

    Returns ``True`` if Cognis installed the suppression patch, ``False`` if an
    operator override was present or the patch was already installed.
    """
    previous = os.environ.get("CHATGPT_DEFAULT_INSTRUCTIONS")
    if previous is not None:
        # Already set by operator or a prior call — do not override.
        return False

    os.environ["CHATGPT_DEFAULT_INSTRUCTIONS"] = " "
    try:
        from litellm.llms.chatgpt import common_utils
        from litellm.llms.chatgpt.responses import transformation
    except ImportError:
        logger.debug(
            "chatgpt_patches: ChatGPT helpers not importable — using env-only instruction suppression"
        )
        return True

    if getattr(common_utils, _SUPPRESS_INSTRUCTIONS_SENTINEL, False):
        return False

    def _empty_default_instructions() -> str:
        return ""

    common_utils.get_chatgpt_default_instructions = _empty_default_instructions
    transformation.get_chatgpt_default_instructions = _empty_default_instructions
    setattr(common_utils, _SUPPRESS_INSTRUCTIONS_SENTINEL, True)
    logger.debug(
        "chatgpt_patches: ChatGPT default instructions helper patched to return empty string"
    )
    return True
