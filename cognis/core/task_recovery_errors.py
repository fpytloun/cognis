"""Nonterminal task recovery boundaries, distinct from step failures."""

from __future__ import annotations


class TaskRecoveryBlocked(RuntimeError):
    """Recovery requires explicit reconciliation; its durable source is retained."""
