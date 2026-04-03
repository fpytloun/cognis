"""Channel adapter system for multi-platform messaging.

Channels bridge external messaging platforms (Signal, WhatsApp, Telegram,
Discord, Slack, etc.) into the Cognis conversation model.  Inbound messages
are routed through TurnScheduler; outbound responses flow back through the
channel adapter.

Architecture:
- ``protocol.py`` — ChannelAdapter Protocol + base class
- ``registry.py`` — Known channel types and their metadata
- ``manager.py`` — Lifecycle orchestration (start/stop/reconnect)
- ``inbound.py`` — Inbound message processing pipeline
- ``delivery.py`` — Outbound delivery service (EventBus → channel)
- ``formatting.py`` — Message formatting and splitting
- ``adapters/`` — Concrete adapter implementations
"""
