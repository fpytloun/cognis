from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cognis.models.executor_inference import (
    executor_local_inference_config_confirmed,
    executor_local_inference_configured,
    executor_local_inference_eligible,
    executor_local_inference_routable,
    resolve_executor_local_inference_config,
)


def test_local_inference_legacy_defaults_are_enabled() -> None:
    resolved = resolve_executor_local_inference_config({})

    assert resolved.local_inference_enabled is True
    assert resolved.ollama_runtime.management_enabled is True
    assert resolved.ollama_management_enabled is True
    assert resolved.ollama_runtime.port == 11434
    assert resolved.ollama_runtime.endpoint == "http://127.0.0.1:11434"


def test_legacy_endpoint_normalizes_and_custom_integer_port_derives_endpoint() -> None:
    legacy = resolve_executor_local_inference_config(
        {"ollama_runtime": {"endpoint": "http://127.0.0.1:11434"}}
    )
    custom = resolve_executor_local_inference_config({"ollama_runtime": {"port": 22434}})

    assert legacy.ollama_runtime.port == 11434
    assert custom.ollama_runtime.port == 22434
    assert custom.ollama_runtime.endpoint == "http://127.0.0.1:22434"


def test_local_inference_hard_gate_overrides_ollama_management() -> None:
    resolved = resolve_executor_local_inference_config(
        {
            "local_inference_enabled": False,
            "ollama_runtime": {"management_enabled": True},
        }
    )

    assert resolved.local_inference_enabled is False
    assert resolved.ollama_runtime.management_enabled is True
    assert resolved.ollama_management_enabled is False


def test_explicit_ollama_management_false_is_preserved() -> None:
    resolved = resolve_executor_local_inference_config(
        {"ollama_runtime": {"management_enabled": False}}
    )

    assert resolved.local_inference_enabled is True
    assert resolved.ollama_management_enabled is False


@pytest.mark.parametrize(
    "config",
    [
        {"local_inference_enabled": "false"},
        {"ollama_runtime": {"management_enabled": 0}},
        {"ollama_runtime": {"endpoint": "http://remote.example:11434"}},
        {"ollama_runtime": {"endpoint": "http://127.0.0.1:22434"}},
        {"ollama_runtime": {"port": "11434"}},
        {"ollama_runtime": {"port": 0}},
        {"ollama_runtime": {"port": 65536}},
    ],
)
def test_local_inference_config_rejects_invalid_values(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        resolve_executor_local_inference_config(config)


def test_executor_eligibility_requires_persisted_and_advertised_capability() -> None:
    row = _confirmed_row()

    assert executor_local_inference_eligible(row, advertised=True) is True
    assert executor_local_inference_eligible(row, advertised=False) is False
    row.config = {"local_inference_enabled": False}
    assert executor_local_inference_eligible(row, advertised=True) is False


def test_executor_routing_requires_websocket_type() -> None:
    row = _confirmed_row(executor_type="in_process")
    assert executor_local_inference_routable(row, advertised=True) is False
    row.executor_type = "subprocess"
    assert executor_local_inference_routable(row, advertised=True) is False
    row.executor_type = "websocket"
    assert executor_local_inference_routable(row, advertised=True) is True


def test_declarative_eligibility_requires_websocket_but_not_confirmation() -> None:
    row = _confirmed_row(executor_type="in_process")
    assert executor_local_inference_configured(row) is False

    row.executor_type = "websocket"
    row.desired_config_version = 2
    row.runtime_state = "reconfiguring"
    assert executor_local_inference_configured(row) is True
    assert executor_local_inference_routable(row) is False


def test_executor_routing_fails_closed_during_generation_or_endpoint_mismatch() -> None:
    row = _confirmed_row()
    assert executor_local_inference_config_confirmed(row) is True

    row.desired_config_version = 2
    assert executor_local_inference_routable(row, advertised=True) is False
    assert executor_local_inference_routable(row) is False

    row.applied_config_version = 2
    row.runtime_metadata["ollama_runtime"]["port"] = 22434
    row.runtime_metadata["ollama_runtime"]["endpoint"] = "http://127.0.0.1:22434"
    assert executor_local_inference_routable(row, advertised=True) is False


def test_disabled_inference_confirms_effective_management_state() -> None:
    row = _confirmed_row()
    row.config = {"local_inference_enabled": False}
    row.runtime_metadata["local_inference_enabled"] = False
    row.runtime_metadata["ollama_runtime"]["management_enabled"] = False

    assert executor_local_inference_config_confirmed(row) is True
    assert executor_local_inference_routable(row, advertised=True) is False


def _confirmed_row(*, executor_type: str = "websocket") -> SimpleNamespace:
    return SimpleNamespace(
        status="active",
        config={},
        executor_type=executor_type,
        runtime_state="active",
        desired_config_version=1,
        applied_config_version=1,
        runtime_metadata={
            "local_inference_enabled": True,
            "ollama_runtime": {
                "runtime_type": "ollama",
                "port": 11434,
                "endpoint": "http://127.0.0.1:11434",
                "management_enabled": True,
                "max_concurrent_pulls": 1,
                "disk_headroom_bytes": 5 * 1024**3,
            },
        },
    )
