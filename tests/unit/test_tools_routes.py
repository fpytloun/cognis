from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(  # type: ignore[attr-defined]
        email, email.split("@")[0].title(), role
    )
    return {"Authorization": f"Bearer {token}"}


def test_list_tools_includes_executor_and_controller_tools(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        response = client.get(
            "/api/v1/tools",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

    assert response.status_code == 200
    tools = response.json()
    names = {tool["name"] for tool in tools}
    assert "artifact_read" in names
    assert "artifact_list_recent" in names
    assert "artifact_search" in names
    assert "artifact_get_metadata" in names
    assert "artifact_publish" in names
    assert "artifact_save" in names
    sources = {tool["name"]: tool["source"]["type"] for tool in tools}
    assert sources["artifact_read"] == "builtin"
    assert sources["artifact_list_recent"] == "builtin"
    assert sources["artifact_search"] == "builtin"
    assert sources["artifact_get_metadata"] == "builtin"
    assert sources["artifact_publish"] == "executor"
    assert sources["artifact_save"] == "executor"
