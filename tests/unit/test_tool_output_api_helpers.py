from types import SimpleNamespace

from cognis.api.routes.conversations import _tool_call_belongs_to_events


def test_tool_output_ownership_prefers_tool_result_over_tool_call() -> None:
    events = [
        SimpleNamespace(type="tool_call", data={"call_id": "call_1", "session_id": "sess_1"}),
        SimpleNamespace(
            type="tool_result",
            data={
                "call_id": "call_1",
                "session_id": "sess_1",
                "result": "preview",
                "has_full_output": True,
            },
        ),
    ]

    ownership = _tool_call_belongs_to_events(events, "call_1")

    assert ownership is not None
    data, session_id = ownership
    assert session_id == "sess_1"
    assert data["has_full_output"] is True
    assert data["result"] == "preview"


def test_tool_output_ownership_keeps_tool_call_fallback() -> None:
    events = [SimpleNamespace(type="tool_call", data={"call_id": "call_1"})]

    ownership = _tool_call_belongs_to_events(events, "call_1")

    assert ownership is not None
    data, session_id = ownership
    assert data["call_id"] == "call_1"
    assert session_id is None
