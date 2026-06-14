import pytest

from cognis.channels.adapters.signal import SignalAdapter, _SignalConfig
from cognis.channels.adapters.signal_cli_runtime import SignalCliRuntime
from cognis.models.channel import ChannelAccountConfig


def test_signal_config_defaults_direct_receive_mode_to_on_start() -> None:
    config = _SignalConfig(
        {"transport": "direct_jsonrpc"},
        {"account_number": "+10000000000"},
    )

    assert config.signal_cli_receive_mode == "on-start"


def test_signal_config_normalizes_invalid_receive_mode() -> None:
    config = _SignalConfig(
        {"transport": "direct_jsonrpc", "receive_mode": "invalid"},
        {"account_number": "+10000000000"},
    )

    assert config.signal_cli_receive_mode == "on-start"


def test_signal_runtime_passes_receive_mode_to_jsonrpc_process() -> None:
    runtime = SignalCliRuntime(
        account_number="+10000000000",
        command="signal-cli",
        trust_mode="on-first-use",
        receive_mode="on-connection",
    )

    assert runtime._build_args("/usr/bin/signal-cli") == [
        "/usr/bin/signal-cli",
        "--trust-new-identities",
        "on-first-use",
        "-a",
        "+10000000000",
        "jsonRpc",
        "--receive-mode",
        "on-connection",
    ]


def test_signal_runtime_normalizes_invalid_receive_mode() -> None:
    runtime = SignalCliRuntime(
        account_number="+10000000000",
        receive_mode="invalid",
    )

    assert runtime._build_args("/usr/bin/signal-cli")[-2:] == [
        "--receive-mode",
        "on-start",
    ]


def test_signal_runtime_defaults_receive_mode_to_on_start() -> None:
    runtime = SignalCliRuntime(
        account_number="+10000000000",
    )

    assert runtime._build_args("/usr/bin/signal-cli")[-2:] == [
        "--receive-mode",
        "on-start",
    ]


def _started_adapter() -> tuple[SignalAdapter, list[object]]:
    adapter = SignalAdapter()
    adapter._config = ChannelAccountConfig(
        account_id="signal-main",
        channel_type="signal",
        display_name="Signal",
        agent_id="riker",
        user_email="user@example.com",
    )
    adapter._account_number = "+10000000000"
    messages: list[object] = []

    async def on_message(message: object) -> None:
        messages.append(message)

    adapter._on_message = on_message
    return adapter, messages


@pytest.mark.asyncio
async def test_signal_direct_notification_dispatches_data_message() -> None:
    adapter, messages = _started_adapter()

    await adapter._handle_direct_notification(
        {
            "envelope": {
                "source": "+12223334444",
                "sourceName": "Filip",
                "dataMessage": {
                    "timestamp": 1234567890000,
                    "message": "hello",
                },
            }
        }
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.sender_id == "+12223334444"
    assert message.chat_id == "+12223334444"
    assert message.content == "hello"


@pytest.mark.asyncio
async def test_signal_direct_notification_dispatches_sync_sent_message() -> None:
    adapter, messages = _started_adapter()

    await adapter._handle_direct_notification(
        {
            "envelope": {
                "timestamp": 1234567890000,
                "syncMessage": {
                    "sentMessage": {
                        "destination": "+12223334444",
                        "destinationName": "Filip",
                        "timestamp": 1234567890000,
                        "message": "from linked device",
                    }
                },
            }
        }
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.sender_id == "+10000000000"
    assert message.chat_id == "+12223334444"
    assert message.chat_name == "Filip"
    assert message.content == "from linked device"


@pytest.mark.asyncio
async def test_signal_direct_notification_ignores_unsupported_shapes() -> None:
    adapter, messages = _started_adapter()

    await adapter._handle_direct_notification(
        {
            "envelope": {
                "timestamp": 1234567890000,
                "receiptMessage": {"when": 1234567890000},
            }
        }
    )

    assert messages == []
