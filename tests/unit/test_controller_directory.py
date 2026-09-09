from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.core.controller_directory import ControllerInstanceDirectory
from cognis.core.controller_runtime import ControllerRuntime


@pytest.mark.asyncio
async def test_stop_tolerates_directory_write_failure(caplog: pytest.LogCaptureFixture) -> None:
    directory = ControllerInstanceDirectory(
        cast(Any, MagicMock()),
        ControllerRuntime(instance_id="controller-a", incarnation_id="boot-a"),
        internal_url=None,
    )
    directory._write = AsyncMock(side_effect=RuntimeError("database is locked"))  # type: ignore[method-assign]

    await directory.stop()

    assert directory._state == "stopped"
    assert "controller directory stop write failed" in caplog.text
