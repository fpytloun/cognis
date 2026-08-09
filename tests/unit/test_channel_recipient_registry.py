from __future__ import annotations

from cognis.channels.registry import CHANNEL_REGISTRY


def test_registered_channel_recipient_capabilities_are_complete() -> None:
    expected = {
        "signal": (
            {"signal_e164", "signal_uuid", "signal_group_id"},
            {"direct", "group"},
            False,
            False,
        ),
        "whatsapp": ({"whatsapp_e164"}, {"direct"}, False, False),
        "telegram": (
            {"telegram_chat_id", "telegram_public_username"},
            {"direct", "group"},
            True,
            False,
        ),
        "discord": (
            {"discord_channel_id", "discord_user_id"},
            {"direct", "group"},
            False,
            True,
        ),
        "slack": (
            {"slack_conversation_id", "slack_user_id"},
            {"direct", "group"},
            True,
            True,
        ),
        "matrix": (
            {"matrix_room_id", "matrix_room_alias", "matrix_user_id"},
            {"direct", "group"},
            True,
            True,
        ),
        "irc": ({"irc_nick", "irc_channel"}, {"direct", "group"}, False, False),
        "google_chat": (
            {"google_chat_space", "google_workspace_user"},
            {"direct", "group"},
            True,
            False,
        ),
        "bluebubbles": (
            {"bluebubbles_chat_guid", "imessage_handle"},
            {"direct", "group"},
            True,
            False,
        ),
    }

    assert set(CHANNEL_REGISTRY) == set(expected)
    for channel_type, (address_kinds, chat_kinds, resolves, creates) in expected.items():
        capabilities = CHANNEL_REGISTRY[channel_type].capabilities.recipient_capabilities
        assert set(capabilities.address_kinds) == address_kinds
        assert set(capabilities.chat_kinds) == chat_kinds
        assert capabilities.supports_resolution is resolves
        assert capabilities.supports_creation is creates
