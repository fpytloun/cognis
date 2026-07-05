"""Unit tests for the LSP client."""

from __future__ import annotations

import asyncio
import logging

import pytest

from cognis.tools.executor.lsp.client import LSPClient, file_uri, uri_to_path
from cognis.tools.executor.lsp.types import DiagnosticFreshness, DiagnosticSeverity


class TestFileUri:
    """Test file URI conversion functions."""

    def test_file_uri_absolute(self) -> None:
        uri = file_uri("/home/user/project/src/foo.py")
        assert uri.startswith("file:///")
        assert "foo.py" in uri

    def test_uri_to_path(self) -> None:
        path = uri_to_path("file:///home/user/project/src/foo.py")
        assert path == "/home/user/project/src/foo.py"

    def test_roundtrip(self) -> None:
        original = "/home/user/project/src/foo.py"
        assert uri_to_path(file_uri(original)) == original


class TestPublishDiagnostics:
    """Test the publishDiagnostics notification handler."""

    def test_handle_publish_diagnostics(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._handle_publish_diagnostics(
            {
                "uri": "file:///src/foo.py",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 9, "character": 3},
                            "end": {"line": 9, "character": 10},
                        },
                        "severity": 1,
                        "code": "E001",
                        "source": "pyright",
                        "message": "undefined variable",
                    },
                    {
                        "range": {
                            "start": {"line": 15, "character": 0},
                            "end": {"line": 15, "character": 5},
                        },
                        "severity": 2,
                        "message": "unused import",
                    },
                ],
            }
        )
        diags = client.get_diagnostics("file:///src/foo.py")
        assert "file:///src/foo.py" in diags
        assert len(diags["file:///src/foo.py"]) == 2
        assert diags["file:///src/foo.py"][0].severity == DiagnosticSeverity.ERROR
        assert diags["file:///src/foo.py"][0].message == "undefined variable"
        assert diags["file:///src/foo.py"][1].severity == DiagnosticSeverity.WARNING

    def test_handle_empty_diagnostics(self) -> None:
        """Empty diagnostics should clear previous diagnostics for the URI."""
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._handle_publish_diagnostics({"uri": "file:///src/foo.py", "diagnostics": []})
        # get_diagnostics with URI filter returns {} when list is empty
        diags = client.get_diagnostics("file:///src/foo.py")
        assert diags == {}
        # But internal state has the key with an empty list
        assert client._diagnostics["file:///src/foo.py"] == []

    def test_stale_versioned_diagnostics_do_not_replace_fresh_snapshot(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        client._mark_document_updated(uri, 2)

        client._handle_publish_diagnostics({"uri": uri, "version": 1, "diagnostics": []})

        assert client.get_diagnostic_snapshots(uri) == {}
        assert client.get_diagnostics(uri) == {}

    def test_matching_versioned_diagnostics_are_fresh(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        client._mark_document_updated(uri, 2)

        client._handle_publish_diagnostics({"uri": uri, "version": 2, "diagnostics": []})

        snapshot = client.get_diagnostic_snapshots(uri)[uri]
        assert snapshot.freshness is DiagnosticFreshness.FRESH
        assert snapshot.diagnostic_version == 2
        assert snapshot.document_version == 2

    def test_unversioned_diagnostics_after_update_are_marked_fresh_unversioned(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        client._mark_document_updated(uri, 3)

        client._handle_publish_diagnostics({"uri": uri, "diagnostics": []})

        snapshot = client.get_diagnostic_snapshots(uri)[uri]
        assert snapshot.freshness is DiagnosticFreshness.FRESH_UNVERSIONED

    def test_handle_malformed_diagnostic(self) -> None:
        """Malformed diagnostics should be skipped, not crash."""
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._handle_publish_diagnostics(
            {
                "uri": "file:///src/foo.py",
                "diagnostics": [
                    "not a dict",
                    {"range": "invalid"},
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "message": "valid diagnostic",
                    },
                ],
            }
        )
        diags = client.get_diagnostics("file:///src/foo.py")
        # At least the valid diagnostic should be present
        assert len(diags["file:///src/foo.py"]) >= 1

    def test_diagnostics_signals_event(self) -> None:
        """Publishing diagnostics should signal the wait event."""
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        event = asyncio.Event()
        client._diag_events[uri] = event
        assert not event.is_set()

        client._handle_publish_diagnostics({"uri": uri, "diagnostics": []})
        assert event.is_set()


class TestGetDiagnostics:
    """Test diagnostic retrieval."""

    def test_get_all_diagnostics(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._handle_publish_diagnostics(
            {
                "uri": "file:///a.py",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "message": "err a",
                    }
                ],
            }
        )
        client._handle_publish_diagnostics(
            {
                "uri": "file:///b.py",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "message": "err b",
                    }
                ],
            }
        )
        all_diags = client.get_diagnostics()
        assert len(all_diags) == 2
        assert "file:///a.py" in all_diags
        assert "file:///b.py" in all_diags

    def test_get_specific_uri(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._handle_publish_diagnostics(
            {
                "uri": "file:///a.py",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "message": "err",
                    }
                ],
            }
        )
        specific = client.get_diagnostics("file:///a.py")
        assert "file:///a.py" in specific

    def test_get_nonexistent_uri(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        result = client.get_diagnostics("file:///nonexistent.py")
        assert result == {}

    def test_has_pending_diagnostics(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")

        assert not client.has_pending_diagnostics("file:///a.py")
        client._pending_diagnostics.add("file:///a.py")
        assert client.has_pending_diagnostics("file:///a.py")


class TestDispatch:
    """Test the message dispatch logic."""

    def test_configuration_response_returns_matching_sections(self) -> None:
        client = LSPClient(
            "pyright",
            "test-cmd",
            [],
            "file:///tmp",
            workspace_configuration={"python": {"analysis": {"diagnosticMode": "openFilesOnly"}}},
        )

        result = client._configuration_response(
            [{"section": "python"}, {"section": "missing"}, {"scopeUri": "file:///tmp/a.py"}]
        )

        assert result == [{"analysis": {"diagnosticMode": "openFilesOnly"}}, {}, {}]

    def test_configuration_response_ignores_malformed_items(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")

        assert client._configuration_response(["bad", {"section": "python"}]) == [{}, {}]
        assert client._configuration_response({"section": "python"}) == []

    def test_configuration_sections_extracts_log_context(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")

        assert client._configuration_sections(
            ["bad", {"section": "python"}, {"section": 123}, {"scopeUri": "file:///tmp/a.py"}]
        ) == [None, "python", None, None]
        assert client._configuration_sections({"section": "python"}) == []

    @pytest.mark.asyncio()
    async def test_dispatch_logs_workspace_configuration_context(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="cognis.tools.executor.lsp.client")
        client = LSPClient(
            "pyright",
            "test-cmd",
            [],
            "file:///tmp",
            workspace_configuration={"python": {"analysis": {}}},
        )

        client._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "workspace/configuration",
                "params": {"items": [{"section": "python"}]},
            }
        )
        await asyncio.sleep(0)

        assert "server_id=pyright" in caplog.text
        assert "requested_sections=['python']" in caplog.text

    @pytest.mark.asyncio()
    async def test_wait_for_diagnostics_tracks_pending_state(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"

        task = asyncio.create_task(client.wait_for_diagnostics(uri, timeout_ms=1000))
        await asyncio.sleep(0)
        assert client.has_pending_diagnostics(uri)

        client._handle_publish_diagnostics({"uri": uri, "diagnostics": []})
        result = await task

        assert not client.has_pending_diagnostics(uri)
        assert result.status is DiagnosticFreshness.FRESH_UNVERSIONED

    @pytest.mark.asyncio()
    async def test_wait_for_diagnostics_times_out_without_stale_cache(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        client._mark_document_updated(uri, 2)
        client._handle_publish_diagnostics({"uri": uri, "version": 1, "diagnostics": []})

        result = await client.wait_for_diagnostics(uri, target_version=2, timeout_ms=1)

        assert result.status is DiagnosticFreshness.TIMEOUT
        assert result.snapshot is None

    @pytest.mark.asyncio()
    async def test_wait_for_diagnostics_debounces_first_fresh_batch(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        client._mark_document_updated(uri, 1)

        task = asyncio.create_task(
            client.wait_for_diagnostics(uri, target_version=1, timeout_ms=1000, debounce_ms=50)
        )
        await asyncio.sleep(0)
        client._handle_publish_diagnostics({"uri": uri, "version": 1, "diagnostics": []})
        await asyncio.sleep(0.01)
        client._handle_publish_diagnostics(
            {
                "uri": uri,
                "version": 1,
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "severity": 1,
                        "message": "late error",
                    }
                ],
            }
        )

        result = await task

        assert result.status is DiagnosticFreshness.FRESH
        assert result.error_count == 1

    @pytest.mark.asyncio()
    async def test_wait_for_diagnostics_debounces_preexisting_fresh_snapshot(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        client._mark_document_updated(uri, 1)
        client._handle_publish_diagnostics({"uri": uri, "version": 1, "diagnostics": []})

        task = asyncio.create_task(
            client.wait_for_diagnostics(uri, target_version=1, timeout_ms=1000, debounce_ms=50)
        )
        await asyncio.sleep(0.01)
        client._handle_publish_diagnostics(
            {
                "uri": uri,
                "version": 1,
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "severity": 1,
                        "message": "late preexisting error",
                    }
                ],
            }
        )

        result = await task

        assert result.status is DiagnosticFreshness.FRESH
        assert result.error_count == 1

    @pytest.mark.asyncio()
    async def test_wait_for_diagnostics_continues_after_stale_batch(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        uri = "file:///src/foo.py"
        client._mark_document_updated(uri, 2)

        task = asyncio.create_task(
            client.wait_for_diagnostics(uri, target_version=2, timeout_ms=1000, debounce_ms=10)
        )
        await asyncio.sleep(0)
        client._handle_publish_diagnostics({"uri": uri, "version": 1, "diagnostics": []})
        await asyncio.sleep(0.03)
        client._handle_publish_diagnostics({"uri": uri, "version": 2, "diagnostics": []})

        result = await task

        assert result.status is DiagnosticFreshness.FRESH
        assert result.target_version == 2
        assert result.snapshot is not None
        assert result.snapshot.diagnostic_version == 2

    @pytest.mark.asyncio()
    async def test_dispatch_response(self) -> None:
        """Responses should resolve pending futures."""
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
        client._pending[1] = future
        client._dispatch({"id": 1, "result": {"capabilities": {}}})
        assert future.done()
        assert future.result() == {"id": 1, "result": {"capabilities": {}}}

    def test_dispatch_ignores_unknown_notification(self) -> None:
        """Unknown notifications should not crash."""
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._dispatch({"method": "unknown/notification", "params": {}})
        # Should not raise

    def test_dispatch_log_message(self) -> None:
        """window/logMessage should not crash."""
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._dispatch({"method": "window/logMessage", "params": {"type": 3, "message": "info"}})

    def test_is_alive_before_start(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        assert not client.is_alive

    def test_is_alive_after_close(self) -> None:
        client = LSPClient("test", "test-cmd", [], "file:///tmp")
        client._closed = True
        assert not client.is_alive
