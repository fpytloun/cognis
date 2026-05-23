"""Optional vector backend abstraction for knowledgebases."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
SPARSE_ALGORITHM = "hashed_unicode_tokens_v2"


def tokenize_sparse_text(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _TOKEN_RE.findall(normalized)


@dataclass(slots=True)
class SparseVectorData:
    indices: list[int]
    values: list[float]


def sparse_vector_from_text(text: str) -> SparseVectorData:
    """Build a deterministic hashed sparse vector for Qdrant native sparse search."""
    counts: Counter[int] = Counter()
    for token in tokenize_sparse_text(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        index = int.from_bytes(digest, "big", signed=False)
        counts[index] += 1
    indices = sorted(counts)
    return SparseVectorData(indices=indices, values=[1.0 + math.log(counts[i]) for i in indices])


@dataclass(slots=True)
class VectorPoint:
    point_id: str
    vector: list[float]
    sparse_vector: SparseVectorData | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VectorSearchHit:
    point_id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class KnowledgebaseVectorBackend(Protocol):
    name: str

    async def health(self) -> dict[str, Any]: ...
    async def upsert(self, points: list[VectorPoint], *, vector_size: int) -> None: ...
    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
        sparse_vector: SparseVectorData | None = None,
    ) -> list[VectorSearchHit]: ...
    async def delete(
        self, *, point_ids: list[str] | None = None, filters: dict[str, Any] | None = None
    ) -> None: ...


class DisabledVectorBackend:
    name = "disabled"

    async def health(self) -> dict[str, Any]:
        return {"ok": False, "reason": "disabled"}

    async def upsert(self, points: list[VectorPoint], *, vector_size: int) -> None:
        raise RuntimeError("Knowledgebase vector backend is disabled")

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
        sparse_vector: SparseVectorData | None = None,
    ) -> list[VectorSearchHit]:
        raise RuntimeError("Knowledgebase vector backend is disabled")

    async def delete(
        self,
        *,
        point_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> None:
        return None


class QdrantVectorBackend:
    name = "qdrant"

    def __init__(self, *, url: str, api_key: str, collection: str) -> None:
        self._url = url
        self._api_key = api_key or None
        self._collection = collection
        self._client: Any | None = None
        self._vector_size: int | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from qdrant_client import AsyncQdrantClient
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("qdrant-client is not installed") from exc
        self._client = AsyncQdrantClient(url=self._url, api_key=self._api_key)
        return self._client

    async def health(self) -> dict[str, Any]:
        try:
            client = self._get_client()
            await client.get_collections()
            self._assert_hybrid_client_support()
        except Exception as exc:
            return {"ok": False, "backend": self.name, "reason": str(exc)}
        return {
            "ok": True,
            "backend": self.name,
            "collection": self._collection,
            "retrieval_mode": "qdrant_native_hybrid",
            "dense_vector": DENSE_VECTOR_NAME,
            "sparse_vector": SPARSE_VECTOR_NAME,
            "sparse_algorithm": SPARSE_ALGORITHM,
            "fusion": "rrf",
        }

    def _assert_hybrid_client_support(self) -> None:
        client = self._get_client()
        if not hasattr(client, "query_points"):
            raise RuntimeError("Qdrant native hybrid search requires qdrant-client query_points")
        try:
            from qdrant_client import models
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("qdrant-client is not installed") from exc
        required = ("Fusion", "FusionQuery", "Prefetch", "SparseVector", "SparseVectorParams")
        missing = [name for name in required if not hasattr(models, name)]
        if missing:
            raise RuntimeError(
                "Qdrant native hybrid search requires qdrant-client models: " + ", ".join(missing)
            )

    async def _ensure_collection(self, vector_size: int) -> None:
        client = self._get_client()
        try:
            info = await client.get_collection(self._collection)
            params = getattr(info.config, "params", None)
            vectors = getattr(params, "vectors", None)
            sparse_vectors = getattr(params, "sparse_vectors", None)
            if not isinstance(vectors, dict):
                raise RuntimeError(
                    "Qdrant collection is not compatible with native KB hybrid search: "
                    "expected named dense vector 'dense'. Configure a new collection name "
                    "or explicitly rebuild/reset the derived KB collection."
                )
            if (
                DENSE_VECTOR_NAME not in vectors
                or not isinstance(sparse_vectors, dict)
                or SPARSE_VECTOR_NAME not in sparse_vectors
            ):
                raise RuntimeError(
                    "Qdrant collection is not compatible with native KB hybrid search: "
                    "expected named dense vector 'dense' and sparse vector 'sparse'. "
                    "Configure a new collection name or explicitly rebuild/reset the derived KB collection."
                )
            existing_size = getattr(vectors[DENSE_VECTOR_NAME], "size", None)
            if existing_size is not None and int(existing_size) != vector_size:
                raise RuntimeError(
                    f"Qdrant collection vector dimension mismatch: {existing_size} != {vector_size}"
                )
            self._vector_size = vector_size
            return
        except Exception as exc:
            if "not found" not in str(exc).lower() and "doesn't exist" not in str(exc).lower():
                raise
        self._assert_hybrid_client_support()
        from qdrant_client import models

        sparse_params_kwargs: dict[str, Any] = {}
        modifier = getattr(models, "Modifier", None)
        if modifier is not None and hasattr(modifier, "IDF"):
            sparse_params_kwargs["modifier"] = modifier.IDF

        await client.create_collection(
            collection_name=self._collection,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=vector_size, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(**sparse_params_kwargs)
            },
        )
        self._vector_size = vector_size

    async def upsert(self, points: list[VectorPoint], *, vector_size: int) -> None:
        if not points:
            return
        await self._ensure_collection(vector_size)
        from qdrant_client.models import PointStruct, SparseVector

        await self._get_client().upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=point.point_id,
                    vector={
                        DENSE_VECTOR_NAME: point.vector,
                        SPARSE_VECTOR_NAME: SparseVector(
                            indices=(point.sparse_vector.indices if point.sparse_vector else []),
                            values=(point.sparse_vector.values if point.sparse_vector else []),
                        ),
                    },
                    payload=point.payload,
                )
                for point in points
            ],
        )

    def _filter(self, filters: dict[str, Any] | None) -> Any:
        if not filters:
            return None
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        conditions = []
        for key, value in filters.items():
            match = MatchAny(any=value) if isinstance(value, list) else MatchValue(value=value)
            conditions.append(FieldCondition(key=key, match=match))
        return Filter(must=conditions)

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
        sparse_vector: SparseVectorData | None = None,
    ) -> list[VectorSearchHit]:
        self._assert_hybrid_client_support()
        from qdrant_client import models

        sparse_vector = sparse_vector or SparseVectorData(indices=[], values=[])
        result = await self._get_client().query_points(
            collection_name=self._collection,
            prefetch=[
                models.Prefetch(
                    query=vector,
                    using=DENSE_VECTOR_NAME,
                    limit=max(limit, 1),
                    filter=self._filter(filters),
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse_vector.indices, values=sparse_vector.values
                    ),
                    using=SPARSE_VECTOR_NAME,
                    limit=max(limit, 1),
                    filter=self._filter(filters),
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
        hits = getattr(result, "points", result)
        return [
            VectorSearchHit(
                point_id=str(hit.id),
                score=float(hit.score),
                payload=dict(hit.payload or {}),
            )
            for hit in hits
        ]

    async def delete(
        self,
        *,
        point_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> None:
        if not point_ids and not filters:
            return
        from qdrant_client.models import Filter, PointIdsList

        selector: PointIdsList | Filter | None = (
            PointIdsList(points=point_ids) if point_ids else self._filter(filters)
        )
        if selector is None:
            return
        await self._get_client().delete(collection_name=self._collection, points_selector=selector)
