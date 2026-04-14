"""Unit tests for Signal adapter — config parsing, direct runtime, and transport selection."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.channels.adapters.signal import (
    SignalAdapter,
    _extract_direct_attachment_result,
    _fallback_attachment_filename,
    _infer_signal_voice_input,
    _is_fatal_signal_error,
    _normalize_signal_cli_trust_mode,
    _SignalConfig,
)
from cognis.channels.adapters.signal_cli_runtime import (
    SignalCliRuntime,
    SignalCliRuntimeError,
)
from cognis.channels.protocol import NonRetryableChannelError
from cognis.models.channel import (
    ChannelAccountConfig,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
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

    def test_trust_mode_normalizes_for_signal_cli(self) -> None:
        config = _SignalConfig(
            {"trust_mode": "trust-all-known"},
            {"account_number": "+1"},
        )
        assert config.signal_cli_trust_mode == "on-first-use"


class TestSignalTrustModeNormalization:
    def test_maps_legacy_values(self) -> None:
        assert _normalize_signal_cli_trust_mode("trust-all-known") == "on-first-use"
        assert _normalize_signal_cli_trust_mode("always-trust") == "always"
        assert _normalize_signal_cli_trust_mode("on-first-use") == "on-first-use"

    def test_unknown_value_falls_back_safely(self) -> None:
        assert _normalize_signal_cli_trust_mode("weird-value") == "on-first-use"


class TestSignalFatalErrorClassification:
    def test_registered_error_is_fatal(self) -> None:
        assert _is_fatal_signal_error("User +447727940997 is not registered.") is True

    def test_generic_error_is_not_fatal(self) -> None:
        assert _is_fatal_signal_error("signal-cli process exited unexpectedly") is False


class TestSignalVoiceInference:
    def test_voice_flags_mark_voice_input(self) -> None:
        assert (
            _infer_signal_voice_input(
                "",
                [{"contentType": "audio/ogg", "voiceNote": True}],
            )
            is True
        )

    def test_empty_single_audio_attachment_is_treated_as_voice_input(self) -> None:
        assert (
            _infer_signal_voice_input(
                "",
                [{"contentType": "audio/ogg", "id": "att-1"}],
            )
            is True
        )

    def test_null_body_single_audio_attachment_is_treated_as_voice_input(self) -> None:
        assert (
            _infer_signal_voice_input(
                None,
                [{"contentType": "audio/ogg", "id": "att-1"}],
            )
            is True
        )

    def test_audio_with_text_is_not_treated_as_voice_input(self) -> None:
        assert (
            _infer_signal_voice_input(
                "please analyze this file",
                [{"contentType": "audio/mpeg", "id": "att-1"}],
            )
            is False
        )


class TestSignalInboundDeduplication:
    @pytest.mark.asyncio
    async def test_replayed_envelope_is_ignored(self) -> None:
        adapter = SignalAdapter()
        adapter._config = ChannelAccountConfig(
            account_id="acc-1",
            channel_type="signal",
            display_name="Signal",
            credential_refs={},
            agent_id="agent-1",
            user_email="user@example.com",
        )
        adapter._dispatch_inbound = AsyncMock()  # type: ignore[method-assign]

        envelope = {
            "source": "+1234567890",
            "sourceName": "Alice",
            "dataMessage": {
                "timestamp": 1710000000000,
                "message": "hello",
            },
        }

        await adapter._process_envelope(envelope, envelope["dataMessage"])
        await adapter._process_envelope(envelope, envelope["dataMessage"])

        adapter._dispatch_inbound.assert_awaited_once()


class TestSignalDirectAttachmentResult:
    def test_extracts_inline_base64_attachment_payload(self) -> None:
        attachment = MediaAttachment(mime_type="audio/ogg", filename="voice.ogg")

        result = _extract_direct_attachment_result(
            {"data": base64.b64encode(b"audio-bytes").decode("ascii")},
            attachment,
        )

        assert result == (b"audio-bytes", "audio/ogg", "voice.ogg")

    def test_extracts_nested_attachment_path_alias(self, tmp_path) -> None:
        path = tmp_path / "voice.ogg"
        path.write_bytes(b"audio-bytes")
        attachment = MediaAttachment(mime_type="audio/ogg", filename="voice.ogg")

        result = _extract_direct_attachment_result(
            {"attachment": {"path": str(path)}},
            attachment,
        )

        assert result == (b"audio-bytes", "audio/ogg", "voice.ogg")

    def test_extracts_inline_base64_attachment_with_mime_based_filename(self) -> None:
        attachment = MediaAttachment(mime_type="audio/ogg")

        result = _extract_direct_attachment_result(
            {"data": base64.b64encode(b"audio-bytes").decode("ascii")},
            attachment,
        )

        assert result == (b"audio-bytes", "audio/ogg", "attachment.ogg")


class TestSignalFallbackAttachmentFilename:
    def test_infers_audio_filename_extension(self) -> None:
        assert (
            _fallback_attachment_filename(MediaAttachment(mime_type="audio/ogg"))
            == "attachment.ogg"
        )
        assert (
            _fallback_attachment_filename(MediaAttachment(mime_type="audio/mpeg"))
            == "attachment.mp3"
        )

    def test_uses_bin_when_mime_type_is_unknown(self) -> None:
        assert (
            _fallback_attachment_filename(MediaAttachment(mime_type="application/octet-stream"))
            == "attachment.bin"
        )


# ---------------------------------------------------------------------------
# SignalCliRuntime tests
# ---------------------------------------------------------------------------


class FakeProcess:
    """Fake asyncio subprocess for testing.

    Responses are fed through an asyncio.Queue so the reader task
    only sees a response after it has been enqueued, which allows
    tests to control timing between requests and responses.
    """

    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        *,
        eof_when_empty: bool = False,
    ) -> None:
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdout = MagicMock()
        self.stderr = MagicMock()
        self.returncode = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._eof_when_empty = eof_when_empty
        for resp in responses or []:
            self._queue.put_nowait(json.dumps(resp).encode() + b"\n")

        # Set up readline as async
        self.stdout.readline = AsyncMock(side_effect=self._read_stdout_line)
        self.stderr.readline = AsyncMock(return_value=b"")

    def enqueue(self, response: dict[str, Any]) -> None:
        """Add a response to be read by the stdout reader."""
        self._queue.put_nowait(json.dumps(response).encode() + b"\n")

    async def _read_stdout_line(self) -> bytes:
        if self._eof_when_empty and self._queue.empty():
            return b""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=30.0)
        except (TimeoutError, asyncio.CancelledError):
            return b""


class _LimitOverrunStream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    async def readuntil(self, separator: bytes) -> bytes:
        del separator
        remaining = self._payload[self._offset :]
        newline_index = remaining.find(b"\n")
        if newline_index == -1:
            self._offset = len(self._payload)
            raise asyncio.IncompleteReadError(partial=remaining, expected=None)
        if len(remaining[: newline_index + 1]) > 10:
            raise asyncio.LimitOverrunError("chunk exceeded", consumed=10)
        chunk = remaining[: newline_index + 1]
        self._offset += len(chunk)
        return chunk

    async def readexactly(self, n: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + n]
        self._offset += len(chunk)
        return chunk

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
    async def test_start_fails_when_process_exits_during_version_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process = FakeProcess(eof_when_empty=True)
        process.returncode = 9

        monkeypatch.setattr(
            "cognis.channels.adapters.signal_cli_runtime.shutil.which",
            lambda command: "/usr/bin/signal-cli",
        )

        async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
            return process

        monkeypatch.setattr(
            "cognis.channels.adapters.signal_cli_runtime.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        )

        runtime = SignalCliRuntime(account_number="+1234567890")

        with pytest.raises(SignalCliRuntimeError, match=r"returncode=9"):
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

    def test_exit_message_includes_stderr_tail_when_debug_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COGNIS_SIGNAL_STDIO_DEBUG", "true")
        runtime = SignalCliRuntime(account_number="+1234567890")
        process = FakeProcess()
        process.returncode = 9
        runtime._process = process
        runtime._stderr_line_count = 2
        runtime._stderr_tail.append("first failure line")
        runtime._stderr_tail.append("second failure line")

        message = runtime._process_exit_message()

        assert "stderr_tail=[" in message
        assert "first failure line" in message
        assert "second failure line" in message

    @pytest.mark.asyncio
    async def test_stderr_debug_logs_text_in_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COGNIS_SIGNAL_STDIO_DEBUG", "true")
        runtime = SignalCliRuntime(account_number="+1234567890")
        process = FakeProcess()
        process.stderr.readline = AsyncMock(side_effect=[b"java failure line\n", b""])
        runtime._process = process
        runtime._running = True

        warning = MagicMock()
        monkeypatch.setattr("cognis.channels.adapters.signal_cli_runtime.logger.warning", warning)

        await runtime._drain_stderr()

        assert any(
            call.args
            and call.args[0] == "signal-cli stderr: %s"
            and call.args[1] == "java failure line"
            for call in warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_read_stream_line_handles_limit_overrun(self) -> None:
        runtime = SignalCliRuntime(account_number="+1234567890")
        payload = json.dumps({"jsonrpc": "2.0", "result": {"ok": True}, "id": 1}).encode() + b"\n"

        line = await runtime._read_stream_line(_LimitOverrunStream(payload))

        assert json.loads(line) == {"jsonrpc": "2.0", "result": {"ok": True}, "id": 1}


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


class TestDirectSendBehavior:
    @pytest.mark.asyncio
    async def test_direct_send_ignores_reply_without_quote_author(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc"},
            {"account_number": "+1"},
        )
        adapter._account_number = "+1"

        mock_runtime = MagicMock()
        mock_runtime.is_running = True
        mock_runtime.single_account_mode = True
        mock_runtime.request = AsyncMock(return_value={"timestamp": 12345})
        adapter._runtime = mock_runtime

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="hello",
            reply_to_id="1700000000000",
        )

        result = await adapter._send_direct(message)

        assert result == "12345"
        called_params = mock_runtime.request.await_args.args[1]
        assert "quoteTimestamp" not in called_params

    @pytest.mark.asyncio
    async def test_direct_send_formats_markdown_into_text_styles(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc"},
            {"account_number": "+1"},
        )
        adapter._account_number = "+1"

        mock_runtime = MagicMock()
        mock_runtime.is_running = True
        mock_runtime.single_account_mode = True
        mock_runtime.request = AsyncMock(return_value={"timestamp": 12345})
        adapter._runtime = mock_runtime

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="# Heading\n\n**Bold** and `code`",
        )

        result = await adapter.send_message(message)

        assert result == "12345"
        called_params = mock_runtime.request.await_args.args[1]
        assert called_params["message"] == "Heading\n\nBold and code"
        assert called_params["textStyle"] == ["0:7:BOLD", "9:4:BOLD", "18:4:MONOSPACE"]

    @pytest.mark.asyncio
    async def test_direct_send_uses_inline_media_without_download(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc"},
            {"account_number": "+1"},
        )
        adapter._account_number = "+1"

        mock_runtime = MagicMock()
        mock_runtime.is_running = True
        mock_runtime.single_account_mode = True

        async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "send"
            assert params["attachment"]
            path = Path(params["attachment"][0])
            assert path.exists()
            assert path.suffix == ".pdf"
            assert path.read_bytes() == b"%PDF-1"
            return {"timestamp": 12345}

        mock_runtime.request = AsyncMock(side_effect=fake_request)
        adapter._runtime = mock_runtime
        adapter._temp_dir = tempfile.TemporaryDirectory(prefix="signal-test-")

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="",
            media=[
                MediaAttachment(
                    filename="report.pdf",
                    mime_type="application/pdf",
                    content_b64="JVBERi0x",
                )
            ],
        )

        result = await adapter.send_message(message)

        assert result == "12345"
        called_params = mock_runtime.request.await_args.args[1]
        assert "attachment" in called_params
        assert called_params["message"] == "\u200b"
        assert called_params["attachment"][0].endswith(".pdf")
        adapter._temp_dir.cleanup()

    @pytest.mark.asyncio
    async def test_rest_send_uses_placeholder_message_for_media_only(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"

        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"timestamp": "999"}
        response.raise_for_status.return_value = None
        client.post = AsyncMock(return_value=response)
        adapter._client = client

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="",
            media=[
                MediaAttachment(filename="image.jpg", mime_type="image/jpeg", content_b64="YWJj")
            ],
        )

        result = await adapter.send_message(message)

        assert result == "999"
        payload = client.post.await_args.kwargs["json"]
        assert payload["message"] == "\u200b"
        assert payload["base64_attachments"] == ["YWJj"]

    @pytest.mark.asyncio
    async def test_rest_send_normalizes_numeric_timestamp_to_string(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"

        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"timestamp": 999}
        response.raise_for_status.return_value = None
        client.post = AsyncMock(return_value=response)
        adapter._client = client

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="hello",
        )

        result = await adapter.send_message(message)

        assert result == "999"

    @pytest.mark.asyncio
    async def test_rest_send_rejects_malformed_timestamp(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"

        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"timestamp": {"bad": True}}
        response.raise_for_status.return_value = None
        client.post = AsyncMock(return_value=response)
        adapter._client = client

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="hello",
        )

        result = await adapter.send_message(message)

        assert result is None


class TestRestSendBehavior:
    @pytest.mark.asyncio
    async def test_rest_send_uses_signal_styled_text_mode(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"

        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"timestamp": "999"}
        response.raise_for_status.return_value = None
        client.post = AsyncMock(return_value=response)
        adapter._client = client

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="# Heading\n\n**Bold** and `code`",
        )

        result = await adapter.send_message(message)

        assert result == "999"
        payload = client.post.await_args.kwargs["json"]
        assert payload["message"] == "**Heading**\n\n**Bold** and `code`"
        assert payload["text_mode"] == "styled"

    @pytest.mark.asyncio
    async def test_rest_send_includes_explicit_preview_fields(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"

        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"timestamp": "999"}
        response.raise_for_status.return_value = None
        client.post = AsyncMock(return_value=response)
        adapter._client = client

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="https://example.com/image.png",
            platform_data={
                "signal_preview": {
                    "url": "https://example.com/image.png",
                    "title": "banner.png",
                    "image": "https://example.com/image.png",
                }
            },
        )

        result = await adapter.send_message(message)

        assert result == "999"
        payload = client.post.await_args.kwargs["json"]
        assert payload["preview_url"] == "https://example.com/image.png"
        assert payload["preview_title"] == "banner.png"
        assert payload["preview_image"] == "https://example.com/image.png"

    @pytest.mark.asyncio
    async def test_rest_send_only_applies_preview_to_first_chunk(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"
        adapter.capabilities.max_message_length = 12

        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"timestamp": "999"}
        response.raise_for_status.return_value = None
        client.post = AsyncMock(return_value=response)
        adapter._client = client

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="one two three four five six",
            platform_data={
                "signal_preview": {
                    "url": "https://example.com/image.png",
                    "title": "banner.png",
                    "image": "https://example.com/image.png",
                }
            },
        )

        result = await adapter.send_message(message)

        assert result == "999"
        assert client.post.await_count > 1
        first_payload = client.post.await_args_list[0].kwargs["json"]
        second_payload = client.post.await_args_list[1].kwargs["json"]
        assert first_payload["preview_url"] == "https://example.com/image.png"
        assert "preview_url" not in second_payload

    @pytest.mark.asyncio
    async def test_send_message_returns_none_when_first_media_chunk_has_no_id(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"
        adapter.capabilities.max_message_length = 12
        adapter._send_rest = AsyncMock(side_effect=[None, "later-id"])  # type: ignore[method-assign]

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="one two three four five six",
            media=[
                MediaAttachment(filename="image.jpg", mime_type="image/jpeg", content_b64="YWJj")
            ],
        )

        result = await adapter.send_message(message)

        assert result is None
        assert adapter._send_rest.await_count == 1

    @pytest.mark.asyncio
    async def test_send_message_keeps_media_chunk_id_when_later_chunk_has_no_id(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig({}, {"account_number": "+1"})
        adapter._account_number = "+1"
        adapter.capabilities.max_message_length = 12
        adapter._send_rest = AsyncMock(side_effect=["media-id", None, None, None])  # type: ignore[method-assign]

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="one two three four five six",
            media=[
                MediaAttachment(filename="image.jpg", mime_type="image/jpeg", content_b64="YWJj")
            ],
        )

        result = await adapter.send_message(message)

        assert result == "media-id"
        assert adapter._send_rest.await_count >= 2


class TestDirectPreviewBehavior:
    @pytest.mark.asyncio
    async def test_direct_send_includes_explicit_preview_fields(self) -> None:
        adapter = SignalAdapter()
        adapter._signal_config = _SignalConfig(
            {"transport": "direct_jsonrpc"},
            {"account_number": "+1"},
        )
        adapter._account_number = "+1"

        mock_runtime = MagicMock()
        mock_runtime.is_running = True
        mock_runtime.single_account_mode = True
        mock_runtime.request = AsyncMock(return_value={"timestamp": 12345})
        adapter._runtime = mock_runtime

        message = OutboundMessage(
            channel_type="signal",
            account_id="acct-1",
            chat_id="+420111222333",
            content="https://example.com/image.png",
            platform_data={
                "signal_preview": {
                    "url": "https://example.com/image.png",
                    "title": "banner.png",
                    "image": "https://example.com/image.png",
                }
            },
        )

        result = await adapter._send_direct(message)

        assert result == "12345"
        called_params = mock_runtime.request.await_args.args[1]
        assert called_params["previewUrl"] == "https://example.com/image.png"
        assert called_params["previewTitle"] == "banner.png"
        assert called_params["previewImage"] == "https://example.com/image.png"


class TestDirectRuntimeFatalFailures:
    @pytest.mark.asyncio
    async def test_run_direct_raises_non_retryable_on_unregistered_user(self) -> None:
        adapter = SignalAdapter()
        runtime = MagicMock()
        runtime.is_running = False
        runtime._process_exit_message.return_value = "signal-cli process exited unexpectedly (returncode=1, stderr_lines=1, stderr_tail=['User +447727940997 is not registered.'])"
        adapter._runtime = runtime
        adapter._stop_event.clear()

        with pytest.raises(NonRetryableChannelError, match="not registered"):
            await adapter._run_direct()
