"""Canonical inference routing roles and companion inheritance."""

from __future__ import annotations

# Internal storage marker for effort-only companion overrides (model is NOT NULL).
# HTTP represents this as model=null; only companion resolution consumes it.
INHERIT_COMPANION_MODEL = "__inherit_companion_model__"

CORE_ROUTES = (
    "default",
    "classifier",
    "auxiliary_generation",
    "compaction",
    "evaluator",
    "speech_to_text",
    "text_to_speech",
    "audio_narrative",
    "image_generation",
    "video_generation",
    "video_editing",
    "video_analysis",
    "attachment_analysis",
    "embedding",
)
COMPANION_DEFAULTS = ("companion_lite", "companion_smart", "companion_embedding")
COMPANION_ROUTES = {
    "intaris_evaluation": "companion_lite",
    "intaris_judge": "companion_smart",
    "intaris_analysis": "companion_smart",
    "intaris_analysis_deep": "companion_smart",
    "intaris_embedding": "companion_embedding",
    "mnemory_extraction": "companion_lite",
    "mnemory_retrieval": "companion_lite",
    "mnemory_consolidation": "companion_smart",
    "mnemory_maintenance": "companion_smart",
    "mnemory_embedding": "companion_embedding",
}
ROUTING_TASK_TYPES = (*CORE_ROUTES, *COMPANION_DEFAULTS, *COMPANION_ROUTES)
EMBEDDING_ROUTES = frozenset(
    {"embedding", "companion_embedding", "intaris_embedding", "mnemory_embedding"}
)
TEXT_ROUTING_TASK_TYPES = frozenset(
    {"default", "classifier", "auxiliary_generation", "compaction", "evaluator", "audio_narrative"}
    | ((set(COMPANION_DEFAULTS) | set(COMPANION_ROUTES)) - EMBEDDING_ROUTES)
)


def route_chain(role: str) -> tuple[str, ...]:
    """Return bounded inheritance; embedding roles never fall back to chat."""
    if role in COMPANION_ROUTES:
        return (role, COMPANION_ROUTES[role])
    if role == "audio_narrative":
        return ("audio_narrative", "companion_lite")
    if role == "video_editing":
        return ("video_editing", "video_generation")
    if role == "video_analysis":
        return ("video_analysis", "attachment_analysis")
    return (role,)


def companion_routes(service: str) -> frozenset[str]:
    """Return the inference roles allowed for one companion identity."""
    return frozenset(role for role in COMPANION_ROUTES if role.startswith(f"{service}_"))
