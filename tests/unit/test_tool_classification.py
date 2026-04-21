from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa

from cognis.bootstrap import run_schema_bootstrap
from cognis.core.tool_classification_queue import ToolClassificationQueue
from cognis.models.tool import ToolDefinition, ToolSource, stable_tool_id
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import ToolClassificationRow
from cognis.store.queries import (
    upsert_tool_classification,
    upsert_tool_classification_override,
)
from cognis.tools.classification import (
    classify_tool_definitions,
    resolve_tool_classifications,
    tool_fingerprint,
)


def _dynamic_tool() -> ToolDefinition:
    return ToolDefinition(
        name="mcp_github__search_issues",
        description="Search GitHub issues",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
        read_only=True,
    )


def _dynamic_tool_named(name: str, raw_name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool {name}",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name=raw_name),
        category="mcp",
        read_only=True,
    )


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    async def enqueue_tools(self, tools: list[ToolDefinition], *, owner_email: str | None) -> None:
        self.calls.append(([stable_tool_id(tool) for tool in tools], owner_email))


class _FakeLLM:
    async def generate(self, *_args, **_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"tools":[{"tool_id":"mcp:github:search/issues","category":"web","capabilities":["read"],"confidence":0.92}]}'
                    }
                }
            ]
        }


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, **_kwargs):
        self.calls += 1
        tool_payload = json.loads(messages[1]["content"])
        tools = [
            {
                "tool_id": tool["tool_id"],
                "profile_group": "web",
                "capabilities": ["read"],
                "confidence": 0.9,
            }
            for tool in tool_payload["tools"]
        ]
        return {"choices": [{"message": {"content": json.dumps({"tools": tools})}}]}


@pytest.mark.asyncio
async def test_resolve_tool_classifications_marks_dynamic_tools_pending_and_enqueues(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/classifications.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    queue = _RecordingQueue()

    resolved = await resolve_tool_classifications(
        [_dynamic_tool()],
        session_factory=session_factory,
        owner_email=None,
        queue=queue,
    )

    assert resolved[0].classification_status == "pending"
    assert resolved[0].classification_source == "heuristic"
    assert resolved[0].profile_group == "development"
    assert queue.calls == [(["mcp:github:search/issues"], None)]

    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_tool_classifications_overlays_ready_persisted_state(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/classifications.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    tool = _dynamic_tool()
    async with session_factory() as session:
        await upsert_tool_classification(
            session,
            scope_key="__global__",
            owner_email=None,
            tool_id=stable_tool_id(tool),
            source_type=tool.source.type,
            fingerprint=tool_fingerprint(tool),
            tool_payload=tool.model_dump(mode="json"),
            status="ready",
            category="web",
            capabilities=["read"],
            classification_source="llm",
            classification_confidence=0.93,
        )
        await session.commit()

    resolved = await resolve_tool_classifications(
        [tool],
        session_factory=session_factory,
        owner_email=None,
        queue=None,
    )

    assert resolved[0].classification_status == "ready"
    assert resolved[0].profile_group == "web"
    assert resolved[0].classification_source == "llm"

    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_tool_classifications_applies_manual_override_precedence(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/classifications.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    tool = _dynamic_tool()
    async with session_factory() as session:
        await upsert_tool_classification(
            session,
            scope_key="__global__",
            owner_email=None,
            tool_id=stable_tool_id(tool),
            source_type=tool.source.type,
            fingerprint=tool_fingerprint(tool),
            tool_payload=tool.model_dump(mode="json"),
            status="ready",
            category="web",
            capabilities=["read"],
            classification_source="llm",
            classification_confidence=0.93,
        )
        await upsert_tool_classification_override(
            session,
            scope_key="__global__",
            owner_email=None,
            tool_id=stable_tool_id(tool),
            profile_group="development",
            capabilities=["read", "privileged"],
        )
        await session.commit()

    resolved = await resolve_tool_classifications(
        [tool],
        session_factory=session_factory,
        owner_email=None,
        queue=None,
    )

    assert resolved[0].classification_source == "override"
    assert resolved[0].profile_group == "development"
    assert resolved[0].capabilities == ["read", "privileged"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_classification_queue_promotes_pending_rows_to_ready(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/classifications.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    queue = ToolClassificationQueue(session_factory=session_factory, llm_provider=_FakeLLM())
    tool = _dynamic_tool()

    await queue.start()
    await queue.enqueue_tools([tool], owner_email=None)

    for _ in range(30):
        async with session_factory() as session:
            result = await session.execute(
                sa.select(ToolClassificationRow).where(
                    ToolClassificationRow.scope_key == "__global__",
                    ToolClassificationRow.tool_id == stable_tool_id(tool),
                )
            )
            row = result.scalar_one_or_none()
            if row is not None and row.status == "ready":
                assert row.category == "web"
                assert row.capabilities == ["read"]
                assert row.classification_source == "llm"
                break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("tool classification row was not promoted to ready")

    await queue.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_classification_queue_batches_same_server_tools(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/classifications.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    llm = _CountingLLM()
    queue = ToolClassificationQueue(session_factory=session_factory, llm_provider=llm)
    tool_a = _dynamic_tool_named("mcp_github__search_issues", "search/issues")
    tool_b = _dynamic_tool_named("mcp_github__list_prs", "list/prs")

    await queue.start()
    await queue.enqueue_tools([tool_a, tool_b], owner_email=None)

    for _ in range(30):
        async with session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(ToolClassificationRow).where(ToolClassificationRow.scope_key == "__global__")
                )
            ).scalars().all()
            if len(rows) == 2 and all(row.status == "ready" for row in rows):
                break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("tool classification rows were not promoted to ready")

    assert llm.calls == 1

    await queue.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_classification_queue_does_not_reset_running_rows_on_reenqueue(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/classifications.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    queue = ToolClassificationQueue(session_factory=session_factory, llm_provider=_FakeLLM())
    tool = _dynamic_tool()
    async with session_factory() as session:
        await upsert_tool_classification(
            session,
            scope_key="__global__",
            owner_email=None,
            tool_id=stable_tool_id(tool),
            source_type=tool.source.type,
            fingerprint=tool_fingerprint(tool),
            tool_payload=tool.model_dump(mode="json"),
            status="running",
            attempts=3,
        )
        await session.commit()

    await queue.enqueue_tools([tool], owner_email=None)

    async with session_factory() as session:
        result = await session.execute(
            sa.select(ToolClassificationRow).where(
                ToolClassificationRow.scope_key == "__global__",
                ToolClassificationRow.tool_id == stable_tool_id(tool),
            )
        )
        row = result.scalar_one()
        assert row.status == "running"
        assert row.attempts == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_llm_group_is_rejected_back_to_heuristic() -> None:
    tool = ToolDefinition(
        name="mcp_unknown__sequentialthinking",
        description="Structured reasoning helper",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            type="intaris_mcp", server_name="unknown", raw_tool_name="sequentialthinking"
        ),
        category="mcp",
        read_only=True,
    )

    class _BadLLM:
        async def generate(self, *_args, **_kwargs):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"tools":[{"tool_id":"mcp:unknown:sequentialthinking","profile_group":"mcp","capabilities":["read"],"confidence":0.95}]}'
                        }
                    }
                ]
            }

    classified = await classify_tool_definitions([tool], llm=_BadLLM())

    assert classified[0].classification_source == "heuristic"
    assert classified[0].classification_status == "ready"
    assert classified[0].profile_group == "development"
