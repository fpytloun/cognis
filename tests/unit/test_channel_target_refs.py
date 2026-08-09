from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from cognis.channels.target_refs import (
    ChannelTargetRef,
    ChannelTargetRefCodec,
    ChannelTargetRefError,
)


def _target() -> ChannelTargetRef:
    return ChannelTargetRef(
        kind="target",
        user_email="owner@example.com",
        account_id="account-private",
        channel_type="signal",
        chat_id="+420111222333",
        chat_kind="direct",
    )


def test_target_ref_round_trip_is_opaque_and_user_scoped() -> None:
    codec = ChannelTargetRefCodec("stable-application-secret")
    token = codec.encode(_target(), now=datetime(2026, 8, 2, tzinfo=UTC))

    decoded_bytes = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert b"account-private" not in decoded_bytes
    assert b"+420111222333" not in decoded_bytes
    assert (
        codec.decode(
            token,
            user_email="owner@example.com",
            expected_kind="target",
            now=datetime(2026, 8, 2, 1, tzinfo=UTC),
        )
        == _target()
    )

    with pytest.raises(ChannelTargetRefError, match="not available"):
        codec.decode(
            token,
            user_email="other@example.com",
            expected_kind="target",
            now=datetime(2026, 8, 2, 1, tzinfo=UTC),
        )


def test_target_ref_rejects_tampering_expiry_and_secret_rotation() -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    codec = ChannelTargetRefCodec("stable-application-secret", ttl_seconds=60)
    token = codec.encode(_target(), now=now)
    replacement = "A" if token[-1] != "A" else "B"

    with pytest.raises(ChannelTargetRefError, match="Invalid"):
        codec.decode(
            token[:-1] + replacement,
            user_email="owner@example.com",
            expected_kind="target",
            now=now,
        )
    with pytest.raises(ChannelTargetRefError, match="expired"):
        codec.decode(
            token,
            user_email="owner@example.com",
            expected_kind="target",
            now=now + timedelta(seconds=60),
        )
    with pytest.raises(ChannelTargetRefError, match="Invalid"):
        ChannelTargetRefCodec("rotated-application-secret").decode(
            token,
            user_email="owner@example.com",
            expected_kind="target",
            now=now,
        )
