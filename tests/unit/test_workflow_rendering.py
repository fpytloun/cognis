from __future__ import annotations

from types import SimpleNamespace

import pytest

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
