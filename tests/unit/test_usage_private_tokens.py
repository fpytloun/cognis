"""Private usage refs/cursors authenticate and hide restricted correlations."""

from __future__ import annotations

import base64

import pytest

from cognis.core.usage.query_cursors import UsageCursors
from cognis.core.usage.query_service_errors import UsageQueryError


def test_private_tokens_encrypt_expire_and_bind_query(monkeypatch):
    monkeypatch.setattr("cognis.core.usage.query_cursors.time.time", lambda: 100)
    cursors = UsageCursors(bytes(range(32)))
    token = cursors.encode("query-scope", ["private-client", "private-request", None])
    decoded = base64.urlsafe_b64decode(token[3:] + "=" * (-len(token[3:]) % 4))
    assert b"private-client" not in decoded and b"private-request" not in decoded
    assert cursors.decode(token, "query-scope") == ["private-client", "private-request", None]
    assert cursors.encode("query-scope", ["private-client"]) != token
    with pytest.raises(UsageQueryError):
        cursors.decode(token, "different-scope")
    with pytest.raises(UsageQueryError):
        UsageCursors(bytes(range(1, 33))).decode(token, "query-scope")
    with pytest.raises(UsageQueryError):
        cursors.decode(token[:8] + ("A" if token[8] != "A" else "B") + token[9:], "query-scope")
    with pytest.raises(UsageQueryError):
        cursors.decode(token + "=", "query-scope")
    monkeypatch.setattr("cognis.core.usage.query_cursors.time.time", lambda: 3700)
    with pytest.raises(UsageQueryError):
        cursors.decode(token, "query-scope")
