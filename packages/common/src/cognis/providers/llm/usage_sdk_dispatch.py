"""Audited SDK resource owners mint purpose at matching request construction.

Only scalar endpoint metadata and weak client/scope identities are retained.
Resource arguments, prompts, bodies, headers and SDK response objects are never
copied into a capability.
"""

from __future__ import annotations

import importlib
import weakref
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from importlib.metadata import version
from inspect import Parameter, signature
from typing import Any, Protocol, cast

import httpx

from cognis.models.usage import UsageOperationScope, UsageServiceTierApplicability
from cognis.providers.llm.usage_sdk_purpose import PurposeScope, grant_request

_OPENAI_RESOURCES = (
    ("chat.completions", "AsyncCompletions", "create", "/chat/completions", "chat", "model"),
    ("responses", "AsyncResponses", "create", "/responses", "responses", "model"),
    ("embeddings", "AsyncEmbeddings", "create", "/embeddings", "embeddings", "model"),
    ("images", "AsyncImages", "generate", "/images/generations", "native", "prompt"),
    ("images", "AsyncImages", "edit", "/images/edits", "native", "image"),
    ("audio.speech", "AsyncSpeech", "create", "/audio/speech", "native", "input"),
    (
        "audio.transcriptions",
        "AsyncTranscriptions",
        "create",
        "/audio/transcriptions",
        "native",
        "file",
    ),
)


class _SDKResourceOwner(Protocol):
    _client: Any


@dataclass(frozen=True, slots=True)
class _Resource:
    owner: type
    name: str
    original: Callable[..., Any]
    endpoint: str
    operation: str
    untiered_scope: UsageOperationScope | None = None


@dataclass(frozen=True, slots=True)
class _ConstructionOptions:
    """Projection from the audited SDK call, not from a body or headers."""

    model: str | None
    tier_present: bool
    extra_body_present: bool
    extra_query_present: bool
    extra_headers_present: bool


def _construction_options(parameters: Mapping[str, Any] | None) -> _ConstructionOptions | None:
    if parameters is None:
        return None
    model = parameters.get("model")
    return _ConstructionOptions(
        model if isinstance(model, str) and len(model) <= 256 else None,
        "service_tier" in parameters,
        bool(parameters.get("extra_body")),
        bool(parameters.get("extra_query")),
        bool(parameters.get("extra_headers")),
    )


@dataclass(frozen=True, slots=True)
class _Call:
    scope: weakref.ReferenceType[PurposeScope]
    client: weakref.ReferenceType[Any]
    endpoint: str
    operation: str
    applicability: UsageServiceTierApplicability | None = None
    raw_headers_pending: bool = False


_CALL: ContextVar[_Call | None] = ContextVar("cognis_sdk_resource_call", default=None)
_RESOURCES: dict[Callable[..., Any], _Resource] = {}
_BUILDERS: dict[type, Callable[..., Any]] = {}
_RAW_BUILDERS: dict[type, Callable[..., Any]] = {}


def _no_scope() -> PurposeScope | None:
    return None


_scope_provider: Callable[[], PurposeScope | None] = _no_scope


@contextmanager
def _resource_call(
    resource: _Resource,
    owner: Any,
    parameters: Mapping[str, Any] | None = None,
    *,
    raw_headers_pending: bool = False,
) -> Iterator[None]:
    call = None
    with suppress(Exception):
        scope = _scope_provider()
        if (
            scope is not None
            and not scope.closed
            and isinstance(owner, resource.owner)
            and scope.accepts_operation(resource.operation)
        ):
            client = cast(_SDKResourceOwner, owner)._client
            options = _construction_options(parameters)
            prior = _CALL.get()
            raw_headers_proven = (
                prior is not None
                and prior.raw_headers_pending
                and prior.applicability is not None
                and prior.scope() is scope
                and prior.client() is client
                and prior.endpoint == resource.endpoint
                and prior.operation == resource.operation
            )
            # Only safe presence/identity evidence from the audited construction
            # ABI. Never inspect serialized bodies, query contents or headers.
            applicability = None
            if (
                resource.untiered_scope is not None
                and options is not None
                and scope.accepts_untiered_model(options.model)
                and not options.tier_present
                and not options.extra_body_present
                and not options.extra_query_present
                and (not options.extra_headers_present or raw_headers_proven)
                and not getattr(client, "_custom_query", None)
                and not getattr(client, "_custom_headers", None)
            ):
                applicability = UsageServiceTierApplicability(
                    state="not_applicable", scope=resource.untiered_scope
                )
            call = _Call(
                weakref.ref(scope),
                weakref.ref(client),
                resource.endpoint,
                resource.operation,
                applicability,
                raw_headers_pending,
            )
    token = _CALL.set(call)
    try:
        yield
    finally:
        _CALL.reset(token)


@contextmanager
def direct_resource(
    source: Callable[..., Any] | None, parameters: Mapping[str, Any] | None = None
) -> Iterator[None]:
    """Recognize a genuine bound resource method captured before installation."""
    function = getattr(source, "__func__", None)
    resource = _RESOURCES.get(function) if callable(function) else None
    if resource is None:
        yield
    else:
        with _resource_call(resource, getattr(source, "__self__", None), parameters):
            yield


def _wrap_resource(resource: _Resource) -> Callable[..., Any]:
    @wraps(resource.original)
    async def observed(owner: Any, *args: Any, **kwargs: Any) -> Any:
        with _resource_call(resource, owner, kwargs if not args else None):
            return await resource.original(owner, *args, **kwargs)

    return observed


def _wrap_raw_constructor(
    original: Callable[..., Any],
    resource: _Resource,
) -> Callable[..., Any]:
    """Bind the audited SDK raw-response option before it adds its own header.

    The child resource consumes this one construction step. No header names or
    values are examined, copied, compared or retained.
    """

    @wraps(original)
    def observed(owner: Any, api: Any) -> None:
        original(owner, api)
        source = getattr(owner, resource.name)

        @wraps(source)
        async def raw(*args: Any, **kwargs: Any) -> Any:
            with _resource_call(
                resource,
                api,
                kwargs if not args else None,
                raw_headers_pending=True,
            ):
                return await source(*args, **kwargs)

        setattr(owner, resource.name, raw)

    return observed


def _wrap_builder(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def observed(client: Any, options: Any, *, retries_taken: int = 0) -> httpx.Request:
        endpoint = method = None
        with suppress(Exception):
            # Azure's builder legitimately rewrites options.url in place.
            endpoint, method = options.url, options.method
        request: httpx.Request = original(client, options, retries_taken=retries_taken)
        with suppress(Exception):
            call = _CALL.get()
            scope = _scope_provider()
            if (
                call is not None
                and scope is not None
                and not scope.closed
                and call.scope() is scope
                and call.client() is client
                and isinstance(method, str)
                and method.upper() == "POST"
                and endpoint == call.endpoint
            ):
                # extra_json is the SDK's typed override channel. Its absence
                # does not authorize anything without the resource-owned proof.
                applicability = call.applicability if not options.extra_json else None
                grant_request(scope, request, call.operation, applicability=applicability)
        return request

    return observed


def install_openai_dispatch(
    scope_provider: Callable[[], PurposeScope | None],
) -> bool:
    """Fence canonical SDK resources and request builders to OpenAI 2.30.0."""
    global _scope_provider
    try:
        if version("openai") != "2.30.0":
            return False
        from openai import AsyncAzureOpenAI
        from openai._base_client import AsyncAPIClient

        if _BUILDERS:
            return all(
                cast(Any, owner)._build_request is wrapped for owner, wrapped in _BUILDERS.items()
            ) and all(
                vars(owner).get("__init__") is wrapped for owner, wrapped in _RAW_BUILDERS.items()
            )
        expected = (
            ("self", Parameter.POSITIONAL_OR_KEYWORD),
            ("options", Parameter.POSITIONAL_OR_KEYWORD),
            ("retries_taken", Parameter.KEYWORD_ONLY),
        )
        builders = (AsyncAPIClient, AsyncAzureOpenAI)
        for owner in builders:
            if (
                tuple((p.name, p.kind) for p in signature(owner._build_request).parameters.values())
                != expected
            ):
                return False
        resources = []
        raw_resources = []
        for module, class_name, name, endpoint, operation, required in _OPENAI_RESOURCES:
            owner = getattr(importlib.import_module(f"openai.resources.{module}"), class_name)
            original = getattr(owner, name)
            parameters = signature(original).parameters
            if next(iter(parameters)) != "self" or required not in parameters:
                return False
            untiered_scope = None
            if (module, name) in {("embeddings", "create"), ("images", "generate")}:
                if (
                    "service_tier" in parameters
                    or not {"model", "extra_body", "extra_query", "extra_headers"}.issubset(
                        parameters
                    )
                    or any(p.kind == Parameter.VAR_KEYWORD for p in parameters.values())
                ):
                    return False
                untiered_scope = UsageOperationScope(resource=f"openai.{module}.{name}")
            resource = _Resource(owner, name, original, endpoint, operation, untiered_scope)
            resources.append(resource)
            if untiered_scope is not None:
                raw_owner = getattr(
                    importlib.import_module(f"openai.resources.{module}"),
                    f"{class_name}WithRawResponse",
                )
                raw_init = raw_owner.__init__
                if tuple(signature(raw_init).parameters) != ("self", module):
                    return False
                raw_resources.append((raw_owner, raw_init, resource))
        _scope_provider = scope_provider
        for resource in resources:
            wrapped = _wrap_resource(resource)
            _RESOURCES[resource.original] = resource
            _RESOURCES[wrapped] = resource
            setattr(resource.owner, resource.name, wrapped)
        for raw_owner, original, resource in raw_resources:
            wrapped = _wrap_raw_constructor(original, resource)
            raw_owner.__init__ = wrapped
            _RAW_BUILDERS[raw_owner] = wrapped
        for owner in builders:
            wrapped = _wrap_builder(owner._build_request)
            setattr(owner, "_build_request", wrapped)  # noqa: B010 - SDK compatibility hook
            _BUILDERS[owner] = wrapped
        return True
    except Exception:
        return False
