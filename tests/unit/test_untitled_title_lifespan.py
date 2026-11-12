"""The real application lifespan owns background title recovery."""

from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient
from sqlalchemy import text

from cognis.api.app import create_app
from cognis.core.invariants import UntitledConversationReconciler


def test_startup_schedules_recovery_without_a_turn_and_shutdown_joins_it(monkeypatch, tmp_path):
    started = threading.Event()
    joined = threading.Event()
    shutdown_errors = []

    class ObservedReconciler(UntitledConversationReconciler):
        async def reconcile(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                try:
                    assert not self._guardrails.client.is_closed
                    async with self._session_factory() as db:
                        assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
                except Exception as exc:
                    shutdown_errors.append(exc)
                joined.set()

    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/lifespan.db")
    monkeypatch.setattr("cognis.core.invariants.UntitledConversationReconciler", ObservedReconciler)
    with TestClient(create_app()):
        assert started.wait(5)
        assert not joined.is_set()
    assert joined.is_set()
    assert not shutdown_errors
