from __future__ import annotations

import pytest

from cognis.core.local_models import parse_local_model_reference
from cognis.models.local_models import LocalModelSource


@pytest.mark.parametrize(
    ("reference", "canonical", "source", "revision"),
    [
        ("llama3.2", "llama3.2:latest", LocalModelSource.OLLAMA, "latest"),
        ("library/gemma3:4b", "library/gemma3:4b", LocalModelSource.OLLAMA, "4b"),
        (
            "registry.ollama.ai/library/qwen3:8b",
            "registry.ollama.ai/library/qwen3:8b",
            LocalModelSource.OLLAMA,
            "8b",
        ),
        (
            "hf.co/bartowski/Llama-3.2-GGUF:Q4_K_M",
            "hf.co/bartowski/Llama-3.2-GGUF:Q4_K_M",
            LocalModelSource.HUGGINGFACE,
            "Q4_K_M",
        ),
    ],
)
def test_parse_local_model_reference_accepts_supported_forms(
    reference: str,
    canonical: str,
    source: LocalModelSource,
    revision: str,
) -> None:
    parsed = parse_local_model_reference(reference)

    assert parsed.requested_ref == reference
    assert parsed.canonical_name == canonical
    assert parsed.runtime_name == canonical
    assert parsed.source == source
    assert parsed.revision == revision


@pytest.mark.parametrize(
    "reference",
    [
        "https://ollama.com/library/llama3.2",
        "http://example.com/model",
        "/tmp/model",
        "../model",
        "./model",
        r"C:\models\llama",
        "namespace/../model",
        "docker.io/library/llama3.2:latest",
        "example.com/model:tag",
        "hf.co/org/repo",
        "hf.co/org/repo:quant/extra",
        "model name",
        "model\nname",
        "model?tag",
        "model@digest",
        "Uppercase",
    ],
)
def test_parse_local_model_reference_rejects_unsafe_or_unknown_forms(reference: str) -> None:
    with pytest.raises(ValueError):
        parse_local_model_reference(reference)
