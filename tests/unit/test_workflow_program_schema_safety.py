from __future__ import annotations

import socket
import urllib.request

import pytest

from cognis.core.workflow_engine import _validate_program_json_value


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("program inputs", {"record": "untrusted"}),
        ("program output", {"record": "untrusted"}),
    ],
)
def test_program_schema_external_refs_fail_without_network_io(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    value: dict[str, str],
) -> None:
    schema = {
        "type": "object",
        "properties": {"record": {"$ref": "https://json-schema.org/draft/2020-12/schema"}},
    }
    network_attempts: list[str] = []

    def block_network(*args: object, **kwargs: object) -> None:
        network_attempts.append("network")
        raise AssertionError("JSON Schema validation must not perform network I/O")

    monkeypatch.setattr(urllib.request, "urlopen", block_network)
    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", block_network)

    with pytest.raises(ValueError, match=f"{label} failed JSON Schema validation"):
        _validate_program_json_value(schema, value, label=label)

    assert network_attempts == []


def test_program_schema_local_refs_remain_supported() -> None:
    schema = {
        "$defs": {"record_id": {"type": "string", "minLength": 1}},
        "type": "object",
        "properties": {"id": {"$ref": "#/$defs/record_id"}},
        "required": ["id"],
    }

    _validate_program_json_value(schema, {"id": "record-1"}, label="program inputs")

    with pytest.raises(ValueError, match="program inputs failed JSON Schema validation"):
        _validate_program_json_value(schema, {"id": ""}, label="program inputs")
