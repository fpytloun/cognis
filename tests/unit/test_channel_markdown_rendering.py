import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.channels.adapters.discord import DiscordAdapter
from cognis.channels.formatting import split_message
from cognis.channels.markdown_rendering import (
    markdown_to_chat_text,
    markdown_to_discord_markdown,
    markdown_to_plain_text,
    markdown_to_slack_mrkdwn,
    markdown_to_telegram_html,
)
from cognis.models.channel import MediaAttachment, OutboundMessage

SHOWCASE = """# Heading

Normal **bold**, *italic*, ~~struck~~ and `code`.

[Link](https://example.com)

- Parent
  - Nested
- [ ] Pending
- [x] Done

| Column | Value |
| --- | --- |
| Text | 42 |
"""


def test_plain_renderer_preserves_structure_without_markdown_syntax() -> None:
    rendered = markdown_to_plain_text(SHOWCASE)

    assert "Heading" in rendered
    assert "Normal bold, italic, struck and code." in rendered
    assert "Link (https://example.com)" in rendered
    assert "• Parent" in rendered
    assert "  • Nested" in rendered
    assert "☐ Pending" in rendered
    assert "Column: Text · Value: 42" in rendered


def test_slack_renderer_uses_native_mrkdwn() -> None:
    rendered = markdown_to_slack_mrkdwn(SHOWCASE)

    assert "*Heading*" in rendered
    assert "*bold*" in rendered
    assert "_italic_" in rendered
    assert "~struck~" in rendered
    assert "<https://example.com|Link>" in rendered
    assert "*Column:* Text · *Value:* 42" in rendered


def test_slack_renderer_escapes_platform_control_tokens() -> None:
    rendered = markdown_to_slack_mrkdwn(
        "<!here> <!channel> <@USER> [safe](https://example.com?a=1&b=2)"
    )

    assert "<!here>" not in rendered
    assert "<!channel>" not in rendered
    assert "<@USER>" not in rendered
    assert "&lt;!here&gt;" in rendered
    assert "<https://example.com?a=1&b=2|safe>" in rendered


def test_slack_renderer_rejects_control_delimiters_in_link_destinations() -> None:
    rendered = markdown_to_slack_mrkdwn(
        '<a href="https://example.com/|">unsafe</a> &lt;!channel&gt;'
    )

    assert "<!channel>" not in rendered
    assert "unsafe" in rendered


def test_discord_renderer_uses_native_markdown() -> None:
    rendered = markdown_to_discord_markdown(SHOWCASE)

    assert "**Heading**" in rendered
    assert "**bold**" in rendered
    assert "*italic*" in rendered
    assert "~~struck~~" in rendered
    assert "[Link](https://example.com)" in rendered


def test_telegram_renderer_uses_supported_html_only() -> None:
    rendered = markdown_to_telegram_html(SHOWCASE)

    assert "<b>Heading</b>" in rendered
    assert "<b>bold</b>" in rendered
    assert "<i>italic</i>" in rendered
    assert "<s>struck</s>" in rendered
    assert '<a href="https://example.com">Link</a>' in rendered
    assert "<table>" not in rendered


def test_chat_renderer_uses_portable_native_markers() -> None:
    rendered = markdown_to_chat_text(SHOWCASE)

    assert "*Heading*" in rendered
    assert "*bold*" in rendered
    assert "_italic_" in rendered
    assert "~struck~" in rendered
    assert "Link (https://example.com)" in rendered


def test_table_shaped_content_inside_code_fence_is_not_linearized() -> None:
    source = "```text\n| raw | text |\n| --- | --- |\n```"

    assert "| raw | text |" in markdown_to_slack_mrkdwn(source)
    assert "*raw:*" not in markdown_to_slack_mrkdwn(source)


def test_markdown_splitter_preserves_links_fences_and_table_headers() -> None:
    source = """[runbook](https://example.com/really/long/path)

```python
print("one")
print("two")
print("three")
```

| Name | State |
| --- | --- |
| api | healthy |
| worker | healthy |
"""
    chunks = split_message(source, 80)

    assert all(len(chunk) <= 80 for chunk in chunks)
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
    assert sum("[runbook](https://example.com/really/long/path)" in chunk for chunk in chunks) == 1
    table_chunks = [chunk for chunk in chunks if "| Name | State |" in chunk]
    assert table_chunks
    assert all("| --- | --- |" in chunk for chunk in table_chunks)


@pytest.mark.asyncio
async def test_discord_adapter_splits_after_native_markdown_expansion() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(dict(json.loads(request.content)))
        return httpx.Response(200, json={"id": f"message-{len(payloads)}"})

    adapter = DiscordAdapter()
    adapter._rest_client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://discord.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        message_id = await adapter.send_message(
            OutboundMessage(
                channel_type="discord",
                account_id="discord-account",
                chat_id="channel",
                content="# " + ("x" * adapter.capabilities.max_message_length),
            )
        )
    finally:
        await adapter._rest_client.aclose()  # noqa: SLF001

    assert message_id == f"message-{len(payloads)}"
    assert len(payloads) == 2
    assert all(
        len(str(payload["content"])) <= adapter.capabilities.max_message_length
        for payload in payloads
    )
    assert all(payload["allowed_mentions"] == {"parse": []} for payload in payloads)


@pytest.mark.asyncio
async def test_discord_adapter_aborts_after_failed_attachment_chunk() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "unexpected"})

    adapter = DiscordAdapter()
    adapter._rest_client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://discord.example",
        transport=httpx.MockTransport(handler),
    )
    adapter._send_with_attachments = AsyncMock(return_value=None)  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="attachment delivery returned no message ID"):
            await adapter.send_message(
                OutboundMessage(
                    channel_type="discord",
                    account_id="discord-account",
                    chat_id="channel",
                    content="# " + ("x" * adapter.capabilities.max_message_length),
                    media=[MediaAttachment(url="https://example.com/image.png")],
                )
            )
    finally:
        await adapter._rest_client.aclose()  # noqa: SLF001

    assert requests == []
