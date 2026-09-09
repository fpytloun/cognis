"""Pure channel address metadata shared by controller and executor code."""

from __future__ import annotations

ADDRESS_KINDS: dict[str, tuple[str, ...]] = {
    "signal": ("signal_e164", "signal_uuid", "signal_group_id"),
    "whatsapp": ("whatsapp_e164",),
    "telegram": ("telegram_chat_id", "telegram_public_username"),
    "discord": ("discord_channel_id", "discord_user_id"),
    "slack": ("slack_conversation_id", "slack_user_id"),
    "matrix": ("matrix_room_id", "matrix_room_alias", "matrix_user_id"),
    "irc": ("irc_nick", "irc_channel"),
    "google_chat": ("google_chat_space", "google_workspace_user"),
    "bluebubbles": ("bluebubbles_chat_guid", "imessage_handle"),
}
