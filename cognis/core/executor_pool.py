"""Executor pool resolution for multi-executor agents (Stage 36).

Resolves the set of executors assigned to an agent: the *primary* set
(from ``execution.executor_id`` or ``execution.executor_selector``) and
the *additional* set (from ``execution.additional_executors``).

Hard rules enforced here:

- Primary executors are auto-eligible for initial selection and as
  routing targets. Additional executors are NEVER auto-selected; they
  are reachable only via explicit ``target_executor`` per-call routing
  or via an explicit ``switch_executor`` / ``/executor`` command.
- A primary selector matching N >= 1 usable executors yields a primary
  set of size N (no longer raises on multi-match).
- Deduplication: if the same executor matches both primary and
  additional bindings, primary membership wins.
- Unusable executors remain in the pool with a factual ``state`` so
  context and UI can present them. The controller never speculates.
- The controller picks the initial active executor only from the usable
  primary set; the choice is persisted to ``conversations.active_executor_id``
  and never re-picked by the controller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cognis.core.executor_policy import (
    ExecutorPolicy,
    is_executor_type_allowed,
)
from cognis.core.executor_resolution import labels_match
from cognis.logging import get_logger

logger = get_logger(__name__)


class ExecutorAvailability(StrEnum):
    """Factual availability state for an assigned executor.

    USABLE means: status=active, runtime_state in {active, degraded},
    desired/applied config versions match, deployment policy allows the
    type. (Connection presence is verified by the runtime path; the pool
    resolver only sees DB rows.)

    DEGRADED is a subset of USABLE — the executor can still route work,
    but its runtime is reporting reduced health.
    """

    USABLE = "usable"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    BLOCKED = "blocked"  # status != active
    RECONFIGURING = "reconfiguring"  # desired != applied
    POLICY_DENIED = "policy_denied"
    NOT_FOUND = "not_found"  # executor row missing
    UNAUTHORIZED = "unauthorized"  # row exists but not visible to user


_USABLE_STATES = frozenset({ExecutorAvailability.USABLE, ExecutorAvailability.DEGRADED})


def is_state_usable(state: ExecutorAvailability) -> bool:
    """Return whether a tool can be routed to an executor in this state."""

    return state in _USABLE_STATES


@dataclass(frozen=True)
class ResolvedExecutorTarget:
    """One assigned executor resolved from the agent's bindings."""

    executor_id: str
    executor_type: str
    is_primary: bool
    selection_source: str  # "explicit" | "selector" | "additional_explicit" | "additional_selector"
    description: str | None
    state: ExecutorAvailability
    enabled_tools: list[str] = field(default_factory=list)
    enabled_tool_groups: list[str] = field(default_factory=list)
    observed_tools: list[dict[str, Any]] = field(default_factory=list)
    runtime_state: str = "offline"
    desired_config_version: int = 0
    applied_config_version: int = 0
    owner_email: str | None = None
    labels: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    last_observed_at: Any | None = None
    row: Any | None = None  # opaque ExecutorRow handle for downstream

    @property
    def usable(self) -> bool:
        return is_state_usable(self.state)

    @property
    def observed_tool_names(self) -> set[str]:
        names: set[str] = set()
        for entry in self.observed_tools:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("tool_name")
                if isinstance(name, str) and name:
                    names.add(name)
            elif isinstance(entry, str):
                names.add(entry)
        return names


@dataclass
class ExecutorPool:
    """Resolved pool of executors assigned to an agent.

    Iteration order: primaries first (in the order they were discovered),
    then additional executors. Use ``by_id``, ``usable_primaries``, and
    ``is_assigned`` for membership tests.
    """

    primary: list[ResolvedExecutorTarget] = field(default_factory=list)
    additional: list[ResolvedExecutorTarget] = field(default_factory=list)

    @property
    def all(self) -> list[ResolvedExecutorTarget]:
        return [*self.primary, *self.additional]

    def by_id(self, executor_id: str) -> ResolvedExecutorTarget | None:
        for target in self.all:
            if target.executor_id == executor_id:
                return target
        return None

    def usable_primaries(self) -> list[ResolvedExecutorTarget]:
        return [t for t in self.primary if t.usable]

    def is_assigned(self, executor_id: str) -> bool:
        return self.by_id(executor_id) is not None

    def is_primary(self, executor_id: str) -> bool:
        return any(t.executor_id == executor_id for t in self.primary)


def parse_additional_executors(execution: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read the ``additional_executors`` field from a freeform execution dict.

    Validates structurally: each entry must be a dict with exactly one of
    ``executor_id`` (non-empty string) or ``executor_selector`` (non-empty
    dict mapping str->str). Optional ``description`` (string).

    Invalid entries are skipped with a debug log; the caller is expected
    to have run API-side validation.
    """

    raw = (execution or {}).get("additional_executors")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        executor_id = item.get("executor_id")
        selector = item.get("executor_selector")
        description = item.get("description")
        has_id = bool(isinstance(executor_id, str) and executor_id.strip())
        has_selector = bool(
            isinstance(selector, dict)
            and selector
            and all(
                isinstance(k, str) and isinstance(v, (str, int, bool)) for k, v in selector.items()
            )
        )
        if has_id == has_selector:
            # Need exactly one
            logger.debug(
                "skipping malformed additional executor binding",
                extra={"extra_data": {"entry": str(item)[:200]}},
            )
            continue
        normalized: dict[str, Any] = {}
        if has_id and isinstance(executor_id, str):
            normalized["executor_id"] = executor_id.strip()
        elif has_selector and isinstance(selector, dict):
            normalized["executor_selector"] = {str(k): str(v) for k, v in selector.items()}
        if isinstance(description, str) and description.strip():
            normalized["description"] = description.strip()
        entries.append(normalized)
    return entries


def _classify_state(
    row: Any,
    *,
    policy: ExecutorPolicy,
    owner_email: str | None,
) -> ExecutorAvailability:
    """Classify an executor row into a factual availability state."""

    if row is None:
        return ExecutorAvailability.NOT_FOUND

    # Visibility / scope check (mirrors is_executor_row_usable's owner gate)
    row_owner = getattr(row, "owner_email", None)
    from cognis.ownership import is_shared_owner_email

    if (
        owner_email is not None
        and row_owner != owner_email
        and not is_shared_owner_email(row_owner)
    ):
        return ExecutorAvailability.UNAUTHORIZED

    if getattr(row, "status", None) != "active":
        return ExecutorAvailability.BLOCKED

    if not is_executor_type_allowed(getattr(row, "executor_type", ""), policy):
        return ExecutorAvailability.POLICY_DENIED

    desired = int(getattr(row, "desired_config_version", 0) or 0)
    applied = int(getattr(row, "applied_config_version", 0) or 0)
    if desired != applied:
        return ExecutorAvailability.RECONFIGURING

    runtime_state = str(getattr(row, "runtime_state", "offline") or "offline")
    if runtime_state == "active":
        return ExecutorAvailability.USABLE
    if runtime_state == "degraded":
        return ExecutorAvailability.DEGRADED
    return ExecutorAvailability.OFFLINE


def _target_from_row(
    row: Any,
    *,
    is_primary: bool,
    selection_source: str,
    description: str | None,
    state: ExecutorAvailability,
) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=getattr(row, "executor_id", "") or "",
        executor_type=getattr(row, "executor_type", "") or "",
        is_primary=is_primary,
        selection_source=selection_source,
        description=description,
        state=state,
        enabled_tools=list(getattr(row, "enabled_tools", []) or []),
        enabled_tool_groups=list(getattr(row, "enabled_tool_groups", []) or []),
        observed_tools=list(getattr(row, "observed_tools", []) or []),
        runtime_state=str(getattr(row, "runtime_state", "offline") or "offline"),
        desired_config_version=int(getattr(row, "desired_config_version", 0) or 0),
        applied_config_version=int(getattr(row, "applied_config_version", 0) or 0),
        owner_email=getattr(row, "owner_email", None),
        labels=dict(getattr(row, "labels", {}) or {}),
        config=dict(getattr(row, "config", {}) or {}),
        last_observed_at=getattr(row, "last_observed_at", None),
        row=row,
    )


async def resolve_executor_pool(
    *,
    session_factory: Any,
    agent_execution: dict[str, Any] | None,
    user_email: str,
    executor_owner_email: str,
    policy: ExecutorPolicy,
) -> ExecutorPool:
    """Resolve the agent's primary + additional executor pool.

    Reads from the executors table once and partitions matching rows into
    primary and additional buckets. Primary membership wins on overlap.
    """

    from cognis.store.queries import get_executor_row, list_executors

    execution = agent_execution or {}
    primary_explicit = execution.get("executor_id")
    primary_selector = execution.get("executor_selector")
    additional_entries = parse_additional_executors(execution)

    primary_targets: list[ResolvedExecutorTarget] = []
    additional_targets: list[ResolvedExecutorTarget] = []
    seen_ids: set[str] = set()

    async with session_factory() as session:
        all_rows: list[Any] = await list_executors(
            session, owner_email=executor_owner_email, include_shared=True
        )

        # Primary explicit id
        if isinstance(primary_explicit, str) and primary_explicit.strip():
            row = await get_executor_row(
                session,
                str(primary_explicit),
                owner_email=executor_owner_email,
                include_shared=True,
            )
            if row is None:
                # Synthetic placeholder so context/UI can show "configured but missing"
                primary_targets.append(
                    ResolvedExecutorTarget(
                        executor_id=str(primary_explicit),
                        executor_type="",
                        is_primary=True,
                        selection_source="explicit",
                        description=None,
                        state=ExecutorAvailability.NOT_FOUND,
                    )
                )
                seen_ids.add(str(primary_explicit))
            else:
                state = _classify_state(row, policy=policy, owner_email=executor_owner_email)
                primary_targets.append(
                    _target_from_row(
                        row,
                        is_primary=True,
                        selection_source="explicit",
                        description=None,
                        state=state,
                    )
                )
                seen_ids.add(row.executor_id)

        # Primary selector — matches MAY produce N>=1 results (Stage 36 change)
        elif isinstance(primary_selector, dict) and primary_selector:
            normalized = {str(k): str(v) for k, v in primary_selector.items()}
            for row in all_rows:
                if row.executor_id in seen_ids:
                    continue
                if not labels_match(row.labels, normalized):
                    continue
                # Only include rows that are at least visible (not unauthorized)
                state = _classify_state(row, policy=policy, owner_email=executor_owner_email)
                if state == ExecutorAvailability.UNAUTHORIZED:
                    continue
                primary_targets.append(
                    _target_from_row(
                        row,
                        is_primary=True,
                        selection_source="selector",
                        description=None,
                        state=state,
                    )
                )
                seen_ids.add(row.executor_id)

        # Additional executors
        for entry in additional_entries:
            description = entry.get("description")
            entry_id = entry.get("executor_id")
            entry_selector = entry.get("executor_selector")
            if isinstance(entry_id, str) and entry_id:
                if entry_id in seen_ids:
                    # Already covered by primary or earlier additional
                    continue
                row = await get_executor_row(
                    session,
                    entry_id,
                    owner_email=executor_owner_email,
                    include_shared=True,
                )
                if row is None:
                    additional_targets.append(
                        ResolvedExecutorTarget(
                            executor_id=entry_id,
                            executor_type="",
                            is_primary=False,
                            selection_source="additional_explicit",
                            description=description,
                            state=ExecutorAvailability.NOT_FOUND,
                        )
                    )
                    seen_ids.add(entry_id)
                    continue
                state = _classify_state(row, policy=policy, owner_email=executor_owner_email)
                if state == ExecutorAvailability.UNAUTHORIZED:
                    continue
                additional_targets.append(
                    _target_from_row(
                        row,
                        is_primary=False,
                        selection_source="additional_explicit",
                        description=description,
                        state=state,
                    )
                )
                seen_ids.add(row.executor_id)
            elif isinstance(entry_selector, dict) and entry_selector:
                normalized = {str(k): str(v) for k, v in entry_selector.items()}
                for row in all_rows:
                    if row.executor_id in seen_ids:
                        continue
                    if not labels_match(row.labels, normalized):
                        continue
                    state = _classify_state(row, policy=policy, owner_email=executor_owner_email)
                    if state == ExecutorAvailability.UNAUTHORIZED:
                        continue
                    additional_targets.append(
                        _target_from_row(
                            row,
                            is_primary=False,
                            selection_source="additional_selector",
                            description=description,
                            state=state,
                        )
                    )
                    seen_ids.add(row.executor_id)

    return ExecutorPool(primary=primary_targets, additional=additional_targets)


def pick_initial_active(pool: ExecutorPool) -> ResolvedExecutorTarget | None:
    """Choose the initial active executor for a new conversation.

    Selects only from the usable primary set, preferring runtime_state
    ``active`` over ``degraded``, breaking ties by sorted ``executor_id``.

    Returns None if no usable primary exists. The controller must NOT
    fall back to an additional executor — that would violate the
    "additional never auto-selected" invariant.
    """

    candidates = pool.usable_primaries()
    if not candidates:
        return None

    def _sort_key(t: ResolvedExecutorTarget) -> tuple[int, str]:
        # active=0 (preferred), degraded=1, then executor_id
        rank = 0 if t.state == ExecutorAvailability.USABLE else 1
        return (rank, t.executor_id)

    candidates_sorted = sorted(candidates, key=_sort_key)
    return candidates_sorted[0]


def is_assigned(pool: ExecutorPool, executor_id: str) -> bool:
    """Convenience: is ``executor_id`` in the agent's assigned pool?"""

    return pool.is_assigned(executor_id)


def tool_observed_on(target: ResolvedExecutorTarget, tool_name: str) -> bool:
    """Return whether ``tool_name`` is observed on ``target`` and enabled."""

    from cognis.core.executor_resolution import is_tool_enabled
    from cognis.models.tool import NativeToolDefinition, ToolSource

    if not target.usable:
        return False
    # Build a minimal stub for is_tool_enabled which just needs name + category
    stub = NativeToolDefinition(
        name=tool_name,
        description="",
        category="",
        parameters={"type": "object"},
        source=ToolSource(type="executor"),
    )
    if not is_tool_enabled(stub, target.enabled_tools, target.enabled_tool_groups):
        return False
    if not target.observed_tools:
        # If we have no observation yet, be permissive when the tool is in
        # enabled_tools/enabled_tool_groups. The runtime list_tools call
        # will be the ultimate gate.
        return True
    return tool_name in target.observed_tool_names
