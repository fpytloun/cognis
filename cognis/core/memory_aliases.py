"""Session-scoped aliases for durable Mnemory record revisions."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

MEMORY_ALIASES_METADATA = "memory_aliases"
_ALIAS_RE = re.compile(r"^m([1-9][0-9]*)$")
_VOLATILE_REVISION_KEYS = {
    "access_count",
    "checked_at",
    "last_accessed",
    "last_accessed_at",
    "score",
}
_NATIVE_REVISION_KEYS = ("revision_id", "revision", "version", "provenance_id")


class MemoryAliasError(ValueError):
    """Raised when a model supplies an unusable session alias."""

    def __init__(self, alias: str, reason: str) -> None:
        super().__init__(f"Memory alias {alias} is {reason} in this session.")
        self.alias = alias
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MemoryAliasBinding:
    """One immutable alias-to-record-revision binding."""

    alias: str
    memory_id: str
    revision: str
    expected_revision: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "alias": self.alias,
            "memory_id": self.memory_id,
            "revision": self.revision,
        }
        if self.expected_revision is not None:
            result["expected_revision"] = self.expected_revision
        return result


@dataclass(slots=True)
class MemoryAliasState:
    """Replay-derived alias state for one Intaris session."""

    bindings: dict[str, MemoryAliasBinding] = field(default_factory=dict)
    aliases_by_revision: dict[tuple[str, str], str] = field(default_factory=dict)
    active_by_memory_id: dict[str, str] = field(default_factory=dict)
    invalidated: set[str] = field(default_factory=set)
    next_number: int = 1

    def resolve(self, value: str) -> str:
        """Resolve a current alias or preserve a canonical UUID."""

        candidate = value.strip()
        if not _ALIAS_RE.fullmatch(candidate):
            try:
                uuid.UUID(candidate)
            except ValueError:
                return candidate
            return candidate
        binding = self.resolve_binding(candidate)
        return binding.memory_id if binding is not None else candidate

    def resolve_binding(self, value: str) -> MemoryAliasBinding | None:
        """Return an active alias binding, or none for a canonical ID."""

        candidate = value.strip()
        if not _ALIAS_RE.fullmatch(candidate):
            return None
        binding = self.bindings.get(candidate)
        if binding is None:
            raise MemoryAliasError(candidate, "unknown")
        if (
            candidate in self.invalidated
            or self.active_by_memory_id.get(binding.memory_id) != candidate
        ):
            raise MemoryAliasError(candidate, "stale")
        return binding

    def active_alias(self, memory_id: str) -> str | None:
        """Return the active alias for a canonical ID, if one exists."""

        alias = self.active_by_memory_id.get(memory_id)
        if alias is None or alias in self.invalidated:
            return None
        return alias

    def plan_records(
        self, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Project record IDs to aliases without mutating durable-derived state."""

        projected: list[dict[str, Any]] = []
        new_bindings: list[MemoryAliasBinding] = []
        invalidated: set[str] = set()
        planned_by_revision: dict[tuple[str, str], str] = {}
        planned_active = dict(self.active_by_memory_id)
        next_number = self.next_number

        for record in records:
            item = dict(record)
            memory_id = str(item.get("id") or "").strip()
            if not memory_id:
                projected.append(item)
                continue
            revision, expected_revision = memory_revision(item)
            revision_key = (memory_id, revision)
            alias = self.aliases_by_revision.get(revision_key) or planned_by_revision.get(
                revision_key
            )
            if alias is None:
                alias = f"m{next_number}"
                next_number += 1
                binding = MemoryAliasBinding(alias, memory_id, revision, expected_revision)
                new_bindings.append(binding)
                planned_by_revision[revision_key] = alias
            previous = planned_active.get(memory_id)
            if previous and previous != alias:
                invalidated.add(previous)
            planned_active[memory_id] = alias
            item["id"] = alias
            projected.append(item)

        if not new_bindings and not invalidated:
            return projected, None
        return projected, {
            "version": 1,
            "bindings": [binding.as_dict() for binding in new_bindings],
            "invalidated": sorted(invalidated, key=_alias_sort_key),
        }

    def invalidate_memory(self, memory_id: str) -> dict[str, Any] | None:
        alias = self.active_by_memory_id.get(memory_id)
        if alias is None or alias in self.invalidated:
            return None
        return {"version": 1, "bindings": [], "invalidated": [alias]}

    def apply(self, raw: Any) -> bool:
        """Apply one replay delta. Return false for malformed or conflicting data."""

        if not isinstance(raw, dict) or raw.get("version") != 1:
            return False
        raw_bindings = raw.get("bindings", [])
        raw_invalidated = raw.get("invalidated", [])
        if not isinstance(raw_bindings, list) or not isinstance(raw_invalidated, list):
            return False
        parsed: list[MemoryAliasBinding] = []
        parsed_by_alias: dict[str, MemoryAliasBinding] = {}
        parsed_by_revision: dict[tuple[str, str], str] = {}
        for item in raw_bindings:
            if not isinstance(item, dict):
                return False
            alias = item.get("alias")
            memory_id = item.get("memory_id")
            revision = item.get("revision")
            expected_revision = item.get("expected_revision")
            if (
                not isinstance(alias, str)
                or _ALIAS_RE.fullmatch(alias) is None
                or not isinstance(memory_id, str)
                or not memory_id
                or not isinstance(revision, str)
                or not revision
                or (expected_revision is not None and not isinstance(expected_revision, str))
                or not _valid_expected_revision(revision, expected_revision)
            ):
                return False
            binding = MemoryAliasBinding(alias, memory_id, revision, expected_revision)
            existing = self.bindings.get(alias) or parsed_by_alias.get(alias)
            if existing is not None and existing != binding:
                return False
            revision_key = (memory_id, revision)
            existing_alias = self.aliases_by_revision.get(revision_key) or parsed_by_revision.get(
                revision_key
            )
            if existing_alias is not None and existing_alias != alias:
                return False
            parsed_by_alias[alias] = binding
            parsed_by_revision[revision_key] = alias
            parsed.append(binding)
        invalidated = {
            alias
            for alias in raw_invalidated
            if isinstance(alias, str) and _ALIAS_RE.fullmatch(alias)
        }
        if len(invalidated) != len(raw_invalidated):
            return False

        self.invalidated.update(invalidated)
        for binding in parsed:
            self.bindings[binding.alias] = binding
            self.aliases_by_revision[(binding.memory_id, binding.revision)] = binding.alias
            previous = self.active_by_memory_id.get(binding.memory_id)
            if previous and previous != binding.alias:
                self.invalidated.add(previous)
            self.active_by_memory_id[binding.memory_id] = binding.alias
            self.invalidated.discard(binding.alias)
            self.next_number = max(
                self.next_number,
                int(_ALIAS_RE.fullmatch(binding.alias).group(1)) + 1,  # type: ignore[union-attr]
            )
        return True

    def snapshot(self) -> dict[str, Any]:
        """Return an acceleration-only snapshot for Redis."""

        return {
            "version": 1,
            "bindings": [
                binding.as_dict()
                for binding in sorted(
                    self.bindings.values(), key=lambda item: _alias_sort_key(item.alias)
                )
            ],
            "invalidated": sorted(self.invalidated, key=_alias_sort_key),
        }


def memory_revision_identity(record: dict[str, Any]) -> str:
    """Return a native revision identity, or a deterministic snapshot fallback."""

    return memory_revision(record)[0]


def memory_revision(record: dict[str, Any]) -> tuple[str, str | None]:
    """Return the stable identity and optional native mutation precondition."""

    native = _native_revision(record)
    if native is not None:
        source, value = native
        return f"native:{source}:{value}", value
    normalized = _normalize_revision_value(record)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"snapshot-sha256:{hashlib.sha256(encoded.encode()).hexdigest()}", None


def _native_revision(record: dict[str, Any]) -> tuple[str, str] | None:
    candidates: list[tuple[str, Any]] = [(key, record.get(key)) for key in _NATIVE_REVISION_KEYS]
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend((f"metadata.{key}", metadata.get(key)) for key in _NATIVE_REVISION_KEYS)
        provenance = metadata.get("provenance")
        if isinstance(provenance, dict):
            candidates.extend(
                (f"metadata.provenance.{key}", provenance.get(key)) for key in _NATIVE_REVISION_KEYS
            )
    for source, value in candidates:
        if isinstance(value, str | int):
            token = str(value).strip()
            if token and len(token) <= 512 and all(33 <= ord(char) <= 126 for char in token):
                return source, token
    return None


def _valid_expected_revision(revision: str, expected_revision: str | None) -> bool:
    if expected_revision is None:
        return True
    parts = revision.split(":", 2)
    if len(parts) != 3 or parts[0] != "native" or not parts[1]:
        return False
    if (
        not expected_revision
        or len(expected_revision) > 512
        or not all(33 <= ord(char) <= 126 for char in expected_revision)
    ):
        return False
    return parts[2] == expected_revision


def _normalize_revision_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_revision_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_REVISION_KEYS
        }
    if isinstance(value, list):
        return [_normalize_revision_value(item) for item in value]
    if isinstance(value, float):
        return format(value, ".17g")
    return value


def _alias_sort_key(alias: str) -> int:
    match = _ALIAS_RE.fullmatch(alias)
    return int(match.group(1)) if match else 0
