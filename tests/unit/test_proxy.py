from __future__ import annotations

from types import SimpleNamespace

from cognis.api.proxy import trusted_request_scheme


def _connection(*, peer: str, forwarded: str, trusted_cidrs: tuple[str, ...]) -> object:
    return SimpleNamespace(
        url=SimpleNamespace(scheme="http"),
        client=SimpleNamespace(host=peer),
        headers={"x-forwarded-proto": forwarded},
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(trusted_proxy_cidrs=trusted_cidrs))
        ),
    )


def test_untrusted_peer_cannot_override_request_scheme() -> None:
    connection = _connection(
        peer="203.0.113.20",
        forwarded="https",
        trusted_cidrs=("10.0.0.0/8",),
    )
    assert trusted_request_scheme(connection) == "http"


def test_trusted_peer_can_supply_sanitized_request_scheme() -> None:
    connection = _connection(
        peer="10.1.2.3",
        forwarded="https",
        trusted_cidrs=("10.0.0.0/8",),
    )
    assert trusted_request_scheme(connection) == "https"
