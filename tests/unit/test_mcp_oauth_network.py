from __future__ import annotations

import base64
import json
import socket
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpcore
import httpx
import pytest

from cognis.config import load_config, mcp_oauth_trusted_destinations
from cognis.core.mcp_oauth import MCPOAuthError, MCPOAuthService, _safe_url
from cognis.core.mcp_oauth_network import OAuthDestinationTransport, resolve_oauth_destination

ENV = "COGNIS_MCP_OAUTH_TRUSTED_DESTINATIONS"
HOST = "oauth-gateway.example.test"
URL = f"https://{HOST}/mcp"
IP = "10.73.5.19"


@pytest.fixture(autouse=True)
def clean_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


def trust(monkeypatch: pytest.MonkeyPatch, port: int = 443) -> None:
    monkeypatch.setenv(ENV, json.dumps({f"{HOST}:{port}": [f"{IP}/32"]}))


def dns(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
        ],
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "null",
        "[]",
        "true",
        "{",
        '{"oauth-gateway.example.test:443":["10.73.5.19/32"],"oauth-gateway.example.test:443":["10.0.0.1/32"]}',
        '{"*.example.test:443":["10.73.5.19/32"]}',
        '{"https://oauth-gateway.example.test:443":["10.73.5.19/32"]}',
        '{"oauth-gateway.example.test":["10.73.5.19/32"]}',
        '{"oauth-gateway.example.test:0":["10.73.5.19/32"]}',
        '{"OAUTH-GATEWAY.example.test:443":["10.73.5.19/32"]}',
        '{"oauth-gateway.example.test.:443":["10.73.5.19/32"]}',
        '{"10.73.5.19:443":["10.73.5.19/32"]}',
        '{"oauth-gateway.example.test:443/path":["10.73.5.19/32"]}',
        '{"user@oauth-gateway.example.test:443":["10.73.5.19/32"]}',
        '{"oauth-gateway.example.test:443":[]}',
        '{"oauth-gateway.example.test:443":"10.73.5.19/32"}',
    ],
)
def test_invalid_settings_fail_closed(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV, raw)
    with pytest.raises(ValueError, match=ENV):
        mcp_oauth_trusted_destinations()


@pytest.mark.parametrize(
    "cidr",
    [
        "0.0.0.0/0",
        "192.168.0.0/16",
        "10.0.0.0/8",
        "10.73.5.19/24",
        "169.254.169.254/32",
        "127.0.0.1/32",
        "224.0.0.1/32",
        "8.8.8.8/32",
        "::/0",
        "fd00::/64",
        "fe80::1/128",
        "::1/128",
        "::ffff:10.73.5.19/128",
        "10.73.5.19",
        None,
    ],
)
def test_broad_or_special_networks_rejected(monkeypatch: pytest.MonkeyPatch, cidr: str) -> None:
    monkeypatch.setenv(ENV, json.dumps({f"{HOST}:443": [cidr]}))
    with pytest.raises(ValueError, match=ENV):
        mcp_oauth_trusted_destinations()


def test_startup_configuration_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "[]")
    with pytest.raises(ValueError, match=ENV):
        load_config()


def test_default_deny_and_exact_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    dns(monkeypatch, IP)
    with pytest.raises(MCPOAuthError, match="private"):
        _safe_url(URL)
    trust(monkeypatch)
    assert _safe_url(URL) == URL
    assert resolve_oauth_destination(URL) == IP


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}:8443/mcp",
        f"https://{HOST}:0/mcp",
        f"http://{HOST}/mcp",
        f"https://sub.{HOST}/mcp",
        "https://other.example.test/mcp",
        f"https://{HOST}./mcp",
        f"https://user@{HOST}/mcp",
        f"https://{HOST}/mcp#fragment",
        f"https://{IP}/mcp",
    ],
)
def test_exception_requires_exact_origin(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    trust(monkeypatch)
    dns(monkeypatch, IP)
    with pytest.raises(ValueError):
        resolve_oauth_destination(url)


@pytest.mark.parametrize("address", ["10.73.5.20", "169.254.169.254", "127.0.0.1", "8.8.8.8"])
def test_all_dns_answers_must_match(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    trust(monkeypatch)
    dns(monkeypatch, IP, address)
    with pytest.raises(ValueError, match="outside"):
        resolve_oauth_destination(URL)


def test_nondefault_port_and_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, json.dumps({f"{HOST}:8443": ["fd00::208/128"]}))
    dns(monkeypatch, "fd00::208")
    assert resolve_oauth_destination(f"https://{HOST}:8443/token") == "fd00::208"
    with pytest.raises(ValueError):
        resolve_oauth_destination(URL)


def test_public_default_and_loopback_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    dns(monkeypatch, "8.8.8.8")
    assert resolve_oauth_destination("https://public.example.test/") == "8.8.8.8"
    assert resolve_oauth_destination("http://127.0.0.1:8080/") == "127.0.0.1"
    dns(monkeypatch, "127.0.0.1")
    with pytest.raises(ValueError, match="loopback aliases"):
        resolve_oauth_destination(URL)
    with pytest.raises(ValueError, match="https"):
        resolve_oauth_destination("http://127.0.0.1/", allow_http_localhost=False)


class WireStream(httpcore.AsyncNetworkStream):
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.tls: list[tuple[ssl.SSLContext, str | None]] = []
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.tls.append((ssl_context, server_hostname))
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


@pytest.mark.asyncio
async def test_real_httpx_stack_pins_ip_preserves_tls_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust(monkeypatch, 8443)
    dns(monkeypatch, IP)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    wire = WireStream()
    connections = []

    async def connect(self, host, port, **kwargs):
        connections.append((host, port))
        return wire

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(transport=OAuthDestinationTransport(), trust_env=False) as client:
        response = await client.post(
            f"https://{HOST}:8443/token", data={"grant_type": "refresh_token"}
        )
    assert response.url == f"https://{HOST}:8443/token"
    assert connections == [(IP, 8443)]
    context, sni = wire.tls[0]
    assert sni == HOST
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert f"Host: {HOST}:8443\r\n".encode() in b"".join(wire.writes)
    assert wire.closed


@pytest.mark.asyncio
async def test_rebinding_between_preflight_and_connect_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust(monkeypatch)
    dns(monkeypatch, IP)
    _safe_url(URL)
    dns(monkeypatch, "169.254.169.254")
    async with httpx.AsyncClient(transport=OAuthDestinationTransport(), trust_env=False) as client:
        with pytest.raises(httpx.ConnectError, match="outside"):
            await client.get(URL)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["/metadata", "https://other.example.test/metadata"])
async def test_metadata_redirects_use_guard(
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    tmp_path: Path,
) -> None:
    trust(monkeypatch)
    dns(monkeypatch, IP)
    seen = []

    async def handle(self, request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, json={"issuer": f"https://{HOST}"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    key = tmp_path / "key"
    key.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    service = MCPOAuthService(
        session_factory=None,
        key_path=str(key),
        public_base_url="https://cognis.example",
    )
    if location.startswith("/"):
        assert await service._fetch_json(URL) == {"issuer": f"https://{HOST}"}
        assert len(seen) == 2
        assert all(request.url.host == IP for request in seen)
    else:
        with pytest.raises(MCPOAuthError, match="private"):
            await service._fetch_json(URL)
        assert len(seen) == 1


@pytest.fixture
def service(tmp_path: Path) -> MCPOAuthService:
    key = tmp_path / "key"
    key.write_bytes(base64.urlsafe_b64encode(b"0" * 32))
    return MCPOAuthService(
        session_factory=None,
        key_path=str(key),
        public_base_url="https://cognis.example",
    )


async def credential_request(service: MCPOAuthService, operation: str) -> Any:
    common = {"client_id": "test-client", "client_secret": None, "resource": URL}
    if operation == "registration":
        return await service._register_dynamic_client(
            registration_endpoint=URL,
            redirect_uri="https://cognis.example/callback",
            scopes=[],
            client_metadata_document_url=None,
        )
    if operation == "device":
        return await service._request_device_authorization(device_endpoint=URL, scopes=[], **common)
    if operation == "device_token":
        return await service._exchange_device_code(
            token_endpoint=URL,
            device_code="test-device-code",
            **common,
        )
    if operation == "code":
        return await service._exchange_code(
            token_endpoint=URL,
            code="test-code",
            code_verifier="test-verifier",
            redirect_uri="https://cognis.example/callback",
            **common,
        )
    return await service._refresh_token(token_endpoint=URL, refresh_token="test-refresh", **common)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["registration", "device", "device_token", "code", "refresh"])
@pytest.mark.parametrize("trusted", [False, True])
async def test_all_credential_paths_enforce_guard(
    monkeypatch: pytest.MonkeyPatch,
    service: MCPOAuthService,
    operation: str,
    trusted: bool,
) -> None:
    dns(monkeypatch, IP)
    if trusted:
        trust(monkeypatch)
    seen = []

    async def handle(self, request):
        seen.append(request)
        return httpx.Response(200, json={"access_token": "test-access", "client_id": "test-client"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    if not trusted:
        with pytest.raises((MCPOAuthError, httpx.ConnectError)):
            await credential_request(service, operation)
        assert not seen
    else:
        await credential_request(service, operation)
        assert len(seen) == 1 and seen[0].url.host == IP
        if operation != "registration":
            data = parse_qs((await seen[0].aread()).decode())
            assert data["resource"] == [URL]
            if operation == "code":
                assert data["code_verifier"] == ["test-verifier"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["registration", "device", "device_token", "code", "refresh"])
async def test_credential_redirects_never_follow(
    monkeypatch: pytest.MonkeyPatch,
    service: MCPOAuthService,
    operation: str,
) -> None:
    trust(monkeypatch)
    dns(monkeypatch, IP)
    seen = []

    async def handle(self, request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://other.fpy.cz/token"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    with pytest.raises(MCPOAuthError):
        await credential_request(service, operation)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_rebinding_after_first_request_cannot_reuse_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust(monkeypatch)
    dns(monkeypatch, IP)
    seen = []

    async def handle(self, request):
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    async with httpx.AsyncClient(transport=OAuthDestinationTransport(), trust_env=False) as client:
        await client.get(URL)
        dns(monkeypatch, "192.168.33.209")
        with pytest.raises(httpx.ConnectError, match="outside"):
            await client.get(URL)
    assert len(seen) == 1


@pytest.mark.parametrize("addresses", [[], ["8.8.8.8", "192.168.33.208"]])
def test_default_empty_or_mixed_dns_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    addresses: list[str],
) -> None:
    dns(monkeypatch, *addresses)
    with pytest.raises(ValueError):
        resolve_oauth_destination(URL)
