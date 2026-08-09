from __future__ import annotations

import pytest

from cognis.core.controller_runtime import ControllerLifecycleState, ControllerRuntime


def test_controller_runtime_lifecycle() -> None:
    runtime = ControllerRuntime("controller-a")
    restarted = ControllerRuntime("controller-a")

    assert runtime.controller_id == "controller-a"
    assert runtime.incarnation_id.startswith("boot_")
    assert runtime.owner_id == f"controller-a:{runtime.incarnation_id}"
    assert restarted.incarnation_id != runtime.incarnation_id
    assert runtime.state is ControllerLifecycleState.STARTING

    runtime.mark_schema_compatible()
    runtime.mark_ready()
    runtime.begin_draining()
    runtime.mark_stopped()

    assert runtime.state is ControllerLifecycleState.STOPPED


def test_controller_cannot_be_ready_before_schema_validation() -> None:
    runtime = ControllerRuntime("controller-a")
    with pytest.raises(RuntimeError, match="schema validation"):
        runtime.mark_ready()
