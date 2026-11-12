"""Every added inference primitive requires an explicit coverage disposition."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREES = (
    "cognis/providers/llm",
    "packages/common/src/cognis/providers/llm",
    "packages/executor/src/cognis/executor/backends",
)
FILES = (
    "packages/common/src/cognis/providers/video.py",
    "packages/executor/src/cognis/executor/video.py",
    "packages/executor/src/cognis/executor/inference.py",
)


class Dispatches(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path.relative_to(ROOT).as_posix()
        self.owners: list[str] = []
        self.sites: Counter[tuple[str, str, str]] = Counter()
        self.parents: list[ast.AST] = []

    def visit(self, node):
        self.parents.append(node)
        try:
            return super().visit(node)
        finally:
            self.parents.pop()

    def visit_ClassDef(self, node):
        self._owner(node)

    def visit_FunctionDef(self, node):
        self._owner(node)

    def visit_AsyncFunctionDef(self, node):
        self._owner(node)

    def _owner(self, node):
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    def visit_Attribute(self, node):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "litellm"
            and node.attr.startswith("a")
        ):
            parent = self.parents[-2]
            depth = 2
            while isinstance(parent, ast.IfExp):
                depth += 1
                parent = self.parents[-depth]
            assert (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Name)
                and parent.func.id == "observed_sdk"
                and node in ast.walk(parent.args[0])
            ), f"Unobserved SDK primitive: {self.path}:{node.lineno}"
            self.sites[(self.path, ".".join(self.owners), f"sdk:{node.attr}")] += 1
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module == "litellm":
            assert not any(alias.name.startswith("a") for alias in node.names), (
                f"Direct SDK imports bypass the inventory: {self.path}:{node.lineno}"
            )

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "post",
            "send",
            "stream",
            "request",
        }:
            self.sites[(self.path, ".".join(self.owners), node.func.attr)] += 1
        self.generic_visit(node)


# Qualified owner, primitive, multiplicity and role are intentionally explicit.
# Native-owned HTTP lifecycle is valid without an SDK wrapper; RPC/auth/discovery
# entries must never become physical inference attempts.
DISPATCH_INVENTORY = {
    "cognis/providers/llm/ollama.py": {
        ("fetch_ollama_show", "post"): "excluded: model metadata, not generation",
    },
    "cognis/providers/llm/litellm.py": {
        (
            "LiteLLMProvider._native_anthropic_stream",
            "stream",
        ): "facade: native transport owns HTTP",
        ("LiteLLMProvider._refresh_chatgpt_auth_record", "post"): "excluded: OAuth refresh",
        ("LiteLLMProvider._request_chatgpt_device_code", "post"): "excluded: OAuth device flow",
        ("LiteLLMProvider._poll_chatgpt_device_code_once", "post"): "excluded: OAuth device flow",
        ("LiteLLMProvider._exchange_chatgpt_auth_code", "post"): "excluded: OAuth exchange",
        ("LiteLLMProvider.transcribe", "post"): "observed_http: native transcription",
        ("LiteLLMProvider.embed_response", "sdk:aembedding"): "observed_sdk: embeddings",
        ("LiteLLMProvider._image_generate_via_api", "sdk:aimage_edit"): "observed_sdk: image edit",
        ("LiteLLMProvider._image_generate_via_api", "sdk:aimage_generation"): "observed_sdk: image",
        (
            "LiteLLMProvider._image_generate_via_completion",
            "sdk:acompletion",
        ): "observed_sdk: Gemini image",
        ("_run_synthesize_local", "post"): "observed_http: speech",
    },
    "cognis/providers/llm/service.py": {
        ("LLMService._execute", "stream"): "facade: resolved provider owns submission",
    },
    "cognis/providers/llm/usage_proxy.py": {
        ("proxy_request", "request"): "logical attribution only; provider owns submission",
    },
    "cognis/providers/llm/codex_transport.py": {
        ("DirectCodexTransport._submit", "send"): "native owned physical HTTP lifetime",
        ("DirectCodexTransport._submit", "post"): "native owned HTTP adapter alternative",
    },
    "cognis/providers/llm/usage_attribution.py": {
        ("controller_request", "request"): "logical attribution only",
    },
    "cognis/providers/llm/transport.py": {
        ("LiteLLMTransport.completion", "sdk:acompletion"): "observed_sdk: chat",
        ("LiteLLMTransport.responses", "sdk:aresponses"): "observed_sdk: Responses",
    },
    "cognis/providers/llm/anthropic_subscription.py": {
        ("exchange_authorization_code", "post"): "excluded: OAuth exchange",
        ("refresh_authorized_record", "post"): "excluded: OAuth refresh",
    },
    "cognis/providers/llm/codex.py": {
        ("test_codex_responses", "stream"): "native owned probe; early close, usage unknown",
    },
    "cognis/providers/llm/monitoring.py": {
        ("probe_connectivity", "stream"): "excluded: non-generating GET connectivity probe",
    },
    "packages/common/src/cognis/providers/llm/systemone.py": {
        ("request_systemone", "post"): "native owned single HTTP submission; no SDK retries",
    },
    "packages/common/src/cognis/providers/llm/elevenlabs.py": {
        ("synthesize", "post"): "observed_http: ElevenLabs speech",
        ("transcribe", "post"): "observed_http: ElevenLabs transcription",
    },
    "packages/common/src/cognis/providers/llm/speech_streaming.py": {
        ("stream_synthesis", "send"): (3, "native owned synthesis websocket lifetime"),
        ("stream_synthesis", "stream"): "observed HTTP speech fallback in the native lifetime",
        ("stream_transcription", "send"): "native owned transcription websocket lifetime",
        ("stream_transcription.commit", "send"): "transcription commit in the observed lifetime",
        ("stream_transcription.send_audio", "send"): "transcription PCM in the observed lifetime",
    },
    "packages/common/src/cognis/providers/llm/anthropic/transport.py": {
        ("AnthropicMessagesClient._request", "send"): "native owned HTTP and decoded SSE lifetime",
    },
    "packages/executor/src/cognis/executor/backends/litellm.py": {
        (
            "LiteLLMExecutorBackend.stream_complete",
            "sdk:aresponses",
        ): "observed_sdk: Responses stream",
        ("LiteLLMExecutorBackend.stream_complete", "sdk:acompletion"): "observed_sdk: chat stream",
        ("LiteLLMExecutorBackend.generate", "sdk:aembedding"): "observed_sdk: embeddings",
        ("LiteLLMExecutorBackend.generate", "sdk:aresponses"): "observed_sdk: Responses",
        ("LiteLLMExecutorBackend.generate", "sdk:acompletion"): "observed_sdk: chat",
        ("LiteLLMExecutorBackend.image_generate", "sdk:acompletion"): "observed_sdk: Gemini image",
        ("LiteLLMExecutorBackend.image_generate", "sdk:aimage_edit"): "observed_sdk: image edit",
        ("LiteLLMExecutorBackend.image_generate", "sdk:aimage_generation"): "observed_sdk: image",
        ("LiteLLMExecutorBackend.transcribe", "post"): "observed_http: transcription",
    },
    "packages/executor/src/cognis/executor/backends/tts.py": {
        ("run_synthesize_local", "post"): "observed_http: speech",
    },
    "packages/executor/src/cognis/executor/backends/anthropic.py": {
        ("AnthropicMessagesExecutorBackend.stream_complete", "stream"): "facade: native transport",
    },
    "packages/common/src/cognis/providers/video.py": {
        (
            "VideoHTTPAdapter._request_json",
            "stream",
        ): "native owned POST submit only; GETs excluded",
        ("VideoHTTPAdapter._image", "stream"): "excluded: image download",
        ("VideoHTTPAdapter.output_stream", "stream"): "excluded: result download",
    },
    "packages/executor/src/cognis/executor/inference.py": {
        ("InferenceHandler.image_generate", "sdk:acompletion"): "observed_sdk: legacy Gemini image",
        ("InferenceHandler.image_generate", "sdk:aimage_edit"): "observed_sdk: legacy image edit",
        ("InferenceHandler.image_generate", "sdk:aimage_generation"): "observed_sdk: legacy image",
        ("InferenceHandler.transcribe", "post"): "observed_http: legacy transcription",
    },
}


def test_inference_dispatch_inventory():
    paths = [path for tree in TREES for path in (ROOT / tree).rglob("*.py")]
    paths.extend(ROOT / path for path in FILES)
    actual: Counter[tuple[str, str, str]] = Counter()
    for path in paths:
        visitor = Dispatches(path)
        visitor.visit(ast.parse(path.read_text()))
        actual.update(visitor.sites)
    expected = Counter(
        {
            (path, owner, primitive): role[0] if isinstance(role, tuple) else 1
            for path, sites in DISPATCH_INVENTORY.items()
            for (owner, primitive), role in sites.items()
        }
    )
    extra = "\n".join(f"{key!r}: {count}" for key, count in (actual - expected).items())
    missing = "\n".join(f"{key!r}: {count}" for key, count in (expected - actual).items())
    assert actual == expected, (
        f"Unclassified physical/RPC/auth boundaries:\n{extra}\nRemoved:\n{missing}"
    )
