"""Request-scoped physical HTTP observation below SDK retry/client replacement.

The installed HTTPX version has no public per-client-factory instrumentation
hook. Install one transparent send-boundary wrapper per process (module import
is serialized by Python). Never replace a shared client or change retry globals.
Only a configured, task-local SDK scope enables observation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import isawaitable, signature
from typing import Any, Literal, cast
from weakref import WeakKeyDictionary

import httpx

from cognis.models.usage import UsageServiceTierApplicability
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_capture import UsageAttempt
from cognis.providers.llm.usage_http import HTTPObservationError
from cognis.providers.llm.usage_normalization import field, meaningful
from cognis.providers.llm.usage_parameters import project_parameters
from cognis.providers.llm.usage_sdk_dispatch import direct_resource, install_openai_dispatch
from cognis.providers.llm.usage_sdk_litellm import install_litellm_dispatch
from cognis.providers.llm.usage_sdk_purpose import (
    admitted,
    claim_delegation,
    delegation_applicability,
    inherit_redirect,
    request_applicability,
)
from cognis.providers.llm.usage_sdk_purpose import delegated as delegated_request

_PENDING_LIMIT = 32
_ACTIVE: ContextVar[SDKScope | None] = ContextVar("cognis_sdk_usage_scope", default=None)
_LAST_HTTP: ContextVar[_Submission | None] = ContextVar("cognis_sdk_usage_http", default=None)


@dataclass
class _Submission:
    scope: SDKScope
    attempt: UsageAttempt
    raw_profile: Literal["openai", "anthropic", "unknown"]
    decoded: bool = False
    raw_decoded: bool = False
    network_done: bool = False
    detached: bool = False

    def observe(self, value: Any) -> None:
        if self.attempt.finished:
            return
        self.decoded = True
        # SDK models can contain synthesized counts even when the provider sent
        # none. They are completion/delivery evidence, never provider usage.

    def observe_raw(self, value: Any, *, generated: bool = False) -> None:
        if self.attempt.finished:
            return
        self.raw_decoded = self.decoded = True
        # The client's existing decoder owns the content. Project counters now;
        # never retain a second body, text buffer or arbitrary response mapping.
        self.attempt.observe(value, generated=generated, semantic_profile=self.raw_profile)

    def finish(
        self,
        *,
        error: BaseException | None = None,
        uncertain: bool = False,
    ) -> None:
        if self.attempt.finished:
            return
        self.attempt.finish(error=error, outcome="unknown" if uncertain else "success")
        self.scope.pending.pop(self.attempt.context.trace.attempt_id, None)
        for response, submission in tuple(self.scope.responses.items()):
            if submission is self:
                del self.scope.responses[response]


class _ObservedBytes(httpx.AsyncByteStream):
    """Forward original bytes without buffering the inference response body."""

    def __init__(self, source: httpx.AsyncByteStream, submission: _Submission):
        self.source = source
        self.submission = submission

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.source:
                try:
                    _LAST_HTTP.set(self.submission)
                except Exception:
                    self.submission.scope.binding.request.capture.failed_events += 1
                yield chunk
        except GeneratorExit:
            # SDKs can close the HTTP body after a semantic end marker. The
            # consuming SDK stream, not raw-generator closure, owns the outcome.
            raise
        except BaseException as exc:
            self.submission.finish(error=exc)
            raise
        finally:
            self.submission.network_done = True
            if self.submission.detached:
                self.submission.finish(uncertain=True)

    async def aclose(self) -> None:
        try:
            await self.source.aclose()
        finally:
            self.submission.network_done = True
            if self.submission.detached:
                self.submission.finish(uncertain=True)


class SDKScope:
    """One SDK invocation's bounded association state, never provider singleton state."""

    def __init__(
        self,
        binding: UsageBinding,
        *,
        operation: Literal[
            "chat",
            "responses",
            "embeddings",
            "systemone",
            "native",
        ],
        parameters: Mapping[str, Any],
        profile: Literal[
            "openai",
            "anthropic",
            "unknown",
        ] = "openai",
    ):
        self.binding = binding
        self.operation = operation
        self.streaming = parameters.get("stream") is True
        # Do not retain prompts, inputs, tool schemas, clients or credentials.
        self.parameters = project_parameters(parameters, source="adapter_override")
        # Projection drops unsupported tier labels, so record omission separately.
        self.tier_omitted = not {"service_tier", "speed"} & parameters.keys()
        self.untiered_overrides_absent = "service_tier" not in parameters and all(
            not parameters.get(key)
            for key in (
                "extra_body",
                "extra_query",
                "extra_headers",
                "default_query",
                "default_headers",
                "client",
                "http_client",
            )
        )
        self.profile = profile
        text = parameters.get("input") or parameters.get("text")
        self.input_characters = (
            len(text) if binding.media_operation == "synthesize" and isinstance(text, str) else None
        )
        self.pending: dict[str, _Submission] = {}
        self.responses: WeakKeyDictionary[Any, _Submission] = WeakKeyDictionary()
        self.submissions = 0
        self.closed = False
        self.detached = False
        if self.streaming and not _OPENAI_DECODER_INSTALLED:
            self.binding.request.capture.failed_events += 1

    def accepts_operation(self, operation: str) -> bool:
        """Match a registered canonical resource to this live dispatch."""
        if self.closed:
            return False
        if self.operation in {"chat", "responses"}:
            return operation in {"chat", "responses"}
        if self.operation == "native":
            return operation in {"native", "chat", "responses"}
        return operation == self.operation

    def accepts_untiered_model(self, model: str | None) -> bool:
        """Only the bound physical target without caller construction overrides."""
        return self.untiered_overrides_absent and model == self.binding.model

    @contextmanager
    def activate(self) -> Iterator[None]:
        scope_token = _ACTIVE.set(self)
        http_token = _LAST_HTTP.set(None)
        try:
            yield
        finally:
            _LAST_HTTP.reset(http_token)
            _ACTIVE.reset(scope_token)

    def begin(
        self, path: str, *, applicability: UsageServiceTierApplicability | None = None
    ) -> _Submission | None:
        if self.closed:
            return None
        previous = _LAST_HTTP.get()
        if previous is not None and previous.scope is self and not previous.attempt.finished:
            # A same-task resubmission ends the previous physical lifetime. Do
            # not attach the eventual retry's usage or error to that request.
            previous.finish(uncertain=not previous.decoded)
        self.submissions += 1
        attempt = self.binding.begin(
            operation=self.operation,
            streaming=self.streaming,
            parameters=self.parameters,
            semantic_profile=self.profile,
            applicability=applicability,
            tier_omitted=self.tier_omitted,
        )
        if attempt is None:
            return None
        if self.input_characters is not None:
            attempt.accumulator.report_extra(
                "input_characters", self.input_characters, source="adapter", derived=True
            )
        path = path.rstrip("/")
        raw_profile: Literal["openai", "anthropic", "unknown"] = "unknown"
        if path.endswith("/messages"):
            raw_profile = "anthropic"
        elif path.endswith(
            (
                "/chat/completions",
                "/responses",
                "/embeddings",
                "/images/generations",
                "/images/edits",
                "/audio/transcriptions",
                "/audio/speech",
            )
        ):
            raw_profile = "openai"
        submission = _Submission(self, attempt, raw_profile)
        if len(self.pending) < _PENDING_LIMIT:
            self.pending[attempt.context.trace.attempt_id] = submission
        else:
            # Keep physical start/final capture bounded even for SDK fan-out;
            # only decoded-result association is unavailable beyond this limit.
            submission.detached = True
            self.detached = True
            self.binding.request.capture.failed_events += 1
        return submission

    def bind_response(self, response: Any, submission: _Submission) -> None:
        """Associate an existing client response without mutating client state."""
        if self.closed or submission.attempt.finished:
            return
        status = response.status_code if isinstance(response, httpx.Response) else response.status
        submission.attempt.observe(
            {
                "request_id": response.headers.get("x-request-id")
                or response.headers.get("request-id"),
            },
            generated=False,
        )
        if status >= 400:
            submission.finish(error=HTTPObservationError(status))
        elif status >= 300 or submission.detached:
            submission.finish(uncertain=True)
        elif len(self.responses) < _PENDING_LIMIT * 2:
            self.responses[response] = submission
        else:
            self.binding.request.capture.failed_events += 1
        _LAST_HTTP.set(submission)

    def decoded_response(self, response: Any, value: Any, *, generated: bool = False) -> None:
        if not self.closed:
            submission = self.responses.get(response)
            if submission is not None:
                submission.observe_raw(value, generated=generated)

    def observe(self, value: Any) -> None:
        if self.closed:
            return
        current = _LAST_HTTP.get()
        if current is None or current.scope is not self or current.attempt.finished:
            candidates = [item for item in self.pending.values() if not item.attempt.finished]
            current = candidates[0] if len(candidates) == 1 and not self.detached else None
            if current is None:
                request_id = field(
                    field(field(value, "_hidden_params"), "additional_headers"), "x-request-id"
                ) or field(value, "_request_id")
                matches = [
                    item
                    for item in candidates
                    if request_id is not None and item.attempt.accumulator.request_id == request_id
                ]
                current = matches[0] if len(matches) == 1 else None
        if current is not None:
            current.observe(value)
        elif self.pending:
            # Aggregated SDK results must not be charged to an arbitrary request.
            self.binding.request.capture.failed_events += 1
        if self.streaming and meaningful(value):
            self.binding.request.mark_generated()

    def finish(self, *, error: BaseException | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        current = _LAST_HTTP.get()
        pending = tuple(self.pending.values())
        for item in pending:
            attributable_error = error if item is current or len(pending) == 1 else None
            item.finish(
                error=attributable_error,
                uncertain=attributable_error is None and not item.decoded,
            )
        self.responses.clear()


class SDKStream:
    """Keep the same correlation active during lazy SDK retries and cleanup."""

    def __init__(self, source: Any, scope: SDKScope):
        self.source, self.iterator, self.scope = source, aiter(source), scope
        self.closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    def __aiter__(self) -> SDKStream:
        return self

    async def __anext__(self) -> Any:
        with self.scope.activate():
            try:
                value = await anext(self.iterator)
            except StopAsyncIteration:
                self.scope.finish()
                raise
            except BaseException as exc:
                self.scope.finish(error=exc)
                raise
            try:
                self.scope.observe(value)
            except Exception:
                self.scope.binding.request.capture.failed_events += 1
            return value

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        with self.scope.activate():
            try:
                sources = (
                    (self.iterator,)
                    if self.source is self.iterator
                    else (
                        self.iterator,
                        self.source,
                    )
                )
                for source in sources:
                    close = getattr(source, "aclose", None) or getattr(source, "close", None)
                    if close is not None:
                        result = close()
                        if isawaitable(result):
                            await result
            except BaseException as exc:
                self.scope.finish(error=exc)
                raise
            finally:
                self.scope.finish(error=GeneratorExit())

    async def close(self) -> None:
        await self.aclose()


async def observe_sdk(
    dispatch: Callable[[], Awaitable[Any]],
    *,
    binding: UsageBinding,
    operation: Literal["chat", "responses", "embeddings", "systemone", "native"],
    parameters: Mapping[str, Any],
    dispatch_source: Callable[..., Any] | None = None,
) -> Any:
    """Observe actual submissions while preserving the SDK's configured retry policy."""
    install_litellm_dispatch(_ACTIVE.get)
    active = _ACTIVE.get()
    if (
        active is not None
        and not active.closed
        and active.binding == binding
        and active.operation == operation
    ):
        with direct_resource(dispatch_source, parameters):
            return await dispatch()
    try:
        scope = SDKScope(
            binding,
            operation=operation,
            parameters=parameters,
            profile="unknown" if operation == "native" else "openai",
        )
    except Exception:
        binding.request.capture.failed_events += 1
        return await dispatch()
    with scope.activate(), direct_resource(dispatch_source, parameters):
        try:
            result = await dispatch()
        except BaseException as exc:
            scope.finish(error=exc)
            raise
        if parameters.get("stream") is True and hasattr(result, "__aiter__"):
            return SDKStream(result, scope)
        try:
            scope.observe(result)
        except Exception:
            binding.request.capture.failed_events += 1
        scope.finish()
        if (
            not scope.submissions
            and field(field(result, "_hidden_params"), "cache_hit") is not True
        ):
            binding.request.capture.failed_events += 1
        return result


_HTTPXSend = Callable[[httpx.AsyncClient, httpx.Request], Awaitable[httpx.Response]]
_send_single_request = cast(
    _HTTPXSend | None, getattr(httpx.AsyncClient, "_send_single_request", None)
)
_response_json = httpx.Response.json
_AIOHTTP_INSTALLED = False


def _enabled(scope: SDKScope | None, request: httpx.Request) -> bool:
    try:
        return admitted(scope, request)
    except Exception:
        if scope is not None:
            scope.binding.request.capture.failed_events += 1
        return False


def _observe_json(response: Any, value: Any) -> None:
    scope = _ACTIVE.get()
    if scope is not None and not scope.closed:
        try:
            scope.decoded_response(response, value)
        except Exception:
            scope.binding.request.capture.failed_events += 1


def _observe_provider_event(response: httpx.Response, value: object) -> None:
    """Only the original response identity can authorize provider-event evidence."""
    scope = _ACTIVE.get()
    if scope is None or scope.closed or not scope.streaming:
        return
    try:
        scope.decoded_response(response, value, generated=True)
    except Exception:
        scope.binding.request.capture.failed_events += 1


@wraps(_response_json)
def _observed_json(response: httpx.Response, **kwargs: Any) -> Any:
    value = _response_json(response, **kwargs)
    _observe_json(response, value)
    return value


async def _observed_send(client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
    assert _send_single_request is not None
    scope = _ACTIVE.get()
    if not _enabled(scope, request):
        return await _send_single_request(client, request)
    assert scope is not None
    try:
        # This installed transport delegates HTTP to ClientSession, whose
        # middleware is the lower boundary (including redirects/retries). Do
        # not emit a second, outer HTTPX ledger for the same submission.
        transport = client._transport_for_url(request.url)
        delegated = type(transport).__module__ == "litellm.llms.custom_httpx.aiohttp_transport"
        submission = (
            None
            if delegated
            else scope.begin(request.url.path, applicability=request_applicability(scope, request))
        )
    except Exception:
        scope.binding.request.capture.failed_events += 1
        submission = None
        delegated = False
    try:
        with delegated_request(scope, request) if delegated else nullcontext():
            response = await _send_single_request(client, request)
    except BaseException as exc:
        if submission is not None:
            submission.finish(error=exc)
        raise
    if delegated:
        current = _LAST_HTTP.get()
        submission = current if current is not None and current.scope is scope else None
    if submission is not None and not submission.attempt.finished:
        try:
            scope.bind_response(response, submission)
            if not submission.attempt.finished:
                if isinstance(response.stream, httpx.AsyncByteStream):
                    response.stream = _ObservedBytes(response.stream, submission)
                else:
                    scope.binding.request.capture.failed_events += 1
        except Exception:
            scope.binding.request.capture.failed_events += 1
    return response


def _install_aiohttp() -> bool:
    """Use request-local public middleware without modifying a shared session."""
    try:
        from aiohttp import ClientResponse, ClientSession
    except ImportError:
        return False
    original = getattr(ClientSession, "_request", None)
    if original is None or "middlewares" not in signature(original).parameters:
        return False
    original_json = ClientResponse.json

    @wraps(original)
    async def observed_request(session: Any, method: str, url: Any, **kwargs: Any) -> Any:
        scope = _ACTIVE.get()
        allowed = False
        with suppress(Exception):
            allowed = not getattr(session, "closed", False) and claim_delegation(
                scope,
                method,
                url,
            )
        if not allowed:
            return await original(session, method, url, **kwargs)

        async def physical(req: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
            if scope is None or scope.closed or req.method not in {"POST", "PUT"}:
                return await handler(req)
            try:
                submission = scope.begin(
                    req.url.path, applicability=delegation_applicability(scope)
                )
            except Exception:
                scope.binding.request.capture.failed_events += 1
                submission = None
            try:
                response = await handler(req)
            except BaseException as exc:
                if submission is not None:
                    submission.finish(error=exc)
                raise
            if submission is not None:
                try:
                    scope.bind_response(response, submission)
                except Exception:
                    scope.binding.request.capture.failed_events += 1
            return response

        # None means the session's existing middleware chain. Append our
        # observer innermost so retry middleware still invokes it per send.
        selected = kwargs.get("middlewares")
        if selected is None:
            selected = session._middlewares
        kwargs["middlewares"] = (*selected, physical)
        return await original(session, method, url, **kwargs)

    @wraps(original_json)
    async def observed_json(response: Any, **kwargs: Any) -> Any:
        value = await original_json(response, **kwargs)
        _observe_json(response, value)
        return value

    # These are version-checked compatibility hooks, not injected provider or
    # application state. They remain inert outside a live task-local SDK scope.
    setattr(ClientSession, "_request", observed_request)  # noqa: B010 - class compatibility hook
    setattr(ClientResponse, "json", observed_json)  # noqa: B010 - class compatibility hook
    return True


if _send_single_request is not None:
    setattr(  # noqa: B010 - class compatibility hook
        httpx.AsyncClient, "_send_single_request", wraps(_send_single_request)(_observed_send)
    )
    setattr(httpx.Response, "json", _observed_json)  # noqa: B010 - class compatibility hook
_redirect_request = httpx.AsyncClient._build_redirect_request


@wraps(_redirect_request)
def _observed_redirect(
    client: httpx.AsyncClient,
    request: httpx.Request,
    response: httpx.Response,
) -> httpx.Request:
    redirected = _redirect_request(client, request, response)
    with suppress(Exception):
        inherit_redirect(_ACTIVE.get(), request, redirected)
    return redirected


setattr(httpx.AsyncClient, "_build_redirect_request", _observed_redirect)  # noqa: B010

try:
    _AIOHTTP_INSTALLED = _install_aiohttp()
except Exception:
    # An unsupported optional HTTP backend is a coverage gap, not an inference
    # admission failure. Installed-path dispatch tests must reject that gap.
    _AIOHTTP_INSTALLED = False

from cognis.providers.llm.usage_sdk_openai import install_openai_decoder  # noqa: E402

_OPENAI_DECODER_INSTALLED = install_openai_decoder(_observe_provider_event)
_OPENAI_DISPATCH_INSTALLED = install_openai_dispatch(_ACTIVE.get)
