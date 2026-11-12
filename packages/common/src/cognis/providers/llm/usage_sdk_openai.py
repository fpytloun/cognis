"""Version-fenced private OpenAI decoder compatibility, not a public SDK hook.

The SDK already decoded each provider event. Observe it before model construction
and LiteLLM synthesis; never parse or retain bodies/events here.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from functools import wraps
from importlib.metadata import version
from inspect import Parameter, signature
from typing import Any

import httpx

_SUPPORTED_VERSION = "2.30.0"
_installed: Callable[..., Any] | None = None


def install_openai_decoder(observer: Callable[[httpx.Response, object], None]) -> bool:
    """Install once only for the audited asynchronous SDK signature/version."""
    global _installed
    try:
        from openai._base_client import AsyncAPIClient

        if version("openai") != _SUPPORTED_VERSION:
            return False
        original = AsyncAPIClient._process_response_data
        expected = (
            ("self", Parameter.POSITIONAL_OR_KEYWORD),
            ("data", Parameter.KEYWORD_ONLY),
            ("cast_to", Parameter.KEYWORD_ONLY),
            ("response", Parameter.KEYWORD_ONLY),
        )
        if tuple((p.name, p.kind) for p in signature(original).parameters.values()) != expected:
            return False
        if _installed is not None:
            return original is _installed

        @wraps(original)
        def observed(
            client: Any,
            *,
            data: object,
            cast_to: Any,
            response: httpx.Response,
        ) -> Any:
            with suppress(Exception):
                observer(response, data)
            # Never catch, replace or retry the original SDK operation.
            return original(client, data=data, cast_to=cast_to, response=response)

        setattr(AsyncAPIClient, "_process_response_data", observed)  # noqa: B010
        _installed = observed
        return True
    except Exception:
        return False
