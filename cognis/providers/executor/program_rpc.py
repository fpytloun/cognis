"""Execution-scoped identity helpers for reverse program RPC registrations."""

from __future__ import annotations

from typing import Any

from cognis.models.program import ProgramRuntimeBinding

ProgramRPCKey = tuple[str, str, str, str, str]


def program_rpc_key(binding: ProgramRuntimeBinding) -> ProgramRPCKey:
    return (
        binding.executor_id,
        binding.executor_instance_id,
        binding.scope_id,
        binding.execution_id,
        binding.program_id,
    )


def program_rpc_key_from_params(params: dict[str, Any]) -> ProgramRPCKey | None:
    values = (
        params.get("executor_id"),
        params.get("executor_instance_id"),
        params.get("scope_id"),
        params.get("execution_id"),
        params.get("program_id"),
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    return tuple(values)  # type: ignore[return-value]
