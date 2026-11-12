"""Request-local inference capabilities; an open SDK scope is not authority."""

from __future__ import annotations

import hashlib
import weakref
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from cognis.models.usage import UsageServiceTierApplicability

_EXTENSION = "cognis.inference_submission"


class PurposeScope(Protocol):
    @property
    def closed(self) -> bool: ...

    def accepts_operation(self, operation: str) -> bool: ...

    def accepts_untiered_model(self, model: str | None) -> bool: ...


@dataclass(frozen=True, slots=True)
class _RequestGrant:
    scope: weakref.ReferenceType[PurposeScope]
    request_ref: weakref.ReferenceType[httpx.Request]
    operation: str
    applicability: UsageServiceTierApplicability | None = None


def grant_request(
    scope: PurposeScope,
    request: httpx.Request,
    operation: str,
    *,
    applicability: UsageServiceTierApplicability | None = None,
) -> None:
    """Called only by a registered canonical resource's request constructor."""
    if not scope.closed and scope.accepts_operation(operation):
        request.extensions[_EXTENSION] = _RequestGrant(
            weakref.ref(scope),
            weakref.ref(request),
            operation,
            applicability,
        )


def admitted(scope: PurposeScope | None, request: httpx.Request) -> bool:
    """Validate the actual capability object and both live owning identities."""
    grant = request.extensions.get(_EXTENSION)
    return (
        scope is not None
        and not scope.closed
        and request.method.upper() in {"POST", "PUT"}
        and isinstance(grant, _RequestGrant)
        and grant.scope() is scope
        and grant.request_ref() is request
        and scope.accepts_operation(grant.operation)
    )


def request_applicability(
    scope: PurposeScope, request: httpx.Request
) -> UsageServiceTierApplicability | None:
    """Read semantic evidence only from a live, request-identity-bound grant."""
    if admitted(scope, request):
        grant = request.extensions[_EXTENSION]
        if isinstance(grant, _RequestGrant):
            return grant.applicability
    return None


def delegation_applicability(scope: PurposeScope) -> UsageServiceTierApplicability | None:
    """Carry the same grant through the existing one-shot transport delegation."""
    state = _DELEGATED.get()
    if state is not None and state.claimed and state.grant.scope() is scope:
        return state.grant.applicability
    return None


def inherit_redirect(
    scope: PurposeScope | None,
    original: httpx.Request,
    redirected: httpx.Request,
) -> None:
    """Only HTTPX's actual redirect constructor may rebind a valid grant."""
    if (
        scope is not None
        and admitted(scope, original)
        and redirected.extensions.get(_EXTENSION) is original.extensions.get(_EXTENSION)
    ):
        grant_request(scope, redirected, original.extensions[_EXTENSION].operation)


def target_identity(url: Any) -> bytes:
    """Opaque exact-target association, never a URL classification policy."""
    return hashlib.sha256(str(httpx.URL(str(url))).encode()).digest()


@dataclass(slots=True)
class _Delegation:
    grant: _RequestGrant
    target: bytes
    method: str
    claimed: bool = False


_DELEGATED: ContextVar[_Delegation | None] = ContextVar(
    "cognis_inference_delegated_request",
    default=None,
)


@contextmanager
def delegated(scope: PurposeScope, request: httpx.Request) -> Iterator[None]:
    """Bind one exact HTTPX-to-aiohttp transport delegation, not sibling I/O."""
    state = None
    with suppress(Exception):
        if admitted(scope, request):
            state = _Delegation(
                request.extensions[_EXTENSION],
                target_identity(request.url),
                request.method,
            )
    if state is None:
        yield
        return
    token = _DELEGATED.set(state)
    try:
        yield
    finally:
        _DELEGATED.reset(token)


def claim_delegation(scope: PurposeScope | None, method: str, url: Any) -> bool:
    """Admission occurs once per request-local aiohttp middleware chain."""
    state = _DELEGATED.get()
    if (
        scope is None
        or scope.closed
        or state is None
        or state.claimed
        or state.grant.scope() is not scope
        or state.grant.request_ref() is None
        or not scope.accepts_operation(state.grant.operation)
        or method.upper() != state.method
        or target_identity(url) != state.target
    ):
        return False
    state.claimed = True
    return True
