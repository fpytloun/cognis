"""IRC adapter via asyncio TCP connection.

Implements a basic IRC client using raw asyncio TCP streams.
Supports TLS, NickServ authentication, and multi-channel.

Required credentials:
- server: IRC server hostname
- port: IRC server port
- nickname: Bot nickname
- password: NickServ or server password (optional)
"""

from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime

from cognis.channels.markdown_rendering import markdown_to_plain_text
from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import IRC_META
from cognis.logging import get_logger
from cognis.models.channel import (
    AgentProfile,
    ChannelCapabilities,
    InboundMessage,
    OutboundMessage,
)

logger = get_logger(__name__)


class IRCAdapter(BaseChannelAdapter):
    """IRC adapter via asyncio TCP."""

    channel_type = "irc"
    capabilities: ChannelCapabilities = IRC_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._server: str = ""
        self._port: int = 6697
        self._nickname: str = ""
        self._password: str = ""
        self._channels: list[str] = []
        self._use_tls: bool = True

    async def _connect(self) -> None:
        """Connect to IRC server."""
        self._server = self._credentials.get("server", "")
        self._port = int(self._credentials.get("port", "6697"))
        self._nickname = self._credentials.get("nickname", "")
        self._password = self._credentials.get("password", "")
        self._use_tls = self._credentials.get("use_tls", "true").lower() in {"true", "1", "yes"}
        channels_str = self._credentials.get("channels", "")
        self._channels = [c.strip() for c in channels_str.split(",") if c.strip()]

        if not self._server:
            msg = "IRC adapter requires server credential"
            raise ValueError(msg)
        if not self._nickname:
            msg = "IRC adapter requires nickname credential"
            raise ValueError(msg)

        ssl_context = ssl.create_default_context() if self._use_tls else None

        self._reader, self._writer = await asyncio.open_connection(
            self._server,
            self._port,
            ssl=ssl_context,
        )

        # IRC registration
        if self._password:
            self._send_raw(f"PASS {self._password}")
        self._send_raw(f"NICK {self._nickname}")
        self._send_raw(f"USER {self._nickname} 0 * :Cognis Bot")

        # Wait for welcome message (001)
        while True:
            line = await self._read_line()
            if line is None:
                msg = "IRC connection closed during registration"
                raise ConnectionError(msg)
            if " 001 " in line:
                break
            if "ERROR" in line:
                msg = f"IRC registration failed: {line}"
                raise ConnectionError(msg)

        # Join channels
        for channel in self._channels:
            self._send_raw(f"JOIN {channel}")

    async def _disconnect(self) -> None:
        if self._writer is not None:
            try:
                self._send_raw("QUIT :Cognis shutting down")
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def _run(self) -> None:
        """Read and process IRC messages."""
        while not self._stop_event.is_set():
            line = await self._read_line()
            if line is None:
                msg = "IRC connection closed"
                raise ConnectionError(msg)

            # Handle PING/PONG
            if line.startswith("PING"):
                self._send_raw(f"PONG {line[5:]}")
                continue

            await self._handle_line(line)

    async def sync_profile(self, profile: AgentProfile) -> None:
        import re

        raw = (profile.effective_name or "")[:9]
        sanitized = re.sub(r"[^A-Za-z0-9\-\[\]\\^{|}~]", "", raw)
        if sanitized and sanitized[0].isdigit():
            sanitized = f"_{sanitized}"[:9]
        if not sanitized:
            return
        if sanitized != raw:
            logger.warning(
                "irc adapter: nickname sanitized",
                extra={"extra_data": {"original": raw, "sanitized": sanitized}},
            )
        if sanitized != self._nickname and self._writer is not None:
            self._send_raw(f"NICK {sanitized}")
            self._nickname = sanitized
            logger.info(
                "irc adapter: nickname synced",
                extra={"extra_data": {"account_id": self.account_id, "nickname": sanitized}},
            )

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message to an IRC channel or user."""
        if self._writer is None:
            return None

        target = message.chat_id
        max_content = 400

        # Text fallback for media (IRC has no native media support)
        for media in message.media:
            note = f"[attachment: {media.filename or 'file'}]"
            if media.url:
                note += f" {media.url}"
            self._send_raw(f"PRIVMSG {target} :{note[:max_content]}")

        lines = markdown_to_plain_text(message.content).split("\n")
        for line in lines:
            while line:
                chunk = line[:max_content]
                line = line[max_content:]
                self._send_raw(f"PRIVMSG {target} :{chunk}")

        return None  # IRC doesn't have message IDs

    # ------------------------------------------------------------------
    # IRC protocol helpers
    # ------------------------------------------------------------------

    def _send_raw(self, line: str) -> None:
        """Send a raw IRC line."""
        if self._writer is not None:
            self._writer.write(f"{line}\r\n".encode())

    async def _read_line(self) -> str | None:
        """Read a single IRC line."""
        if self._reader is None:
            return None
        try:
            data = await asyncio.wait_for(self._reader.readline(), timeout=300)
            if not data:
                return None
            return data.decode("utf-8", errors="replace").strip()
        except TimeoutError:
            # Send a PING to keep connection alive
            self._send_raw("PING :keepalive")
            return ""

    async def _handle_line(self, line: str) -> None:
        """Process an IRC message line."""
        # Parse IRC message format: :prefix COMMAND params :trailing
        if not line.startswith(":"):
            return

        parts = line[1:].split(" ", 3)
        if len(parts) < 3:
            return

        prefix = parts[0]
        command = parts[1]

        if command != "PRIVMSG":
            return

        target = parts[2]
        content = (
            parts[3][1:]
            if len(parts) > 3 and parts[3].startswith(":")
            else parts[3]
            if len(parts) > 3
            else ""
        )

        # Extract sender info
        sender_nick = prefix.split("!")[0] if "!" in prefix else prefix
        sender_user = prefix.split("!")[1].split("@")[0] if "!" in prefix else ""

        # Determine chat type
        chat_type = "group" if target.startswith(("#", "&")) else "direct"
        chat_id = target if chat_type == "group" else sender_nick

        # Check for mention
        was_mentioned = self._nickname.lower() in content.lower()

        message = InboundMessage(
            channel_type="irc",
            account_id=self.account_id,
            message_id="",  # IRC doesn't have message IDs
            sender_id=sender_nick,
            sender_name=sender_nick,
            sender_username=sender_user,
            chat_id=chat_id,
            chat_type=chat_type,
            content=content,
            was_mentioned=was_mentioned,
            timestamp=datetime.now(UTC),
            platform_data={"raw_line": line},
        )

        await self._dispatch_inbound(message)
