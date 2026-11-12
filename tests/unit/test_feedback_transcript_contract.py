from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cognis.core.feedback.primitives import Tokens, digest, encode
from cognis.models import feedback as dto


def legacy_source() -> dict:
    return {
        "kind": "transcript",
        "conversation_id": "conversation",
        "source_event": {
            "store": "intaris",
            "session_id": "session",
            "seq": 2,
            "event_type": "assistant_message",
            "event_id": None,
        },
        "source_item_id": "message:turn:phase:0",
        "field_path": ["content"],
    }


def test_absent_and_null_representation_preserve_all_legacy_serialized_identity() -> None:
    old = legacy_source()
    old_bytes = encode(old)
    signer = Tokens("contract-secret")
    claims = {"kind": "source", "actor": "owner", "source": old, "exp": 10000000000}
    token = signer.seal(claims)
    for value in (old, {**old, "representation": None}):
        parsed = dto.TranscriptSourceRef.model_validate(value)
        assert parsed.representation is None
        assert parsed.model_dump(mode="json") == old
        assert json.loads(parsed.model_dump_json()) == old
        assert encode(parsed.model_dump(mode="json")) == old_bytes
        assert digest(encode(parsed.model_dump(mode="json"))) == digest(old_bytes)
        assert signer.seal({**claims, "source": parsed.model_dump(mode="json")}) == token
        nested = dto.ResolveSourceRequest.model_validate(
            {
                "source": value,
                "context": {"kind": "standalone"},
            }
        )
        assert nested.model_dump(mode="json")["source"] == old


def test_explicit_representation_is_strict_and_changes_persisted_source_identity() -> None:
    old = legacy_source()
    value = {**old, "representation": "chat-item-as-of-event-v1"}
    parsed = dto.TranscriptSourceRef.model_validate(value)
    assert parsed.model_dump(mode="json") == value
    assert digest(encode(parsed.model_dump(mode="json"))) != digest(encode(old))
    for representation in ("latest", "", "chat-item-as-of-event-v2", False):
        with pytest.raises(ValidationError):
            dto.TranscriptSourceRef.model_validate({**old, "representation": representation})


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_transcript_api_schema_keeps_strict_properties(mode: str) -> None:
    schema = dto.TranscriptSourceRef.model_json_schema(mode=mode)
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "kind",
        "conversation_id",
        "source_event",
        "source_item_id",
        "field_path",
    ]
    assert schema["properties"]["representation"]["anyOf"] == [
        {"const": "chat-item-as-of-event-v1", "type": "string"},
        {"type": "null"},
    ]
