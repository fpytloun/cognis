from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from cognis.api.app import create_app
from cognis.core import local_model_service
from cognis.core.local_model_catalog import LocalModelCatalog
from cognis.core.local_model_service import (
    LocalModelDeploymentService,
    LocalModelValidationError,
)
from cognis.models.local_models import LocalModelRuntimeOperationCreateRequest
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.models import (
    ExecutorRow,
    LLMProvider,
    LocalModelDeployment,
    LocalModelTargetStatus,
)
from cognis.store.queries import create_executor, create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(  # type: ignore[attr-defined]
        email,
        email.split("@")[0].title(),
        role,
    )
    return {"Authorization": f"Bearer {token}"}


def _confirm_executor(executor: ExecutorRow) -> None:
    executor.runtime_state = "active"
    executor.desired_config_version = 1
    executor.applied_config_version = 1
    executor.runtime_metadata = {
        "local_inference_enabled": True,
        "ollama_runtime": {
            "runtime_type": "ollama",
            "port": 11434,
            "endpoint": "http://127.0.0.1:11434",
            "management_enabled": True,
            "max_concurrent_pulls": 1,
            "disk_headroom_bytes": 5 * 1024**3,
        },
    }


async def _seed(app: object) -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        for email, role in [
            ("owner@example.com", "user"),
            ("other@example.com", "user"),
            ("viewer@example.com", "viewer"),
            ("admin@example.com", "admin"),
        ]:
            await create_user(
                session,
                email=email,
                name=email.split("@")[0].title(),
                password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
                role=role,
            )
        owner_gpu = await create_executor(
            session,
            executor_id="owner-gpu",
            name="Owner GPU",
            executor_type="websocket",
            owner_email="owner@example.com",
            labels={"gpu": "nvidia", "site": "lab"},
        )
        owner_cpu = await create_executor(
            session,
            executor_id="owner-cpu",
            name="Owner CPU",
            executor_type="websocket",
            owner_email="owner@example.com",
            labels={"gpu": "none", "site": "lab"},
        )
        other_gpu = await create_executor(
            session,
            executor_id="other-gpu",
            name="Other GPU",
            executor_type="websocket",
            owner_email="other@example.com",
            labels={"gpu": "nvidia", "site": "lab"},
        )
        shared_gpu = await create_executor(
            session,
            executor_id="shared-gpu",
            name="Shared GPU",
            executor_type="websocket",
            owner_email="admin@example.com",
            labels={"gpu": "nvidia", "site": "lab"},
            shared=True,
        )
        for executor in (owner_gpu, owner_cpu, other_gpu, shared_gpu):
            _confirm_executor(executor)
        session.add_all(
            [
                LLMProvider(
                    provider_id="owner-ollama",
                    display_name="Owner Ollama",
                    location="executor",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={
                        "preset": "ollama",
                        "executor_id": "owner-gpu",
                        "unknown": {"keep": True},
                        "models": [
                            {"model_id": "llama3.2:latest", "first": True},
                            {"model_id": "llama3.2:latest", "second": True},
                        ],
                    },
                    status="active",
                ),
                LLMProvider(
                    provider_id="shared-ollama",
                    display_name="Shared Ollama",
                    location="executor",
                    backend="litellm",
                    owner_email=SYSTEM_USER_EMAIL,
                    config={
                        "preset": "ollama",
                        "executor_id": "shared-gpu",
                        "models": [],
                    },
                    status="active",
                ),
                LLMProvider(
                    provider_id="owner-ollama-alt",
                    display_name="Owner Ollama Alt",
                    location="executor",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={
                        "preset": "ollama",
                        "executor_id": "owner-gpu",
                        "models": [],
                    },
                    status="active",
                ),
            ]
        )
        await session.commit()


def test_catalog_detail_decode_failure_returns_documented_upstream_error(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=b"not-gzip",
        )

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        asyncio.run(client.app.state.local_model_catalog.aclose())
        client.app.state.local_model_catalog = LocalModelCatalog(client=transport_client)
        response = client.get(
            "/api/v1/local-model-catalog/detail",
            params={"repo": "acme/model-GGUF"},
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
    asyncio.run(transport_client.aclose())

    assert calls == 2
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "huggingface_detail_unavailable",
            "message": "Hugging Face returned an invalid encoded response.",
            "details": {"retry_after_seconds": None},
        }
    }


def test_local_model_crud_rbac_selector_resolution_and_pending_contract(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        owner_headers = _auth_headers(client.app, email="owner@example.com")
        other_headers = _auth_headers(client.app, email="other@example.com")
        viewer_headers = _auth_headers(
            client.app,
            email="viewer@example.com",
            role="viewer",
        )
        admin_headers = _auth_headers(
            client.app,
            email="admin@example.com",
            role="admin",
        )

        created = client.post(
            "/api/v1/local-model-deployments",
            headers=owner_headers,
            json={
                "requested_ref": "llama3.2",
                "selector": {"match_labels": {"gpu": "nvidia"}},
                "provider_id": "owner-ollama",
                "capacity_override_acknowledged": True,
                "capacity_assessment_generation": 3,
            },
        )
        assert created.status_code == 201, created.text
        deployment = created.json()
        deployment_id = deployment["deployment_id"]
        assert deployment["canonical_name"] == "llama3.2:latest"
        assert deployment["generation"] == 1
        assert deployment["capacity_override_acknowledged"] is True

        targets = client.get(
            f"/api/v1/local-model-deployments/{deployment_id}/targets",
            headers=owner_headers,
        )
        assert targets.status_code == 200
        assert [(target["executor_id"], target["state"]) for target in targets.json()] == [
            ("owner-gpu", "pending")
        ]
        operations = client.get(
            f"/api/v1/local-model-deployments/{deployment_id}/operations",
            headers=owner_headers,
        )
        assert operations.status_code == 200
        assert operations.json() == []
        missing_provider = client.post(
            "/api/v1/local-model-deployments",
            headers=owner_headers,
            json={
                "requested_ref": "gemma3:4b",
                "selector": {"executor_ids": ["owner-gpu"]},
            },
        )
        assert missing_provider.status_code == 422

        assert (
            client.get(
                f"/api/v1/local-model-deployments/{deployment_id}",
                headers=other_headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/local-model-deployments",
                headers=viewer_headers,
                json={
                    "requested_ref": "gemma3:4b",
                    "selector": {"executor_ids": ["owner-gpu"]},
                    "provider_id": "owner-ollama",
                },
            ).status_code
            == 403
        )
        shared_target_denied = client.post(
            "/api/v1/local-model-deployments",
            headers=owner_headers,
            json={
                "requested_ref": "gemma3:4b",
                "selector": {"executor_ids": ["shared-gpu"]},
                "provider_id": "shared-ollama",
            },
        )
        assert shared_target_denied.status_code == 403
        private_target_hidden = client.post(
            "/api/v1/local-model-deployments",
            headers=owner_headers,
            json={
                "requested_ref": "gemma3:4b",
                "selector": {"executor_ids": ["other-gpu"]},
                "provider_id": "owner-ollama",
            },
        )
        assert private_target_hidden.status_code == 404
        null_patch = client.patch(
            f"/api/v1/local-model-deployments/{deployment_id}",
            headers=owner_headers,
            json={"desired_state": None},
        )
        assert null_patch.status_code == 422

        patched = client.patch(
            f"/api/v1/local-model-deployments/{deployment_id}",
            headers=owner_headers,
            json={
                "desired_state": "absent",
                "prune_policy": "retain",
                "capacity_assessment_generation": 4,
            },
        )
        assert patched.status_code == 200
        assert patched.json()["generation"] == 2
        assert patched.json()["desired_state"] == "absent"

        reconcile = client.post(
            f"/api/v1/local-model-deployments/{deployment_id}/reconciliation-requests",
            headers=owner_headers,
        )
        assert reconcile.status_code == 202
        assert reconcile.json()["generation"] == 3
        assert reconcile.json()["reconcile_requested_at"] is not None
        assert (
            client.get(
                f"/api/v1/local-model-deployments/{deployment_id}/operations",
                headers=owner_headers,
            ).json()
            == []
        )
        provider_deleted = client.delete(
            "/api/v1/llm-providers/owner-ollama",
            headers=owner_headers,
        )
        assert provider_deleted.status_code == 409
        after_provider_delete = client.get(
            f"/api/v1/local-model-deployments/{deployment_id}",
            headers=owner_headers,
        )
        assert after_provider_delete.status_code == 200
        assert after_provider_delete.json()["provider_id"] == "owner-ollama"

        shared_created = client.post(
            "/api/v1/local-model-deployments",
            headers=admin_headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"executor_ids": ["shared-gpu"]},
                "provider_id": "shared-ollama",
                "shared": True,
            },
        )
        assert shared_created.status_code == 201, shared_created.text
        shared_deployment_id = shared_created.json()["deployment_id"]
        assert shared_created.json()["shared"] is True
        assert (
            client.get(
                f"/api/v1/local-model-deployments/{shared_deployment_id}",
                headers=owner_headers,
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"/api/v1/local-model-deployments/{shared_deployment_id}",
                headers=owner_headers,
                json={"max_parallel": 2},
            ).status_code
            == 403
        )
        admin_private = client.post(
            "/api/v1/local-model-deployments",
            headers=admin_headers,
            json={
                "requested_ref": "gemma3:4b",
                "selector": {"executor_ids": ["shared-gpu"]},
                "provider_id": "shared-ollama",
            },
        )
        assert admin_private.status_code == 201, admin_private.text
        admin_reconcile = client.post(
            f"/api/v1/local-model-deployments/{admin_private.json()['deployment_id']}"
            "/reconciliation-requests",
            headers=admin_headers,
        )
        assert admin_reconcile.status_code == 202, admin_reconcile.text
        admin_targets = client.get(
            f"/api/v1/local-model-deployments/{admin_private.json()['deployment_id']}/targets",
            headers=admin_headers,
        )
        assert [target["executor_id"] for target in admin_targets.json()] == ["shared-gpu"]

        deleted = client.delete(
            f"/api/v1/local-model-deployments/{deployment_id}",
            headers=owner_headers,
        )
        assert deleted.status_code == 204
        assert (
            client.get(
                f"/api/v1/local-model-deployments/{deployment_id}",
                headers=owner_headers,
            ).status_code
            == 404
        )


def test_atomic_provider_model_upsert_preserves_fields_and_enforces_rbac(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        owner_headers = _auth_headers(client.app, email="owner@example.com")
        viewer_headers = _auth_headers(
            client.app,
            email="viewer@example.com",
            role="viewer",
        )

        upserted = client.post(
            "/api/v1/llm-providers/owner-ollama/local-models:upsert",
            headers=owner_headers,
            json={
                "requested_ref": "llama3.2",
                "model_config": {"context_window": 32768},
                "set_default": True,
            },
        )
        assert upserted.status_code == 200, upserted.text
        config = upserted.json()["config"]
        assert config["unknown"] == {"keep": True}
        assert config["default_model"] == "llama3.2:latest"
        matching = [
            model
            for model in config["models"]
            if isinstance(model, dict) and model.get("model_id") == "llama3.2:latest"
        ]
        assert matching == [
            {
                "model_id": "llama3.2:latest",
                "first": True,
                "second": True,
                "context_window": 32768,
            }
        ]
        second_default = client.post(
            "/api/v1/llm-providers/owner-ollama/local-models:upsert",
            headers=owner_headers,
            json={
                "requested_ref": "gemma3:4b",
                "set_default": True,
            },
        )
        assert second_default.status_code == 200
        assert second_default.json()["config"]["default_model"] == "gemma3:4b"

        shared_denied = client.post(
            "/api/v1/llm-providers/shared-ollama/local-models:upsert",
            headers=owner_headers,
            json={"requested_ref": "gemma3:4b"},
        )
        assert shared_denied.status_code == 403
        viewer_denied = client.post(
            "/api/v1/llm-providers/owner-ollama/local-models:upsert",
            headers=viewer_headers,
            json={"requested_ref": "gemma3:4b"},
        )
        assert viewer_denied.status_code == 403


def test_provider_recommendation_find_or_create_subset_and_legacy_state(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        headers = _auth_headers(client.app, email="owner@example.com")

        recommendation = client.post(
            "/api/v1/local-model-providers/recommendations",
            headers=headers,
            json={
                "requested_ref": "llama3.2",
                "selector": {"executor_ids": ["owner-gpu"]},
            },
        )
        assert recommendation.status_code == 200, recommendation.text
        body = recommendation.json()
        assert body["recommended_provider_id"] == "owner-ollama"
        assert body["candidates"][0]["reason_codes"] == [
            "compatible_ollama_provider",
            "target_subset",
            "model_already_configured",
            "healthy_hosts",
            "user_owned",
        ]

        outside_scope = client.post(
            "/api/v1/local-model-deployments",
            headers=headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"executor_ids": ["owner-cpu"]},
                "provider_id": "owner-ollama",
            },
        )
        assert outside_scope.status_code == 422
        assert "subset" in outside_scope.json()["error"]["message"]

        label_deployment = client.post(
            "/api/v1/local-model-deployments",
            headers=headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"match_labels": {"gpu": "nvidia"}},
                "provider_id": "owner-ollama",
            },
        )
        assert label_deployment.status_code == 201, label_deployment.text

        async def expand_label_selector() -> None:
            async with client.app.state.session_factory() as session:
                executor = await session.get(ExecutorRow, "owner-cpu")
                assert executor is not None
                executor.labels = {"gpu": "nvidia", "site": "lab"}
                executor.desired_config_version = 2
                executor.runtime_state = "reconfiguring"
                await session.commit()

        asyncio.run(expand_label_selector())
        invalid_reconcile = client.post(
            f"/api/v1/local-model-deployments/{label_deployment.json()['deployment_id']}"
            "/reconciliation-requests",
            headers=headers,
        )
        assert invalid_reconcile.status_code == 422
        unchanged = client.get(
            f"/api/v1/local-model-deployments/{label_deployment.json()['deployment_id']}",
            headers=headers,
        )
        assert unchanged.json()["generation"] == 1

        find_payload = {
            "requested_ref": "qwen3:8b",
            "selector": {"executor_ids": ["owner-cpu"]},
        }
        first = client.post(
            "/api/v1/local-model-providers:find-or-create",
            headers=headers,
            json=find_payload,
        )
        second = client.post(
            "/api/v1/local-model-providers:find-or-create",
            headers=headers,
            json=find_payload,
        )
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True
        assert second.status_code == 200, second.text
        assert second.json() == {
            "provider_id": first.json()["provider_id"],
            "created": False,
            "reason_code": "reused_eligible_provider",
        }
        immutable_routing = client.put(
            f"/api/v1/llm-providers/{first.json()['provider_id']}",
            headers=headers,
            json={
                "config": {
                    "preset": "ollama",
                    "executor_id": "owner-gpu",
                    "models": [],
                }
            },
        )
        assert immutable_routing.status_code == 409, immutable_routing.text

        managed = client.post(
            "/api/v1/local-model-deployments:managed",
            headers=headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"executor_ids": ["owner-cpu"]},
            },
        )
        assert managed.status_code == 201, managed.text
        assert managed.json()["provider_id"] == first.json()["provider_id"]
        assert managed.json()["provider_created"] is False
        assert managed.json()["deployment"]["lifecycle_state"] == "managed"

        async def seed_legacy() -> None:
            async with client.app.state.session_factory() as session:
                session.add(
                    LocalModelDeployment(
                        deployment_id="legacy-needs-provider",
                        owner_email="owner@example.com",
                        requested_ref="gemma3:4b",
                        canonical_name="gemma3:4b",
                        runtime_name="gemma3:4b",
                        source="ollama",
                        revision="4b",
                        selector={
                            "executor_ids": ["owner-gpu"],
                            "match_labels": {},
                        },
                    )
                )
                await session.commit()

        asyncio.run(seed_legacy())
        legacy = client.get(
            "/api/v1/local-model-deployments/legacy-needs-provider",
            headers=headers,
        )
        assert legacy.status_code == 200
        assert legacy.json()["provider_id"] is None
        assert legacy.json()["lifecycle_state"] == "needs_provider"
        cannot_reconcile = client.post(
            "/api/v1/local-model-deployments/legacy-needs-provider/reconciliation-requests",
            headers=headers,
        )
        assert cannot_reconcile.status_code == 422
        repaired = client.patch(
            "/api/v1/local-model-deployments/legacy-needs-provider",
            headers=headers,
            json={"provider_id": "owner-ollama"},
        )
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["lifecycle_state"] == "managed"
        can_reconcile = client.post(
            "/api/v1/local-model-deployments/legacy-needs-provider/reconciliation-requests",
            headers=headers,
        )
        assert can_reconcile.status_code == 202, can_reconcile.text

        referenced = client.post(
            "/api/v1/local-model-deployments",
            headers=headers,
            json={
                "requested_ref": "llama3.2",
                "selector": {"executor_ids": ["owner-gpu"]},
                "provider_id": "owner-ollama",
            },
        )
        assert referenced.status_code == 201, referenced.text
        provider_update = client.put(
            "/api/v1/llm-providers/owner-ollama",
            headers=headers,
            json={
                "config": {
                    "preset": "ollama",
                    "executor_id": "owner-cpu",
                    "models": [],
                }
            },
        )
        assert provider_update.status_code == 409
        assert provider_update.json()["error"]["code"] == "local_model_dependencies"
        operation = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=headers,
            json={
                "deployment_id": referenced.json()["deployment_id"],
                "action": "pull",
                "idempotency_key": "provider-update-active",
            },
        )
        assert operation.status_code == 202, operation.text
        active_update = client.put(
            "/api/v1/llm-providers/owner-ollama",
            headers=headers,
            json={
                "config": {
                    "preset": "ollama",
                    "executor_id": "owner-cpu",
                    "models": [],
                }
            },
        )
        assert active_update.status_code == 409
        assert "operations are active" in active_update.json()["error"]["message"]
        reassignment = client.patch(
            f"/api/v1/local-model-deployments/{referenced.json()['deployment_id']}",
            headers=headers,
            json={"provider_id": "owner-ollama-alt"},
        )
        assert reassignment.status_code == 409


def test_managed_provider_repair_rolls_back_provider_when_attachment_fails(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        headers = _auth_headers(client.app, email="owner@example.com")

        async def seed_legacy() -> None:
            async with client.app.state.session_factory() as session:
                session.add(
                    LocalModelDeployment(
                        deployment_id="legacy-atomic-repair",
                        owner_email="owner@example.com",
                        requested_ref="qwen3:8b",
                        canonical_name="qwen3:8b",
                        runtime_name="qwen3:8b",
                        source="ollama",
                        revision="8b",
                        selector={
                            "executor_ids": ["owner-cpu"],
                            "match_labels": {},
                        },
                    )
                )
                await session.commit()

        asyncio.run(seed_legacy())
        original_update = LocalModelDeploymentService.update_deployment

        async def fail_attachment(*args: object, **kwargs: object) -> object:
            raise LocalModelValidationError("simulated attachment failure")

        monkeypatch.setattr(  # type: ignore[attr-defined]
            LocalModelDeploymentService,
            "update_deployment",
            fail_attachment,
        )
        failed = client.post(
            "/api/v1/local-model-deployments/legacy-atomic-repair:attach-managed-provider",
            headers=headers,
            json={"force_create_provider": True},
        )
        assert failed.status_code == 422
        monkeypatch.setattr(  # type: ignore[attr-defined]
            LocalModelDeploymentService,
            "update_deployment",
            original_update,
        )

        async def count_created_providers() -> int:
            async with client.app.state.session_factory() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(LLMProvider)
                            .where(LLMProvider.managed_local_key.is_not(None))
                        )
                    ).scalar_one()
                )

        assert asyncio.run(count_created_providers()) == 0
        deployment = client.get(
            "/api/v1/local-model-deployments/legacy-atomic-repair",
            headers=headers,
        )
        assert deployment.json()["lifecycle_state"] == "needs_provider"


def test_provider_recommendation_uses_effective_local_inference_eligibility(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        headers = _auth_headers(client.app, email="owner@example.com")

        # Missing legacy settings retain WS-A's effective enabled defaults after reconnect.
        legacy_default = client.post(
            "/api/v1/local-model-providers/recommendations",
            headers=headers,
            json={
                "requested_ref": "llama3.2",
                "selector": {"executor_ids": ["owner-gpu"]},
            },
        )
        assert legacy_default.status_code == 200, legacy_default.text
        assert legacy_default.json()["recommended_provider_id"] == "owner-ollama"

        async def disable_management() -> None:
            async with client.app.state.session_factory() as session:
                executor = await session.get(ExecutorRow, "owner-cpu")
                assert executor is not None
                executor.config = {
                    "local_inference_enabled": True,
                    "ollama_runtime": {"management_enabled": False},
                }
                session.add(
                    LLMProvider(
                        provider_id="disabled-ollama",
                        display_name="Disabled Ollama",
                        location="executor",
                        backend="litellm",
                        owner_email="owner@example.com",
                        config={
                            "preset": "ollama",
                            "executor_id": "owner-cpu",
                            "models": [],
                        },
                        status="active",
                    )
                )
                await session.commit()

        asyncio.run(disable_management())
        recommendation = client.post(
            "/api/v1/local-model-providers/recommendations",
            headers=headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"executor_ids": ["owner-cpu"]},
            },
        )
        assert recommendation.status_code == 422
        assert "Allow Cognis to manage Ollama models" in recommendation.json()["error"]["message"]

        managed = client.post(
            "/api/v1/local-model-deployments:managed",
            headers=headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"executor_ids": ["owner-cpu"]},
                "force_create_provider": True,
            },
        )
        assert managed.status_code == 422
        assert "executor settings" in managed.json()["error"]["message"]


def test_runtime_operations_require_exact_authorized_targets_and_safe_payloads(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        owner_headers = _auth_headers(client.app, email="owner@example.com")
        other_headers = _auth_headers(client.app, email="other@example.com")
        viewer_headers = _auth_headers(
            client.app,
            email="viewer@example.com",
            role="viewer",
        )
        admin_headers = _auth_headers(
            client.app,
            email="admin@example.com",
            role="admin",
        )

        present = client.post(
            "/api/v1/local-model-deployments",
            headers=owner_headers,
            json={
                "requested_ref": "llama3.2",
                "selector": {"executor_ids": ["owner-gpu"]},
                "provider_id": "owner-ollama",
            },
        ).json()
        absent = client.post(
            "/api/v1/local-model-deployments",
            headers=owner_headers,
            json={
                "requested_ref": "llama3.2",
                "selector": {"executor_ids": ["owner-gpu"]},
                "provider_id": "owner-ollama",
                "desired_state": "absent",
                "prune_policy": "delete",
            },
        ).json()

        unsafe = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=owner_headers,
            json={
                "deployment_id": absent["deployment_id"],
                "action": "delete",
                "idempotency_key": "unsafe-endpoint",
                "endpoint": "http://169.254.169.254",
            },
        )
        assert unsafe.status_code == 422

        dependent = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=owner_headers,
            json={
                "deployment_id": absent["deployment_id"],
                "action": "delete",
                "idempotency_key": "delete-with-dependency",
            },
        )
        assert dependent.status_code == 409
        assert dependent.json()["error"]["code"] == "local_model_dependencies"
        assert dependent.json()["error"]["details"]["deployment_ids"] == [present["deployment_id"]]

        wrong_owner = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=other_headers,
            json={
                "deployment_id": absent["deployment_id"],
                "action": "delete",
                "idempotency_key": "wrong-owner",
            },
        )
        assert wrong_owner.status_code == 404
        viewer_denied = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=viewer_headers,
            json={
                "deployment_id": absent["deployment_id"],
                "action": "delete",
                "idempotency_key": "viewer-denied",
            },
        )
        assert viewer_denied.status_code == 403

        shared = client.post(
            "/api/v1/local-model-deployments",
            headers=admin_headers,
            json={
                "requested_ref": "gemma3:4b",
                "selector": {"executor_ids": ["shared-gpu"]},
                "provider_id": "shared-ollama",
                "shared": True,
            },
        ).json()
        shared_denied = client.post(
            "/api/v1/executors/shared-gpu/local-model-runtime/operations",
            headers=owner_headers,
            json={
                "deployment_id": shared["deployment_id"],
                "action": "pull",
                "idempotency_key": "shared-user-denied",
            },
        )
        assert shared_denied.status_code == 403

        shared_read = client.get(
            "/api/v1/executors/shared-gpu/local-model-runtime",
            headers=viewer_headers,
        )
        assert shared_read.status_code == 409
        assert shared_read.json()["error"]["code"] == "local_model_runtime_unavailable"


def test_runtime_operations_require_current_provider_scoped_target(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        headers = _auth_headers(client.app, email="owner@example.com")

        async def seed_legacy_target() -> None:
            async with client.app.state.session_factory() as session:
                session.add(
                    LocalModelDeployment(
                        deployment_id="legacy-runtime-operation",
                        owner_email="owner@example.com",
                        requested_ref="qwen3:8b",
                        canonical_name="qwen3:8b",
                        runtime_name="qwen3:8b",
                        source="ollama",
                        revision="8b",
                        selector={
                            "executor_ids": ["owner-gpu"],
                            "match_labels": {},
                        },
                    )
                )
                session.add(
                    LocalModelTargetStatus(
                        target_id="legacy-runtime-operation-owner-gpu",
                        deployment_id="legacy-runtime-operation",
                        executor_id="owner-gpu",
                        generation=1,
                    )
                )
                await session.commit()

        asyncio.run(seed_legacy_target())
        needs_provider = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=headers,
            json={
                "deployment_id": "legacy-runtime-operation",
                "action": "pull",
                "idempotency_key": "needs-provider-denied",
            },
        )
        assert needs_provider.status_code == 422
        assert "requires an Ollama provider" in needs_provider.json()["error"]["message"]
        needs_provider_delete = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=headers,
            json={
                "deployment_id": "legacy-runtime-operation",
                "action": "delete",
                "idempotency_key": "needs-provider-delete-denied",
            },
        )
        assert needs_provider_delete.status_code == 422
        assert "requires an Ollama provider" in needs_provider_delete.json()["error"]["message"]

        deployment = client.post(
            "/api/v1/local-model-deployments",
            headers=headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"executor_ids": ["owner-gpu"]},
                "provider_id": "owner-ollama",
            },
        )
        assert deployment.status_code == 201, deployment.text
        deployment_id = deployment.json()["deployment_id"]

        async def move_provider_scope(executor_id: str) -> None:
            async with client.app.state.session_factory() as session:
                provider = await session.get(LLMProvider, "owner-ollama")
                assert provider is not None
                provider.config = {**provider.config, "executor_id": executor_id}
                await session.commit()

        asyncio.run(move_provider_scope("owner-cpu"))
        outside_provider = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=headers,
            json={
                "deployment_id": deployment_id,
                "action": "pull",
                "idempotency_key": "outside-provider-denied",
            },
        )
        assert outside_provider.status_code == 404
        assert outside_provider.json()["error"]["message"] == (
            "local-model deployment target not found"
        )

        asyncio.run(move_provider_scope("owner-gpu"))
        valid_deployment = client.post(
            "/api/v1/local-model-deployments",
            headers=headers,
            json={
                "requested_ref": "gemma3:4b",
                "selector": {"executor_ids": ["owner-gpu"]},
                "provider_id": "owner-ollama",
            },
        )
        assert valid_deployment.status_code == 201, valid_deployment.text
        valid = client.post(
            "/api/v1/executors/owner-gpu/local-model-runtime/operations",
            headers=headers,
            json={
                "deployment_id": valid_deployment.json()["deployment_id"],
                "action": "pull",
                "idempotency_key": "provider-scoped-valid",
            },
        )
        assert valid.status_code == 202, valid.text
        assert valid.json()["executor_id"] == "owner-gpu"


def test_runtime_operation_provider_scope_check_serializes_with_scope_update(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        headers = _auth_headers(client.app, email="owner@example.com")
        deployment = client.post(
            "/api/v1/local-model-deployments",
            headers=headers,
            json={
                "requested_ref": "qwen3:8b",
                "selector": {"executor_ids": ["owner-gpu"]},
                "provider_id": "owner-ollama",
            },
        )
        assert deployment.status_code == 201, deployment.text
        deployment_id = deployment.json()["deployment_id"]

        async def race() -> tuple[str, str]:
            entered_scope_check = asyncio.Event()
            release_scope_check = asyncio.Event()
            original_resolver = local_model_service.resolve_provider_scoped_deployment_executors

            async def paused_resolver(
                session: object,
                row: LocalModelDeployment,
            ) -> list[ExecutorRow]:
                entered_scope_check.set()
                await release_scope_check.wait()
                return await original_resolver(session, row)  # type: ignore[arg-type]

            monkeypatch.setattr(  # type: ignore[attr-defined]
                local_model_service,
                "resolve_provider_scoped_deployment_executors",
                paused_resolver,
            )

            async def create_operation() -> str:
                async with client.app.state.session_factory() as session:
                    service = LocalModelDeploymentService(
                        session,
                        actor_email="owner@example.com",
                        actor_role="user",
                    )
                    operation = await service.create_runtime_operation(
                        "owner-gpu",
                        LocalModelRuntimeOperationCreateRequest(
                            deployment_id=deployment_id,
                            action="pull",
                            idempotency_key="scope-race",
                        ),
                    )
                    await session.commit()
                    return operation.operation_id

            async def update_scope() -> str:
                await entered_scope_check.wait()
                response = await asyncio.to_thread(
                    client.put,
                    "/api/v1/executors/owner-gpu",
                    headers=headers,
                    json={"config": {"local_inference_enabled": False}},
                )
                assert response.status_code == 200, response.text
                return str(response.json()["runtime_state"])

            operation_task = asyncio.create_task(create_operation())
            update_task = asyncio.create_task(update_scope())
            await entered_scope_check.wait()
            await asyncio.sleep(0.05)
            assert not update_task.done()
            release_scope_check.set()
            return await asyncio.gather(operation_task, update_task)

        operation_id, runtime_state = asyncio.run(race())
        assert operation_id.startswith("lmo_")
        assert runtime_state in {"stale", "reconfiguring"}


def test_catalog_and_fit_plan_api_are_typed_advisory_surfaces(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed(client.app))
        headers = _auth_headers(client.app, email="owner@example.com")

        catalog = client.get(
            "/api/v1/local-model-catalog?source=ollama&query=qwen",
            headers=headers,
        )
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["items"][0]["requested_ref"] == "qwen3:8b"
        assert catalog.json()["sources"][0]["source"] == "installed"

        direct = client.get(
            "/api/v1/local-model-catalog/resolve",
            params={"ref": "hf.co/acme/model:Q4_K_M"},
            headers=headers,
        )
        assert direct.status_code == 200
        assert direct.json()["requested_ref"] == "hf.co/acme/model:Q4_K_M"

        fit = client.post(
            "/api/v1/local-model-fit-plans",
            headers=headers,
            json={
                "model": {
                    "requested_ref": "qwen3:8b",
                    "weights_bytes": 4 * 1024**3,
                    "layer_count": 32,
                    "kv_head_count": 8,
                    "head_dimension": 128,
                    "advertised_max_context": 16_384,
                },
                "selector": {"executor_ids": ["owner-gpu"]},
                "context_tokens": 200_000,
            },
        )
        assert fit.status_code == 200, fit.text
        payload = fit.json()
        assert payload["advisory_only"] is True
        assert payload["requested_context_tokens"] == 200_000
        assert payload["advertised_max_exceeded"] is True
        assert payload["executors"][0]["admission"]["status"] == "UNKNOWN"
        partial_provider_fit = client.post(
            "/api/v1/local-model-fit-plans",
            headers=headers,
            json={
                "model": {"requested_ref": "qwen3:8b"},
                "selector": {"match_labels": {"site": "lab"}},
                "provider_id": "owner-ollama",
                "context_tokens": 8192,
            },
        )
        assert partial_provider_fit.status_code == 422
        assert "subset" in partial_provider_fit.json()["error"]["message"]

        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        document = openapi.json()
        paths = document["paths"]
        assert paths["/api/v1/local-model-catalog"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/LocalModelCatalogResponse"}
        assert paths["/api/v1/local-model-catalog/resolve"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/LocalModelCatalogItem"}
        assert paths["/api/v1/local-model-catalog/detail"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/LocalModelCatalogItem"}
        assert paths["/api/v1/local-model-fit-plans"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/LocalModelFitPlanResponse"}
        assert paths["/api/v1/local-model-fit-plans"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/LocalModelFitPlanRequest"}

        schemas = document["components"]["schemas"]

        def assert_shape(
            name: str,
            properties: set[str],
            required: set[str],
        ) -> None:
            component = schemas[name]
            assert set(component["properties"]) == properties
            assert set(component.get("required", [])) == required

        def non_null(schema: dict[str, object]) -> dict[str, object]:
            variants = schema.get("anyOf")
            if not isinstance(variants, list):
                return schema
            return next(
                variant
                for variant in variants
                if isinstance(variant, dict) and variant.get("type") != "null"
            )

        catalog_parameters = {
            parameter["name"]: parameter["schema"]
            for parameter in paths["/api/v1/local-model-catalog"]["get"]["parameters"]
        }
        assert set(catalog_parameters) == {
            "source",
            "query",
            "cursor",
            "limit",
            "parameter_range",
            "download_size_range",
            "quantization",
            "min_context",
            "include_unknown",
        }
        assert catalog_parameters["query"]["maxLength"] == 100
        assert non_null(catalog_parameters["cursor"])["maxLength"] == 512
        assert catalog_parameters["limit"] == {
            "type": "integer",
            "maximum": 24,
            "minimum": 1,
            "default": 20,
            "title": "Limit",
        }
        resolve_parameters = {
            parameter["name"]: parameter["schema"]
            for parameter in paths["/api/v1/local-model-catalog/resolve"]["get"]["parameters"]
        }
        assert set(resolve_parameters) == {"ref"}
        assert resolve_parameters["ref"]["minLength"] == 1
        assert resolve_parameters["ref"]["maxLength"] == 255
        detail_parameters = {
            parameter["name"]: parameter["schema"]
            for parameter in paths["/api/v1/local-model-catalog/detail"]["get"]["parameters"]
        }
        assert set(detail_parameters) == {"repo", "revision_sha"}
        assert detail_parameters["repo"]["maxLength"] == 193
        assert non_null(detail_parameters["revision_sha"])["maxLength"] == 40

        assert_shape(
            "LocalModelCatalogResponse",
            {"items", "next_cursor", "sources", "cached", "pagination_note"},
            {"items", "sources"},
        )
        catalog_item = schemas["LocalModelCatalogItem"]
        assert_shape(
            "LocalModelCatalogItem",
            {
                "catalog_id",
                "source",
                "requested_ref",
                "title",
                "publisher",
                "repository_url",
                "model_card_url",
                "revision_sha",
                "license",
                "description",
                "downloads",
                "likes",
                "last_modified",
                "pipeline_tag",
                "tags",
                "base_models",
                "capabilities",
                "parameter_count",
                "quantizations",
                "file_size_bytes",
                "advertised_max_context",
                "architecture",
                "architecture_name",
                "metadata_status",
                "metadata_confidence",
                "metadata_diagnostics",
                "reference_integrity",
                "warnings",
            },
            {"catalog_id", "source", "requested_ref", "title"},
        )
        assert_shape(
            "LocalModelQuantization",
            {
                "name",
                "requested_ref",
                "file_name",
                "size_bytes",
                "bits_per_weight",
            },
            {"name", "requested_ref"},
        )
        assert_shape(
            "LocalModelCatalogSourceStatus",
            {"source", "available", "detail", "retry_after_seconds"},
            {"source", "available"},
        )
        assert catalog_item["properties"]["reference_integrity"]["enum"] == [
            "pinned",
            "floating",
            "unknown",
        ]
        quantization = schemas["LocalModelQuantization"]["properties"]
        assert quantization["name"]["minLength"] == 1
        assert quantization["name"]["maxLength"] == 64
        assert quantization["requested_ref"]["minLength"] == 1
        assert quantization["requested_ref"]["maxLength"] == 255
        assert non_null(quantization["file_name"])["maxLength"] == 512
        assert non_null(quantization["size_bytes"])["minimum"] == 0
        assert non_null(quantization["size_bytes"])["maximum"] == float(2**63 - 1)
        assert non_null(quantization["bits_per_weight"])["exclusiveMinimum"] == 0
        assert non_null(quantization["bits_per_weight"])["maximum"] == 32
        catalog_properties = catalog_item["properties"]
        assert catalog_properties["catalog_id"]["maxLength"] == 255
        assert catalog_properties["requested_ref"]["maxLength"] == 255
        assert catalog_properties["title"]["maxLength"] == 255
        assert catalog_properties["quantizations"]["maxItems"] == 100
        assert catalog_properties["warnings"]["maxItems"] == 20
        assert non_null(catalog_properties["parameter_count"])["minimum"] == 1
        assert non_null(catalog_properties["parameter_count"])["maximum"] == float(2**63 - 1)
        assert non_null(catalog_properties["advertised_max_context"])["minimum"] == 1
        assert non_null(catalog_properties["advertised_max_context"])["maximum"] == 2**31 - 1
        source_status = schemas["LocalModelCatalogSourceStatus"]["properties"]
        assert non_null(source_status["detail"])["maxLength"] == 500
        assert non_null(source_status["retry_after_seconds"])["minimum"] == 0
        assert non_null(source_status["retry_after_seconds"])["maximum"] == 86_400
        assert schemas["LocalModelCatalogSource"]["enum"] == [
            "installed",
            "ollama",
            "huggingface",
        ]
        assert schemas["LocalModelCatalogCapability"]["enum"] == [
            "chat",
            "tools",
            "vision",
            "embeddings",
            "reasoning",
        ]

        fit_request = schemas["LocalModelFitPlanRequest"]
        assert_shape(
            "LocalModelSelector",
            {"executor_ids", "match_labels"},
            set(),
        )
        assert_shape(
            "LocalModelFitMetadata",
            {
                "requested_ref",
                "weights_bytes",
                "file_size_bytes",
                "parameter_count",
                "quantization",
                "bits_per_weight",
                "layer_count",
                "kv_head_count",
                "head_dimension",
                "kv_bytes_per_element_min",
                "kv_bytes_per_element_max",
                "advertised_max_context",
            },
            {"requested_ref"},
        )
        assert_shape(
            "LocalModelFitPlanRequest",
            {"model", "selector", "provider_id", "context_tokens"},
            {"model", "selector", "context_tokens"},
        )
        assert fit_request["properties"]["context_tokens"]["exclusiveMinimum"] == 0
        selector_properties = schemas["LocalModelSelector"]["properties"]
        assert selector_properties["executor_ids"]["maxItems"] == 100
        assert selector_properties["match_labels"]["maxProperties"] == 32
        fit_metadata = schemas["LocalModelFitMetadata"]["properties"]
        assert fit_metadata["requested_ref"]["minLength"] == 1
        assert fit_metadata["requested_ref"]["maxLength"] == 255
        for field in ("weights_bytes", "file_size_bytes"):
            assert non_null(fit_metadata[field])["minimum"] == 0
            assert non_null(fit_metadata[field])["maximum"] == float(2**63 - 1)
        assert non_null(fit_metadata["parameter_count"])["minimum"] == 1
        assert non_null(fit_metadata["parameter_count"])["maximum"] == float(2**63 - 1)
        assert non_null(fit_metadata["quantization"])["maxLength"] == 64
        assert non_null(fit_metadata["bits_per_weight"])["exclusiveMinimum"] == 0
        assert non_null(fit_metadata["bits_per_weight"])["maximum"] == 32
        assert non_null(fit_metadata["layer_count"])["minimum"] == 1
        assert non_null(fit_metadata["layer_count"])["maximum"] == 1000
        assert non_null(fit_metadata["kv_head_count"])["minimum"] == 1
        assert non_null(fit_metadata["kv_head_count"])["maximum"] == 1000
        assert non_null(fit_metadata["head_dimension"])["minimum"] == 1
        assert non_null(fit_metadata["head_dimension"])["maximum"] == 65_536
        assert fit_metadata["kv_bytes_per_element_min"]["minimum"] == 1
        assert fit_metadata["kv_bytes_per_element_min"]["maximum"] == 8
        assert fit_metadata["kv_bytes_per_element_max"]["minimum"] == 1
        assert fit_metadata["kv_bytes_per_element_max"]["maximum"] == 8
        assert non_null(fit_metadata["advertised_max_context"])["minimum"] == 1
        assert non_null(fit_metadata["advertised_max_context"])["maximum"] == 2**31 - 1
        assert_shape(
            "LocalModelFitAssessment",
            {
                "status",
                "confidence",
                "available_bytes",
                "accelerator_available_bytes",
                "host_available_bytes",
                "reason_codes",
            },
            {"status", "confidence", "reason_codes"},
        )
        assert_shape(
            "LocalModelFitBreakdown",
            {
                "weights_bytes",
                "kv_cache_min_bytes",
                "kv_cache_max_bytes",
                "runtime_buffer_bytes",
                "reserved_headroom_bytes",
                "required_min_bytes",
                "required_max_bytes",
            },
            set(),
        )
        assert_shape(
            "LocalModelExecutorFitResult",
            {
                "executor_id",
                "executor_name",
                "context_tokens",
                "static",
                "admission",
                "breakdown",
                "unified_memory",
                "snapshot_age_seconds",
                "advertised_max_exceeded",
                "assumptions",
            },
            {
                "executor_id",
                "executor_name",
                "context_tokens",
                "static",
                "admission",
                "breakdown",
            },
        )
        assert_shape(
            "LocalModelContextOption",
            {"context_tokens", "zone", "limiting_executor_ids"},
            {"context_tokens", "zone"},
        )
        assert_shape(
            "LocalModelFitPlanResponse",
            {
                "assessment_generation",
                "advisory_only",
                "requested_context_tokens",
                "advertised_max_context",
                "advertised_max_exceeded",
                "recommended_context_tokens",
                "context_options",
                "executors",
            },
            {
                "assessment_generation",
                "requested_context_tokens",
                "advertised_max_exceeded",
                "context_options",
                "executors",
            },
        )
        fit_response = schemas["LocalModelFitPlanResponse"]
        assert fit_response["properties"]["assessment_generation"]["minimum"] == 0
        assert fit_response["properties"]["assessment_generation"]["maximum"] == 2**53 - 1
        assert fit_response["properties"]["advisory_only"]["const"] is True
        deployment_create = schemas["LocalModelDeploymentCreateRequest"]["properties"]
        assert non_null(deployment_create["capacity_assessment_generation"])["maximum"] == 2**53 - 1
        assert schemas["LocalModelContextOption"]["properties"]["zone"]["enum"] == [
            "green",
            "yellow",
            "red",
            "unknown",
        ]
        assert schemas["LocalModelFitStatus"]["enum"] == [
            "FIT",
            "FIT_WITH_OFFLOAD",
            "NO_FIT",
            "UNKNOWN",
        ]
        assert schemas["LocalModelFitConfidence"]["enum"] == [
            "high",
            "medium",
            "low",
        ]
