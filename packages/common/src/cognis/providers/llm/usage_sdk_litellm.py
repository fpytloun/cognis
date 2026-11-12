"""Positive purpose for LiteLLM 1.82.6's canonical asynchronous HTTP resources.

The registered resource owns inference intent. Its actual URL resolver and
client factory supply weak, request-local construction evidence. An ambient
scope, an arbitrary POST, or a credential helper is not sufficient authority.
"""

from __future__ import annotations

import sys
import weakref
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from importlib.metadata import version
from inspect import signature
from typing import Any

import httpx

from cognis.providers.llm.usage_sdk_purpose import (
    PurposeScope,
    grant_request,
    target_identity,
)

# Method, operation, client parameter, config parameter, already-resolved URL.
_RESOURCES = (
    ("_make_common_async_call", "chat", "async_httpx_client", None, True),
    ("aembedding", "embeddings", "client", None, True),
    ("async_response_api_handler", "responses", "client", "responses_api_provider_config", False),
    (
        "async_text_to_speech_handler",
        "native",
        "client",
        "text_to_speech_provider_config",
        False,
    ),
    (
        "async_image_generation_handler",
        "native",
        "client",
        "image_generation_provider_config",
        False,
    ),
    ("async_image_edit_handler", "native", "client", "image_edit_provider_config", False),
    (
        "async_audio_transcriptions",
        "native",
        "client",
        "provider_config",
        False,
    ),
)


@dataclass(frozen=True, slots=True)
class _Resource:
    owner: Any
    name: str
    operation: str
    clients: tuple[str, ...]
    target: str | None = "api_base"
    config: str | None = None
    resolver: str = "get_complete_url"
    resolver_index: int | None = None
    suffix: str | None = None
    default_client: Callable[[], Any] | None = None


@dataclass(slots=True)
class _Call:
    scope: weakref.ReferenceType[PurposeScope]
    operation: str
    target: bytes | None
    config: weakref.ReferenceType[Any] | None = None
    clients: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)


_CALL: ContextVar[_Call | None] = ContextVar("cognis_litellm_resource_call", default=None)
_RESOLVERS: dict[tuple[type, str], Callable[..., Any]] = {}
_installed = False


@dataclass(slots=True)
class _Construction:
    call: _Call
    client: weakref.ReferenceType[httpx.AsyncClient]
    claimed: bool = False


_CONSTRUCTION: ContextVar[_Construction | None] = ContextVar(
    "cognis_litellm_request_construction",
    default=None,
)


def _post_wrapper(original: Callable[..., Any], *, replacement: bool) -> Callable[..., Any]:
    @wraps(original)
    async def observed(instance: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        construction = None
        with suppress(Exception):
            call = _CALL.get()
            if (
                call is not None
                and instance in call.clients
                and call.target == target_identity(url)
            ):
                client = (
                    kwargs.get("client", args[0] if args else None)
                    if replacement
                    else instance.client
                )
                if isinstance(client, httpx.AsyncClient):
                    construction = _Construction(call, weakref.ref(client))
        token = _CONSTRUCTION.set(construction)
        try:
            return await original(instance, url, *args, **kwargs)
        finally:
            _CONSTRUCTION.reset(token)

    return observed


def _observe_resolver(config: Any, resource: _Resource) -> None:
    owner = type(config)
    key = (owner, resource.resolver)
    if key in _RESOLVERS:
        return
    if not owner.__module__.startswith("litellm."):
        return
    original = getattr(owner, resource.resolver)
    if next(iter(signature(original).parameters)) != "self":
        return

    @wraps(original)
    def resolved(instance: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(instance, *args, **kwargs)
        with suppress(Exception):
            call = _CALL.get()
            # Vertex's exact resolver returns (authentication, URL). Do not
            # inspect, copy or retain the authentication element.
            url = result[resource.resolver_index] if resource.resolver_index is not None else result
            if (
                call is not None
                and call.config is not None
                and call.config() is instance
                and isinstance(url, str)
            ):
                call.target = target_identity(url)
        return result

    setattr(owner, resource.resolver, resolved)
    _RESOLVERS[key] = resolved


def _resource_wrapper(
    original: Callable[..., Any],
    resource: _Resource,
    scope_provider: Callable[[], PurposeScope | None],
) -> Callable[..., Any]:
    positions = tuple(signature(original).parameters)

    def argument(args: tuple[Any, ...], kwargs: dict[str, Any], name: str) -> Any:
        index = positions.index(name)
        return kwargs.get(name, args[index] if len(args) > index else None)

    @wraps(original)
    async def observed(*args: Any, **kwargs: Any) -> Any:
        call = None
        with suppress(Exception):
            scope = scope_provider()
            if (
                scope is not None
                and not scope.closed
                and scope.accepts_operation(resource.operation)
            ):
                url = argument(args, kwargs, resource.target) if resource.target else None
                # Exact SDK-relative endpoint wiring, not inference classification.
                if isinstance(url, str) and resource.suffix and not url.endswith(resource.suffix):
                    url += resource.suffix
                call = _Call(
                    weakref.ref(scope),
                    resource.operation,
                    target_identity(url) if isinstance(url, str) else None,
                )
                client = next(
                    (
                        value
                        for name in resource.clients
                        if (value := argument(args, kwargs, name)) is not None
                    ),
                    None,
                )
                if client is None and resource.default_client is not None:
                    client = resource.default_client()
                if client is not None:
                    call.clients.add(client)
                if resource.config is not None:
                    config = argument(args, kwargs, resource.config)
                    call.config = weakref.ref(config)
                    _observe_resolver(config, resource)
        token = _CALL.set(call)
        try:
            return await original(*args, **kwargs)
        finally:
            _CALL.reset(token)

    return observed


def _factory_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def observed(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with suppress(Exception):
            call = _CALL.get()
            if call is not None:
                call.clients.add(result)
        return result

    return observed


def install_litellm_dispatch(
    scope_provider: Callable[[], PurposeScope | None],
) -> bool:
    """Install only for the audited native-resource and request-construction ABI."""
    global _installed
    if _installed:
        return True
    if "litellm" not in sys.modules:
        return False
    try:
        if version("litellm") != "1.82.6" or version("httpx") != "0.28.1":
            return False
        import litellm
        import litellm.llms.bedrock.chat.converse_handler as converse
        import litellm.llms.bedrock.chat.invoke_handler as invoke
        import litellm.llms.custom_httpx.llm_http_handler as module
        import litellm.llms.ollama.completion.handler as ollama
        import litellm.llms.openai_like.chat.handler as openai_like
        import litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini as gemini
        import litellm.llms.vertex_ai.gemini_embeddings.batch_embed_content_handler as embeddings
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        resources = [
            _Resource(
                module.BaseLLMHTTPHandler,
                name,
                operation,
                (client,),
                "api_base" if resolved_url else None,
                config,
            )
            for name, operation, client, config, resolved_url in _RESOURCES
        ]
        resources.extend(
            (
                _Resource(
                    gemini.VertexLLM,
                    "async_completion",
                    "chat",
                    ("client",),
                    target=None,
                    config="self",
                    resolver="_get_token_and_url",
                    resolver_index=1,
                ),
                _Resource(gemini, "make_call", "chat", ("gemini_client", "client")),
                _Resource(
                    embeddings.GoogleBatchEmbeddings,
                    "async_batch_embeddings",
                    "embeddings",
                    ("client",),
                    target="url",
                ),
                _Resource(converse.BedrockConverseLLM, "async_completion", "chat", ("client",)),
                _Resource(invoke.BedrockLLM, "async_completion", "chat", ("client",)),
                # Converse imports this function by value; both aliases are producers.
                _Resource(invoke, "make_call", "chat", ("client",)),
                _Resource(converse, "make_call", "chat", ("client",)),
                _Resource(
                    openai_like.OpenAILikeChatHandler,
                    "acompletion_function",
                    "chat",
                    ("client",),
                    default_client=lambda: litellm.module_level_aclient,
                ),
                _Resource(
                    openai_like,
                    "make_call",
                    "chat",
                    ("client",),
                    default_client=lambda: litellm.module_level_aclient,
                ),
                _Resource(
                    ollama,
                    "ollama_aembeddings",
                    "embeddings",
                    (),
                    suffix="/api/embed",
                    default_client=lambda: litellm.module_level_aclient,
                ),
            )
        )
        contracts = []
        for resource in resources:
            original = getattr(resource.owner, resource.name)
            parameters = signature(original).parameters
            required = set(resource.clients)
            if resource.target is not None:
                required.add(resource.target)
            if resource.config is not None:
                required.add(resource.config)
            if not required.issubset(parameters):
                return False
            contracts.append((resource, original))
        factories = []
        for factory_module in (module, gemini, embeddings, converse, invoke):
            factory = vars(factory_module)["get_async_httpx_client"]
            if "llm_provider" not in signature(factory).parameters:
                return False
            factories.append((factory_module, factory))
        post = AsyncHTTPHandler.post
        replacement_post = AsyncHTTPHandler.single_connection_post_request
        if not {"self", "url"}.issubset(signature(post).parameters):
            return False
        if not {"self", "url", "client"}.issubset(signature(replacement_post).parameters):
            return False
        builder = httpx.AsyncClient.build_request

        @wraps(builder)
        def observed_builder(
            client: httpx.AsyncClient,
            method: str,
            url: Any,
            *args: Any,
            **kwargs: Any,
        ) -> httpx.Request:
            result = builder(client, method, url, *args, **kwargs)
            with suppress(Exception):
                call = _CALL.get()
                construction = _CONSTRUCTION.get()
                scope = scope_provider()
                if (
                    call is not None
                    and construction is not None
                    and not construction.claimed
                    and construction.call is call
                    and construction.client() is client
                    and scope is not None
                    and not scope.closed
                    and call.scope() is scope
                    and call.target is not None
                    and method.upper() == "POST"
                    and target_identity(url) == call.target
                ):
                    grant_request(scope, result, call.operation)
                    construction.claimed = True
            return result

        for resource, original in contracts:
            setattr(
                resource.owner,
                resource.name,
                _resource_wrapper(original, resource, scope_provider),
            )
        for factory_module, factory in factories:
            vars(factory_module)["get_async_httpx_client"] = _factory_wrapper(factory)
        setattr(AsyncHTTPHandler, "post", _post_wrapper(post, replacement=False))  # noqa: B010
        setattr(  # noqa: B010 - audited SDK compatibility hook
            AsyncHTTPHandler,
            "single_connection_post_request",
            _post_wrapper(replacement_post, replacement=True),
        )
        setattr(httpx.AsyncClient, "build_request", observed_builder)  # noqa: B010
        _installed = True
        return True
    except Exception:
        return False
