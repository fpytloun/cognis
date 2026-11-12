"""Inference authorization and wire-contract regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis import json_codec as json
from cognis.api.inference_limits import InferenceBodyLimitMiddleware
from cognis.api.middleware import AuthenticationMiddleware
from cognis.api.routes.inference import router
from cognis.api.routes.settings import router as settings_router
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.auth.inference_credentials import provision_companion_tokens
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.llm.hosted import systemone_metadata
from cognis.providers.llm.inference_api import (
    INFERENCE_TOKENS,
    InferenceAPIService,
)
from cognis.providers.llm.service import LLMService
from cognis.providers.llm.usage_delivery import UsageDeliveryMiddleware
from cognis.security import generate_api_key_material
from cognis.store.models import Base, InferenceCompanion, LLMProvider
from cognis.store.queries import (
    create_api_key,
    create_llm_provider,
    create_user,
    upsert_model_routing,
)


@pytest.fixture
def gateway(tmp_path: Path, usage_capture):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/inference.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    key = ec.generate_private_key(ec.SECP256R1())
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    auth = JWTAuthProvider(private, public)

    async def seed():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            password_hash = PasswordHasher().hash("inference-test-password")
            await create_user(session, "operator@cognis.local", "operator", password_hash, "admin")
            await create_user(session, "reader@cognis.local", "reader", password_hash, "user")
            for provider_id, owner in (
                ("shared-inference", SYSTEM_USER_EMAIL),
                ("private-inference", "operator@cognis.local"),
            ):
                await create_llm_provider(
                    session,
                    provider_id=provider_id,
                    display_name=provider_id,
                    location="controller",
                    backend="litellm",
                    owner_email=owner,
                    config={
                        "preset": "openai",
                        "models": [
                            {"model_id": "gpt-4o-mini", "supports_responses_api": False},
                            {"model_id": "text-embedding-3-small", "supports_embedding": True},
                        ],
                    },
                )
            for role, model in (
                ("companion_lite", "gpt-4o-mini"),
                ("companion_smart", "gpt-4o-mini"),
                ("companion_embedding", "text-embedding-3-small"),
            ):
                await upsert_model_routing(
                    session,
                    task_type=role,
                    model=model,
                    provider_id="shared-inference",
                )
            session.add(InferenceCompanion(service="intaris", token_version=0, enabled=True))
            session.add(InferenceCompanion(service="mnemory", token_version=0, enabled=True))
            await session.commit()

    llm = LLMService(factory, usage_capture=usage_capture)
    llm.generate = AsyncMock(
        return_value={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Ready"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 7},
            },
        }
    )
    llm.embed_response = AsyncMock(
        return_value={
            "object": "list",
            "data": [{"index": 0, "object": "embedding", "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        }
    )
    app = FastAPI()
    app.state.session_factory = factory
    app.state.auth_provider = auth
    app.state.password_hasher = PasswordHasher()
    app.state.providers = SimpleNamespace(llm=llm)
    app.state.inference_api = InferenceAPIService(llm, factory)
    app.state.config = SimpleNamespace(inference_credential_dir=None)
    app.add_middleware(InferenceBodyLimitMiddleware, max_bytes=1024)
    app.add_middleware(AuthenticationMiddleware)
    app.include_router(router)
    app.include_router(settings_router)
    with TestClient(UsageDeliveryMiddleware(app, capture=usage_capture)) as client:
        client.portal.call(seed)
        yield client, app, auth, factory, llm
        client.portal.call(engine.dispose)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_standard_model_retrieval_exposes_only_proven_limits(gateway, monkeypatch):
    client, _, _, _, llm = gateway
    monkeypatch.setattr(
        llm,
        "get_model_info",
        AsyncMock(
            return_value=SimpleNamespace(
                context_window=262144,
                max_input_tokens=240000,
                max_output_tokens=16000,
                provider_metadata={
                    "field_sources": {"context_window": "config", "max_input_tokens": "config"}
                },
            )
        ),
    )
    response = client.get("/v1/models/routes/intaris_evaluation", headers=companion(gateway))
    assert response.status_code == 200, response.text
    model = response.json()
    assert model["id"] == "routes/intaris_evaluation"
    assert model["object"] == "model"
    assert model["context_window"] == 262144
    assert model["max_input_tokens"] == 240000
    assert model["max_output_tokens"] is None
    assert len(model["resolution_fingerprint"]) == 64
    assert "shared-inference" not in model["resolution_fingerprint"]
    llm.generate.assert_not_awaited()
    llm.get_model_info.return_value.provider_metadata = {}
    unknown = client.get("/v1/models/routes/intaris_evaluation", headers=companion(gateway)).json()
    assert unknown["context_window"] is None
    assert unknown["resolution_fingerprint"] != model["resolution_fingerprint"]
    assert (
        client.get(
            "/v1/models/providers/private-inference/gpt-4o-mini", headers=companion(gateway)
        ).status_code
        == 404
    )
    assert client.get("/v1/models/routes/intaris_evaluation").status_code == 401


def companion(gateway, service="intaris"):
    return bearer(gateway[2].sign_inference_companion_token(service, 0))


def body(model="routes/intaris_evaluation", **kwargs):
    return {"model": model, "messages": [{"role": "user", "content": "Check"}], **kwargs}


@pytest.mark.parametrize(
    ("override_model", "override_effort", "client_effort", "expected"),
    [
        (False, None, None, "low"),
        (False, "medium", None, "medium"),
        (True, None, None, "low"),
        (True, "high", None, "high"),
        (False, "default", None, None),
        (False, "medium", "high", "high"),
        (False, "medium", "none", "none"),
        (False, "medium", "default", None),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
def test_companion_effort_roundtrip_and_request_precedence(
    gateway,
    monkeypatch,
    override_model,
    override_effort,
    client_effort,
    expected,
    streaming,
):
    client, _, auth, _, llm = gateway
    monkeypatch.setattr(
        llm,
        "get_model_info",
        AsyncMock(
            return_value=SimpleNamespace(
                reasoning_efforts=["low", "medium", "high", "none"],
                supports_embedding=False,
            )
        ),
    )
    admin = bearer(auth.sign_access_token("operator@cognis.local", None, "admin"))
    payload = {
        "companion_lite": {"model": "shared-inference/gpt-4o-mini", "reasoning_effort": "low"},
        "intaris_evaluation": {
            "model": "shared-inference/gpt-4o-mini" if override_model else None,
            "reasoning_effort": override_effort,
        },
    }
    saved = client.put("/api/v1/model-routing", headers=admin, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["intaris_evaluation"] == payload["intaris_evaluation"]
    loaded = client.get("/api/v1/model-routing", headers=admin).json()
    assert loaded == saved.json()
    assert client.put("/api/v1/model-routing", headers=admin, json=loaded).json() == loaded
    observed = []

    async def stream(*args, **kwargs):
        observed.append(kwargs)
        yield {"choices": [{"index": 0, "delta": {"content": "Ready"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(llm, "stream_generate", stream)
    request = body(stream=streaming)
    if client_effort is not None:
        request["reasoning_effort"] = client_effort
    response = client.post("/v1/chat/completions", headers=companion(gateway), json=request)
    assert response.status_code == 200, response.text
    kwargs = observed[0] if streaming else llm.generate.call_args.kwargs
    assert kwargs.get("reasoning_effort") == expected
    if expected is None:
        assert "reasoning_effort" not in kwargs


def test_effort_only_override_cannot_outlive_parent_model(gateway, monkeypatch):
    client, _, auth, _, llm = gateway
    monkeypatch.setattr(
        llm,
        "get_model_info",
        AsyncMock(
            return_value=SimpleNamespace(
                reasoning_efforts=["low", "medium"],
                supports_embedding=False,
            )
        ),
    )
    admin = bearer(auth.sign_access_token("operator@cognis.local", None, "admin"))
    response = client.put(
        "/api/v1/model-routing",
        headers=admin,
        json={
            "intaris_evaluation": {"model": None, "reasoning_effort": "medium"},
        },
    )
    assert response.status_code == 422
    response = client.put(
        "/api/v1/model-routing",
        headers=admin,
        json={
            "companion_lite": {"model": "shared-inference/gpt-4o-mini", "reasoning_effort": "low"},
            "intaris_evaluation": {"model": None, "reasoning_effort": "high"},
        },
    )
    assert response.status_code == 422
    assert (
        client.get("/api/v1/model-routing", headers=admin).json()["intaris_evaluation"][
            "reasoning_effort"
        ]
        is None
    )


@pytest.mark.parametrize("streaming", [False, True])
def test_info_logging_preserves_success_and_structured_attribution(
    gateway, caplog, monkeypatch, streaming
):
    import logging

    client, _, _, _, llm = gateway

    async def stream(*args, **kwargs):
        yield {
            "choices": [{"index": 0, "delta": {"content": "Ready"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 6},
            },
        }

    monkeypatch.setattr(llm, "stream_generate", stream)
    caplog.set_level(logging.INFO, logger="cognis.providers.llm.inference_api")
    response = client.post(
        "/v1/chat/completions", headers=companion(gateway), json=body(stream=streaming)
    )
    assert response.status_code == 200, response.text
    if streaming:
        assert "[DONE]" in response.text and '"error"' not in response.text
    event = next(
        record.extra_data for record in caplog.records if record.message == "inference_request"
    )
    assert event["client_id"] == "intaris"
    assert event["requested_model"] == "routes/intaris_evaluation"
    assert event["outcome"] == "success"
    assert event["input_tokens"] == (10 if streaming else 12)
    assert event["cached_input_tokens"] == (6 if streaming else 7)
    assert not {"messages", "prompt", "completion", "api_key", "token"}.intersection(event)


def test_companion_is_route_confined_and_global(gateway, monkeypatch):
    # Intaris calls back into Cognis for inference: this must never recursively
    # evaluate the request. Recovery remains owned by the shared service.
    from cognis.core.tool_router import ToolRouter

    evaluation = AsyncMock(side_effect=AssertionError("recursive guardrail evaluation"))
    monkeypatch.setattr(ToolRouter, "_evaluate_tool_call", evaluation)
    client, _, _, _, llm = gateway
    assert client.get("/v1/models", headers=companion(gateway)).status_code == 200
    response = client.post("/v1/chat/completions", headers=companion(gateway), json=body())
    assert response.status_code == 200, response.text
    assert response.json()["object"] == "chat.completion"
    assert llm.generate.call_args.kwargs["acting_user_email"] == SYSTEM_USER_EMAIL
    assert "max_retries" not in llm.generate.call_args.kwargs
    assert llm.generate.call_args.kwargs["timeout"] == 3.5
    evaluation.assert_not_awaited()
    for model in ("providers/shared-inference/gpt-4o-mini", "routes/mnemory_extraction"):
        assert (
            client.post(
                "/v1/chat/completions", headers=companion(gateway), json=body(model)
            ).status_code
            == 403
        )
    assert client.get("/api/v1/model-routing", headers=companion(gateway)).status_code == 401


@pytest.mark.parametrize("mode", ["image_generation", "audio_speech", "rerank"])
def test_chat_gateway_rejects_nonchat_catalog_protocols(gateway, monkeypatch, mode):
    from cognis.models.config import ModelInfo

    client, _, _, _, llm = gateway
    monkeypatch.setattr(
        llm,
        "get_model_info",
        AsyncMock(return_value=ModelInfo(model_id="gpt-4o-mini", model_mode=mode)),
    )
    response = client.post("/v1/chat/completions", headers=companion(gateway), json=body())
    assert response.status_code == 400
    llm.generate.assert_not_awaited()


def configure_systemone(gateway, monkeypatch, preset="systemone"):
    client, _, _, factory, llm = gateway
    original_info = llm.get_model_info

    async def info(model, **kwargs):
        if model == "jev-1.13":
            from cognis.models.config import ModelInfo

            return ModelInfo(model_id=model, **systemone_metadata())
        return await original_info(model, **kwargs)

    monkeypatch.setattr(llm, "get_model_info", info)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-typesafe-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-router-key")

    async def seed():
        async with factory() as session:
            await create_llm_provider(
                session,
                provider_id="typed-inference",
                display_name="Typed",
                location="controller",
                backend="litellm",
                owner_email=SYSTEM_USER_EMAIL,
                config={
                    "preset": preset,
                    **(
                        {
                            "base_url": "https://openrouter.ai/api/v1",
                            "auth_config": {"mode": "env", "env_var": "OPENROUTER_API_KEY"},
                        }
                        if preset in {"openai_compatible", "litellm_proxy"}
                        else {}
                    ),
                    "default_model": "jev-1.13",
                    "models": [{"model_id": "jev-1.13", **systemone_metadata()}],
                },
            )
            await upsert_model_routing(
                session,
                task_type="intaris_evaluation",
                model="jev-1.13",
                provider_id="typed-inference",
            )
            await session.commit()

    client.portal.call(seed)


def typed_body(model="routes/intaris_evaluation"):
    return {
        "model": model,
        "state": {"action": "read"},
        "questions": {"aligned": {"type": "noul", "instructions": "Is it aligned?"}},
    }


@pytest.mark.parametrize(
    ("preset", "url", "key"),
    [
        ("systemone", "https://api.typesafe.ai/v1/systemone", "test-typesafe-key"),
        ("openrouter", "https://openrouter.ai/api/v1/systemone", "test-router-key"),
        ("openai_compatible", "https://openrouter.ai/api/v1/systemone", "test-router-key"),
        ("litellm_proxy", "https://openrouter.ai/api/v1/systemone", "test-router-key"),
    ],
)
def test_native_systemone_proxy_and_existing_intaris_route(
    gateway, monkeypatch, respx_mock, preset, url, key, caplog
):
    import logging

    import httpx

    configure_systemone(gateway, monkeypatch, preset)
    client, _, _, _, llm = gateway
    upstream = {
        "model": "typesafe/jev-1.13",
        "answers": {"aligned": {"type": "noul", "noul": 0.98}},
        "usage": {"input_tokens": 275, "output_tokens": 20, "cost": 0.00003},
        "provider": "TypeSafe",
        "id": "provider-decision",
    }
    route = respx_mock.post(url).mock(return_value=httpx.Response(200, json=upstream))
    caplog.set_level(logging.INFO, logger="cognis.providers.llm.inference_api")
    response = client.post("/v1/systemone", headers=companion(gateway), json=typed_body())
    assert response.status_code == 200, response.text
    assert response.json() == upstream
    assert response.headers["x-request-id"].startswith("systemone-")
    assert route.calls.last.request.headers["authorization"] == f"Bearer {key}"
    wire = json.loads(route.calls.last.request.content)
    assert wire == {**typed_body(), "model": "jev-1.13"}
    event = next(r.extra_data for r in caplog.records if r.message == "inference_request")
    assert event["operation"] == "systemone"
    assert event["input_tokens"] == 275 and event["output_tokens"] == 20
    llm.generate.assert_not_called()
    assert (
        client.post("/v1/chat/completions", headers=companion(gateway), json=body()).status_code
        == 400
    )
    for model in ("providers/typed-inference/jev-1.13", "routes/mnemory_extraction"):
        assert (
            client.post(
                "/v1/systemone", headers=companion(gateway), json=typed_body(model)
            ).status_code
            == 403
        )
    assert any(
        m["id"] == "routes/intaris_evaluation"
        for m in client.get("/v1/models", headers=companion(gateway)).json()["data"]
    )


def test_models_report_resolvable_operations_per_caller(gateway, monkeypatch):
    client, _, auth, _, _ = gateway

    def operations(headers):
        data = client.get("/v1/models", headers=headers).json()["data"]
        return {item["id"]: item["cognis"]["operations"] for item in data}

    chat = operations(companion(gateway))
    assert chat["routes/intaris_evaluation"] == ["chat"]
    assert chat["routes/intaris_judge"] == ["chat"]
    assert operations(companion(gateway, "mnemory"))["routes/mnemory_embedding"] == ["embeddings"]
    reader = operations(bearer(auth.sign_access_token("reader@cognis.local", None, "user")))
    assert reader["providers/shared-inference/gpt-4o-mini"] == ["chat"]
    assert reader["providers/shared-inference/text-embedding-3-small"] == ["embeddings"]

    configure_systemone(gateway, monkeypatch)
    systemone = operations(companion(gateway))
    assert systemone["routes/intaris_evaluation"] == ["systemone"]
    assert systemone["routes/intaris_judge"] == ["chat"]
    raw = client.get("/v1/models", headers=companion(gateway)).text
    assert "typesafe" not in raw.lower() and "test-typesafe-key" not in raw
    assert all(
        set(item)
        == {
            "id",
            "object",
            "created",
            "owned_by",
            "cognis",
            "context_window",
            "max_input_tokens",
            "max_output_tokens",
            "resolution_fingerprint",
        }
        and set(item["cognis"]) == {"operations"}
        and all(
            item[field] is None or type(item[field]) is int and item[field] > 0
            for field in ("context_window", "max_input_tokens", "max_output_tokens")
        )
        and len(item["resolution_fingerprint"]) == 64
        for item in client.get("/v1/models", headers=companion(gateway)).json()["data"]
    )


def test_systemone_invalid_requests_and_sanitized_upstream_failure(
    gateway, monkeypatch, respx_mock
):
    import httpx

    configure_systemone(gateway, monkeypatch)
    client, _, _, _, _ = gateway
    for extra in ({"stream": True}, {"temperature": 0.1}, {"reasoning_effort": "high"}):
        assert (
            client.post(
                "/v1/systemone", headers=companion(gateway), json={**typed_body(), **extra}
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/v1/systemone", headers=companion(gateway), json={**typed_body(), "questions": {}}
        ).status_code
        == 422
    )
    route = respx_mock.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json={"model": "jev-1.13", "answers": {}})
    )
    response = client.post("/v1/systemone", headers=companion(gateway), json=typed_body())
    assert response.status_code == 502
    assert "state" not in response.text
    route.mock(return_value=httpx.Response(401, json={"error": "upstream-secret"}))
    response = client.post("/v1/systemone", headers=companion(gateway), json=typed_body())
    assert response.status_code == 502 and "upstream-secret" not in response.text


def test_systemone_route_save_skips_inherited_thinking_and_rejects_other_roles(
    gateway, monkeypatch
):
    configure_systemone(gateway, monkeypatch)
    client, _, auth, _, _ = gateway
    admin = bearer(auth.sign_access_token("operator@cognis.local", None, "admin"))
    payload = {
        "intaris_evaluation": {"model": "typed-inference/jev-1.13", "reasoning_effort": "default"}
    }
    response = client.put("/api/v1/model-routing", headers=admin, json=payload)
    assert response.status_code == 200, response.text
    response = client.put(
        "/api/v1/model-routing",
        headers=admin,
        json={"classifier": {"model": "typed-inference/jev-1.13"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["classifier"]["model"] == "typed-inference/jev-1.13"
    assert "auxiliary_generation" in response.json()
    for role in ("default", "auxiliary_generation", "companion_lite", "mnemory_extraction"):
        response = client.put(
            "/api/v1/model-routing",
            headers=admin,
            json={role: {"model": "typed-inference/jev-1.13"}},
        )
        assert response.status_code == 422


def test_discovery_filters_private_models_and_companion_routes(gateway):
    client, _, auth, _, _ = gateway
    models = client.get("/v1/models", headers=companion(gateway)).json()["data"]
    assert models and all(item["id"].startswith("routes/intaris_") for item in models)
    reader = bearer(auth.sign_access_token("reader@cognis.local", None, "user"))
    visible = client.get("/v1/models", headers=reader).json()["data"]
    assert any(item["id"].startswith("providers/shared-inference/") for item in visible)
    assert not any("private-inference" in item["id"] for item in visible)


def test_companion_rotates_and_revocation_survives_restart(gateway):
    client, app, auth, _, _ = gateway
    old = companion(gateway)
    admin = bearer(auth.sign_access_token("operator@cognis.local", None, "admin"))
    rotated = client.post("/api/v1/inference/companions/intaris/token", headers=admin)
    assert rotated.status_code == 200, rotated.text
    new = bearer(rotated.json()["token"])
    assert client.get("/v1/models", headers=old).status_code == 401
    assert client.get("/v1/models", headers=new).status_code == 200
    assert (
        client.delete("/api/v1/inference/companions/intaris/token", headers=admin).status_code
        == 200
    )
    app.state.auth_provider = JWTAuthProvider(auth.private_key_path, auth.public_key_path)
    assert client.get("/v1/models", headers=new).status_code == 401


def test_routing_requires_admin_and_global_provider(gateway):
    client, _, auth, _, _ = gateway
    admin = bearer(auth.sign_access_token("operator@cognis.local", None, "admin"))
    reader = bearer(auth.sign_access_token("reader@cognis.local", None, "user"))
    payload = {"companion_lite": {"model": "private-inference/gpt-4o-mini"}}
    assert client.put("/api/v1/model-routing", headers=reader, json=payload).status_code == 403
    assert client.put("/api/v1/model-routing", headers=admin, json=payload).status_code == 422


def test_stale_private_override_and_disabled_provider_fail_closed(gateway):
    client, _, _, factory, _ = gateway

    async def corrupt():
        async with factory() as session:
            await upsert_model_routing(
                session,
                task_type="intaris_evaluation",
                model="gpt-4o-mini",
                provider_id="private-inference",
            )
            await session.commit()

    client.portal.call(corrupt)
    assert (
        client.post("/v1/chat/completions", headers=companion(gateway), json=body()).status_code
        == 503
    )


def test_embedding_rejects_actual_target_mismatch(gateway):
    client, _, _, _, llm = gateway
    llm.embed_response.return_value["_cognis_inference"] = {
        "provider_id": "changed-provider",
        "model": "changed-model",
    }
    response = client.post(
        "/v1/embeddings",
        headers=companion(gateway, "mnemory"),
        json={"model": "routes/mnemory_embedding", "input": "memory"},
    )
    assert response.status_code == 409, response.text


def test_proxy_client_header_partitions_affinity(gateway):
    client, _, _, _, llm = gateway
    headers = companion(gateway)
    identities = []
    for identity in ("first-client", "second-client", "first-client"):
        response = client.post(
            "/v1/chat/completions",
            headers={**headers, "X-Cognis-Client-ID": identity},
            json={
                "model": "routes/intaris_evaluation",
                "messages": [{"role": "user", "content": "evaluate"}],
            },
        )
        assert response.status_code == 200, response.text
        identities.append(llm.generate.call_args.kwargs["cognis_session_id"])
    assert identities[0] == identities[2]
    assert identities[0] != identities[1]


def test_embeddings_preserve_usage_and_base64_encoding(gateway):
    client, _, _, _, _ = gateway
    response = client.post(
        "/v1/embeddings",
        headers=companion(gateway, "mnemory"),
        json={
            "model": "routes/mnemory_embedding",
            "input": ["memory"],
            "encoding_format": "base64",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["usage"]["prompt_tokens"] == 5
    assert isinstance(response.json()["data"][0]["embedding"], str)
    assert (
        client.post(
            "/v1/embeddings",
            headers=companion(gateway),
            json={"model": "routes/intaris_evaluation", "input": "memory"},
        ).status_code
        == 400
    )


def test_cached_tokens_are_attributed_without_token_id_labels(gateway):
    client, _, _, _, _ = gateway
    counter = INFERENCE_TOKENS.labels(
        "intaris",
        "inference_companion",
        "shared-inference",
        "gpt-4o-mini",
        "chat",
        "intaris_evaluation",
        "cached",
    )
    before = counter._value.get()
    assert (
        client.post("/v1/chat/completions", headers=companion(gateway), json=body()).status_code
        == 200
    )
    assert counter._value.get() - before == 7


@pytest.mark.parametrize("include_usage", [True, False])
def test_stream_has_usage_and_releases_admission(gateway, include_usage):
    client, app, _, _, llm = gateway
    counter = INFERENCE_TOKENS.labels(
        "intaris",
        "inference_companion",
        "shared-inference",
        "gpt-4o-mini",
        "chat",
        "intaris_evaluation",
        "input",
    )
    before = counter._value.get()

    async def stream(*args, **kwargs):
        assert kwargs["stream_options"] == {"include_usage": True}
        yield {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Ready"}}]}
        yield {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 1}}

    llm.stream_generate = stream
    response = client.post(
        "/v1/chat/completions",
        headers=companion(gateway),
        json=body(stream=True, stream_options={"include_usage": True})
        if include_usage
        else body(stream=True),
    )
    assert response.status_code == 200
    assert "chat.completion.chunk" in response.text
    assert ('"prompt_tokens":4' in response.text.replace(" ", "")) is include_usage
    assert counter._value.get() - before == 4
    assert "data: [DONE]" in response.text
    assert app.state.inference_api._total == 0


def test_body_limit_and_untrusted_upstream_parameters(gateway):
    client, _, _, _, llm = gateway
    assert (
        client.post(
            "/v1/chat/completions",
            headers=companion(gateway),
            json=body(api_base="http://127.0.0.1"),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/chat/completions",
            headers=companion(gateway),
            json=body(messages=[{"role": "user", "content": "x" * 2048}]),
        ).status_code
        == 413
    )
    llm.generate.assert_not_called()


def test_managed_provisioning_does_not_resurrect_revoked_service(gateway, tmp_path):
    client, _, _, factory, _ = gateway

    async def provision():
        async with factory() as session:
            row = await session.get(InferenceCompanion, "intaris")
            row.enabled = False
            await session.commit()
        await provision_companion_tokens(tmp_path, factory, gateway[2])

    client.portal.call(provision)
    assert not (tmp_path / "intaris/token.jwt").exists()
    assert (tmp_path / "mnemory/token.jwt").stat().st_mode & 0o777 == 0o600


def test_bearer_api_key_policy(gateway):
    client, app, _, factory, _ = gateway
    key_id, key = generate_api_key_material()

    async def issue():
        async with factory() as session:
            await create_api_key(
                session,
                "operator@cognis.local",
                app.state.password_hasher.hash(key),
                "External inference",
                key_id=key_id,
                scopes={
                    "inference_only": True,
                    "models": ["providers/shared-inference/gpt-4o-mini"],
                    "operations": ["chat"],
                },
            )
            await session.commit()

    client.portal.call(issue)
    headers = bearer(key)
    assert (
        client.post(
            "/v1/chat/completions",
            headers=headers,
            json=body("providers/shared-inference/gpt-4o-mini"),
        ).status_code
        == 200
    )
    assert client.post("/v1/chat/completions", headers=headers, json=body()).status_code == 403
    assert client.get("/api/v1/model-routing", headers=headers).status_code == 401
    assert client.get("/api/v1/model-routing", headers={"X-API-Key": key}).status_code == 403
    visible = client.get("/v1/models", headers=headers).json()["data"]
    assert [model["id"] for model in visible] == ["providers/shared-inference/gpt-4o-mini"]


def test_embedding_space_remains_pinned_until_explicit_admin_reset(gateway):
    client, _, auth, _, llm = gateway
    headers = companion(gateway, "mnemory")
    payload = {"model": "routes/mnemory_embedding", "input": "memory"}
    assert client.post("/v1/embeddings", headers=headers, json=payload).status_code == 200
    llm.embed_response.return_value = {
        "data": [{"index": 0, "object": "embedding", "embedding": [0.1, 0.2, 0.3]}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    changed = client.post("/v1/embeddings", headers=headers, json=payload)
    assert changed.status_code == 503, changed.text
    admin = bearer(auth.sign_access_token("operator@cognis.local", None, "admin"))
    assert (
        client.post(
            "/api/v1/inference/companions/mnemory/reset-embedding",
            headers=admin,
            json={"reindex_migration_prepared": True},
        ).status_code
        == 200
    )
    assert client.post("/v1/embeddings", headers=headers, json=payload).status_code == 200


def test_provider_error_chunk_is_not_a_success(gateway, monkeypatch):
    client, app, _, _, llm = gateway

    async def failing_stream(*args, **kwargs):
        yield {"choices": [{"index": 0, "delta": {"content": "partial"}}]}
        yield {"error": "sensitive provider failure", "mid_stream_failure": True}

    monkeypatch.setattr(llm, "stream_generate", failing_stream)
    observed = []
    monkeypatch.setattr(app.state.inference_api, "observe", lambda *args: observed.append(args))
    response = client.post(
        "/v1/chat/completions", headers=companion(gateway), json={**body(), "stream": True}
    )
    assert '"type":"upstream_error"' in response.text
    assert "sensitive provider failure" not in response.text
    assert observed[0][5] == "error"
    assert app.state.inference_api._total == 0


@pytest.mark.asyncio
async def test_embedding_normalization_preserves_input_indices(monkeypatch):
    from cognis.providers.llm import litellm as implementation

    # Normalization belongs to the adapter. Service recovery is tested with a
    # real route store in test_inference_execution.py.
    llm = implementation.LiteLLMProvider(None)
    llm._resolve_model_target = AsyncMock(
        return_value=("text-embedding-3-small", SimpleNamespace(config={}, location="controller"))
    )
    llm._resolve_provider_kwargs = AsyncMock(return_value={})
    monkeypatch.setattr(llm, "_apply_model_prefix", lambda model, provider: model)
    monkeypatch.setattr(
        implementation.litellm,
        "aembedding",
        AsyncMock(
            return_value={
                "data": [{"index": 1, "embedding": [0.2]}, {"index": 0, "embedding": [0.1]}],
                "usage": {"prompt_tokens": 2},
            }
        ),
    )
    result = await llm.embed_response(["first", "second"])
    assert result["data"] == [
        {"index": 0, "object": "embedding", "embedding": [0.1]},
        {"index": 1, "object": "embedding", "embedding": [0.2]},
    ]


def test_local_network_companion_inference(gateway, monkeypatch, tmp_path):
    """Opt-in sockets + real SDKs + real LiteLLM, with a deterministic upstream."""
    import json
    import os
    import socket
    import subprocess
    import threading
    import time
    from contextlib import contextmanager

    import httpx
    import uvicorn
    from fastapi.responses import JSONResponse, StreamingResponse

    roots = {
        service: os.environ.get(f"COGNIS_E2E_{service.upper()}_ROOT")
        for service in ("intaris", "mnemory")
    }
    if not all(roots.values()):
        pytest.skip("Set COGNIS_E2E_INTARIS_ROOT and COGNIS_E2E_MNEMORY_ROOT for local E2E")
    client, app, auth, factory, configured_llm = gateway
    upstream = FastAPI()
    requests = []

    @upstream.post("/v1/chat/completions")
    async def completion(request: Request):
        payload = await request.json()
        unknown = set(payload) - {
            "model",
            "messages",
            "stream",
            "stream_options",
            "response_format",
            "reasoning_effort",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
        }
        if unknown:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": f"Unsupported request fields: {sorted(unknown)}",
                        "type": "invalid_request_error",
                    },
                },
            )
        requests.append(payload)
        usage = {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 7},
        }
        if payload.get("stream"):

            async def chunks():
                for delta in ({"role": "assistant"}, {"content": '{"ok":true}'}):
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "id": "chatcmpl-local",
                                "object": "chat.completion.chunk",
                                "created": 1,
                                "model": payload["model"],
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                            }
                        )
                        + "\n\n"
                    )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": "chatcmpl-local",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": payload["model"],
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": "chatcmpl-local",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": payload["model"],
                            "choices": [],
                            "usage": usage,
                        }
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(chunks(), media_type="text/event-stream")
        return {
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "created": 1,
            "model": payload["model"],
            "usage": usage,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"ok":true}'},
                    "finish_reason": "stop",
                }
            ],
        }

    @upstream.post("/v1/embeddings")
    async def embedding(request: Request):
        payload = await request.json()
        requests.append(payload)
        inputs = payload["input"]
        return {
            "object": "list",
            "model": payload["model"],
            "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
            "data": [
                {"index": index, "object": "embedding", "embedding": [float(index), 0.5]}
                for index in reversed(range(len(inputs)))
            ],
        }

    @contextmanager
    def serve(application):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application, lifespan="off", log_level="error"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            sock.close()
            assert not worker.is_alive()

    with serve(upstream) as provider_url:
        llm = LLMService(factory, usage_capture=configured_llm.usage_capture)

        async def configure_real_provider():
            async with factory() as session:
                provider = await session.get(LLMProvider, "shared-inference")
                provider.config = {
                    "preset": "openai_compatible",
                    "base_url": provider_url,
                    "default_model": "gpt-4o-mini",
                    "auth_config": {"mode": "env", "env_var": "COGNIS_TEST_INFERENCE_API_KEY"},
                    "models": [
                        {
                            "model_id": "gpt-4o-mini",
                            "supports_reasoning": True,
                            "supports_responses_api": False,
                            "reasoning_efforts": ["low", "medium", "high"],
                        },
                        {"model_id": "text-embedding-3-small", "supports_embedding": True},
                    ],
                }
                await upsert_model_routing(
                    session,
                    task_type="companion_lite",
                    model="gpt-4o-mini",
                    provider_id="shared-inference",
                    owner_email=SYSTEM_USER_EMAIL,
                    config={"reasoning_effort": "low"},
                )
                await upsert_model_routing(
                    session,
                    task_type="companion_smart",
                    model="gpt-4o-mini",
                    provider_id="shared-inference",
                    owner_email=SYSTEM_USER_EMAIL,
                    config={"reasoning_effort": "high"},
                )
                await session.commit()

        client.portal.call(configure_real_provider)
        monkeypatch.setenv("COGNIS_TEST_INFERENCE_API_KEY", "local-fixture-credential")
        failures = []
        original_generate = llm.generate

        async def record_failure(*args, **kwargs):
            try:
                return await original_generate(*args, **kwargs)
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
                raise

        monkeypatch.setattr(llm, "generate", record_failure)
        app.state.inference_api.llm = llm
        with serve(app) as gateway_url:
            for service, root in roots.items():
                token_file = tmp_path / f"{service}.jwt"
                token_file.write_text(auth.sign_inference_companion_token(service, 0))
                source = f"""
import json
from {service}.config import load_config
from {service}.llm import LLMClient
config = load_config()
"""
                if service == "intaris":
                    source += """
for role in (config.llm, config.llm_analysis, config.llm_l3_analysis, config.llm_judge):
    assert role.reasoning_effort is None
    client = LLMClient(role)
    result = client.generate([{"role": "user", "content": "Return JSON"}])
    assert json.loads(result) == {"ok": True}, result
    client.close()
"""
                if service == "mnemory":
                    source += """
from dataclasses import replace
for role, effort in (
    (config.llm.model, config.llm.reasoning_effort),
    (config.memory.find_model, config.memory.find_reasoning_effort),
    (config.memory.consolidation_model, config.memory.consolidation_reasoning_effort),
    (config.memory.fsck_model, config.memory.fsck_reasoning_effort),
):
    assert effort is None
    client = LLMClient(replace(config.llm, model=role, reasoning_effort=effort))
    result = client.generate([{"role": "user", "content": "Return JSON"}])
    assert json.loads(result) == {"ok": True}, result
    client._client.close()
from mnemory.embeddings import EmbeddingClient
vectors = EmbeddingClient(config.embed).embed_batch(["first", "second"])
assert vectors == [[0.0, 0.5], [1.0, 0.5]], vectors
"""
                environment = {
                    **os.environ,
                    "COGNIS_INFERENCE_URL": gateway_url + "/v1",
                    "COGNIS_INFERENCE_TOKEN_FILE": str(token_file),
                    "EMBED_DIMS": "2",
                    "JWT_PUBLIC_KEY": "",
                }
                for key in tuple(environment):
                    if "REASONING_EFFORT" in key:
                        environment.pop(key)
                completed = subprocess.run(
                    [str(Path(root) / ".venv/bin/python"), "-c", source],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert completed.returncode == 0, (
                    str(failures) + completed.stdout + completed.stderr
                )
            assert [
                request.get("reasoning_effort") for request in requests if "messages" in request
            ] == ["low", "high", "high", "high", "low", "low", "high", "high"]
            # Exercise the same provider check the settings UI uses, with both
            # supported URL forms, rather than mocking the check's transport.
            for suffix in ("", "/v1"):

                async def configure_base(current_suffix=suffix):
                    async with factory() as session:
                        provider = await session.get(LLMProvider, "shared-inference")
                        provider.config = {
                            **provider.config,
                            "base_url": provider_url + current_suffix,
                        }
                        await session.commit()

                client.portal.call(configure_base)
                checked = client.portal.call(llm.test_provider, "shared-inference")
                assert checked["ok"], checked
            headers = bearer(auth.sign_access_token("operator@cognis.local", None, "admin"))
            with httpx.Client(timeout=30) as network:
                stream = network.post(
                    gateway_url + "/v1/chat/completions",
                    headers=headers,
                    json={
                        **body("providers/shared-inference/gpt-4o-mini"),
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                )
                assert stream.status_code == 200, stream.text
                assert "[DONE]" in stream.text and "cached_tokens" in stream.text, stream.text
                revoked = client.delete(
                    "/api/v1/inference/companions/intaris/token", headers=headers
                )
                assert revoked.status_code == 200
                denied = network.post(
                    gateway_url + "/v1/chat/completions",
                    headers=bearer(auth.sign_inference_companion_token("intaris", 0)),
                    json=body(),
                )
                assert denied.status_code == 401
            assert app.state.inference_api._total == 0
    assert len(requests) >= 4
