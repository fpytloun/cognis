"""Unit tests for Signal adapter — config parsing, direct runtime, and transport selection."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.channels.adapters.signal import SignalAdapter, _SignalConfig
from cognis.channels.adapters.signal_cli_runtime import (
    SignalCliRuntime,
    SignalCliRuntimeError,
)
from cognis.models.channel import (
    ChannelAccountConfig,
    InboundMessage,
)

# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------


class TestSignalConfig:
    def test_defaults_to_rest_api(self) -> None:
        config = _SignalConfig({}, {"account_number": "+1234567890"})
        assert config.transport == "rest_api"
        assert not config.is_direct
        assert config.account_number == "+1234567890"
        assert config.send_read_receipts is True
        assert config.enable_typing is True
        assert config.sync_profile is True
        assert config.ignore_stories is True
        assert config.trust_mode == "trust-all-known"

    def test_direct_jsonrpc_mode(self) -> None:
        config = _SignalConfig(
            {"transport": "direct_jsonrpc"},
            {"account_number": "+1234567890"},
        )
        assert config.transport == "direct_jsonrpc"
        assert config.is_direct

    def test_string_bool_coercion(self) -> None:
        config = _SignalConfig(
            {
                "send_read_receipts": "false",
                "enable_typing": "true",
                "sync_profile": "0",
                "ignore_stories": "yes",
            },
            {"account_number": "+1"},
        )
        assert config.send_read_receipts is False
        assert config.enable_typing is True
        assert config.sync_profile is False
        assert config.ignore_stories is True

    def test_executor_command_from_settings(self) -> None:
        config = _SignalConfig(
            {"_signal_cli_command": "/usr/local/bin/signal-cli"},
            {"account_number": "+1"},
        )
        assert config.signal_cli_command == "/usr/local/bin/signal-cli"

    def test_missing_account_number(self) -> None:
        config = _SignalConfig({}, {})
        assert config.account_number == ""


# ---------------------------------------------------------------------------
# SignalCliRuntime tests
# ---------------------------------------------------------------------------


class FakeProcess:
    """Fake asyncio subprocess for testing.

    Responses are fed through an asyncio.Queue so the reader task
    only sees a response after it has been enqueued, which allows
    tests to control timing between requests and responses.
    """

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdout = MagicMock()
        self.stderr = MagicMock()
        self.returncode = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        for resp in responses or []:
            self._queue.put_nowait(json.dumps(resp).encode() + b"\n")

        # Set up readline as async
        self.stdout.readline = AsyncMock(side_effect=self._read_stdout_line)
        self.stderr.readline = AsyncMock(return_value=b"")

    def enqueue(self, response: dict[str, Any]) -> None:
        """Add a response to be read by the stdout reader."""
        self._queue.put_nowait(json.dumps(response).encode() + b"\n")

    async def _read_stdout_line(self) -> bytes:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=30.0)
        except (TimeoutError, asyncio.CancelledError):
            return b""

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class TestSignalCliRuntime:
    @pytest.mark.asyncio
    async def test_start_fails_if_command_not_found(self) -> None:
        runtime = SignalCliRuntime(
            account_number="+1234567890",
            command="/nonexistent/signal-cli",
        )
        with pytest.raises(SignalCliRuntimeError, match="not found"):
            await runtime.start()

    @pytest.mark.asyncio
    async def test_request_response_correlation(self) -> None:
        """Test that requests are correlated with responses by ID."""
        version_response = {"jsonrpc": "2.0", "result": {"version": "0.13.0"}, "id": 1}

        # Only enqueue the first response initially
        process = FakeProcess([version_response])

        runtime = SignalCliRuntime(account_number="+1234567890")
        runtime._process = process
        runtime._running = True
        runtime._reader_task = asyncio.create_task(runtime._read_stdout())
        runtime._stderr_task = asyncio.create_task(runtime._drain_stderr())

        try:
            # First request (version probe)
            result1 = await runtime.request("version", timeout=2.0)
            assert result1 == {"version": "0.13.0"}

            # Enqueue the second response after the first completes
            send_response = {"jsonrpc": "2.0", "result": {"timestamp": 12345}, "id": 2}
            process.enqueue(send_response)

            # Second request (send)
            result2 = await runtime.request("send", {"message": "hi"}, timeout=2.0)
            assert result2 == {"timestamp": 12345}
        finally:
            runtime._running = False
            if runtime._reader_task:
                runtime._reader_task.cancel()
            if runtime._stderr_task:
                runtime._stderr_task.cancel()

    @pytest.mark.asyncio
    async def test_request_error_response(self) -> None:
        """Test that JSON-RPC error responses raise SignalCliRuntimeError."""
        error_response = {
            "jsonrpc": "2.0",
            "error": {"code": -1, "message": "Unknown account"},
            "id": 1,
        }
        process = FakeProcess([error_response])

        runtime = SignalCliRuntime(account_number="+1234567890")
        runtime._process = process
        runtime._running = True
        runtime._reader_task = asyncio.create_task(runtime._read_stdout())
        runtime._stderr_task = asyncio.create_task(runtime._drain_stderr())

        try:
            with pytest.raises(SignalCliRuntimeError, match="Unknown account"):
                await runtime.request("version", timeout=2.0)
        finally:
            runtime._running = False
            if runtime._reader_task:
                runtime._reader_task.cancel()
            if runtime._stderr_task:
                runtime._stderr_task.cancel()

    @pytest.mark.asyncio
    async def test_notification_dispatch(self) -> None:
        """Test that receive notifications are dispatched to the callback."""
        notification = {
            "jsonrpc": "2.0",
            "method": "receive",
            "params": {
                "envelope": {
                    "source": "+420111222333",
                    "sourceName": "Test",
                    "dataMessage": {"timestamp": 1000, "message": "hello"},
                }
            },
        }
        process = FakeProcess([notification])

        received: list[dict] = []

        async def on_notification(params: dict) -> None:
            received.append(params)

        runtime = SignalCliRuntime(
            account_number="+1234567890",
            on_notification=on_notification,
        )
        runtime._process = process
        runtime._running = True
        runtime._reader_task = asyncio.create_task(runtime._read_stdout())
        runtime._stderr_task = asyncio.create_task(runtime._drain_stderr())

        try:
            # Give the reader time to process
            await asyncio.sleep(0.1)
            assert len(received) == 1
            assert received[0]["envelope"]["source"] == "+420111222333"
        finally:
            runtime._running = False
            if runtime._reader_task:
                runtime._reader_task.cancel()
            if runtime._stderr_task:
                runtime._stderr_task.cancel()

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_requests(self) -> None:
        """Test that stop() fails all pending requests."""
        runtime = SignalCliRuntime(account_number="+1234567890")
        runtime._running = True

        # Create a pending future
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        runtime._pending[1] = future

        await runtime.stop()

        assert future.done()
        with pytest.raises(SignalCliRuntimeError, match="shutting down"):
            future.result()

    @pytest.mark.asyncio
    async def test_request_when_not_running(self) -> None:
        runtime = SignalCliRuntime(account_number="+1234567890")
        with pytest.raises(SignalCliRuntimeError, match="not running"):
            await runtime.request("version")

    @pytest.mark.asyncio
    async def test_is_running_property(self) -> None:
        runtime = SignalCliRuntime(account_number="+1234567890")
        assert not runtime.is_running

        runtime._running = True
        runtime._process = FakeProcess()
        assert runtime.is_running

        runtime._process.returncode = 1
        assert not runtime.is_running

    def test_single_account_mode_property(self) -> None:
        runtime = SignalCliRuntime(account_number="+1234567890")
        assert runtime.single_account_mode is True

    @pytest.mark.asyncio
    async def test_exit_message_includes_returncode_and_stderr_count(self) -> None:
        runtime = SignalCliRuntime(account_number="+1234567890")
        process = FakeProcess()
        process.returncode = 7
        runtime._process = process
        runtime._stderr_line_count = 3

        message = runtime._process_exit_message()

        assert "returncode=7" in message
        assert "stderr_lines=3" in message


# ---------------------------------------------------------------------------
# SignalAdapter transport selection tests
# ---------------------------------------------------------------------------


class TestSignalAdapterTransportSelection:
    def _make_config(
        self,
        transport: str = "rest_api",
        adapter_location: str = "controller",
    ) -> ChannelAccountConfig:
        return ChannelAccountConfig(
            account_id="acct-signal-1",
            channel_type="signal",
            display_name="Signal",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
            settings={"transport": transport},
            adapter_location=adapter_location,
        )

    @pytest.mark.asyncio
    async def test_rest_mode_requires_api_url(self) -> None:
        adapter = SignalAdapter()
        config = self._make_config("rest_api")
        with pytest.raises(ValueError, match="api_url"):
            await adapter._connect.__wrapped__(adapter) if hasattr(
                adapter._connect, "__wrapped__"
            ) else None  # type: ignore
            # Use start() path instead
            adapter._config = config
            adapter._credentials = {"account_number": "+1234567890"}
            await adapter._connect()

    @pytest.mark.asyncio
    async def test_direct_mode_requires_account_number(self) -> None:
        adapter = SignalAdapter()
        config = self._make_config("direct_jsonrpc", "executor")
        adapter._config = config
        adapter._credentials = {}
        with pytest.raises(ValueError, match="account_number"):
            await adapter._connect()


# ---------------------------------------------------------------------------
# Inbound message normalization tests
# ---------------------------------------------------------------------------


class TestSignalInboundNormalization:
    @pytest.mark.asyncio
    async def test_process_envelope_direct_message(self) -> None:
        adapter = SignalAdapter()
        adapter._config = ChannelAccountConfig(
            account_id="acct-1",
            channel_type="signal",
            display_name="Signal",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
        )
        adapter._account_number = "+1234567890"

        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        envelope = {
            "source": "+420111222333",
            "sourceName": "Filip",
            "dataMessage": {
                "timestamp": 1700000000000,
                "message": "Hello!",
                "attachments": [],
                "mentions": [],
            },
        }
        await adapter._process_envelope(envelope, envelope["dataMessage"])

        assert len(dispatched) == 1
        msg = dispatched[0]
        assert msg.sender_id == "+420111222333"
        assert msg.sender_name == "Filip"
        assert msg.content == "Hello!"
        assert msg.chat_type == "direct"
        assert msg.chat_id == "+420111222333"

    @pytest.mark.asyncio
    async def test_process_envelope_group_message(self) -> None:
        adapter = SignalAdapter()
        adapter._config = ChannelAccountConfig(
            account_id="acct-1",
            channel_type="signal",
            display_name="Signal",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
        )
        adapter._account_number = "+1234567890"

        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        envelope = {
            "source": "+420111222333",
            "sourceName": "Filip",
            "dataMessage": {
                "timestamp": 1700000000000,
                "message": "Group msg",
                "groupInfo": {"groupId": "grp-abc", "groupName": "Test Group"},
                "mentions": [{"number": "+1234567890"}],
            },
        }
        await adapter._process_envelope(envelope, envelope["dataMessage"])

        assert len(dispatched) == 1
        msg = dispatched[0]
        assert msg.chat_type == "group"
        assert msg.chat_id == "grp-abc"
        assert msg.chat_name == "Test Group"
        assert msg.was_mentioned is True

    @pytest.mark.asyncio
    async def test_process_envelope_ignores_empty_body_no_attachments(self) -> None:
        adapter = SignalAdapter()
        adapter._config = ChannelAccountConfig(
            account_id="acct-1",
            channel_type="signal",
            display_name="Signal",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
        )
        adapter._account_number = "+1234567890"

        dispatched: list[InboundMessage] = []

        async def fake_dispatch(msg: InboundMessage) -> None:
            dispatched.append(msg)

        adapter._dispatch_inbound = fake_dispatch  # type: ignore[assignment]

        envelope = {
            "source": "+420111222333",
            "dataMessage": {"timestamp": 1700000000000, "message": ""},
        }
        await adapter._process_envelope(envelope, envelope["dataMessage"])
        assert len(dispatched) == 0


# ---------------------------------------------------------------------------
# Capability degradation tests
# ---------------------------------------------------------------------------


class TestCapabilityDegradation:
    @pytest.mark.asyncio
    async def test_typing_disabled_by_config(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc", "enable_typing": "false"},
            {"account_number": "+1"},
        )
        # Should not raise even without a runtime
        await adapter.send_typing("+420111222333")

    @pytest.mark.asyncio
    async def test_read_receipts_disabled_by_config(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc", "send_read_receipts": "false"},
            {"account_number": "+1"},
        )
        await adapter.mark_read("+420111222333", "12345")

    @pytest.mark.asyncio
    async def test_profile_sync_disabled_by_config(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc", "sync_profile": "false"},
            {"account_number": "+1"},
        )
        from cognis.models.channel import AgentProfile

        await adapter.sync_profile(AgentProfile(name="Test"))

    @pytest.mark.asyncio
    async def test_direct_typing_degrades_on_failure(self) -> None:
        """After a failure, typing should be silently disabled."""
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc", "enable_typing": "true"},
            {"account_number": "+1"},
        )
        adapter._account_number = "+1"

        # Create a mock runtime that fails
        mock_runtime = MagicMock()
        mock_runtime.is_running = True
        mock_runtime.request = AsyncMock(side_effect=SignalCliRuntimeError("method not found"))
        adapter._runtime = mock_runtime

        # First call should attempt and fail, adding to degraded
        await adapter._send_typing_direct("+420111222333")
        assert "sendTyping" in adapter._degraded_capabilities

        # Second call should skip entirely
        mock_runtime.request.reset_mock()
        await adapter._send_typing_direct("+420111222333")
        mock_runtime.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_profile_sync_skips_when_runtime_version_unknown(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc", "sync_profile": "true"},
            {"account_number": "+1"},
        )
        adapter._account_number = "+1"

        mock_runtime = MagicMock()
        mock_runtime.is_running = True
        mock_runtime.version = None
        mock_runtime.request = AsyncMock()
        adapter._runtime = mock_runtime

        from cognis.models.channel import AgentProfile

        await adapter._sync_profile_direct(AgentProfile(name="Test"))

        assert "updateProfile" in adapter._degraded_capabilities
        mock_runtime.request.assert_not_called()


class TestDirectParamNormalization:
    def test_direct_params_strip_account_in_single_account_mode(self) -> None:
        adapter = SignalAdapter()
        runtime = MagicMock()
        runtime.single_account_mode = True
        adapter._runtime = runtime

        params = adapter._direct_params({"account": "+1234567890", "recipient": ["+420111222333"]})

        assert "account" not in params
        assert params["recipient"] == ["+420111222333"]

    def test_direct_params_keep_account_without_runtime(self) -> None:
        adapter = SignalAdapter()

        params = adapter._direct_params({"account": "+1234567890", "recipient": ["+420111222333"]})

        assert params["account"] == "+1234567890"
