"""Shared access-token authentication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.store.queries import get_user


@dataclass
class AuthenticatedUser:
    email: str
    role: str
    name: str | None = None
    auth_type: str = "jwt"


class AccessTokenAuthenticationError(ValueError):
    """An access token cannot authenticate a current local user."""

    def __init__(self, message: str, *, account_disabled: bool = False) -> None:
        super().__init__(message)
        self.account_disabled = account_disabled


async def authenticate_access_token(
    *,
    token: str,
    auth_provider: Any,
    session_factory: async_sessionmaker[Any],
) -> tuple[AuthenticatedUser, dict[str, Any]]:
    """Validate an access JWT and resolve its identity from current database state."""

    try:
        claims = auth_provider.verify_jwt(token, audience=["cognis"])
    except Exception as exc:
        raise AccessTokenAuthenticationError("Invalid or expired token") from exc
    if claims.get("typ") != "access":
        raise AccessTokenAuthenticationError("Invalid token type")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AccessTokenAuthenticationError("Missing token subject")
    async with session_factory() as session:
        user = await get_user(session, subject)
    if user is None:
        raise AccessTokenAuthenticationError("Unknown user")
    if not user.is_active:
        raise AccessTokenAuthenticationError("Account disabled", account_disabled=True)
    token_auth_version = claims.get("authv", 0)
    if (
        not isinstance(token_auth_version, int)
        or isinstance(token_auth_version, bool)
        or token_auth_version != user.auth_version
    ):
        raise AccessTokenAuthenticationError("Revoked token")
    return (
        AuthenticatedUser(
            email=user.email,
            role=user.role,
            name=user.name,
            auth_type="jwt",
        ),
        claims,
    )
