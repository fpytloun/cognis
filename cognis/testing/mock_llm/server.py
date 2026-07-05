"""Mock LLM server — Starlette app implementing OpenAI-compatible endpoints.

Serves deterministic scripted responses for e2e testing.  The control-plane
endpoints (/__mock/*) allow runtime scenario injection for interactive
debugging by a coding agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from cognis.testing.mock_llm.scenarios import (
    ScenarioCatalog,
    build_default_response,
    get_step_delays,
    render_chat_completion_stream,
)

logger = logging.getLogger(__name__)

# Module-level catalog — shared across all requests.
_catalog = ScenarioCatalog()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "")
    return ""


def _first_user_message(messages: list[dict[str, Any]]) -> str:
    """Return the first user message — used to identify the scenario trigger."""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "")
    return ""


def _conversation_turn_index(messages: list[dict[str, Any]]) -> int:
    """Return which LLM turn this is (0-based).

    Each time the agent loop calls the LLM, the message list grows:
    turn 0: [system, user]
    turn 1: [system, user, assistant(tool_calls), tool_result, ...]
    turn 2: [system, user, assistant, tool_result, assistant(tool_calls), tool_result, ...]

    We count the number of assistant messages to determine the turn index.
    """
    return sum(1 for m in messages if m.get("role") == "assistant")


# ---------------------------------------------------------------------------
# Chat completions endpoint
# ---------------------------------------------------------------------------

async def chat_completions(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    messages = body.get("messages", [])
    model = body.get("model", "mock-model")
    stream = body.get("stream", False)
    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Resolve scenario from the FIRST user message (the original trigger).
    # On subsequent LLM calls (after tool execution), the last message is a
    # tool result — but the scenario is still identified by the original trigger.
    first_msg = _first_user_message(messages)
    last_msg = _last_user_message(messages)
    scenario = _catalog.resolve(first_msg) or _catalog.resolve(last_msg)
    scenario_id = scenario.get("id") if scenario else None

    # Determine which turn of the multi-turn conversation this is.
    turn_index = _conversation_turn_index(messages)

    _catalog.record_request(
        {
            "messages_count": len(messages),
            "first_user_message": first_msg[:200],
            "last_user_message": last_msg[:200],
            "turn_index": turn_index,
            "stream": stream,
        },
        scenario_id,
    )

    if scenario is None:
        logger.debug("No scenario matched for message: %r", last_msg[:100])
        if stream:
            # Return a minimal streaming response
            async def _default_stream() -> Any:
                chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Mock response — no scenario matched."},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                stop_chunk = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(stop_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_default_stream(), media_type="text/event-stream")
        return JSONResponse(build_default_response(model))

    if stream:
        chunks = render_chat_completion_stream(
            scenario, model=model, request_id=request_id, turn_index=turn_index
        )
        delays = get_step_delays(scenario, turn_index=turn_index)

        async def _stream() -> Any:
            for i, chunk_line in enumerate(chunks):
                delay = delays[i] if i < len(delays) else 0.0
                if delay > 0:
                    await asyncio.sleep(delay)
                yield f"{chunk_line}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    # Non-streaming: collect all text and return
    all_content = ""
    turns = scenario.get("turns", [])
    # Select the right turn for multi-turn scenarios
    assistant_turns = [t for t in turns if t.get("role") == "assistant"]
    active_turn = assistant_turns[turn_index] if turn_index < len(assistant_turns) else (assistant_turns[-1] if assistant_turns else {})
    for step in active_turn.get("steps", []):
            if step.get("type") == "text":
                chunks_list = step.get("chunks", [step.get("content", "")])
                all_content += "".join(chunks_list)

    return JSONResponse({
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": all_content or "Mock response."},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    })


# ---------------------------------------------------------------------------
# Responses API endpoint (for Codex/GPT-5 paths)
# ---------------------------------------------------------------------------

async def responses_api(request: Request) -> Response:
    """Minimal Responses API — delegates to chat completions logic."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # Convert Responses API format to chat completions format
    input_data = body.get("input", [])
    messages: list[dict[str, Any]] = []
    if isinstance(input_data, str):
        messages = [{"role": "user", "content": input_data}]
    elif isinstance(input_data, list):
        messages = input_data

    stream = body.get("stream", False)
    model = body.get("model", "mock-model")

    # Reuse chat completions logic
    fake_request_body = {"messages": messages, "model": model, "stream": stream}
    fake_request = Request(request.scope, request.receive)
    fake_request._body = json.dumps(fake_request_body).encode()

    return await chat_completions(fake_request)


# ---------------------------------------------------------------------------
# Embeddings endpoint
# ---------------------------------------------------------------------------

async def embeddings(request: Request) -> Response:
    """Return deterministic fixed-dim embeddings (zeros with a hash seed)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    input_data = body.get("input", [])
    if isinstance(input_data, str):
        input_data = [input_data]

    dims = int(os.environ.get("MOCK_LLM_EMBEDDING_DIMS", "1536"))

    def _make_embedding(text: str) -> list[float]:
        # Deterministic: hash the text to seed a simple pattern
        h = hash(text) % 1000
        vec = [0.0] * dims
        vec[h % dims] = 1.0
        return vec

    data = [
        {"object": "embedding", "index": i, "embedding": _make_embedding(str(inp))}
        for i, inp in enumerate(input_data)
    ]

    return JSONResponse({
        "object": "list",
        "data": data,
        "model": body.get("model", "mock-embedding"),
        "usage": {"prompt_tokens": len(input_data) * 5, "total_tokens": len(input_data) * 5},
    })


# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------

async def list_models(request: Request) -> Response:
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": "mock-model", "object": "model", "created": 0, "owned_by": "mock"},
            {"id": "mock-embedding", "object": "model", "created": 0, "owned_by": "mock"},
        ],
    })


# ---------------------------------------------------------------------------
# Control-plane endpoints (/__mock/*)
# ---------------------------------------------------------------------------

async def mock_upsert_scenario(request: Request) -> Response:
    """POST /__mock/scenario — inject or replace a scenario at runtime."""
    try:
        scenario = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(scenario, dict) or not scenario.get("id"):
        return JSONResponse({"error": "scenario must have an 'id' field"}, status_code=400)

    _catalog.upsert(scenario)
    return JSONResponse({"ok": True, "id": scenario["id"]})


async def mock_set_active(request: Request) -> Response:
    """POST /__mock/active — set the active scenario for the next turn."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    scenario_id = body.get("id")  # None to clear
    ok = _catalog.set_active(scenario_id)
    if not ok:
        return JSONResponse({"error": f"Unknown scenario id: {scenario_id!r}"}, status_code=404)
    return JSONResponse({"ok": True, "active": scenario_id})


async def mock_list_scenarios(request: Request) -> Response:
    """GET /__mock/scenarios — list all loaded scenarios."""
    return JSONResponse({"scenarios": _catalog.list_scenarios()})


async def mock_get_history(request: Request) -> Response:
    """GET /__mock/history — last N request/response pairs."""
    limit = int(request.query_params.get("limit", "10"))
    return JSONResponse({"history": _catalog.get_history(limit)})


async def mock_clear_history(request: Request) -> Response:
    """DELETE /__mock/history — clear history."""
    _catalog.clear_history()
    return JSONResponse({"ok": True})


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok", "scenarios": len(_catalog.list_scenarios())})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(scenarios_dir: Path | None = None) -> Starlette:
    """Create the mock LLM Starlette app.

    Args:
        scenarios_dir: Directory containing *.yaml scenario files.
                       Defaults to MOCK_LLM_SCENARIOS_DIR env var or
                       tests/e2e/scenarios/ relative to cwd.
    """
    if scenarios_dir is None:
        env_dir = os.environ.get("MOCK_LLM_SCENARIOS_DIR")
        if env_dir:
            scenarios_dir = Path(env_dir)
        else:
            # Default: tests/e2e/scenarios/ relative to repo root
            scenarios_dir = Path(__file__).parent.parent.parent.parent / "tests" / "e2e" / "scenarios"

    _catalog.load_directory(scenarios_dir)

    routes = [
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/responses", responses_api, methods=["POST"]),
        Route("/v1/embeddings", embeddings, methods=["POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        # Control plane
        Route("/__mock/scenario", mock_upsert_scenario, methods=["POST"]),
        Route("/__mock/active", mock_set_active, methods=["POST"]),
        Route("/__mock/scenarios", mock_list_scenarios, methods=["GET"]),
        Route("/__mock/history", mock_get_history, methods=["GET"]),
        Route("/__mock/history", mock_clear_history, methods=["DELETE"]),
    ]

    app = Starlette(routes=routes)
    app.state.catalog = _catalog
    return app
