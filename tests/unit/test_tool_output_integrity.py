from __future__ import annotations

import pytest

from cognis.core.tool_output_store import (
    FilesystemToolOutputBackend,
    ToolOutputIntegrityError,
    ToolOutputStore,
)


@pytest.mark.asyncio
async def test_filesystem_save_replaces_atomically_and_round_trips(tmp_path) -> None:
    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))

    await store.save("call_atomic", "old")
    await store.save("call_atomic", "new")

    result = await store.read("call_atomic")
    assert result is not None
    assert result.content == "1: new"
    assert not list((tmp_path / "tool-outputs").glob("*.tmp"))


@pytest.mark.asyncio
async def test_store_does_not_confirm_output_when_round_trip_changes(tmp_path) -> None:
    backend = FilesystemToolOutputBackend(tmp_path)
    store = ToolOutputStore(backend)
    original_load = backend.load

    await backend.save("call_hash", "old")

    async def altered_load(call_id: str) -> str | None:
        value = await original_load(call_id)
        return "tampered" if value is not None else None

    backend.load = altered_load  # type: ignore[method-assign]
    with pytest.raises(ToolOutputIntegrityError):
        await store.save("call_hash", "new", anchors=[{"anchor": "x"}])
