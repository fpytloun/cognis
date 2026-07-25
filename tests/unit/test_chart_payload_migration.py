from __future__ import annotations

import importlib
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from cognis.rendering.rich_visuals import (
    normalize_chart,
    rich_payload_has_noncanonical_chart,
    upgrade_legacy_chart_block,
    upgrade_legacy_chart_payload,
)

MIGRATION = "cognis.store.migrations.versions.093_canonical_chart_payloads"


def test_upgrade_legacy_flat_chart_preserves_metadata() -> None:
    result = upgrade_legacy_chart_block(
        {
            "type": "chart",
            "title": "Coverage",
            "description": "Covered paths",
            "chart_type": "pie",
            "data": [
                {"label": "Covered", "value": 3},
                {"label": "Missing", "value": 1},
            ],
            "source": "Test report",
            "source_url": "https://reports.example.test/coverage",
            "observed_at": "2026-07-15T10:00:00Z",
            "legend_position": "right",
            "palette_token": "categorical",
        }
    )

    assert result.status == "upgraded"
    assert result.reason is None
    assert result.block is not None
    assert result.block["chart_type"] == "donut"
    assert result.block["title"] == "Coverage"
    assert result.block["source"] == "Test report"
    assert "data" not in result.block
    assert normalize_chart(result.block) is not None


def test_upgrade_legacy_chart_returns_clear_unupgradable_result() -> None:
    block = {
        "type": "chart",
        "chart_type": "line",
        "data": [{"label": "Broken", "value": "not-a-number"}],
    }

    result = upgrade_legacy_chart_block(block)

    assert result.status == "unupgradable"
    assert result.block is None
    assert result.reason == "invalid_flat_rows"
    assert block["data"][0]["value"] == "not-a-number"


def test_upgrade_legacy_chart_rejects_unsupported_shape_variants() -> None:
    unsupported = [
        {
            "type": "chart",
            "chart_type": "bar",
            "rows": [{"label": "A", "value": 1}],
        },
        {
            "type": "chart",
            "chart_type": "bar",
            "data": [{"day": "Mon", "series": "API", "value": 1}],
            "series_key": "series",
            "x_key": "day",
            "y_key": "value",
        },
        {
            "type": "chart",
            "chart_type": "bar",
            "rows": [{"day": "Mon", "series": "API", "value": 1}],
            "series_key": "series",
            "x_key": "day",
        },
    ]

    for block in unsupported:
        original = dict(block)
        result = upgrade_legacy_chart_block(block)
        assert result.status == "unupgradable"
        assert result.reason == "unsupported_legacy_shape"
        assert result.block is None
        assert block == original


def test_upgrade_legacy_long_form_chart_infers_time_axis() -> None:
    result = upgrade_legacy_chart_block(
        {
            "type": "chart",
            "chart_type": "line",
            "rows": [
                {"date": "2026-07-14", "series": "Requests", "value": 4},
                {"date": "2026-07-15", "series": "Requests", "value": 7},
            ],
            "series_key": "series",
            "x_key": "date",
            "y_key": "value",
            "range_selector": [{"id": "all", "label": "All"}],
        }
    )

    assert result.status == "upgraded"
    assert result.block is not None
    assert result.block["x_axis"] == {"type": "time"}
    assert result.block["range_selector"] == [{"id": "all", "label": "All"}]


def test_payload_traversal_limit_does_not_partially_upgrade() -> None:
    payload = {
        "blocks": [
            {
                "type": "chart",
                "chart_type": "bar",
                "data": [{"label": "A", "value": 2}],
            },
            {"type": "section", "children": [{"type": "markdown", "content": "Keep"}]},
        ]
    }

    result = upgrade_legacy_chart_payload(payload, max_nodes=4)

    assert result.reason == "chart_payload_traversal_limit"
    assert result.upgraded_blocks == 0
    assert result.payload == payload
    assert "spec_version" not in result.payload["blocks"][0]


def test_payload_upgrade_leaves_chart_shaped_auxiliary_records_opaque() -> None:
    auxiliary_chart = {
        "type": "chart",
        "chart_type": "bar",
        "data": [{"label": "Do not migrate", "value": 9}],
    }
    payload = {
        "blocks": [{"type": "markdown", "content": "Keep"}],
        "datasets": [auxiliary_chart],
        "metadata": {"analytics": auxiliary_chart},
    }

    result = upgrade_legacy_chart_payload(payload)

    assert result.reason is None
    assert result.upgraded_blocks == 0
    assert result.payload == payload
    assert not rich_payload_has_noncanonical_chart(payload)


def test_payload_upgrade_visits_only_rendered_item_backed_blocks() -> None:
    payload = {
        "blocks": [
            {
                "type": "accordion",
                "items": [
                    {
                        "type": "chart",
                        "chart_type": "bar",
                        "data": [{"label": "A", "value": 2}],
                    }
                ],
            },
            {
                "type": "day_agenda",
                "items": [
                    {
                        "type": "chart",
                        "chart_type": "bar",
                        "data": [{"label": "Opaque agenda item", "value": 3}],
                    }
                ],
            },
        ]
    }

    result = upgrade_legacy_chart_payload(payload)

    assert result.upgraded_blocks == 1
    assert result.payload["blocks"][0]["items"][0]["spec_version"] == "cognis.chart.v1"
    assert result.payload["blocks"][1]["items"][0] == payload["blocks"][1]["items"][0]
    assert not rich_payload_has_noncanonical_chart(result.payload)


def test_data_migration_upgrades_only_convertible_chart_blocks() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    deliverables = sa.Table(
        "deliverables",
        metadata,
        sa.Column("deliverable_id", sa.String(), primary_key=True),
        sa.Column("rich_payload", sa.JSON()),
    )
    metadata.create_all(engine)
    convertible = {
        "blocks": [
            {
                "type": "chart",
                "chart_type": "bar",
                "data": [{"label": "A", "value": 2}],
            }
        ]
    }
    unupgradable = {
        "blocks": [
            {
                "type": "chart",
                "chart_type": "range",
                "data": [{"label": "A", "value": 2}],
            }
        ]
    }
    module: ModuleType = importlib.import_module(MIGRATION)
    with engine.begin() as connection:
        connection.execute(
            deliverables.insert(),
            [
                {"deliverable_id": "dlv_convertible", "rich_payload": convertible},
                {"deliverable_id": "dlv_unupgradable", "rich_payload": unupgradable},
                *[
                    {
                        "deliverable_id": f"dlv_page_{index:03d}",
                        "rich_payload": convertible,
                    }
                    for index in range(101)
                ],
            ],
        )
        original_op = module.op
        module.op = Operations(MigrationContext.configure(connection))
        try:
            module.upgrade()
            module.downgrade()
        finally:
            module.op = original_op
        rows = {
            deliverable_id: payload
            for deliverable_id, payload in connection.execute(
                sa.select(deliverables.c.deliverable_id, deliverables.c.rich_payload)
            )
        }

    assert rows["dlv_convertible"]["blocks"][0]["spec_version"] == "cognis.chart.v1"
    assert rows["dlv_page_100"]["blocks"][0]["spec_version"] == "cognis.chart.v1"
    assert rows["dlv_unupgradable"] == unupgradable
