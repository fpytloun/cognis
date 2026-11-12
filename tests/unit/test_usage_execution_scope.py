"""Trusted accounting scope and its provider capture boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cognis.providers.llm.usage_attribution import controller_request
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.llm.usage_scope import UsageExecutionBinding, current_usage_execution


def capture(**kwargs):
    request = controller_request(
        UsageCapture(
            sink=SimpleNamespace(try_emit=lambda _: True),
            controller_owner_id="controller_usage",
            coverage_epoch_id="epoch_usage",
        ),
        actor=kwargs.pop("acting_user_email", None),
        purpose="chat",
        model=None,
        kwargs=kwargs,
    )
    return request.attribution.resources or SimpleNamespace(task_id=None)


@pytest.mark.asyncio
async def test_accounting_scope_isolates_concurrent_executions_and_actor():
    async def run(task):
        token = current_usage_execution.set(
            UsageExecutionBinding(
                actor="actor@usage.test",
                session_id=f"session_{task}",
                conversation_id="conv_usage",
                agent_id="agent_usage",
                task_id=task,
                step_run_id=f"step_{task}",
            )
        )
        try:
            await asyncio.sleep(0)
            assert capture(acting_user_email="actor@usage.test").task_id == task
            assert capture(acting_user_email="other@usage.test").task_id is None
            assert (
                capture(
                    acting_user_email="actor@usage.test",
                    cognis_conversation_id="conv_other",
                ).task_id
                is None
            )
            assert (
                capture(
                    acting_user_email="actor@usage.test",
                    cognis_session_id="session_rotated",
                ).session_id
                == "session_rotated"
            )
        finally:
            current_usage_execution.reset(token)

    await asyncio.gather(run("task_first"), run("task_second"))
    assert current_usage_execution.get() is None
