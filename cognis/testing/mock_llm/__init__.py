"""Deterministic mock LLM provider for e2e testing.

Implements OpenAI-compatible endpoints that replay scripted scenario
sequences, enabling fully deterministic streaming-chat tests.

Usage:
    # As a subprocess (pytest fixture):
    python -m cognis.testing.mock_llm --port 8090

    # As a compose service:
    docker run -p 8090:8090 cognis-mock-llm

Endpoints:
    POST /v1/chat/completions   — Chat completions (stream + non-stream)
    POST /v1/responses          — Responses API
    POST /v1/embeddings         — Deterministic embeddings
    POST /__mock/scenario       — Inject/override a scenario at runtime
    POST /__mock/active         — Set the active scenario for next turn
    GET  /__mock/scenarios      — List loaded scenarios
    GET  /__mock/history        — Last N request/response pairs
    DELETE /__mock/history      — Clear history
"""

from cognis.testing.mock_llm.server import create_app

__all__ = ["create_app"]
