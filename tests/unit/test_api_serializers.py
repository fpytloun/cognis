from __future__ import annotations

from cognis.api.serializers import event_to_response, serialize_event_rows


def test_event_to_response_normalizes_non_mapping_data() -> None:
    response = event_to_response(
        {
            "seq": 1,
            "type": "assistant_message",
            "data": None,
            "timestamp": "2026-03-28T00:00:00Z",
        }
    )

    assert response.seq == 1
    assert response.type == "assistant_message"
    assert response.data == {}
    assert response.timestamp == "2026-03-28T00:00:00Z"


def test_serialize_event_rows_skips_malformed_rows() -> None:
    responses = serialize_event_rows(
        [
            {
                "seq": 1,
                "type": "assistant_message",
                "data": {"content": "ok"},
                "ts": "2026-03-28T00:00:00Z",
            },
            ["broken"],
        ],
        log_label="serializer_test",
        log_context={"session_id": "sess_1"},
    )

    assert len(responses) == 1
    assert responses[0].type == "assistant_message"
    assert responses[0].data == {"content": "ok"}
