"""JWT auth provider using ES256."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jose import JWTError, jwt

from cognis.models.config import ProviderHealth


def _b64url_uint(value: int) -> str:
    data = value.to_bytes(32, "big")
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class JWTAuthProvider:
    """Sign and verify user, refresh, service, and exchange tokens."""

    def __init__(
        self, private_key_path: Path, public_key_path: Path, token_ttl_seconds: int = 3600
    ) -> None:
        self.private_key_path = private_key_path
        self.public_key_path = public_key_path
        self.token_ttl_seconds = token_ttl_seconds
        self.refresh_ttl_seconds = 7 * 24 * 60 * 60
        self.exchange_ttl_seconds = 60
        self._revoked_jtis: set[str] = set()
        self._used_exchange_jtis: set[str] = set()

        self._private_key = private_key_path.read_text(encoding="utf-8")
        self._public_key = public_key_path.read_text(encoding="utf-8")
        self._kid = hashlib.sha256(self._public_key.encode()).hexdigest()[:16]

    def _sign(self, claims: dict[str, Any], expires_in: int) -> str:
        now = datetime.now(UTC)
        payload = {
            **claims,
            "iss": "cognis",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(payload, self._private_key, algorithm="ES256", headers={"kid": self._kid})

    def sign_access_token(self, subject: str, name: str | None, role: str) -> str:
        return self._sign(
            {"sub": subject, "name": name, "role": role, "aud": ["cognis"], "typ": "access"},
            self.token_ttl_seconds,
        )

    def sign_refresh_token(self, subject: str) -> str:
        return self._sign(
            {"sub": subject, "aud": ["cognis"], "typ": "refresh"}, self.refresh_ttl_seconds
        )

    def sign_service_jwt(self, subject: str, agent_id: str, audience: list[str]) -> str:
        return self._sign(
            {"sub": subject, "agent_id": agent_id, "aud": audience, "typ": "service"},
            self.token_ttl_seconds,
        )

    def sign_exchange_token(self, subject: str, target: str) -> str:
        return self._sign(
            {"sub": subject, "aud": [target], "typ": "exchange", "target": target},
            self.exchange_ttl_seconds,
        )

    def verify_jwt(self, token: str, audience: list[str] | None = None) -> dict[str, Any]:
        options = {"verify_aud": audience is not None}
        audience_value = audience[0] if audience else None
        claims = jwt.decode(
            token,
            self._public_key,
            algorithms=["ES256"],
            audience=audience_value,
            issuer="cognis",
            options=options,
        )
        jti = claims.get("jti")
        if jti in self._revoked_jtis:
            raise JWTError("Token revoked")
        return claims

    def revoke_token(self, jti: str) -> None:
        self._revoked_jtis.add(jti)

    def consume_exchange_token(self, jti: str) -> bool:
        if jti in self._used_exchange_jtis:
            return False
        self._used_exchange_jtis.add(jti)
        return True

    def jwks(self) -> dict[str, Any]:
        public_key = serialization.load_pem_public_key(self._public_key.encode())
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise TypeError("Public key is not an EC key")
        numbers = public_key.public_numbers()
        return {
            "keys": [
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "use": "sig",
                    "alg": "ES256",
                    "kid": self._kid,
                    "x": _b64url_uint(numbers.x),
                    "y": _b64url_uint(numbers.y),
                }
            ]
        }

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name="auth", status="healthy", details={"kid": self._kid})
