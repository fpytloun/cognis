from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cognis import json_codec
from cognis.core import workflow_rendering
from cognis.core.workflow_rendering import (
    WorkflowRenderer,
    WorkflowRenderError,
    build_render_audit_record,
    normalize_deterministic_output,
)
from cognis.models.workflow import DeterministicOutputConfig


@pytest.fixture
def renderer() -> WorkflowRenderer:
    return WorkflowRenderer()


def test_text_expression_and_recursive_native_modes(renderer: WorkflowRenderer) -> None:
    context = {
        "vars": {"name": "Ada", "enabled": True, "count": 3},
        "steps": {"fetch": {"outputs": {"items": [1, 2]}}},
    }

    assert renderer.render_text("Hello {{ vars.name }}", context) == "Hello Ada"
    assert renderer.render_expression("{{ vars.enabled and vars.count > 1 }}", context) is True
    assert renderer.render_native(
        {"count": "{{ vars.count }}", "items": "{{ steps.fetch.outputs.items }}"},
        context,
    ) == {"count": 3, "items": [1, 2]}


def test_expression_requires_strict_boolean(renderer: WorkflowRenderer) -> None:
    with pytest.raises(WorkflowRenderError, match="boolean"):
        renderer.render_expression("{{ vars.count }}", {"vars": {"count": 1}})


def test_receipt_manifest_tojson_is_safe_but_missing_runtime_path_is_not(
    renderer: WorkflowRenderer,
) -> None:
    manifest = [{"filename": "control.py", "size_bytes": 42}]
    context = {
        "steps": {
            "transport_assets": {
                "outputs": {"asset_manifest": manifest, "python_environment": None}
            }
        }
    }

    rendered = renderer.render_native(
        "{{ steps.transport_assets.outputs.asset_manifest | tojson }}", context
    )
    assert type(rendered) is str
    assert json.loads(rendered) == manifest
    args = {"env": {"ASSET_MANIFEST": rendered}}
    assert json_codec.loads(json_codec.dumps(args)) == args
    assert build_render_audit_record(template={}, rendered=args)["rendered"] == args
    with pytest.raises(WorkflowRenderError, match="unsafe value"):
        renderer.render_native(
            "{{ steps.transport_assets.outputs.python_environment.python_path }}", context
        )


def test_tojson_keeps_jinja_escaping_and_indent(renderer: WorkflowRenderer) -> None:
    value = {"value": "<script>&'\u00e9"}
    rendered = renderer.render_native("{{ value | tojson(indent=2) }}", {"value": value})

    assert type(rendered) is str
    assert json.loads(rendered) == value
    assert "\\u003cscript\\u003e\\u0026\\u0027" in rendered
    assert "\n  " in rendered


def test_evidence_filters_remain_bounded(renderer: WorkflowRenderer) -> None:
    assert renderer.render_expression(
        "'implemented' in (message | lower)",
        {
            "message": "IMPLEMENTED",
        },
    )
    assert (
        renderer.render_text("{{ files | join(', ') }}", {"files": ["a.py", "b.py"]})
        == "a.py, b.py"
    )
    assert (
        renderer.render_text("{{ output | tojson }}", {"output": {"id": "report"}})
        == '{"id": "report"}'
    )
    with pytest.raises(WorkflowRenderError):
        renderer.render_text(
            "{{ items | join(separator) }}",
            {
                "items": [""] * 1000,
                "separator": "x" * 1000,
            },
        )
    with pytest.raises(WorkflowRenderError):
        renderer.validate_template("{{ value | unknown_filter }}")


@pytest.mark.parametrize("value", [None, "true", "false", 1, 0, [], {}])
def test_default_preserves_invalid_condition_types(
    renderer: WorkflowRenderer, value: object
) -> None:
    with pytest.raises(WorkflowRenderError, match="boolean"):
        renderer.render_expression(
            "{{ vars.notify | default(false) }}", {"vars": {"notify": value}}
        )
    assert (
        renderer.render_native("{{ vars.notify | default(false) }}", {"vars": {"notify": value}})
        == value
    )


def test_default_uses_jinja_missing_value_semantics(renderer: WorkflowRenderer) -> None:
    assert renderer.render_expression("{{ vars.notify | default(false) }}", {"vars": {}}) is False
    assert (
        renderer.render_native("{{ vars.notify | default(true) }}", {"vars": {"notify": False}})
        is False
    )
    assert renderer.render_text("{{ vars.missing | default('absent') }}", {"vars": {}}) == "absent"
    with pytest.raises(WorkflowRenderError):
        renderer.render_expression("{{ vars.missing }}", {"vars": {}})


@pytest.mark.parametrize(
    "expression",
    [
        "{{ vars.missing | attr('__class__') | default(false) }}",
        "{{ vars.items() | default(false) }}",
        "{{ vars.__class__.__mro__() | default(false) }}",
        "{{ vars.missing | default(now.__globals__) }}",
    ],
)
def test_default_does_not_enable_object_access(renderer: WorkflowRenderer, expression: str) -> None:
    with pytest.raises(WorkflowRenderError):
        renderer.render_native(expression, {"vars": {}})


def test_strict_undefined_is_redaction_safe(renderer: WorkflowRenderer) -> None:
    secret = "super-secret-value"
    with pytest.raises(WorkflowRenderError) as exc:
        renderer.render_text("{{ vars.missing }} " + secret, {"vars": {}})

    assert secret not in str(exc.value)
    assert "missing" not in str(exc.value)


@pytest.mark.parametrize(
    "template",
    [
        "{% import 'x' as x %}",
        "{% include 'x' %}",
        "{% macro x() %}bad{% endmacro %}",
        "{% call x() %}bad{% endcall %}",
        "{{ vars.__class__ }}",
        "{{ vars.items() }}",
        "{{ 'x' * 1000000000 }}",
        '{{ "%1000000000s" % "x" }}',
        "{{ vars.items ~ vars.items }}",
        "{% for item in vars.items %}{{ item }}{% endfor %}",
    ],
)
def test_unsafe_constructs_and_object_access_fail(
    renderer: WorkflowRenderer, template: str
) -> None:
    with pytest.raises(WorkflowRenderError):
        renderer.render_text(template, {"vars": {"items": "safe"}})


@pytest.mark.parametrize(
    "context",
    [
        {"credentials": SimpleNamespace(token="secret")},
        {"session": object()},
        {"router": lambda: None},
    ],
)
def test_raw_runtime_objects_are_rejected(
    renderer: WorkflowRenderer, context: dict[str, object]
) -> None:
    with pytest.raises(WorkflowRenderError, match="unsafe value"):
        renderer.render_text("safe", context)


def test_date_helpers_are_typed_and_timezone_safe(renderer: WorkflowRenderer) -> None:
    context = {"vars": {"date": "2024-01-31T12:00:00+00:00"}}

    assert (
        renderer.render_native("{{ date_add(vars.date, months=1) }}", context)
        == "2024-02-29T12:00:00+00:00"
    )
    assert (
        renderer.render_native(
            "{{ format_datetime(convert_timezone(vars.date, 'UTC', 'Europe/Prague'), "
            "format='date_only') }}",
            context,
        )
        == "2024-01-31"
    )
    with pytest.raises(WorkflowRenderError):
        renderer.render_native("{{ now('Not/AZone') }}", context)


def test_size_limits_and_audit_truncation_and_redaction(
    renderer: WorkflowRenderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow_rendering, "MAX_RENDER_OUTPUT_BYTES", 20)
    with pytest.raises(WorkflowRenderError, match="output exceeds"):
        renderer.render_text("x" * 100, {})

    monkeypatch.setattr(workflow_rendering, "MAX_AUDIT_BYTES", 100)
    audit = build_render_audit_record(
        template={"password": "template-secret"},
        rendered={"token": "result-secret", "payload": "x" * 200},
    )
    assert audit["truncated"] is True
    assert "template-secret" not in str(audit)
    assert "result-secret" not in str(audit)


def test_deterministic_output_normalization(renderer: WorkflowRenderer) -> None:
    output = normalize_deterministic_output(
        DeterministicOutputConfig(
            summary="Fetched {{ vars.count }}",
            outputs={"count": "{{ vars.count }}"},
            metadata={
                "source": "test",
                "deterministic_step": False,
                "step_type": "run",
            },
        ),
        renderer,
        {"vars": {"count": 2}},
        step_type="tool_call",
    )

    assert output.summary == "Fetched 2"
    assert output.outputs == {"count": 2}
    assert output.metadata == {
        "deterministic_step": True,
        "step_type": "tool_call",
        "source": "test",
    }
