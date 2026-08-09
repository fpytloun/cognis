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

    def _sign(self, claims: dict[str, Any], expires_in: int | None) -> str:
        now = datetime.now(UTC)
        payload = {
            **claims,
            "iss": "cognis",
            "iat": int(now.timestamp()),
            "jti": uuid.uuid4().hex,
        }
        if expires_in is not None:
            payload["exp"] = int((now + timedelta(seconds=expires_in)).timestamp())
        return jwt.encode(payload, self._private_key, algorithm="ES256", headers={"kid": self._kid})

    def sign_access_token(self, subject: str, name: str | None, role: str) -> str:
        return self._sign(
            {
                "sub": subject,
                "name": name,
                "role": role,
                "aud": ["cognis", "intaris", "mnemory"],
                "typ": "access",
            },
            self.token_ttl_seconds,
        )

    def sign_refresh_token(self, subject: str) -> str:
        return self._sign(
            {"sub": subject, "aud": ["cognis"], "typ": "refresh"}, self.refresh_ttl_seconds
        )

    def sign_service_jwt(
        self,
        subject: str,
        agent_id: str,
        audience: list[str],
        *,
        agent_owner_email: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "sub": subject,
            "agent_id": agent_id,
            "aud": audience,
            "typ": "service",
        }
        if agent_owner_email and agent_owner_email != subject:
            payload["aow"] = agent_owner_email
        return self._sign(payload, self.token_ttl_seconds)

    def sign_executor_token(
        self,
        executor_id: str,
        ttl_seconds: int | None = None,
        *,
        token_version: int = 0,
    ) -> str:
        """Sign a revokable JWT for executor authentication.

        Remote executor tokens do not expire by default because stateless
        executors (for example Kubernetes pods) need stable credentials.
        They are revoked by bumping the persisted executor token version.
        Subprocess executors pass a short ``ttl_seconds`` because the
        controller mints those tokens on demand.
        """
        return self._sign(
            {
                "sub": executor_id,
                "aud": ["cognis-executor"],
                "typ": "executor",
                "etv": token_version,
            },
            ttl_seconds,
        )

    def sign_controller_jwt(self, owner_id: str, ttl_seconds: int = 30) -> str:
        """Sign a short-lived controller peer credential."""

        return self._sign(
            {
                "sub": owner_id,
                "aud": ["cognis-controller"],
                "typ": "controller",
            },
            ttl_seconds,
        )

    def verify_controller_jwt(self, token: str) -> dict[str, Any]:
        claims = self.verify_jwt(token, audience=["cognis-controller"])
        if claims.get("typ") != "controller":
            raise JWTError("Invalid controller token type")
        if claims.get("aud") != ["cognis-controller"]:
            raise JWTError("Invalid controller token audience")
        for name in ("sub", "jti", "iat", "exp"):
            if not claims.get(name):
                raise JWTError(f"Missing controller token claim: {name}")
        return claims

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

    def verify_executor_token(self, token: str) -> dict[str, Any]:
        """Verify an executor token, including legacy expired tokens.

        Executor tokens are revokable via the executor row's token version,
        so this verifier intentionally ignores ``exp`` for backward
        compatibility with 30-day tokens issued before executor tokens became
        non-expiring.  It must only be used for ``typ=executor`` tokens.
        """
        claims = jwt.decode(
            token,
            self._public_key,
            algorithms=["ES256"],
            audience="cognis-executor",
            issuer="cognis",
            options={"verify_aud": True, "verify_exp": False},
        )
        if claims.get("typ") != "executor":
            raise JWTError("Invalid token type")
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
