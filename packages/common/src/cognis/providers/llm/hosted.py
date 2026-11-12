"""Hosted provider endpoints and read-only catalog discovery shared by runtimes."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from cognis.models.speech import speech_streaming_capabilities

PROVIDER_BASES = {
    "elevenlabs": "https://api.elevenlabs.io",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "systemone": "https://api.typesafe.ai/v1",
}
PROVIDER_ENV_VARS = {
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "systemone": "TYPESAFE_API_KEY",
}


def api_endpoint(base: str, operation: str, version: str = "v1") -> str:
    """Append an operation to a service root or versioned base."""
    parts = urlsplit(base.strip())
    path = parts.path.rstrip("/")
    if not path.endswith(f"/{version}"):
        path += f"/{version}"
    return urlunsplit(parts._replace(path=f"{path}/{operation}"))


def model_mode_from_metadata(info: dict[str, Any]) -> str | None:
    """Resolve the protocol from provider output modalities, then explicit mode."""
    if isinstance(info.get("model_info"), dict):
        info = {**info, **info["model_info"]}
    architecture = info.get("architecture")
    architecture = architecture if isinstance(architecture, dict) else {}
    outputs = architecture.get("output_modalities", info.get("output_modalities"))
    if outputs is None:
        modality = architecture.get("modality", info.get("modality"))
        if isinstance(modality, str) and "->" in modality:
            outputs = modality.partition("->")[2].split("+")
    if isinstance(outputs, list) and all(isinstance(item, str) for item in outputs):
        outputs = {item.strip().lower() for item in outputs}
        if outputs & {"decisions", "systemone"}:
            return "systemone"
        if "text" in outputs:
            # Responses and completions are also text-generation protocols.
            mode = info.get("model_mode", info.get("mode"))
            return mode if mode in {"responses", "completion", "audio_transcription"} else "chat"
        for modalities, mode in (
            ({"embedding", "embeddings"}, "embedding"),
            ({"image"}, "image_generation"),
            ({"audio", "speech"}, "audio_speech"),
            ({"video"}, "video"),
            ({"rerank"}, "rerank"),
        ):
            if outputs & modalities:
                return mode
    mode = info.get("model_mode", info.get("mode"))
    if isinstance(mode, str) and mode.strip():
        return {
            "decisions": "systemone",
            "embeddings": "embedding",
            "video_generation": "video",
        }.get(mode, mode)
    return None


def is_openrouter_catalog(preset: str, base_url: str) -> bool:
    """Recognize the hosting catalog, including compatible-provider configurations."""
    return preset == "openrouter" or urlsplit(base_url).hostname == "openrouter.ai"


def is_chat_model(metadata: dict[str, Any]) -> bool:
    """Keep non-chat catalog protocols out of text-generation consumers."""
    return model_mode_from_metadata(metadata) in {
        None,
        "chat",
        "completion",
        "responses",
    } and not metadata.get("supports_embedding", False)


def systemone_provider_preset(preset: str, base_url: str) -> str | None:
    """Resolve typed transport support independently of a compatibility preset."""
    if preset == "systemone":
        return "systemone"
    return "openrouter" if is_openrouter_catalog(preset, base_url) else None


def is_systemone_model(
    model: str, preset: str = "", metadata: dict[str, Any] | None = None
) -> bool:
    """Use provider metadata, retaining native IDs for existing manual configs."""
    mode = model_mode_from_metadata(metadata or {})
    if mode is not None:
        return mode == "systemone"
    if preset == "systemone":
        return True
    # Existing manually configured Jev versions/aliases may lack metadata.
    # Do not generalize this compatibility fallback to other jev-* products.
    return bool(
        re.fullmatch(r"(?:~?typesafe/)?jev-(?:latest|preview|\d+(?:\.\d+)+(?:-\d{8})?)", model)
    )


def systemone_metadata() -> dict[str, Any]:
    """System One never generates chat text or tools, regardless of defaults."""
    return {
        "model_mode": "systemone",
        "supports_tools": False,
        "supports_parallel_tools": False,
        "supports_streaming": False,
        "supports_embedding": False,
        "supports_reasoning": False,
        "supports_responses_api": False,
        "supports_structured_output": False,
        "reasoning_efforts": [],
        "supported_openai_params": [],
    }


async def discover_hosted_models(
    client: httpx.AsyncClient, *, preset: str, base_url: str, api_key: str
) -> list[dict[str, Any]]:
    """Return provider model records; Gemini catalogs are paginated."""
    base = base_url or PROVIDER_BASES[preset]
    if preset == "elevenlabs":
        transcription_models = (
            ("scribe_v1", "Scribe v1"),
            ("scribe_v2", "Scribe v2"),
            ("scribe_v2_realtime", "Scribe v2 Realtime"),
        )
        transcription_ids = {model_id for model_id, _ in transcription_models}
        response = await client.get(api_endpoint(base, "models"), headers={"xi-api-key": api_key})
        response.raise_for_status()
        return [
            {
                "id": item["model_id"],
                "display_name": item.get("name", item["model_id"]),
                "supports_speech_synthesis": True,
                "model_mode": "audio_speech",
                "supports_tools": False,
                "supports_streaming": False,
            }
            for item in response.json()
            if item.get("can_do_text_to_speech")
            and item.get("model_id")
            and item["model_id"] not in transcription_ids
        ] + [
            {
                "id": model_id,
                "display_name": display_name,
                "supports_transcription": True,
                "supports_speech_synthesis": False,
                "supports_audio_input": True,
                "model_mode": "audio_transcription",
                "supports_tools": False,
                "supports_streaming": speech_streaming_capabilities("elevenlabs", model_id).stt,
            }
            for model_id, display_name in transcription_models
        ]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if preset == "gemini":
        headers = {"x-goog-api-key": api_key} if api_key else {}
        records = []
        page: str | None = None
        seen: set[str] = set()
        while True:
            params = {"pageSize": "1000"}
            if page:
                params["pageToken"] = page
            response = await client.get(
                api_endpoint(base, "models", "v1beta"), headers=headers, params=params
            )
            response.raise_for_status()
            data = response.json()
            for raw in data.get("models", []):
                methods = raw.get("supportedGenerationMethods", [])
                embedding = "embedContent" in methods or "batchEmbedContents" in methods
                model_id = raw["name"].removeprefix("models/")
                mode = model_mode_from_metadata(raw)
                video = mode in {"video", "video_generation"} or (
                    model_id.startswith("veo-") and "predictLongRunning" in methods
                )
                if "generateContent" not in methods and not embedding and not video:
                    continue
                records.append(
                    {
                        **raw,
                        "id": model_id,
                        "display_name": raw.get("displayName"),
                        "context_window": raw.get("inputTokenLimit"),
                        "max_output_tokens": raw.get("outputTokenLimit"),
                        "supports_embedding": embedding,
                        "model_mode": (
                            "video" if video else mode or ("embedding" if embedding else "chat")
                        ),
                        **(
                            {
                                "supports_video_generation": True,
                                "supports_tools": False,
                                "supports_parallel_tools": False,
                                "supports_streaming": False,
                            }
                            if video
                            else {}
                        ),
                    }
                )
            page = data.get("nextPageToken")
            if not page:
                return records
            if page in seen:
                raise ValueError("Repeated Gemini catalog page token")
            seen.add(page)
    catalog_params = {"output_modalities": "all"} if is_openrouter_catalog(preset, base) else None
    response = await client.get(
        api_endpoint(base, "models"), headers=headers, params=catalog_params
    )
    response.raise_for_status()
    data = response.json()
    records = data.get("data", data.get("models", []))
    if isinstance(records, dict):
        records = [{"id": key, **value} for key, value in records.items()]
    return [
        {"id": raw} if isinstance(raw, str) else {**raw, "id": raw.get("id") or raw.get("name")}
        for raw in records
        if isinstance(raw, str | dict)
    ]
