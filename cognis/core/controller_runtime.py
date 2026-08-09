"""Process-local controller identity and lifecycle state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum


class ControllerLifecycleState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"


@dataclass
class ControllerRuntime:
    """Stable controller identity plus a unique identity for this process boot."""

    instance_id: str
    incarnation_id: str = ""
    state: ControllerLifecycleState = ControllerLifecycleState.STARTING
    schema_compatible: bool = False
    schema_error: str | None = None

    def __post_init__(self) -> None:
        if not self.incarnation_id:
            self.incarnation_id = f"boot_{uuid.uuid4().hex}"

    @property
    def controller_id(self) -> str:
        return self.instance_id

    @property
    def owner_id(self) -> str:
        return f"{self.instance_id}:{self.incarnation_id}"

    def mark_schema_compatible(self) -> None:
        self.schema_compatible = True
        self.schema_error = None

    def mark_schema_incompatible(self, error: str) -> None:
        self.schema_compatible = False
        self.schema_error = error

    def mark_ready(self) -> None:
        if self.state is not ControllerLifecycleState.STARTING:
            raise RuntimeError(f"Cannot become ready from {self.state}")
        if not self.schema_compatible:
            raise RuntimeError("Cannot become ready before schema validation")
        self.state = ControllerLifecycleState.READY

    def begin_draining(self) -> None:
        if self.state in {
            ControllerLifecycleState.STARTING,
            ControllerLifecycleState.READY,
        }:
            self.state = ControllerLifecycleState.DRAINING

    def mark_stopped(self) -> None:
        self.state = ControllerLifecycleState.STOPPED
