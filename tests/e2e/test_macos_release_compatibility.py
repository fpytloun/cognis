"""Compiled-client compatibility against isolated current or frozen public controllers.

Set COGNIS_E2E_CONTROLLER_PROJECT to an independently checked out, locked public
controller. No API responses or WebSocket frames are mocked by this test.
The WebKit host bridge alone is simulated; OS permission/action UX is not tested.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import sync_playwright

from .conftest import E2EStack, _free_port, capture_ws_events

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def compiled_client(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile this checkout, independently of the separately blocking budget gate."""
    evidence = tmp_path_factory.mktemp("mac-client-build")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    ui_tree = subprocess.check_output(["git", "rev-parse", "HEAD:ui"], cwd=ROOT, text=True).strip()
    # No stale export can silently qualify a newer UI source tree.
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "ui"], cwd=ROOT, check=True)
    with (evidence / "compile.log").open("w") as log:
        subprocess.run(
            ["node", "node_modules/vite/bin/vite.js", "build"],
            cwd=ROOT / "ui",
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    (evidence / "provenance.json").write_text(
        json.dumps({"sourceRevision": revision, "sourceUiTree": ui_tree})
    )
    return evidence


def test_real_controller_cookie_refresh(e2e_stack: E2EStack) -> None:
    """Keep refresh compatibility independent so other failures stay visible."""
    stack = e2e_stack
    with httpx.Client(base_url=stack.cognis_url, headers={"Origin": stack.cognis_url}) as http:
        assert (
            http.post(
                "/api/auth/login",
                json={"email": stack.admin_email, "password": stack.admin_password},
            ).status_code
            == 200
        )
        assert http.get("/api/auth/me").json()["email"] == stack.admin_email
        assert http.post("/api/auth/refresh").status_code == 200
        assert http.get("/api/auth/me").status_code == 200


def test_compiled_native_client_real_controller(
    e2e_stack: E2EStack, tmp_path: Path, compiled_client: Path
) -> None:
    """Real auth, catalogue, chat completion, sidebar and absent-authority fallback."""
    stack = e2e_stack
    expected_preview = os.environ.get("COGNIS_E2E_EXPECT_REPLY_PREVIEW", "1") == "1"
    with httpx.Client(base_url=stack.cognis_url, headers={"Origin": stack.cognis_url}) as http:
        assert http.get("/api/auth/me").status_code == 401
        login = http.post(
            "/api/auth/login",
            json={"email": stack.admin_email, "password": stack.admin_password},
        )
        assert login.status_code == 200
        assert http.get("/api/auth/me").json()["email"] == stack.admin_email
        agent_response = http.get("/api/v1/agents")
        assert agent_response.status_code == 200, agent_response.text
        agents = agent_response.json()["items"]
        assert any(agent["agent_id"] == stack.e2e_agent_id for agent in agents)
        response = http.post(
            "/api/v1/conversations",
            json={"agent_id": stack.e2e_agent_id, "title": "Isolated Mac release qualification"},
        )
        assert response.status_code == 200
        conversation_id = response.json()["conversation_id"]

    # Serve the integrated compiled SPA; proxy every API/socket to the real
    # isolated controller, including when its code comes from the public tag.
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config = {
        "configFile": False,
        "appType": "spa",
        "build": {"outDir": "build"},
        "preview": {
            "host": "127.0.0.1",
            "strictPort": True,
            "port": port,
            "proxy": {
                "/api": {"target": stack.cognis_url, "ws": True},
                "/.well-known": {"target": stack.cognis_url},
            },
        },
    }
    js = "import { preview } from 'vite'; await preview(" + json.dumps(config) + ");"
    with (tmp_path / "preview.log").open("w") as log:
        server = subprocess.Popen(
            ["node", "--input-type=module", "-e", js],
            cwd=ROOT / "ui",
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                assert server.poll() is None, (tmp_path / "preview.log").read_text()
                try:
                    if httpx.get(origin, timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "Compiled preview did not start"
                time.sleep(0.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    generation = str(uuid4())
                    page.add_init_script(
                        f"window.__cognisNative={{version:1,generation:{json.dumps(generation)},"
                        f"server:{json.dumps(origin)},surface:'main'}};"
                        "window.__nativeMessages=[];"
                        "window.webkit={messageHandlers:{cognis:{postMessage:"
                        "(value)=>window.__nativeMessages.push(value)}}};"
                    )
                    requests: list[tuple[str, str]] = []
                    responses: list[tuple[str, int]] = []
                    request_failures: list[tuple[str, str | None]] = []
                    page_errors: list[str] = []
                    page.on("pageerror", lambda error: page_errors.append(error.stack))
                    page.on("request", lambda req: requests.append((req.method, req.url)))
                    page.on(
                        "response",
                        lambda response: responses.append((response.url, response.status)),
                    )
                    page.on(
                        "requestfailed", lambda req: request_failures.append((req.url, req.failure))
                    )
                    page.goto(f"{origin}/login?returnTo=/companion")
                    page.get_by_label("Email", exact=True).fill(stack.admin_email)
                    page.get_by_label("Password", exact=True).fill(stack.admin_password)
                    page.get_by_role("button", name="Sign in", exact=True).click()
                    page.wait_for_url("**/companion")
                    page.wait_for_function(
                        "id => window.__nativeMessages.some(m => m.type === 'snapshot' && "
                        "m.payload.authenticated && m.payload.agents.some(a => a.agentId === id))",
                        arg=stack.e2e_agent_id,
                    )
                    events = capture_ws_events(
                        stack, conversation_id, "scenario:single-phase-stream"
                    )
                    (tmp_path / "controller-events.json").write_text(json.dumps(events, indent=2))
                    assert any(
                        event.get("type") in {"message_complete", "turn_completed"}
                        for event in events
                    )
                    assert not any(event.get("type") == "error" for event in events)
                    page.wait_for_function(
                        "id => window.__nativeMessages.some(m => m.type === 'notification' && "
                        "m.payload.target === id)",
                        arg=conversation_id,
                    )
                    notice = page.evaluate(
                        "id => window.__nativeMessages.find(m => m.type === 'notification' && "
                        "m.payload.target === id).payload",
                        conversation_id,
                    )
                    assert notice["presentation"]["approval"] is None
                    if expected_preview:
                        assert notice["presentation"]["body"] == (
                            "Hello world! This is a streaming response."
                        )
                        assert notice["presentation"]["sender"]["id"] == stack.e2e_agent_id
                        assert notice["presentation"]["sender"]["name"] == "E2E Test Agent"
                    else:
                        assert notice["presentation"]["body"] == "Open Cognis to review."
                        assert notice["presentation"]["sender"] is None

                    # No live authority was presented. Neither simulated native
                    # decision may manufacture permission or issue a write.
                    for decision in ("approve", "deny"):
                        before = len(requests)
                        page.evaluate(
                            "command => window.__cognisNativeReceive(command)",
                            {
                                "version": 1,
                                "generation": generation,
                                "type": "notification-action",
                                "account": stack.admin_email,
                                "scope": notice["scope"],
                                "actionId": conversation_id,
                                "revision": 1,
                                "submissionId": str(uuid4()),
                                "decision": decision,
                            },
                        )
                        page.wait_for_url("**/notifications")
                        assert not [
                            url
                            for method, url in requests[before:]
                            if method in {"POST", "PUT", "PATCH", "DELETE"}
                            and "/attention-actions/" in url
                        ]
                    page.goto(f"{origin}/chat/{conversation_id}")
                    page.wait_for_function("() => document.body.innerText.includes('Hello')")
                    assert "/login" not in page.url
                    page.screenshot(path=str(tmp_path / "compiled-chat.png"))
                    page.reload()
                    page.wait_for_function("() => document.body.innerText.includes('Hello')")
                    # Missing exact preview must be an explicit retryable
                    # editor state, never an empty successful catalogue/save.
                    before_editor = len(requests)
                    page.goto(f"{origin}/agents/{stack.e2e_agent_id}")
                    with page.expect_response(
                        lambda response: (
                            response.url.endswith("/agents/effective-tools/preview")
                            and response.request.method == "POST"
                        )
                    ) as preview_response:
                        page.get_by_role("tab", name="Tools", exact=True).click()
                    assert preview_response.value.status == (200 if expected_preview else 404)
                    if not expected_preview:
                        page.get_by_role("button", name="Retry loading tools").wait_for()
                    assert not [
                        url
                        for method, url in requests[before_editor:]
                        if method in {"PUT", "PATCH", "DELETE"} and "/agents/" in url
                    ]
                except Exception:
                    (tmp_path / "page-diagnostics.json").write_text(
                        json.dumps(
                            {
                                "url": page.url,
                                "body": page.locator("body").inner_text(),
                                "requests": requests,
                                "responses": responses,
                                "requestFailures": request_failures,
                                "pageErrors": page_errors,
                                "buildEvidence": str(compiled_client),
                            },
                            indent=2,
                        )
                    )
                    page.screenshot(path=str(tmp_path / "failed-page.png"))
                    raise
                finally:
                    browser.close()
        finally:
            server.terminate()
            server.wait(timeout=15)
