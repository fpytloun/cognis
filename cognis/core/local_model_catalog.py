"""Typed, bounded local-model catalog adapters."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from cognis.core.local_models import parse_local_model_reference
from cognis.models.local_models import (
    LocalModelCatalogCapability,
    LocalModelCatalogItem,
    LocalModelCatalogResponse,
    LocalModelCatalogSource,
    LocalModelCatalogSourceStatus,
    LocalModelQuantization,
)

logger = logging.getLogger(__name__)

HF_API_BASE = "https://huggingface.co"
HF_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
HF_MAX_PAGE_SIZE = 24
HF_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HF_MAX_README_BYTES = 256 * 1024
HF_DETAIL_DEADLINE_SECONDS = 8.0
HF_SEARCH_DEADLINE_SECONDS = 8.0
CATALOG_CACHE_TTL_SECONDS = 300.0
CATALOG_CACHE_MAX_ENTRIES = 128
DETAIL_CACHE_MAX_ENTRIES = 256
HF_MAX_DETAIL_TASKS = 24
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_HF_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_QUANT_RE = re.compile(
    r"(?:^|[-_.])((?:I?Q\d(?:_[A-Za-z0-9]+)+)|BF16|F16|F32)(?:[-_.]|$)",
    re.IGNORECASE,
)
_SHARD_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<index>\d{5})-of-(?P<count>\d{5})\.gguf$",
    re.IGNORECASE,
)


class CatalogUpstreamError(RuntimeError):
    """A public catalog is temporarily unavailable."""

    def __init__(self, detail: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(detail)
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    response: LocalModelCatalogResponse


@dataclass(slots=True)
class _DetailCacheEntry:
    expires_at: float
    item: LocalModelCatalogItem


def _gib(value: float) -> int:
    return int(value * 1024**3)


def _quant(
    ref: str,
    name: str,
    size_gib: float,
    bits: float,
) -> LocalModelQuantization:
    return LocalModelQuantization(
        name=name,
        requested_ref=ref,
        size_bytes=_gib(size_gib),
        bits_per_weight=bits,
    )


CURATED_OLLAMA_CATALOG: tuple[LocalModelCatalogItem, ...] = (
    LocalModelCatalogItem(
        catalog_id="ollama:llama3.2:3b",
        source=LocalModelCatalogSource.OLLAMA,
        requested_ref="llama3.2:3b",
        title="Llama 3.2 3B",
        publisher="Meta",
        license="Llama 3.2 Community",
        description="Compact general-purpose chat model for everyday local use.",
        capabilities=[LocalModelCatalogCapability.CHAT, LocalModelCatalogCapability.TOOLS],
        parameter_count=3_210_000_000,
        quantizations=[_quant("llama3.2:3b", "Q4_K_M", 2.0, 4.5)],
        file_size_bytes=_gib(2.0),
        advertised_max_context=131_072,
        architecture={"layer_count": 28, "kv_head_count": 8, "head_dimension": 128},
    ),
    LocalModelCatalogItem(
        catalog_id="ollama:qwen3:8b",
        source=LocalModelCatalogSource.OLLAMA,
        requested_ref="qwen3:8b",
        title="Qwen 3 8B",
        publisher="Qwen",
        license="Apache-2.0",
        description="General chat and reasoning model with tool support.",
        capabilities=[
            LocalModelCatalogCapability.CHAT,
            LocalModelCatalogCapability.TOOLS,
            LocalModelCatalogCapability.REASONING,
        ],
        parameter_count=8_190_000_000,
        quantizations=[_quant("qwen3:8b", "Q4_K_M", 5.2, 4.5)],
        file_size_bytes=_gib(5.2),
        advertised_max_context=40_960,
        architecture={"layer_count": 36, "kv_head_count": 8, "head_dimension": 128},
    ),
    LocalModelCatalogItem(
        catalog_id="ollama:gemma3:4b",
        source=LocalModelCatalogSource.OLLAMA,
        requested_ref="gemma3:4b",
        title="Gemma 3 4B",
        publisher="Google",
        license="Gemma",
        description="Efficient multimodal model for text and image understanding.",
        capabilities=[LocalModelCatalogCapability.CHAT, LocalModelCatalogCapability.VISION],
        parameter_count=4_300_000_000,
        quantizations=[_quant("gemma3:4b", "Q4_K_M", 3.3, 4.5)],
        file_size_bytes=_gib(3.3),
        advertised_max_context=131_072,
        warnings=["Vision support depends on the runtime build and projector artifact."],
    ),
    LocalModelCatalogItem(
        catalog_id="ollama:phi4:14b",
        source=LocalModelCatalogSource.OLLAMA,
        requested_ref="phi4:14b",
        title="Phi-4 14B",
        publisher="Microsoft",
        license="MIT",
        description="Medium-size reasoning and instruction model.",
        capabilities=[LocalModelCatalogCapability.CHAT, LocalModelCatalogCapability.REASONING],
        parameter_count=14_700_000_000,
        quantizations=[_quant("phi4:14b", "Q4_K_M", 9.1, 4.5)],
        file_size_bytes=_gib(9.1),
        advertised_max_context=16_384,
    ),
    LocalModelCatalogItem(
        catalog_id="ollama:mistral-small3.1:24b",
        source=LocalModelCatalogSource.OLLAMA,
        requested_ref="mistral-small3.1:24b",
        title="Mistral Small 3.1 24B",
        publisher="Mistral AI",
        license="Apache-2.0",
        description="Higher-quality local chat model with tool and vision capabilities.",
        capabilities=[
            LocalModelCatalogCapability.CHAT,
            LocalModelCatalogCapability.TOOLS,
            LocalModelCatalogCapability.VISION,
        ],
        parameter_count=24_000_000_000,
        quantizations=[_quant("mistral-small3.1:24b", "Q4_K_M", 14.0, 4.5)],
        file_size_bytes=_gib(14.0),
        advertised_max_context=131_072,
    ),
)


class LocalModelCatalog:
    """Catalog facade with curated, installed-placeholder, and public HF adapters."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        cache_ttl_seconds: float = CATALOG_CACHE_TTL_SECONDS,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=HF_API_BASE,
            timeout=HF_TIMEOUT,
            follow_redirects=False,
            http2=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "cognis-local-model-catalog/1",
            },
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self._cache_ttl_seconds = max(1.0, cache_ttl_seconds)
        self._cache: dict[tuple[object, ...], _CacheEntry] = {}
        self._inflight: dict[
            tuple[object, ...],
            asyncio.Task[LocalModelCatalogResponse],
        ] = {}
        self._detail_cache: dict[tuple[str, str], _DetailCacheEntry] = {}
        self._detail_inflight: dict[
            tuple[str, str],
            asyncio.Task[LocalModelCatalogItem],
        ] = {}
        self._detail_latest: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()
        self._detail_semaphore = asyncio.Semaphore(4)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        *,
        source: LocalModelCatalogSource | None,
        query: str,
        cursor: str | None,
        limit: int,
        parameter_range: str | None = None,
        download_size_range: str | None = None,
        quantization: str | None = None,
        min_context: int | None = None,
        include_unknown: bool = True,
    ) -> LocalModelCatalogResponse:
        normalized_query = " ".join(query.strip().split())[:100]
        bounded_limit = min(max(limit, 1), HF_MAX_PAGE_SIZE)
        source_key = source.value if source is not None else "all"
        normalized_quantization = quantization.strip().upper()[:64] if quantization else None
        key = (
            source_key,
            normalized_query.casefold(),
            cursor or "",
            bounded_limit,
            parameter_range,
            download_size_range,
            normalized_quantization,
            min_context,
            include_unknown,
        )
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > monotonic():
                return cached.response.model_copy(update={"cached": True})
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._search_uncached(
                        source=source,
                        query=normalized_query,
                        cursor=cursor,
                        limit=bounded_limit,
                        parameter_range=parameter_range,
                        download_size_range=download_size_range,
                        quantization=normalized_quantization,
                        min_context=min_context,
                        include_unknown=include_unknown,
                    )
                )
                self._inflight[key] = task
                task.add_done_callback(partial(self._clear_inflight, key))
        try:
            response = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._cache_lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)
        async with self._cache_lock:
            if len(self._cache) >= CATALOG_CACHE_MAX_ENTRIES:
                oldest_key = min(self._cache, key=lambda item: self._cache[item].expires_at)
                self._cache.pop(oldest_key, None)
            self._cache[key] = _CacheEntry(
                expires_at=monotonic() + self._cache_ttl(response),
                response=response,
            )
        return response

    def _clear_inflight(
        self,
        key: tuple[object, ...],
        task: asyncio.Task[LocalModelCatalogResponse],
    ) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)

    def resolve_reference(self, reference: str) -> LocalModelCatalogItem:
        """Map a direct WS2A canonical reference into normalized catalog metadata."""

        parsed = parse_local_model_reference(reference)
        for item in CURATED_OLLAMA_CATALOG:
            refs = {item.requested_ref, *(quant.requested_ref for quant in item.quantizations)}
            if parsed.runtime_name in {
                parse_local_model_reference(candidate).runtime_name for candidate in refs
            }:
                return item
        title = parsed.canonical_name.split("/")[-1].split(":")[0]
        return LocalModelCatalogItem(
            catalog_id=f"direct:{parsed.canonical_name}",
            source=LocalModelCatalogSource(parsed.source.value),
            requested_ref=parsed.requested_ref,
            title=title,
            publisher=(
                parsed.canonical_name.removeprefix("hf.co/").split("/", 1)[0]
                if parsed.source.value == "huggingface"
                else None
            ),
            quantizations=[
                LocalModelQuantization(
                    name=parsed.revision or "latest",
                    requested_ref=parsed.runtime_name,
                )
            ],
            warnings=["Capacity metadata is unavailable for this direct reference."],
        )

    async def _search_uncached(
        self,
        *,
        source: LocalModelCatalogSource | None,
        query: str,
        cursor: str | None,
        limit: int,
        parameter_range: str | None,
        download_size_range: str | None,
        quantization: str | None,
        min_context: int | None,
        include_unknown: bool,
    ) -> LocalModelCatalogResponse:
        items: list[LocalModelCatalogItem] = []
        statuses = [
            LocalModelCatalogSourceStatus(
                source=LocalModelCatalogSource.INSTALLED,
                available=False,
                detail="Live installed-model inventory will appear when runtime APIs are available.",
            )
        ]
        next_cursor: str | None = None

        curated: list[LocalModelCatalogItem] = []
        if source in {None, LocalModelCatalogSource.OLLAMA}:
            terms = query.casefold().split()
            curated = [
                item
                for item in CURATED_OLLAMA_CATALOG
                if (
                    not terms
                    or all(
                        term
                        in " ".join(
                            filter(
                                None,
                                [item.title, item.publisher, item.description, item.requested_ref],
                            )
                        ).casefold()
                        for term in terms
                    )
                )
                and self._matches_filters(
                    item,
                    parameter_range=parameter_range,
                    download_size_range=download_size_range,
                    quantization=quantization,
                    min_context=min_context,
                    include_unknown=include_unknown,
                )
            ]
            if source == LocalModelCatalogSource.OLLAMA:
                items.extend(curated[:limit])
            elif cursor is None:
                # Keep at least one slot for the first HF page so its cursor
                # cannot skip results hidden behind curated entries.
                items.extend(curated[: min(len(curated), max(0, limit - 1))])
            statuses.append(
                LocalModelCatalogSourceStatus(
                    source=LocalModelCatalogSource.OLLAMA,
                    available=True,
                    detail="Bundled curated catalog; no undocumented Ollama API is queried.",
                )
            )

        if source in {None, LocalModelCatalogSource.HUGGINGFACE}:
            try:
                hf_limit = (
                    limit if source == LocalModelCatalogSource.HUGGINGFACE else limit - len(items)
                )
                hf_items, next_cursor = await self._search_huggingface(
                    query=query,
                    cursor=cursor,
                    limit=hf_limit,
                )
                hf_items = [
                    item
                    for item in hf_items
                    if self._matches_filters(
                        item,
                        parameter_range=parameter_range,
                        download_size_range=download_size_range,
                        quantization=quantization,
                        min_context=min_context,
                        include_unknown=include_unknown,
                    )
                ]
                items.extend(hf_items)
                statuses.append(
                    LocalModelCatalogSourceStatus(
                        source=LocalModelCatalogSource.HUGGINGFACE,
                        available=True,
                        detail=(
                            "Public Hugging Face GGUF search metadata. Repository details load "
                            "separately for visible or selected models."
                        ),
                    )
                )
            except CatalogUpstreamError as exc:
                statuses.append(
                    LocalModelCatalogSourceStatus(
                        source=LocalModelCatalogSource.HUGGINGFACE,
                        available=False,
                        detail=str(exc),
                        retry_after_seconds=exc.retry_after_seconds,
                    )
                )

        if source == LocalModelCatalogSource.INSTALLED:
            items = []
        return LocalModelCatalogResponse(
            items=items,
            next_cursor=next_cursor,
            sources=statuses,
            pagination_note=(
                "Filters apply to this bounded upstream page only; fewer than the requested "
                "number may match. Load more continues from Hugging Face's next cursor."
                if any(
                    value is not None
                    for value in (
                        parameter_range,
                        download_size_range,
                        quantization,
                        min_context,
                    )
                )
                or not include_unknown
                else None
            ),
        )

    async def _search_huggingface(
        self,
        *,
        query: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[LocalModelCatalogItem], str | None]:
        params: dict[str, str | int] = {
            "filter": "gguf",
            "full": "true",
            "limit": limit,
            "sort": "downloads",
            "direction": "-1",
        }
        if query:
            params["search"] = query
        if cursor:
            if len(cursor) > 512 or not re.fullmatch(r"[A-Za-z0-9._~=-]+", cursor):
                raise ValueError("invalid Hugging Face catalog cursor")
            params["cursor"] = cursor
        try:
            async with asyncio.timeout(HF_SEARCH_DEADLINE_SECONDS):
                response = await self._request("/api/models", params=params)
        except TimeoutError as exc:
            raise CatalogUpstreamError(
                "Hugging Face search exceeded the bounded deadline."
            ) from exc
        assert response is not None
        payload = response.json()
        if not isinstance(payload, list):
            raise CatalogUpstreamError("Hugging Face returned an invalid catalog response.")

        raw_models = [raw for raw in payload[:limit] if isinstance(raw, dict)]
        normalized: list[LocalModelCatalogItem] = []
        for raw in raw_models:
            item = self._normalize_hf_model(raw)
            if item is not None:
                normalized.append(item)
        return normalized, self._next_cursor(response.headers.get("link"))

    async def detail(
        self,
        *,
        repo_id: str,
        revision_sha: str | None,
    ) -> LocalModelCatalogItem:
        """Resolve one HF repository through the bounded, coalesced detail path."""

        if _HF_REPO_RE.fullmatch(repo_id) is None:
            raise ValueError("invalid Hugging Face repository")
        normalized_sha = revision_sha.lower() if revision_sha else "latest"
        if revision_sha is not None and _HF_SHA_RE.fullmatch(revision_sha) is None:
            raise ValueError("invalid Hugging Face revision SHA")
        key = (repo_id.casefold(), normalized_sha)
        async with self._cache_lock:
            cache_key = key
            if revision_sha is None:
                latest_sha = self._detail_latest.get(repo_id.casefold())
                if latest_sha is not None:
                    cache_key = (repo_id.casefold(), latest_sha)
            cached = self._detail_cache.get(cache_key)
            if cached is not None and cached.expires_at > monotonic():
                return cached.item
            task = self._detail_inflight.get(key)
            if task is None:
                if len(self._detail_inflight) >= HF_MAX_DETAIL_TASKS:
                    raise CatalogUpstreamError(
                        "Hugging Face repository detail capacity is temporarily full."
                    )
                task = asyncio.create_task(
                    self._run_hf_detail(repo_id=repo_id, revision_sha=revision_sha)
                )
                self._detail_inflight[key] = task
                task.add_done_callback(partial(self._clear_detail_inflight, key))
        try:
            item = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._cache_lock:
                    if self._detail_inflight.get(key) is task:
                        self._detail_inflight.pop(key, None)
        actual_sha = item.revision_sha or normalized_sha
        actual_key = (repo_id.casefold(), actual_sha)
        async with self._cache_lock:
            if len(self._detail_cache) >= DETAIL_CACHE_MAX_ENTRIES:
                oldest = min(
                    self._detail_cache,
                    key=lambda candidate: self._detail_cache[candidate].expires_at,
                )
                self._detail_cache.pop(oldest, None)
                for latest_repo, latest_sha in list(self._detail_latest.items()):
                    if (latest_repo, latest_sha) == oldest:
                        self._detail_latest.pop(latest_repo, None)
            entry = _DetailCacheEntry(
                expires_at=monotonic() + self._cache_ttl_seconds,
                item=item,
            )
            self._detail_cache[actual_key] = entry
            if revision_sha is None:
                self._detail_latest[repo_id.casefold()] = actual_sha
        return item

    def _clear_detail_inflight(
        self,
        key: tuple[str, str],
        task: asyncio.Task[LocalModelCatalogItem],
    ) -> None:
        if self._detail_inflight.get(key) is task:
            self._detail_inflight.pop(key, None)

    async def _run_hf_detail(
        self,
        *,
        repo_id: str,
        revision_sha: str | None,
    ) -> LocalModelCatalogItem:
        try:
            async with asyncio.timeout(HF_DETAIL_DEADLINE_SECONDS):
                return await self._resolve_hf_detail(
                    repo_id=repo_id,
                    revision_sha=revision_sha,
                )
        except TimeoutError as exc:
            raise CatalogUpstreamError(
                "Hugging Face repository details exceeded the bounded deadline."
            ) from exc

    async def _resolve_hf_detail(
        self,
        *,
        repo_id: str,
        revision_sha: str | None,
    ) -> LocalModelCatalogItem:
        params: dict[str, str | int] = {"blobs": "true"}
        detail_path = f"/api/models/{repo_id}"
        if revision_sha is not None:
            detail_path = f"{detail_path}/revision/{revision_sha.lower()}"
        async with self._detail_semaphore:
            response = await self._request(
                detail_path,
                params=params,
                max_bytes=HF_MAX_RESPONSE_BYTES,
            )
            assert response is not None
            payload = response.json()
            if not isinstance(payload, dict):
                raise CatalogUpstreamError("Hugging Face returned invalid repository metadata.")
            actual_sha = payload.get("sha")
            if revision_sha is not None and (
                not isinstance(actual_sha, str) or actual_sha.casefold() != revision_sha.casefold()
            ):
                raise CatalogUpstreamError(
                    "Hugging Face repository revision did not match the requested SHA."
                )
            readme = await self._fetch_hf_readme(
                repo_id,
                actual_sha if isinstance(actual_sha, str) else revision_sha,
            )
        item = self._normalize_hf_model(payload, readme=readme, detailed=True)
        if item is None:
            raise CatalogUpstreamError(
                "Hugging Face repository has no selectable non-projector GGUF artifact."
            )
        return item

    async def _fetch_hf_readme(self, repo_id: str, revision_sha: str | None) -> str | None:
        if revision_sha is None or _HF_SHA_RE.fullmatch(revision_sha) is None:
            return None
        response = await self._request(
            f"/{repo_id}/raw/{revision_sha}/README.md",
            params={},
            expect_json=False,
            max_bytes=HF_MAX_README_BYTES,
            allow_not_found=True,
        )
        return None if response is None else response.text

    def _normalize_hf_model(
        self,
        raw: dict[str, Any],
        *,
        readme: str | None = None,
        detailed: bool = False,
    ) -> LocalModelCatalogItem | None:
        repo_id = raw.get("id") or raw.get("modelId")
        if not isinstance(repo_id, str) or _HF_REPO_RE.fullmatch(repo_id) is None:
            return None
        siblings = raw.get("siblings")
        quantizations = self._hf_quantizations(repo_id, siblings)
        if not quantizations:
            return None
        raw_card_data = raw.get("cardData")
        card_data: dict[str, Any] = raw_card_data if isinstance(raw_card_data, dict) else {}
        raw_tags = raw.get("tags")
        raw_tag_values: list[Any] = raw_tags if isinstance(raw_tags, list) else []
        tags = [
            tag[:128]
            for tag in raw_tag_values[:100]
            if isinstance(tag, str) and 0 < len(tag) <= 128
        ]
        license_name = (
            card_data.get("license") if isinstance(card_data.get("license"), str) else None
        )
        if license_name is None:
            license_name = next(
                (
                    tag.split(":", 1)[1]
                    for tag in raw_tag_values
                    if isinstance(tag, str) and tag.startswith("license:")
                ),
                None,
            )
        parameter_count = self._hf_parameter_count(raw)
        advertised_context, architecture = self._hf_architecture(raw)
        capabilities = [LocalModelCatalogCapability.CHAT]
        tag_values = {tag.casefold() for tag in tags}
        if any("vision" in tag or "image-text-to-text" in tag for tag in tag_values):
            capabilities.append(LocalModelCatalogCapability.VISION)
        publisher, title = repo_id.split("/", 1)
        warnings: list[str] = []
        if license_name is None:
            warnings.append(
                "Upstream license metadata was not provided; review the repository before use."
            )
        if any(
            quantization.file_name is not None
            and quantization.file_name.startswith(("Multiple", "Incomplete"))
            for quantization in quantizations
        ):
            warnings.append(
                "Multiple or incomplete GGUF files share a quantization; artifact size remains "
                "unknown because the current pull contract cannot disambiguate them."
            )
        revision_sha = raw.get("sha")
        if not isinstance(revision_sha, str) or _HF_SHA_RE.fullmatch(revision_sha) is None:
            revision_sha = None
        repository_url = f"{HF_API_BASE}/{repo_id}"
        model_card_url = (
            f"{repository_url}/blob/{revision_sha}/README.md"
            if revision_sha is not None
            else f"{repository_url}#model-card"
        )
        pipeline_tag = raw.get("pipeline_tag")
        if not isinstance(pipeline_tag, str):
            pipeline_tag = None
        base_models = self._hf_base_models(card_data, tags)
        architecture_name = self._hf_architecture_name(raw)
        description = self._sanitize_readme_excerpt(readme) if readme else None
        if description is None:
            card_description = card_data.get("description")
            if isinstance(card_description, str):
                description = self._sanitize_readme_excerpt(card_description)
        if description is None and isinstance(card_data.get("model_name"), str):
            description = str(card_data["model_name"])[:1000]
        return LocalModelCatalogItem(
            catalog_id=f"huggingface:{repo_id}",
            source=LocalModelCatalogSource.HUGGINGFACE,
            requested_ref=quantizations[0].requested_ref,
            title=title,
            publisher=publisher,
            repository_url=repository_url,
            model_card_url=model_card_url,
            revision_sha=revision_sha,
            license=license_name,
            description=description,
            downloads=self._bounded_nonnegative_int(raw.get("downloads")),
            likes=self._bounded_nonnegative_int(raw.get("likes")),
            last_modified=self._parse_hf_datetime(raw.get("lastModified")),
            pipeline_tag=pipeline_tag[:128] if pipeline_tag else None,
            tags=tags,
            base_models=base_models,
            capabilities=capabilities,
            parameter_count=parameter_count,
            quantizations=quantizations,
            file_size_bytes=quantizations[0].size_bytes,
            advertised_max_context=advertised_context,
            architecture=architecture,
            architecture_name=architecture_name,
            metadata_status="complete" if detailed else "basic",
            metadata_confidence="high" if detailed else "medium",
            reference_integrity="floating",
            warnings=warnings,
        )

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, str | int],
        expect_json: bool = True,
        max_bytes: int = HF_MAX_RESPONSE_BYTES,
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
        if not self._is_allowed_hf_path(path):
            raise ValueError("invalid Hugging Face endpoint")
        request_url = f"{HF_API_BASE}{path}"
        endpoint_class = self._hf_endpoint_class(path)
        for attempt in range(2):
            # HF catalog bodies are small and bounded. Retiring every HTTP/1.1
            # response connection ensures a decode retry cannot reuse the
            # connection that delivered malformed framing or encoding.
            headers = {
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            if attempt:
                headers.update(
                    {
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    }
                )
            try:
                request = self._client.build_request(
                    "GET",
                    request_url,
                    params=params,
                    headers=headers,
                )
                streamed = await self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            except httpx.DecodingError as exc:
                self._log_hf_decoding_failure(
                    endpoint_class=endpoint_class,
                    status=None,
                    content_encoding="unknown",
                    retry=attempt == 0,
                )
                if attempt == 0:
                    continue
                raise CatalogUpstreamError(
                    "Hugging Face returned an invalid encoded response."
                ) from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise CatalogUpstreamError("Hugging Face is temporarily unreachable.") from exc
            try:
                if streamed.url.scheme != "https" or streamed.url.host != "huggingface.co":
                    raise CatalogUpstreamError("Hugging Face returned an unsafe response origin.")
                if 300 <= streamed.status_code < 400:
                    location = streamed.headers.get("location")
                    if location:
                        redirected = streamed.url.join(location)
                        if redirected.scheme != "https" or redirected.host != "huggingface.co":
                            raise CatalogUpstreamError("Hugging Face returned an unsafe redirect.")
                    raise CatalogUpstreamError("Hugging Face returned an unexpected redirect.")
                if allow_not_found and streamed.status_code == 404:
                    return None
                if streamed.status_code == 429:
                    retry_after = streamed.headers.get("retry-after")
                    retry_seconds = (
                        min(int(retry_after), 86_400)
                        if retry_after and retry_after.isdigit()
                        else None
                    )
                    raise CatalogUpstreamError(
                        "Hugging Face rate limit reached. Curated models remain available.",
                        retry_after_seconds=retry_seconds,
                    )
                if streamed.status_code >= 500:
                    raise CatalogUpstreamError("Hugging Face is temporarily unavailable.")
                if streamed.status_code >= 400:
                    raise CatalogUpstreamError("Hugging Face rejected the public catalog request.")
                declared_length = streamed.headers.get("content-length")
                if (
                    declared_length
                    and declared_length.isdigit()
                    and int(declared_length) > max_bytes
                ):
                    raise CatalogUpstreamError(
                        "Hugging Face response exceeded the bounded body size."
                    )
                body = bytearray()
                try:
                    async for chunk in streamed.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise CatalogUpstreamError(
                                "Hugging Face response exceeded the bounded body size."
                            )
                except httpx.DecodingError as exc:
                    self._log_hf_decoding_failure(
                        endpoint_class=endpoint_class,
                        status=streamed.status_code,
                        content_encoding=self._safe_content_encoding(
                            streamed.headers.get("content-encoding")
                        ),
                        retry=attempt == 0,
                    )
                    if attempt == 0:
                        continue
                    raise CatalogUpstreamError(
                        "Hugging Face returned an invalid encoded response."
                    ) from exc
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    raise CatalogUpstreamError("Hugging Face is temporarily unreachable.") from exc
                response = httpx.Response(
                    streamed.status_code,
                    headers=streamed.headers,
                    content=bytes(body),
                    request=request,
                )
                if expect_json:
                    try:
                        response.json()
                    except ValueError as exc:
                        raise CatalogUpstreamError("Hugging Face returned invalid JSON.") from exc
                return response
            finally:
                await streamed.aclose()
        raise AssertionError("unreachable Hugging Face decode retry state")

    @staticmethod
    def _log_hf_decoding_failure(
        *,
        endpoint_class: str,
        status: int | None,
        content_encoding: str,
        retry: bool,
    ) -> None:
        logger.warning(
            "Hugging Face catalog response decoding failed",
            extra={
                "extra_data": {
                    "endpoint_class": endpoint_class,
                    "status": status,
                    "content_encoding": content_encoding,
                    "retry": retry,
                }
            },
        )

    @staticmethod
    def _hf_endpoint_class(path: str) -> str:
        if path == "/api/models":
            return "search"
        if path.endswith("/README.md"):
            return "readme"
        return "detail"

    @staticmethod
    def _safe_content_encoding(value: str | None) -> str:
        if value is None:
            return "none"
        normalized = value.strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9, _-]{0,63}", normalized):
            return normalized
        return "invalid"

    @staticmethod
    def _is_allowed_hf_path(path: str) -> bool:
        if path == "/api/models":
            return True
        if re.fullmatch(
            r"/api/models/[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
            r"(?:/revision/[0-9a-fA-F]{40})?",
            path,
        ):
            return True
        return (
            re.fullmatch(
                r"/[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/raw/[0-9a-fA-F]{40}/README\.md",
                path,
            )
            is not None
        )

    @staticmethod
    def _bounded_nonnegative_int(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
            return value
        return None

    @staticmethod
    def _parse_hf_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or len(value) > 64:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _hf_base_models(card_data: dict[str, Any], tags: list[str]) -> list[str]:
        raw = card_data.get("base_model")
        values = raw if isinstance(raw, list) else [raw]
        values.extend(
            tag.removeprefix("base_model:") for tag in tags if tag.startswith("base_model:")
        )
        result: list[str] = []
        for value in values:
            if isinstance(value, str) and 0 < len(value) <= 255 and value not in result:
                result.append(value)
            if len(result) >= 20:
                break
        return result

    @staticmethod
    def _hf_architecture_name(raw: dict[str, Any]) -> str | None:
        config = raw.get("config")
        if not isinstance(config, dict):
            return None
        architectures = config.get("architectures")
        if isinstance(architectures, list) and architectures:
            value = architectures[0]
            if isinstance(value, str) and 0 < len(value) <= 128:
                return value
        model_type = config.get("model_type")
        return model_type if isinstance(model_type, str) and 0 < len(model_type) <= 128 else None

    @staticmethod
    def _sanitize_readme_excerpt(value: str) -> str | None:
        text = value[:HF_MAX_README_BYTES]
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
        text = re.sub(
            r"<(script|style)\b[^>]*>.*?</\1>",
            " ",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
        text = re.sub(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)", r"\1", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
        text = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", " ", text)
        text = re.sub(r"[`*_~]+", "", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)
        if not text:
            return None
        return text[:1000].rstrip()

    @staticmethod
    def _matches_filters(
        item: LocalModelCatalogItem,
        *,
        parameter_range: str | None,
        download_size_range: str | None,
        quantization: str | None,
        min_context: int | None,
        include_unknown: bool,
    ) -> bool:
        if parameter_range and not _matches_numeric_preset(
            item.parameter_count,
            parameter_range,
            {
                "le4b": (None, 4_000_000_000),
                "4b_8b": (4_000_000_000, 8_000_000_000),
                "8b_14b": (8_000_000_000, 14_000_000_000),
                "14b_32b": (14_000_000_000, 32_000_000_000),
                "32b_70b": (32_000_000_000, 70_000_000_000),
                "70b_plus": (70_000_000_000, None),
            },
            include_unknown=include_unknown,
        ):
            return False
        matching_quantizations = [
            candidate
            for candidate in item.quantizations
            if quantization is None or candidate.name == quantization
        ]
        if quantization is not None and not matching_quantizations:
            return False
        if download_size_range:
            known_sizes = [
                candidate.size_bytes
                for candidate in matching_quantizations
                if candidate.size_bytes is not None
            ]
            if not known_sizes:
                if not include_unknown:
                    return False
            elif not any(
                _matches_numeric_preset(
                    size,
                    download_size_range,
                    {
                        "le4gib": (None, _gib(4)),
                        "4gib_8gib": (_gib(4), _gib(8)),
                        "8gib_16gib": (_gib(8), _gib(16)),
                        "16gib_32gib": (_gib(16), _gib(32)),
                        "32gib_plus": (_gib(32), None),
                    },
                    include_unknown=False,
                )
                for size in known_sizes
            ):
                return False
        if min_context is not None:
            if item.advertised_max_context is None:
                return include_unknown
            if item.advertised_max_context < min_context:
                return False
        return True

    @staticmethod
    def _hf_quantizations(repo_id: str, siblings: object) -> list[LocalModelQuantization]:
        if not isinstance(siblings, list):
            return []
        grouped: dict[str, list[tuple[str, int | None]]] = {}
        for sibling in siblings[:500]:
            if not isinstance(sibling, dict):
                continue
            filename = sibling.get("rfilename")
            if not isinstance(filename, str) or not filename.lower().endswith(".gguf"):
                continue
            lowered_filename = filename.casefold()
            if "mmproj" in lowered_filename or "projector" in lowered_filename:
                continue
            match = _QUANT_RE.search(filename)
            if match is None:
                continue
            name = match.group(1).upper()
            size = sibling.get("size")
            if not isinstance(size, int):
                lfs = sibling.get("lfs")
                size = lfs.get("size") if isinstance(lfs, dict) else None
            grouped.setdefault(name, []).append(
                (
                    filename,
                    size if isinstance(size, int) and 0 <= size <= 2**63 - 1 else None,
                )
            )
        quantizations: list[LocalModelQuantization] = []
        for name, files in grouped.items():
            requested_ref = f"hf.co/{repo_id}:{name}"
            try:
                parse_local_model_reference(requested_ref)
            except ValueError:
                continue
            total_size, file_name = LocalModelCatalog._gguf_artifact_size(files)
            quantizations.append(
                LocalModelQuantization(
                    name=name,
                    requested_ref=requested_ref,
                    file_name=file_name[:512],
                    size_bytes=total_size,
                    bits_per_weight=_quantization_bits(name),
                )
            )
            if len(quantizations) >= 100:
                break
        return sorted(
            quantizations,
            key=lambda item: (
                item.size_bytes is None,
                item.size_bytes or 0,
                item.name,
            ),
        )

    @staticmethod
    def _gguf_artifact_size(
        files: list[tuple[str, int | None]],
    ) -> tuple[int | None, str]:
        if len(files) == 1:
            shard_match = _SHARD_RE.fullmatch(files[0][0])
            if shard_match is not None:
                index = int(shard_match.group("index"))
                count = int(shard_match.group("count"))
                if index != 1 or count != 1:
                    return None, f"Incomplete GGUF shard set (1/{count} files)"
            return files[0][1], files[0][0]

        shard_matches = [_SHARD_RE.fullmatch(filename) for filename, _ in files]
        if all(match is not None for match in shard_matches):
            matches = [match for match in shard_matches if match is not None]
            prefixes = {match.group("prefix").casefold() for match in matches}
            counts = {int(match.group("count")) for match in matches}
            indexes = {int(match.group("index")) for match in matches}
            expected_count = next(iter(counts)) if len(counts) == 1 else 0
            if (
                len(prefixes) == 1
                and expected_count == len(files)
                and indexes == set(range(1, expected_count + 1))
            ):
                sizes = [size for _, size in files]
                if any(size is None for size in sizes):
                    total_size = None
                else:
                    total_size = sum(size for size in sizes if size is not None)
                    if total_size > 2**63 - 1:
                        total_size = None
                return total_size, f"{len(files)} GGUF shards ({files[0][0]} …)"
            return None, f"Incomplete GGUF shard set ({len(files)} files)"

        return None, f"Multiple independent {len(files)} GGUF artifacts"

    @staticmethod
    def _hf_parameter_count(raw: dict[str, Any]) -> int | None:
        for container_name in ("safetensors", "gguf"):
            container = raw.get(container_name)
            if not isinstance(container, dict):
                continue
            value = container.get("total")
            if isinstance(value, int) and 0 < value <= 2**63 - 1:
                return value
        return None

    @staticmethod
    def _has_complete_gguf_sizes(siblings: object) -> bool:
        if not isinstance(siblings, list):
            return False
        gguf_files = [
            sibling
            for sibling in siblings
            if isinstance(sibling, dict)
            and str(sibling.get("rfilename", "")).lower().endswith(".gguf")
            and "mmproj" not in str(sibling.get("rfilename", "")).casefold()
            and "projector" not in str(sibling.get("rfilename", "")).casefold()
        ]
        if not gguf_files:
            return False
        for sibling in gguf_files:
            size = sibling.get("size")
            if not isinstance(size, int):
                lfs = sibling.get("lfs")
                size = lfs.get("size") if isinstance(lfs, dict) else None
            if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > 2**63 - 1:
                return False
        shard_groups: dict[tuple[str, int], set[int]] = {}
        for sibling in gguf_files:
            filename = sibling.get("rfilename")
            if not isinstance(filename, str):
                return False
            match = _SHARD_RE.fullmatch(filename)
            if match is None:
                continue
            count = int(match.group("count"))
            key = (match.group("prefix").casefold(), count)
            shard_groups.setdefault(key, set()).add(int(match.group("index")))
        return all(
            indexes == set(range(1, count + 1)) for (_, count), indexes in shard_groups.items()
        )

    @staticmethod
    def _hf_architecture(raw: dict[str, Any]) -> tuple[int | None, dict[str, int]]:
        raw_gguf = raw.get("gguf")
        gguf: dict[str, Any] = raw_gguf if isinstance(raw_gguf, dict) else {}
        raw_config = raw.get("config")
        config: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}

        def positive_int(*values: object) -> int | None:
            for value in values:
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 < value <= 2**31 - 1
                ):
                    return value
            return None

        advertised_context = positive_int(
            gguf.get("context_length"),
            config.get("max_position_embeddings"),
            config.get("max_sequence_length"),
            config.get("seq_length"),
        )
        layer_count = positive_int(
            gguf.get("block_count"),
            gguf.get("layer_count"),
            config.get("num_hidden_layers"),
        )
        kv_head_count = positive_int(
            gguf.get("attention_head_count_kv"),
            config.get("num_key_value_heads"),
        )
        head_dimension = positive_int(
            gguf.get("head_dimension"),
            config.get("head_dim"),
        )
        if head_dimension is None:
            hidden_size = positive_int(config.get("hidden_size"))
            attention_heads = positive_int(config.get("num_attention_heads"))
            if hidden_size is not None and attention_heads is not None:
                quotient, remainder = divmod(hidden_size, attention_heads)
                if remainder == 0 and quotient > 0:
                    head_dimension = quotient
        architecture = {
            key: value
            for key, value in (
                ("layer_count", layer_count),
                ("kv_head_count", kv_head_count),
                ("head_dimension", head_dimension),
            )
            if value is not None
        }
        return advertised_context, architecture

    def _cache_ttl(self, response: LocalModelCatalogResponse) -> float:
        degraded = [
            status
            for status in response.sources
            if status.source != LocalModelCatalogSource.INSTALLED and not status.available
        ]
        if not degraded:
            return self._cache_ttl_seconds
        retry_after = [
            status.retry_after_seconds
            for status in degraded
            if status.retry_after_seconds is not None
        ]
        return max(
            1.0,
            min(
                self._cache_ttl_seconds,
                float(min(retry_after)) if retry_after else 10.0,
            ),
        )

    @staticmethod
    def _next_cursor(link_header: str | None) -> str | None:
        if not link_header:
            return None
        for part in link_header.split(","):
            if 'rel="next"' not in part:
                continue
            start = part.find("<")
            end = part.find(">")
            if start < 0 or end <= start:
                continue
            parsed = urlparse(part[start + 1 : end])
            if parsed.scheme != "https" or parsed.netloc != "huggingface.co":
                continue
            values = parse_qs(parsed.query).get("cursor")
            if values and len(values[0]) <= 512:
                return values[0]
        return None


def _quantization_bits(name: str) -> float | None:
    normalized = name.upper()
    if normalized == "F32":
        return 32.0
    if normalized in {"F16", "BF16"}:
        return 16.0
    match = re.match(r"I?Q(\d+)", normalized)
    if match is None:
        return None
    base = float(match.group(1))
    return min(16.0, base + (0.5 if "_K" in normalized or "_S" in normalized else 0.0))


def _matches_numeric_preset(
    value: int | None,
    preset: str,
    ranges: dict[str, tuple[int | None, int | None]],
    *,
    include_unknown: bool,
) -> bool:
    if preset not in ranges:
        raise ValueError(f"invalid catalog filter preset: {preset}")
    if value is None:
        return include_unknown
    lower, upper = ranges[preset]
    return (lower is None or value > lower) and (upper is None or value <= upper)
