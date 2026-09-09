from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.memory.evidence import (
    EvidenceRememberRequest,
    TrustedEventRejectedError,
    canonical_request_bytes,
    canonical_request_hash,
    derive_evidence_root,
    parse_trusted_rejection,
    user_event_request_hash,
)
from cognis.providers.memory.mnemory import MnemoryProvider


def _rejection(reason: str) -> dict[str, Any]:
    return {
        "status": "rejected",
        "outcome": "rejected_before_write",
        "reason": reason,
        "terminal": True,
        "retryable": False,
        "fallback_allowed": False,
        "semantic_effects": "none",
        "source_retention": "caller_queue",
        "operation_id": "b72ca2d1-27cb-4c1e-810e-57df94088021",
    }


@pytest.mark.parametrize(
    "reason",
    [
        "input_budget_exceeded",
        "extraction_fact_budget_exceeded",
        "action_limit_exceeded",
        "plan_budget_exceeded",
    ],
)
@pytest.mark.parametrize("method", ["remember_evidence", "remember_user_event"])
@pytest.mark.asyncio
async def test_typed_rejection_retains_source_without_retry(reason, method):
    body = _body()
    body["messages"][0]["content"] = "Ž" * 40_000
    provider = MnemoryProvider("https://mnemory.test", object())
    client = _Client(_Response(422, {"detail": _rejection(reason)}))
    provider.client = client
    with pytest.raises(TrustedEventRejectedError) as captured:
        await getattr(provider, method)(body, "test-token")
    assert captured.value.source == body
    assert captured.value.rejection.reason == reason
    assert len(client.calls) == 1
    assert provider.breaker.failures == 0
    assert "test-token" not in str(captured.value)


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "validation_error"},
        {"fallback_allowed": True},
        {"retryable": True},
        {"semantic_effects": "unknown"},
        {"terminal": False},
        {"operation_id": ""},
        {"terminal": 1},
        {"retryable": 0},
        {"outcome": "rejected"},
    ],
)
def test_unrelated_422_is_not_budget_rejection(change):
    assert parse_trusted_rejection({**_rejection("input_budget_exceeded"), **change}) is None


@pytest.mark.parametrize("method", ["remember_evidence", "remember_user_event"])
@pytest.mark.parametrize("failure", ["timeout", "503"])
@pytest.mark.asyncio
async def test_uncertain_write_reuses_canonical_request(method, failure):
    body = _body()
    provider = MnemoryProvider("https://mnemory.test", object())

    class Client:
        def __init__(self):
            self.calls = []
            self.roots = set()

        async def post(self, path, *, json, headers):
            self.calls.append((path, json, headers))
            self.roots.add(derive_evidence_root(json))
            if len(self.calls) == 1:
                if failure == "timeout":
                    raise httpx.ReadTimeout("response lost after mutation")
                return _Response(503, {"detail": "unknown"})
            return _Response(200, {"status": "replayed"})

    client = Client()
    provider.client = client
    if method == "remember_evidence":
        uncertain = await provider.remember_evidence(body, "test-token")
        assert uncertain.outcome_unknown and uncertain.retryable
        result = await provider.remember_evidence(body, "test-token")
        assert result.status == "replayed"
    else:
        result = await provider.remember_user_event(body, "test-token")
        assert result["status"] == "replayed"
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1]
    assert len(client.roots) == 1


FIXTURE = Path(__file__).parents[1] / "contract/fixtures/evidence_remember_v1.json"


def _body() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]


def _auth_provider(tmp_path: Path) -> JWTAuthProvider:
    private = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return JWTAuthProvider(private_path, public_path)


def test_mnemory_fixture_is_byte_and_hash_exact() -> None:
    assert (
        hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        == "5e734a5597c40c0b7116b98253525774ef55d283c2cf9412896977ad3be8e407"
    )
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body = EvidenceRememberRequest.model_validate(fixture["body"])

    assert canonical_request_bytes(body) == fixture["canonical_request_bytes_utf8"].encode()
    assert canonical_request_hash(body) == fixture["request_hash"]
    assert derive_evidence_root(body) == fixture["evidence_root"]


def test_evidence_jwt_has_exact_claim_bindings_and_no_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth = _auth_provider(tmp_path)
    body = EvidenceRememberRequest.model_validate(_body())
    monkeypatch.setattr(
        "cognis.providers.auth.jwt.uuid.uuid4", lambda: type("Jti", (), {"hex": "jti-1"})()
    )
    token = auth.sign_evidence_jwt(body, now=datetime.fromtimestamp(1_700_000_000, UTC))
    claims = jwt.decode(
        token,
        auth._public_key,
        algorithms=["ES256"],
        audience="mnemory",
        options={"verify_exp": False},
    )

    assert claims == json.loads(FIXTURE.read_text(encoding="utf-8"))["claims"]
    assert "agent_id" not in claims


def test_evidence_jwt_has_fresh_jti_and_60_second_lifetime(tmp_path: Path) -> None:
    auth = _auth_provider(tmp_path)
    body = EvidenceRememberRequest.model_validate(_body())

    first = jwt.decode(
        auth.sign_evidence_jwt(body),
        options={"verify_signature": False, "verify_aud": False},
    )
    second = jwt.decode(
        auth.sign_evidence_jwt(body),
        options={"verify_signature": False, "verify_aud": False},
    )

    assert first["jti"] != second["jti"]
    assert first["iat"] == first["nbf"]
    assert first["exp"] - first["iat"] == 60


def test_evidence_jwt_always_contains_aow_for_same_owner(tmp_path: Path) -> None:
    auth = _auth_provider(tmp_path)
    body = EvidenceRememberRequest.model_validate(_body())
    same_owner = body.model_copy(
        update={
            "actor": body.actor.model_copy(update={"owner_id": body.actor.user_id}),
        }
    )
    claims = jwt.decode(
        auth.sign_evidence_jwt(same_owner),
        options={"verify_signature": False, "verify_aud": False},
    )
    assert claims["aow"] == claims["sub"]


def test_user_event_jwt_is_route_bound_and_has_no_agent(tmp_path: Path) -> None:
    auth = _auth_provider(tmp_path)
    body = EvidenceRememberRequest.model_validate(_body())
    claims = jwt.decode(
        auth.sign_user_event_jwt(body),
        options={"verify_signature": False, "verify_aud": False},
    )

    assert claims["typ"] == "user_event"
    assert claims["scope"] == "mnemory:remember:user"
    assert claims["evop"] == "remember"
    assert claims["ver"] == 1
    assert claims["request_hash"] == user_event_request_hash(body)
    assert claims["request_hash"] != canonical_request_hash(body)
    assert claims["evidence_root"] == derive_evidence_root(body)
    assert "agent_id" not in claims
    assert claims["exp"] - claims["iat"] == 60


def test_user_event_jwt_rejects_content_larger_than_ingestion_contract(
    tmp_path: Path,
) -> None:
    auth = _auth_provider(tmp_path)
    body = _body()
    body["messages"][0]["content"] = "x" * 400_001

    with pytest.raises(ValueError):
        auth.sign_user_event_jwt(body)

    with pytest.raises(ValueError):
        auth.sign_evidence_jwt(body)


@pytest.mark.parametrize("length", [1_000, 1_001, 40_000, 400_000])
@pytest.mark.parametrize("status", ["accepted", "replayed"])
@pytest.mark.asyncio
async def test_long_signed_source_is_preserved_on_both_routes(
    tmp_path: Path,
    length: int,
    status: str,
) -> None:
    auth = _auth_provider(tmp_path)
    body = _body()
    body["messages"][0]["content"] = "Ž" * length
    evidence_token = auth.sign_evidence_jwt(body)
    ingest_token = auth.sign_user_event_jwt(body)
    evidence_claims = jwt.decode(evidence_token, options={"verify_signature": False})
    ingest_claims = jwt.decode(ingest_token, options={"verify_signature": False})
    assert evidence_claims["evidence_root"] == ingest_claims["evidence_root"]
    assert evidence_claims["request_hash"] != ingest_claims["request_hash"]
    provider = MnemoryProvider("https://mnemory.test", object())
    client = _Client(_Response(200, {"status": status}))
    provider.client = client  # type: ignore[assignment]
    assert (await provider.remember_evidence(body, evidence_token)).status == status
    assert (await provider.remember_user_event(body, ingest_token))["status"] == status
    assert len(client.calls) == 2
    assert all(call[1] == body for call in client.calls)
    assert all(set(call[2]) == {"Authorization"} for call in client.calls)


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        raise httpx.HTTPStatusError(
            "request failed",
            request=httpx.Request("POST", "https://mnemory.test"),
            response=httpx.Response(self.status_code),
        )


class _Client:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def post(self, path: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Response:
        self.calls.append((path, json, headers))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"status": "accepted", "operation_id": "op-1", "result": {}}, "accepted"),
        (200, {"status": "replayed"}, "replayed"),
        (200, {"status": "recovered"}, "recovered"),
        (200, {"status": "skipped"}, "skipped"),
        (409, {"detail": "conflict"}, "conflict"),
        (400, {"detail": "rejected"}, "rejected"),
        (401, {"detail": "rejected"}, "rejected"),
        (403, {"detail": "rejected"}, "rejected"),
        (422, {"detail": "rejected"}, "rejected"),
        (404, {"detail": "missing"}, "unavailable"),
        (425, {"detail": "active"}, "unavailable"),
        (500, {"detail": "failed"}, "unavailable"),
    ],
)
async def test_evidence_status_classification(
    status_code: int, payload: dict[str, Any], expected: str
) -> None:
    provider = MnemoryProvider("https://mnemory.test", object())
    client = _Client(_Response(status_code, payload))
    provider.client = client  # type: ignore[assignment]

    result = await provider.remember_evidence(_body(), "evidence-token")

    assert result.status == expected
    assert result.terminal is (
        expected in {"accepted", "replayed", "recovered", "skipped", "conflict", "rejected"}
    )
    assert result.retryable is (expected == "unavailable")
    assert result.outcome_unknown is (expected == "unavailable")


@pytest.mark.asyncio
async def test_evidence_transport_failure_is_outcome_unknown_and_not_retried() -> None:
    provider = MnemoryProvider("https://mnemory.test", object())
    client = _Client(httpx.ReadTimeout("response lost"))
    provider.client = client  # type: ignore[assignment]

    result = await provider.remember_evidence(_body(), "evidence-token")

    assert result.status == "unavailable"
    assert result.retryable is True
    assert result.outcome_unknown is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_evidence_request_uses_only_dedicated_route_and_authorization() -> None:
    provider = MnemoryProvider("https://mnemory.test", object())
    client = _Client(_Response(200, {"status": "accepted"}))
    provider.client = client  # type: ignore[assignment]

    await provider.remember_evidence(_body(), "evidence-token")

    path, body, headers = client.calls[0]
    assert path == "/api/evidence/remember/v1"
    assert body == _body()
    assert headers == {"Authorization": "Bearer evidence-token"}


@pytest.mark.asyncio
async def test_user_event_request_uses_only_dedicated_route_and_authorization() -> None:
    provider = MnemoryProvider("https://mnemory.test", object())
    client = _Client(_Response(200, {"status": "accepted"}))
    provider.client = client  # type: ignore[assignment]

    result = await provider.remember_user_event(_body(), "user-event-token")

    assert result == {"status": "accepted"}
    path, body, headers = client.calls[0]
    assert path == "/api/user-events/remember/v1"
    assert body == _body()
    assert headers == {"Authorization": "Bearer user-event-token"}
