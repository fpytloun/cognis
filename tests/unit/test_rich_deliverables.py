from __future__ import annotations

import importlib
from copy import deepcopy
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from cognis.api.serializers import deliverable_to_response
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.models.deliverable import (
    RICH_DELIVERABLE_MAX_BLOCKS,
    RICH_DELIVERABLE_MAX_BYTES,
    RICH_DELIVERABLE_MAX_DATASET_ROWS,
    RICH_DELIVERABLE_MAX_STRING_LENGTH,
    RichPayloadValidationError,
    normalize_rich_payload,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.models import Agent, Base, Conversation, Session, User
from cognis.store.queries import (
    create_deliverable,
    create_step_run,
    create_task,
    get_accessible_conversation_deliverable,
    list_deliverables_for_conversation_scope,
)


def _canonical_chart(**overrides: Any) -> dict[str, Any]:
    chart: dict[str, Any] = {
        "type": "chart",
        "title": "Traffic range",
        "description": "Observed request range.",
        "spec_version": "cognis.chart.v1",
        "chart_type": "range",
        "series": [
            {
                "id": "requests",
                "label": "Requests",
                "stack": "traffic",
                "points": [
                    {
                        "x": "2026-07-15T09:00:00Z",
                        "y": [10, 20],
                        "label": "Observed",
                    }
                ],
            }
        ],
        "x_axis": {"type": "time", "label": "Time", "unit": "UTC"},
        "y_axis": {"type": "linear", "label": "Requests", "unit": "req/s", "min": 0},
        "stack": False,
        "legend_position": "bottom",
        "palette_token": "cool",
        "source_ids": ["metrics"],
        "source": "Metrics",
        "source_url": "https://metrics.example.test/requests",
        "observed_at": "2026-07-15T09:05:00Z",
    }
    chart.update(overrides)
    return chart


def _set_nested(value: dict[str, Any], path: tuple[str | int, ...], replacement: Any) -> None:
    target: Any = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement


def _artifact_store(tmp_path) -> ArtifactStore:
    return ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts"),
            base_url="http://testserver",
            signing_secret="test-secret",
        )
    )


def test_rich_payload_normalization_rejects_unknown_blocks() -> None:
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(
            {"blocks": [{"type": "markdown", "content": "# Hi"}, "bad"], "sources": "bad"}
        )

    err = exc_info.value.to_tool_result()
    assert err["path"] == "$.blocks[1]"
    assert err["expected"]
    assert "valid_example" in err


def test_rich_payload_normalization_enforces_block_count_cap() -> None:
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(
            {
                "blocks": [
                    {"type": "markdown", "content": f"block {index}"}
                    for index in range(RICH_DELIVERABLE_MAX_BLOCKS + 1)
                ]
            }
        )

    assert exc_info.value.reason == "rich_block_count_exceeded"


def test_rich_payload_normalization_enforces_dataset_row_cap() -> None:
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(
            {
                "blocks": [{"type": "markdown", "content": "table"}],
                "datasets": [
                    {
                        "id": "rows",
                        "rows": [
                            {"value": index}
                            for index in range(RICH_DELIVERABLE_MAX_DATASET_ROWS + 1)
                        ],
                    }
                ],
            }
        )

    assert exc_info.value.reason == "rich_dataset_rows_exceeded"


def test_rich_payload_normalization_enforces_per_string_cap() -> None:
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(
            {
                "blocks": [
                    {
                        "type": "markdown",
                        "content": "x" * (RICH_DELIVERABLE_MAX_STRING_LENGTH + 1),
                    }
                ]
            }
        )

    assert exc_info.value.reason == "rich_string_too_long"


@pytest.mark.parametrize("field", ["source", "code", "content"])
def test_rich_payload_normalization_accepts_mermaid_source_aliases(field: str) -> None:
    payload, warnings = normalize_rich_payload(
        {"blocks": [{"type": "mermaid", field: "flowchart LR\n  A --> B"}]}
    )

    assert warnings == []
    assert payload is not None
    assert payload["blocks"][0]["source"] == "flowchart LR\n  A --> B"
    assert "code" not in payload["blocks"][0]


@pytest.mark.parametrize("content", [None, "", "   ", 1])
def test_rich_payload_normalization_rejects_invalid_markdown_content(content: Any) -> None:
    block = {"type": "markdown"}
    if content is not None:
        block["content"] = content

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload({"blocks": [block]})

    assert exc_info.value.reason == "missing_markdown_content"
    assert exc_info.value.path == "$.blocks[0].content"


def test_rich_payload_normalization_rejects_empty_mermaid_source() -> None:
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload({"blocks": [{"type": "mermaid", "title": "Empty"}]})

    assert exc_info.value.reason == "missing_mermaid_source"
    assert exc_info.value.path == "$.blocks[0].source"


@pytest.mark.parametrize(
    ("block", "expected_path"),
    [
        ({"type": "mermaid", "source": "", "code": "flowchart LR; A-->B"}, "$.blocks[0].source"),
        (
            {"type": "mermaid", "code": "   ", "content": "flowchart LR; A-->B"},
            "$.blocks[0].code",
        ),
    ],
)
def test_rich_payload_normalization_rejects_invalid_present_mermaid_aliases(
    block: dict[str, Any],
    expected_path: str,
) -> None:
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload({"blocks": [block]})

    assert exc_info.value.reason == "invalid_mermaid_source"
    assert exc_info.value.path == expected_path


def test_rich_payload_normalization_accepts_structured_source_list_reference() -> None:
    payload, warnings = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": "source_list",
                    "sources": [{"source_id": " sweet ", "label": "Sweet blindness"}],
                }
            ],
            "sources": [
                {
                    "id": "sweet",
                    "title": "Sweet-receptor research",
                    "url": "https://example.test/sweet",
                }
            ],
        }
    )

    assert warnings == []
    assert payload is not None


def test_rich_payload_normalization_rejects_unknown_source_list_reference() -> None:
    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload(
            {
                "blocks": [
                    {
                        "type": "source_list",
                        "sources": [{"source_id": "missing", "label": "Missing"}],
                    }
                ],
                "sources": [{"id": "known", "title": "Known"}],
            }
        )

    assert exc_info.value.reason == "invalid_rich_source_reference"
    assert exc_info.value.path == "$.blocks[0].sources[0]"


def test_rich_payload_normalization_accepts_complete_canonical_range_chart() -> None:
    chart = _canonical_chart()

    payload, warnings = normalize_rich_payload({"blocks": [chart]})

    assert warnings == []
    assert payload is not None
    assert payload["blocks"][0] == chart


@pytest.mark.parametrize("legacy_field", ["data", "x_key", "y_key", "series_key"])
def test_rich_payload_normalization_rejects_every_legacy_chart_field(
    legacy_field: str,
) -> None:
    chart = _canonical_chart()
    chart[legacy_field] = [] if legacy_field == "data" else "legacy"

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload({"blocks": [chart]})

    issue = exc_info.value.issues[0]
    assert issue["reason"] == "legacy_chart_field"
    assert issue["path"] == f"$.blocks[0].{legacy_field}"
    assert "migrate to cognis.chart.v1" in issue["expected"]
    assert "series[].points[].x/y" in issue["expected"]


@pytest.mark.parametrize(
    ("field_path", "value", "expected_path", "expected"),
    [
        (("spec_version",), "v0", "$.blocks[0].spec_version", "'cognis.chart.v1'"),
        (("chart_type",), "pie", "$.blocks[0].chart_type", "one of"),
        (("chart_type",), [], "$.blocks[0].chart_type", "one of"),
        (("x_axis", "type"), "log", "$.blocks[0].x_axis.type", "one of"),
        (("y_axis", "min"), True, "$.blocks[0].y_axis.min", "finite number"),
        (("series", 0, "stack"), 1, "$.blocks[0].series[0].stack", "string"),
        (
            ("series", 0, "points", 0, "x"),
            False,
            "$.blocks[0].series[0].points[0].x",
            "ISO 8601",
        ),
        (
            ("series", 0, "points", 0, "y"),
            15,
            "$.blocks[0].series[0].points[0].y",
            "two-item array",
        ),
        (
            ("series", 0, "points", 0, "y", 0),
            10**400,
            "$.blocks[0].series[0].points[0].y",
            "two-item array",
        ),
        (("stack",), "yes", "$.blocks[0].stack", "boolean"),
        (("legend_position",), "left", "$.blocks[0].legend_position", "one of"),
        (("palette_token",), "neon", "$.blocks[0].palette_token", "one of"),
        (("source_ids",), "metrics", "$.blocks[0].source_ids", "array"),
        (("source",), 1, "$.blocks[0].source", "string"),
        (("title",), None, "$.blocks[0].title", "string or omitted"),
        (("unexpected",), "value", "$.blocks[0].unexpected", "remove unknown chart fields"),
    ],
)
def test_canonical_chart_validation_reports_precise_path_and_expected_type(
    field_path: tuple[str | int, ...],
    value: Any,
    expected_path: str,
    expected: str,
) -> None:
    chart = deepcopy(_canonical_chart())
    _set_nested(chart, field_path, value)

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload({"blocks": [chart]})

    issue = exc_info.value.issues[0]
    assert issue["path"] == expected_path
    assert expected in issue["expected"]


def test_non_range_chart_rejects_range_y_with_precise_point_path() -> None:
    chart = _canonical_chart(chart_type="line")

    with pytest.raises(RichPayloadValidationError) as exc_info:
        normalize_rich_payload({"blocks": [chart]})

    issue = exc_info.value.issues[0]
    assert issue["path"] == "$.blocks[0].series[0].points[0].y"
    assert issue["expected"] == (
        "finite number; two-item y ranges are only valid for chart_type='range'"
    )


def test_day_agenda_normalization_filters_aliases_and_malformed_entries() -> None:
    payload, warnings = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": "day_agenda",
                    "timezone": "Europe/Prague",
                    "now": "2026-10-25T02:15:00+02:00",
                    "now_iso": "2026-10-25T09:00:00+01:00",
                    "events": [
                        None,
                        "invalid",
                        {"title": "All day", "allDay": True},
                        {"label": "Valid", "start_time": "2026-10-25T03:00:00+01:00"},
                        {"title": "Invalid", "start": "09:00"},
                    ],
                    "tasks": [None, 1, {"content": "Task alias"}],
                }
            ]
        }
    )
    assert warnings == []
    agenda = payload["blocks"][0]
    assert agenda["timezone"] == "Europe/Prague"
    assert agenda["now"] == "2026-10-25T02:15:00+02:00"
    assert "now_iso" not in agenda
    assert "events" not in agenda
    assert agenda["items"] == [
        {"title": "All day", "all_day": True, "kind": "event"},
        {
            "title": "Valid",
            "all_day": False,
            "start": "2026-10-25T03:00:00+01:00",
            "kind": "event",
        },
    ]
    assert agenda["tasks"] == [{"title": "Task alias"}]


def test_day_agenda_normalization_requires_full_offset_iso_timestamps() -> None:
    payload, _ = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": "day_agenda",
                    "date": "2026-03-29",
                    "timezone": "Europe/Prague",
                    "now_iso": "2026-03-29T01:45:00+01:00",
                    "items": [
                        {"title": "Valid instant", "start": "2026-03-29T01:30:00+01:00"},
                        {"title": "Naive rejected", "start": "2026-03-29T01:30:00"},
                        {"title": "Space rejected", "start": "2026-03-29 03:30:00+02:00"},
                        {"title": "Date rejected", "start": "2026-03-29"},
                        {"title": "All day survives", "all_day": True, "start": None},
                    ],
                    "tasks": [
                        {"title": "Invalid due survives", "due": "not-a-time"},
                        {"title": "Valid due", "due_at": "2026-03-29T03:30:00+02:00"},
                    ],
                }
            ]
        }
    )
    agenda = payload["blocks"][0]
    assert agenda["date"] == "2026-03-29"
    assert agenda["now"] == "2026-03-29T01:45:00+01:00"
    assert [item["title"] for item in agenda["items"]] == ["Valid instant", "All day survives"]
    assert agenda["items"][0]["start"] == "2026-03-29T01:30:00+01:00"
    assert agenda["tasks"] == [
        {"title": "Invalid due survives"},
        {"title": "Valid due", "due": "2026-03-29T03:30:00+02:00"},
    ]


@pytest.mark.parametrize("canonical_items", [None, False, "", {}])
def test_day_agenda_canonical_items_presence_suppresses_events_alias(canonical_items) -> None:
    payload, _ = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": "day_agenda",
                    "items": canonical_items,
                    "events": [{"title": "Must not leak", "all_day": True}],
                }
            ]
        }
    )
    assert payload["blocks"][0]["items"] == []


def test_day_agenda_canonical_invalid_fields_suppress_valid_aliases() -> None:
    payload, _ = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": "day_agenda",
                    "now": None,
                    "now_iso": "2026-07-12T09:00:00Z",
                    "items": [
                        {"title": None, "label": "Hidden", "all_day": True},
                        {
                            "title": "Timed",
                            "all_day": False,
                            "allDay": True,
                            "start": "",
                            "start_iso": "2026-07-12T10:00:00Z",
                        },
                    ],
                    "tasks": [{"title": "", "content": "Hidden task"}],
                }
            ]
        }
    )
    agenda = payload["blocks"][0]
    assert agenda["now"] is None
    assert agenda["items"] == []
    assert agenda["tasks"] == []


@pytest.mark.parametrize(
    "timezone",
    ["/usr/share/zoneinfo/UTC", "../UTC", "Europe\x00/Prague", r"Europe\Prague", "Unknown/Zone"],
)
def test_day_agenda_rejects_unsafe_or_unknown_timezone_keys(timezone: str) -> None:
    payload, _ = normalize_rich_payload(
        {"blocks": [{"type": "day_agenda", "timezone": timezone, "items": []}]}
    )
    assert payload["blocks"][0]["timezone"] is None


def test_rich_deliverables_migration_adds_conversation_scope_unique_constraint(
    tmp_path,
) -> None:
    migration = importlib.import_module("cognis.store.migrations.versions.073_rich_deliverables_v1")
    engine = sa.create_engine(f"sqlite:///{tmp_path}/migration.db")
    metadata = sa.MetaData()
    sa.Table("step_runs", metadata, sa.Column("step_run_id", sa.String, primary_key=True))
    sa.Table(
        "conversations",
        metadata,
        sa.Column("conversation_id", sa.String, primary_key=True),
    )
    sa.Table("sessions", metadata, sa.Column("session_id", sa.String, primary_key=True))
    sa.Table(
        "deliverables",
        metadata,
        sa.Column("deliverable_id", sa.String, primary_key=True),
        sa.Column(
            "step_run_id",
            sa.String,
            sa.ForeignKey("step_runs.step_run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False, default=1),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("format", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.UniqueConstraint("step_run_id", "version", name="uq_deliverables_step_run_version"),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        original_op = migration.op
        try:
            migration.op = Operations(context)
            migration.upgrade()
        finally:
            migration.op = original_op

        inspector = sa.inspect(connection)
        columns = {column["name"]: column for column in inspector.get_columns("deliverables")}
        assert {
            "conversation_id",
            "session_id",
            "turn_id",
            "rich_payload",
            "validation_warnings",
            "render_metadata",
            "export_metadata",
        }.issubset(columns)
        assert columns["step_run_id"]["nullable"] is True
        assert "ix_deliverables_conversation_scope" in {
            index["name"] for index in inspector.get_indexes("deliverables")
        }
        assert "uq_deliverables_conversation_scope_version" in {
            constraint["name"] for constraint in inspector.get_unique_constraints("deliverables")
        }
        foreign_keys = inspector.get_foreign_keys("deliverables")
        assert any(
            key["constrained_columns"] == ["conversation_id"]
            and key["referred_table"] == "conversations"
            for key in foreign_keys
        )
        assert any(
            key["constrained_columns"] == ["session_id"] and key["referred_table"] == "sessions"
            for key in foreign_keys
        )

        connection.execute(
            sa.text(
                """
                INSERT INTO deliverables (
                    deliverable_id,
                    step_run_id,
                    conversation_id,
                    session_id,
                    turn_id,
                    version,
                    content,
                    format,
                    status
                )
                VALUES (
                    'dlv_direct',
                    NULL,
                    'conv',
                    'sess',
                    'turn',
                    1,
                    'Fallback',
                    'rich',
                    'buffered'
                )
                """
            )
        )

        migration.op = Operations(context)
        try:
            migration.downgrade()
        finally:
            migration.op = original_op
        assert "uq_deliverables_conversation_scope_version" not in {
            constraint["name"]
            for constraint in sa.inspect(connection).get_unique_constraints("deliverables")
        }


def test_rich_deliverables_repair_migration_handles_already_applied_old_073(
    tmp_path,
) -> None:
    migration = importlib.import_module(
        "cognis.store.migrations.versions.074_repair_rich_deliverables_step_run_nullable"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path}/repair-migration.db")
    metadata = sa.MetaData()
    sa.Table("step_runs", metadata, sa.Column("step_run_id", sa.String, primary_key=True))
    sa.Table(
        "conversations",
        metadata,
        sa.Column("conversation_id", sa.String, primary_key=True),
    )
    sa.Table("sessions", metadata, sa.Column("session_id", sa.String, primary_key=True))
    sa.Table(
        "deliverables",
        metadata,
        sa.Column("deliverable_id", sa.String, primary_key=True),
        sa.Column(
            "step_run_id",
            sa.String,
            sa.ForeignKey("step_runs.step_run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String, nullable=True),
        sa.Column("session_id", sa.String, nullable=True),
        sa.Column("turn_id", sa.String, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, default=1),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("format", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("rich_payload", sa.JSON, nullable=True),
        sa.Column("validation_warnings", sa.JSON, nullable=True),
        sa.Column("render_metadata", sa.JSON, nullable=True),
        sa.Column("export_metadata", sa.JSON, nullable=True),
        sa.UniqueConstraint("step_run_id", "version", name="uq_deliverables_step_run_version"),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        original_op = migration.op
        try:
            migration.op = Operations(context)
            migration.upgrade()
        finally:
            migration.op = original_op

        inspector = sa.inspect(connection)
        columns = {column["name"]: column for column in inspector.get_columns("deliverables")}
        assert columns["step_run_id"]["nullable"] is True
        assert "uq_deliverables_conversation_scope_version" in {
            constraint["name"] for constraint in inspector.get_unique_constraints("deliverables")
        }
        foreign_keys = inspector.get_foreign_keys("deliverables")
        assert any(
            key["constrained_columns"] == ["conversation_id"]
            and key["referred_table"] == "conversations"
            for key in foreign_keys
        )
        assert any(
            key["constrained_columns"] == ["session_id"] and key["referred_table"] == "sessions"
            for key in foreign_keys
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO deliverables (
                    deliverable_id,
                    step_run_id,
                    conversation_id,
                    session_id,
                    turn_id,
                    version,
                    content,
                    format,
                    status
                )
                VALUES (
                    'dlv_direct',
                    NULL,
                    'conv',
                    'sess',
                    'turn',
                    1,
                    'Fallback',
                    'rich',
                    'buffered'
                )
                """
            )
        )


def test_migration_075_moves_deliverables_to_object_store_metadata(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    migration_075 = importlib.import_module(
        "cognis.store.migrations.versions.075_deliverable_object_store_payloads"
    )

    engine = sa.create_engine(f"sqlite:///{tmp_path}/migration-075.db")
    metadata = sa.MetaData()
    sa.Table(
        "deliverables",
        metadata,
        sa.Column("deliverable_id", sa.String, primary_key=True),
        sa.Column("step_run_id", sa.String, nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("rich_payload", sa.JSON, nullable=True),
        sa.Column("format", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO deliverables "
                "(deliverable_id, content, rich_payload, format, status) "
                "VALUES ('dlv_old', 'payload', '{\"blocks\": []}', 'rich', 'buffered')"
            )
        )
        context = MigrationContext.configure(connection)
        op_obj = Operations(context)
        original_op = migration_075.op
        try:
            migration_075.op = op_obj
            migration_075.upgrade()
        finally:
            migration_075.op = original_op
        columns = {column["name"] for column in sa.inspect(connection).get_columns("deliverables")}
        count = connection.execute(sa.text("SELECT COUNT(*) FROM deliverables")).scalar_one()

    captured = capsys.readouterr()
    assert "deleting 1 existing deliverable rows" in captured.out
    assert count == 0
    assert "content" not in columns
    assert "rich_payload" not in columns
    assert {
        "storage_namespace",
        "storage_object_id",
        "content_key",
        "content_mime",
        "content_size",
        "content_hash",
        "rich_key",
        "rich_size",
        "rich_hash",
        "outputs_key",
        "outputs_mime",
        "outputs_size",
        "outputs_hash",
        "html_cache_key",
        "pdf_cache_key",
    }.issubset(columns)


def test_migration_076_repairs_legacy_content_constraint(tmp_path) -> None:
    migration = importlib.import_module(
        "cognis.store.migrations.versions.076_repair_legacy_deliverable_content_nullable"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path}/migration-076.db")
    metadata = sa.MetaData()
    sa.Table(
        "deliverables",
        metadata,
        sa.Column("deliverable_id", sa.String, primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        original_op = migration.op
        try:
            migration.op = Operations(context)
            migration.upgrade()
        finally:
            migration.op = original_op

        columns = {
            column["name"]: column for column in sa.inspect(connection).get_columns("deliverables")
        }
        connection.execute(
            sa.text("INSERT INTO deliverables (deliverable_id) VALUES ('dlv_metadata_only')")
        )

    assert columns["content"]["nullable"] is True


@pytest.mark.asyncio
async def test_direct_chat_rich_deliverable_scope_and_versions(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/rich.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = _artifact_store(tmp_path)

    async with factory() as session:
        session.add(User(email="owner@example.com", name="Owner", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent", owner_email="owner@example.com", name="Agent"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="owner@example.com",
                agent_id="agent",
                context_type="direct",
            )
        )
        session.add(
            Session(
                session_id="sess",
                conversation_id="conv",
                user_email="owner@example.com",
                agent_id="agent",
            )
        )
        await session.flush()
        first = await create_deliverable(
            session,
            conversation_id="conv",
            session_id="sess",
            turn_id="turn",
            content="Fallback",
            format="rich",
            rich={"blocks": [{"type": "card", "title": "Card", "content": "Body"}]},
            artifact_store=store,
        )
        second = await create_deliverable(
            session,
            conversation_id="conv",
            session_id="sess",
            turn_id="turn",
            content="Fallback 2",
            format="rich",
            rich={"blocks": [{"type": "markdown", "content": "Second"}]},
            artifact_store=store,
        )
        rows = await list_deliverables_for_conversation_scope(
            session, conversation_id="conv", session_id="sess", turn_id="turn"
        )
        await session.commit()

    await engine.dispose()

    assert first.version == 1
    assert second.version == 2
    assert first.status == "superseded"
    assert rows[0].deliverable_id == second.deliverable_id
    with pytest.raises(FileNotFoundError):
        await store.async_load("deliverables", first.deliverable_id, "content.md")
    await hydrate_deliverable_payload(rows[0], store)
    assert rows[0].rich_payload["blocks"][0]["type"] == "markdown"
    assert rows[0].render_metadata["schema"] == "cognis.rich_deliverable.v1"


def test_rich_payload_validation_accepts_gallery_items_without_block_type() -> None:
    payload, warnings = normalize_rich_payload(
        {
            "blocks": [
                {
                    "type": "gallery",
                    "items": [{"url": "https://example.test/a.png", "caption": "A"}],
                }
            ]
        }
    )

    assert warnings == []
    assert payload is not None
    assert payload["blocks"][0]["items"][0]["url"] == "https://example.test/a.png"


@pytest.mark.asyncio
async def test_supersede_blob_cleanup_waits_for_commit(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/rich-rollback.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = _artifact_store(tmp_path)

    async with factory() as session:
        session.add(User(email="owner@example.com", name="Owner", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent", owner_email="owner@example.com", name="Agent"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="owner@example.com",
                agent_id="agent",
                context_type="direct",
            )
        )
        await session.flush()
        first = await create_deliverable(
            session,
            conversation_id="conv",
            content="First",
            format="markdown",
            artifact_store=store,
        )
        first_id = first.deliverable_id
        await session.commit()

        second = await create_deliverable(
            session,
            conversation_id="conv",
            content="Second",
            format="markdown",
            artifact_store=store,
        )
        second_id = second.deliverable_id
        await store.async_load("deliverables", first_id, "content.md")
        await store.async_load("deliverables", second_id, "content.md")
        await session.rollback()

        await store.async_load("deliverables", first_id, "content.md")
        with pytest.raises(FileNotFoundError):
            await store.async_load("deliverables", second_id, "content.md")

        replacement = await create_deliverable(
            session,
            conversation_id="conv",
            content="Replacement",
            format="markdown",
            artifact_store=store,
        )
        replacement_id = replacement.deliverable_id
        await session.commit()

    with pytest.raises(FileNotFoundError):
        await store.async_load("deliverables", first_id, "content.md")
    await store.async_load("deliverables", replacement_id, "content.md")
    await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_scoped_deliverable_access_checks_owner(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/rich-auth.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    store = _artifact_store(tmp_path)

    async with factory() as session:
        session.add_all(
            [
                User(email="owner@example.com", name="Owner", role="user"),
                User(email="other@example.com", name="Other", role="user"),
            ]
        )
        await session.flush()
        session.add(Agent(agent_id="agent", owner_email="owner@example.com", name="Agent"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="owner@example.com",
                agent_id="agent",
                context_type="direct",
            )
        )
        session.add(
            Session(
                session_id="sess",
                conversation_id="conv",
                user_email="owner@example.com",
                agent_id="agent",
            )
        )
        await session.flush()
        row = await create_deliverable(
            session,
            deliverable_id="dlv_direct_auth",
            conversation_id="conv",
            session_id="sess",
            turn_id="turn",
            content="Private fallback",
            format="rich",
            rich={"blocks": [{"type": "markdown", "content": "private"}]},
            artifact_store=store,
        )
        assert (
            await get_accessible_conversation_deliverable(
                session, row.deliverable_id, "owner@example.com"
            )
        ) is not None
        assert (
            await get_accessible_conversation_deliverable(
                session, row.deliverable_id, "other@example.com"
            )
        ) is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_oversized_rich_payload_is_guarded_not_projected(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/rich-oversized.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    oversized = {"blocks": [{"type": "markdown", "content": "x" * RICH_DELIVERABLE_MAX_BYTES}]}

    store = _artifact_store(tmp_path)

    async with factory() as session:
        session.add(User(email="owner@example.com", name="Owner", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent", owner_email="owner@example.com", name="Agent"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="owner@example.com",
                agent_id="agent",
                context_type="direct",
            )
        )
        await session.flush()
        with pytest.raises(RichPayloadValidationError):
            await create_deliverable(
                session,
                conversation_id="conv",
                content="Fallback survives oversized payload",
                format="rich",
                rich=oversized,
                artifact_store=store,
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_task_rich_deliverable_and_lightweight_projection(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/rich-workflow.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    large_payload = {
        "blocks": [{"type": "markdown", "content": "x" * 15_000} for _ in range(5)],
        "exports": [{"type": "pdf", "status": "planned"}],
    }
    store = _artifact_store(tmp_path)

    async with factory() as session:
        session.add(User(email="owner@example.com", name="Owner", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent", owner_email="owner@example.com", name="Agent"))
        await session.flush()
        task = await create_task(
            session,
            task_id="task-rich",
            created_by="owner@example.com",
            agent_id="agent",
            title="Rich task",
        )
        step_run = await create_step_run(
            session,
            step_run_id="sr-rich",
            task_id=task.task_id,
            step_name="execute",
            step_type="direct",
            agent_id="agent",
        )
        row = await create_deliverable(
            session,
            step_run_id=step_run.step_run_id,
            content="Workflow fallback",
            format="rich",
            title="Workflow rich",
            rich=large_payload,
            artifact_store=store,
        )

    await engine.dispose()

    await hydrate_deliverable_payload(row, store)
    full = deliverable_to_response(row, include_rich_payload=True)
    light = deliverable_to_response(row, include_rich_payload=False)
    assert full.rich_payload == row.rich_payload
    assert light.rich_payload is not None
    assert light.rich_payload["metadata"]["projection_truncated"] is True
    assert light.rich_payload["metadata"]["full_payload_required"] is True
    assert light.content == ""
    assert light.render_metadata["schema"] == "cognis.rich_deliverable.v1"


@pytest.mark.asyncio
async def test_deliverable_requires_exactly_one_owner_scope(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/rich-scope.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    async with factory() as session:
        with pytest.raises(ValueError, match="exactly one owner scope"):
            await create_deliverable(session, content="No owner")
        with pytest.raises(ValueError, match="exactly one owner scope"):
            await create_deliverable(
                session,
                step_run_id="sr",
                conversation_id="conv",
                content="Two owners",
            )

    await engine.dispose()
