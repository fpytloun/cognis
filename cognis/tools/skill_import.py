"""Skill import service with URL security policy.

Handles fetching skill content from URLs with SSRF protection,
size limits, and provenance tracking.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from cognis.logging import get_logger
from cognis.models.skill import ImportProvenance
from cognis.tools.skill_parser import (
    detect_format,
    parse_skill_content,
    resolve_github_url,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# URL security policy defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_FETCH_TIMEOUT_SECONDS = 30
DEFAULT_MAX_REDIRECTS = 5

# Private/reserved IP ranges to block (SSRF protection)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(host: str) -> bool:
    """Check if a hostname resolves to a private/reserved IP."""
    try:
        # Resolve hostname to IP addresses
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for info in infos:
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
            for network in _BLOCKED_NETWORKS:
                if ip in network:
                    return True
    except (socket.gaierror, ValueError):
        # If we can't resolve, block by default
        return True
    return False


def validate_import_url(url: str) -> str:
    """Validate and normalize an import URL.

    Raises ``ValueError`` on invalid or blocked URLs.
    Returns the normalized URL.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("https", "http"):
        raise ValueError("Only HTTPS and HTTP URLs are supported for skill import")

    host = parsed.hostname
    if not host:
        raise ValueError("URL must have a hostname")

    # Allow localhost for development
    if host in ("localhost", "127.0.0.1", "::1"):
        if parsed.scheme != "http":
            pass  # Allow http for localhost
        return url

    # Require HTTPS for non-localhost
    if parsed.scheme != "https":
        raise ValueError("HTTPS is required for non-localhost skill imports")

    # SSRF protection: block private IPs
    if _is_private_ip(host):
        raise ValueError(f"Import URL resolves to a private/reserved IP address: {host}")

    return url


async def fetch_skill_content(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> tuple[str, str]:
    """Fetch skill content from a URL.

    Returns (content, final_url) after following redirects.
    Raises ``ValueError`` on security policy violations or fetch errors.
    """
    validated_url = validate_import_url(url)

    # Resolve GitHub URLs to raw content
    raw_url, _sha = resolve_github_url(validated_url)

    # Follow redirects manually to validate each hop for SSRF
    current_url = raw_url
    response = None
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
    ) as client:
        for _hop in range(max_redirects + 1):
            try:
                response = await client.get(current_url)
            except httpx.RequestError as exc:
                raise ValueError(f"Failed to fetch skill from {current_url}: {exc}") from exc

            if response.is_redirect:
                redirect_url = str(response.headers.get("location", ""))
                if not redirect_url:
                    raise ValueError("Redirect without Location header")
                # Resolve relative redirects
                if redirect_url.startswith("/"):
                    parsed = urlparse(current_url)
                    redirect_url = f"{parsed.scheme}://{parsed.netloc}{redirect_url}"
                # Validate redirect target for SSRF
                try:
                    validate_import_url(redirect_url)
                except ValueError as exc:
                    raise ValueError(
                        f"Skill import redirected to a blocked URL: {redirect_url}"
                    ) from exc
                current_url = redirect_url
                continue

            response.raise_for_status()
            break
        else:
            raise ValueError(f"Too many redirects (max {max_redirects})")

    if response is None:
        raise ValueError("No response received")

    content_length = len(response.content)
    if content_length > max_bytes:
        max_mb = max_bytes / (1024 * 1024)
        raise ValueError(
            f"Skill content exceeds maximum size of {max_mb:.0f}MB ({content_length} bytes)"
        )

    final_url = current_url

    try:
        text_content = response.text
    except Exception as exc:
        raise ValueError(f"Failed to decode skill content: {exc}") from exc

    return text_content, final_url


async def import_skill_from_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> tuple[dict[str, object], ImportProvenance]:
    """Fetch and parse a skill from a URL.

    Returns (parsed_skill_data, provenance).
    """
    # Resolve GitHub URL and extract commit SHA
    _, commit_sha = resolve_github_url(url)

    fetched_content, final_url = await fetch_skill_content(
        url, max_bytes=max_bytes, timeout_seconds=timeout_seconds
    )

    # Detect format and parse
    fmt = detect_format(fetched_content)
    skill_data = parse_skill_content(fetched_content, format=fmt)

    # Compute import checksum
    import_checksum = hashlib.sha256(fetched_content.encode()).hexdigest()

    provenance = ImportProvenance(
        source_url=url,
        resolved_url=final_url if final_url != url else None,
        commit_sha=commit_sha,
        import_checksum=import_checksum,
        imported_at=datetime.now(UTC),
        import_format=fmt,
    )

    return skill_data, provenance
