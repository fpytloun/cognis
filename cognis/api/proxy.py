"""Trusted reverse-proxy request metadata."""

from __future__ import annotations

import ipaddress
from typing import Any


def trusted_request_scheme(connection: Any) -> str:
    """Return the direct scheme unless the immediate peer is a trusted proxy."""

    url_scheme = str(connection.url.scheme).lower()
    scheme = "https" if url_scheme == "wss" else "http" if url_scheme == "ws" else url_scheme
    client_host = connection.client.host if connection.client is not None else ""
    try:
        peer_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return scheme
    trusted_cidrs = getattr(connection.app.state.config, "trusted_proxy_cidrs", ())
    trusted_networks = tuple(ipaddress.ip_network(cidr, strict=False) for cidr in trusted_cidrs)
    if not any(peer_ip in network for network in trusted_networks):
        return scheme
    forwarded = connection.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return forwarded if forwarded in {"http", "https"} else scheme
