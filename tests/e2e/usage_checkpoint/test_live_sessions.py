"""Focused live-auth fixture qualification without a UI build or browser."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.e2e.conftest import E2EStack
from tests.e2e.usage_checkpoint.expansion import CONVERSATION, seed_expansion
from tests.e2e.usage_checkpoint.seed import seed

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("COGNIS_USAGE_CHECKPOINT") != "1",
        reason="Explicit isolated usage checkpoint opt-in is required",
    ),
]


def test_expansion_sessions_support_real_chat_reads_without_inference(e2e_stack: E2EStack) -> None:
    """Read actual Chat v2 snapshot/work after genuine Intaris graph registration."""
    root = Path(os.environ["COGNIS_USAGE_RUN_DIR"]).resolve()
    assert e2e_stack.log_dir is not None
    database = (e2e_stack.log_dir.parent / "cognis/cognis.db").resolve()
    assert database.is_relative_to(root)
    (root / "runtime.json").write_text(
        json.dumps(
            {
                "database": str(database),
                "actor": e2e_stack.admin_email,
                "fixture_password": e2e_stack.admin_password,
                "agent": e2e_stack.e2e_agent_id,
                "url": e2e_stack.cognis_url,
                "intaris_url": e2e_stack.intaris_upstream_url,
            }
        )
    )
    history = f"{e2e_stack.mock_llm_url}/__mock/history?limit=100"
    before = e2e_stack.http.get(history).json()
    asyncio.run(seed())
    asyncio.run(seed_expansion())
    for resource in ("snapshot", "work"):
        response = e2e_stack.get(f"/api/v1/chat/v2/conversations/{CONVERSATION}/{resource}")
        assert response.status_code == 200, (resource, response.status_code)
    assert e2e_stack.http.get(history).json() == before
