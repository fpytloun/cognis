"""Conversation search models shared by provider, API, and tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SearchKind = Literal["reasoning", "intention", "summary"]
SearchMode = Literal["auto", "lexical", "vector", "hybrid"]

SEARCH_KINDS: tuple[str, ...] = ("reasoning", "intention", "summary")
KIND_PRIORITY: dict[str, int] = {"reasoning": 0, "intention": 1, "summary": 2}

# Cognis presentation-layer floor for search match scores. Intaris remains
# authoritative on ranking; this constant only hides matches Cognis judges
# too weak to surface in the UI or to LLM tools.
MIN_DISPLAY_SCORE: float = 0.2


class SearchRequestFilters(BaseModel):
    """Structural filters accepted by Intaris search."""

    agent_id: str | None = None
    session_id: str | None = None
    session_ids: list[str] | None = None
    from_ts: str | None = None
    to_ts: str | None = None


class ConversationSearchFilters(SearchRequestFilters):
    """Cognis-only metadata filters layered on top of Intaris search."""

    project_id: str | None = None
    status: Literal["active", "starred", "archived", "all"] = "active"
    context_type: str | None = None


class SearchRequest(BaseModel):
    q: str
    filters: SearchRequestFilters = Field(default_factory=SearchRequestFilters)
    kinds: list[SearchKind] | None = None
    mode: SearchMode = "auto"
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class SearchSessionsRequest(BaseModel):
    q: str
    filters: SearchRequestFilters = Field(default_factory=SearchRequestFilters)
    kinds: list[SearchKind] | None = None
    mode: SearchMode = "auto"
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = None


class ConversationSearchRequest(SearchSessionsRequest):
    filters: ConversationSearchFilters = Field(default_factory=ConversationSearchFilters)


class SearchMatch(BaseModel):
    session_id: str
    kind: str
    ref_id: str | None = None
    role: str | None = None
    ts: str | None = None
    snippet: str
    score: float
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    agent_id: str | None = None
    session_title: str | None = None
    session_intention: str | None = None


class SearchSessionMatch(BaseModel):
    session_id: str
    agent_id: str | None = None
    title: str | None = None
    intention: str | None = None
    last_activity_at: str | None = None
    match_count: int
    top_match: SearchMatch


class SearchBackends(BaseModel):
    lexical: str
    vector: str
    mode_used: str


class SearchResponse(BaseModel):
    matches: list[SearchMatch] = Field(default_factory=list)
    next_cursor: str | None = None
    total_estimated: int | None = None
    backend: SearchBackends = Field(
        default_factory=lambda: SearchBackends(
            lexical="disabled", vector="disabled", mode_used="auto"
        )
    )


class SearchSessionsResponse(BaseModel):
    sessions: list[SearchSessionMatch] = Field(default_factory=list)
    next_cursor: str | None = None
    total_estimated: int | None = None
    backend: SearchBackends = Field(
        default_factory=lambda: SearchBackends(
            lexical="disabled", vector="disabled", mode_used="auto"
        )
    )


class SearchLexicalCapabilities(BaseModel):
    backend: str
    unaccent: bool = False
    pg_trgm: bool = False
    kinds: list[str] = Field(default_factory=list)


class SearchVectorState(BaseModel):
    provider: str
    model: str | None = None
    dim: int | None = None
    sparse_model: str | None = None
    queue_depth: int = 0
    last_index_at: str | None = None
    backfill_status: str = "idle"
    backfill_total: int | None = None
    backfill_processed: int | None = None
    backfill_job_id: str | None = None


class SearchHealth(BaseModel):
    enabled: bool
    lexical: SearchLexicalCapabilities = Field(
        default_factory=lambda: SearchLexicalCapabilities(backend="disabled")
    )
    vector: SearchVectorState = Field(
        default_factory=lambda: SearchVectorState(provider="disabled")
    )
    notes: list[str] = Field(default_factory=list)


class ConversationSearchMatch(BaseModel):
    conversation_id: str
    conversation_title: str | None = None
    agent_id: str
    project_id: str | None = None
    status: str
    session_id: str
    intaris_session_id: str
    title: str | None = None
    intention: str | None = None
    last_activity_at: str | None = None
    match_count: int = 1
    top_match: SearchMatch
    extra_matches: list[SearchMatch] = Field(default_factory=list)
    kind_rank: int = 99


class ConversationSearchResponse(BaseModel):
    matches: list[ConversationSearchMatch] = Field(default_factory=list)
    next_cursor: str | None = None
    total_estimated: int | None = None
    backend: SearchBackends = Field(
        default_factory=lambda: SearchBackends(
            lexical="disabled", vector="disabled", mode_used="auto"
        )
    )


class ConversationFlatSearchMatch(BaseModel):
    conversation_id: str
    conversation_title: str | None = None
    agent_id: str
    project_id: str | None = None
    status: str
    session_id: str
    intaris_session_id: str
    match: SearchMatch
    kind_rank: int = 99


class ConversationFlatSearchResponse(BaseModel):
    matches: list[ConversationFlatSearchMatch] = Field(default_factory=list)
    next_cursor: str | None = None
    total_estimated: int | None = None
    backend: SearchBackends = Field(
        default_factory=lambda: SearchBackends(
            lexical="disabled", vector="disabled", mode_used="auto"
        )
    )


def kind_rank(kind: str | None) -> int:
    return KIND_PRIORITY.get(kind or "", 99)
