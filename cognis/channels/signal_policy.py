"""Destination-scoped local Signal policy, independent of provider block scope."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

from cognis.channels.protocol import NonRetryableChannelError
from cognis.channels.signal_failures import SignalDeliveryFailure
from cognis.store.models import ChannelAccountRow, SignalDestinationPolicyRow

TRANSPORT_TIMEOUT_SECONDS = 100.0


class SignalPolicyBlocked(NonRetryableChannelError):
    """Admission was rejected before this operation called the transport."""

    def __init__(self, diagnostics: dict[str, Any]) -> None:
        self.diagnostics = diagnostics
        super().__init__(f"Signal send not attempted: {diagnostics['state']}")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def project_policy(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    admission = state.get("admission")
    if admission:
        blocked = (
            "transport_outcome_unresolved"
            if admission.get("unresolved") or _instant(admission["deadline"]) <= now
            else "transport_in_flight"
        )
    elif state.get("manual_action_required"):
        blocked = "manual_action_required"
    elif state.get("cooldown_until") and _instant(state["cooldown_until"]) > now:
        blocked = "cooldown"
    else:
        blocked = "available"
    return {
        "state": blocked,
        "platform_scope": "unknown",
        "enforcement_scope": "destination",
        "cooldown_until": state.get("cooldown_until"),
        "last_observed_at": state.get("last_observed_at"),
        "last_failure": state.get("last_failure"),
        "retry_scheduled": False,
        "next_step": (
            "Operator transport reconciliation is required. No clear API exists for this fence."
            if admission
            else "Wait until the deadline or explicitly reconcile and clear local policy. "
            "A clear never resends a message or proves Signal permission to send."
            if blocked != "available"
            else "New explicit sends are locally permitted; previous uncertain sends remain uncertain."
        ),
    }


class SignalDestinationPolicy:
    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def _locked(self, session: Any, owner: str, account_id: str, destination: str) -> Any:
        # An UPDATE also obtains the SQLite writer lock. PostgreSQL serializes
        # against route admission on the same account row. Never hold over RPC.
        account = await session.scalar(
            update(ChannelAccountRow)
            .where(
                ChannelAccountRow.account_id == account_id,
                ChannelAccountRow.user_email == owner,
                ChannelAccountRow.channel_type == "signal",
            )
            .values(account_id=account_id)
            .returning(ChannelAccountRow.account_id)
        )
        if account is None:
            raise ValueError("Signal account not found")
        row = await session.get(SignalDestinationPolicyRow, (account_id, destination))
        if row is None:
            row = SignalDestinationPolicyRow(
                account_id=account_id, destination=destination, user_email=owner, state_json={}
            )
            session.add(row)
        return row

    async def inspect(self, owner: str, account_id: str, destination: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            account = await session.get(ChannelAccountRow, account_id)
            if account is None or account.user_email != owner or account.channel_type != "signal":
                raise ValueError("Signal account not found")
            row = await session.get(SignalDestinationPolicyRow, (account_id, destination))
            return project_policy(row.state_json if row else {}, datetime.now(UTC))

    async def clear(
        self, owner: str, account_id: str, destination: str, *, evidence: str, actor: str
    ) -> dict[str, Any]:
        if not evidence.strip() or len(evidence) > 500:
            raise ValueError("Reconciliation evidence must contain 1–500 characters")
        async with self._session_factory() as session:
            row = await self._locked(session, owner, account_id, destination)
            state = dict(row.state_json)
            if state.get("admission"):
                raise ValueError("Transport admission cannot be cleared by policy reconciliation")
            now = datetime.now(UTC)
            state["reconciliations"] = [
                *state.get("reconciliations", []),
                {
                    "at": now.isoformat(),
                    "actor": actor,
                    "evidence": evidence,
                    "prior_cooldown_until": state.get("cooldown_until"),
                    "prior_manual_action_required": state.get("manual_action_required", False),
                },
            ]
            state["manual_action_required"] = False
            state["cooldown_until"] = None
            row.state_json = state
            await session.commit()
            return project_policy(state, now)

    async def send(self, owner: str, account_id: str, destination: str, operation: Any) -> Any:
        token = uuid.uuid4().hex
        async with self._session_factory() as session:
            row = await self._locked(session, owner, account_id, destination)
            state = dict(row.state_json)
            now = datetime.now(UTC)
            diagnostics = project_policy(state, now)
            if diagnostics["state"] != "available":
                raise SignalPolicyBlocked(diagnostics)
            state["admission"] = {
                "token": token,
                "deadline": (now + timedelta(seconds=TRANSPORT_TIMEOUT_SECONDS)).isoformat(),
            }
            row.state_json = state
            await session.commit()
        # A cancelled commit can leave admission durable without a send. Fail
        # closed: absence of a positive result is never evidence of no effect.
        failure: dict[str, Any] | None = None
        completed = False
        try:
            async with asyncio.timeout(TRANSPORT_TIMEOUT_SECONDS):
                result = await operation()
            if not isinstance(result, str) or not result:
                raise RuntimeError("Signal transport returned no trustworthy send receipt")
            completed = True
            return result
        except SignalDeliveryFailure as exc:
            failure = exc.safe_metadata()
            completed = True  # Complete provider response, NOT definite non-delivery.
            raise
        finally:
            async with self._session_factory() as session:
                row = await self._locked(session, owner, account_id, destination)
                state = dict(row.state_json)
                if state.get("admission", {}).get("token") != token:
                    raise RuntimeError("Signal transport settlement lost admission ownership")
                if completed:
                    state.pop("admission")
                else:
                    state["admission"] = {**state["admission"], "unresolved": True}
                if failure:
                    now = datetime.now(UTC)
                    state["last_observed_at"] = now.isoformat()
                    state["last_failure"] = failure
                    if failure["classification"] in {"rate_limit", "challenge"}:
                        delay = failure["retry_after_seconds"]
                        try:
                            deadline = now + timedelta(seconds=delay) if delay is not None else None
                        except OverflowError:
                            deadline = None
                        if deadline is None:
                            state["manual_action_required"] = True
                        else:
                            previous = state.get("cooldown_until")
                            state["cooldown_until"] = max(
                                deadline, _instant(previous) if previous else deadline
                            ).isoformat()
                row.state_json = state
                await session.commit()
