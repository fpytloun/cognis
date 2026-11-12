"""Retained proxy correlation is deliberately absent from canonical dimensions."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import model_validator

from cognis.core.usage.dimensions import UsageDimensions, encode_usage_dimensions
from cognis.models.usage import UsageAttribution, UsageId, UsageModel


class UsageSessionCorrelation(UsageModel):
    """Exactly one verified Cognis session or unverified client namespace."""

    session_kind: Literal["cognis", "proxy", "direct"]
    session_id: UsageId | None = None
    client_id: UsageId | None = None
    client_session_id: UsageId | None = None

    @model_validator(mode="after")
    def valid_kind(self) -> Self:
        if self.session_kind == "cognis":
            if (
                self.session_id is None
                or self.client_id is not None
                or self.client_session_id is not None
            ):
                raise ValueError("Cognis sessions require only a verified session ID")
        elif self.session_kind == "direct":
            if any((self.session_id, self.client_id, self.client_session_id)):
                raise ValueError("Direct conversation usage cannot invent session identity")
        elif self.session_id is not None or self.client_session_id is None:
            raise ValueError("Proxy sessions require a client label, not a Cognis session")
        return self


def session_correlation(attribution: UsageAttribution) -> UsageSessionCorrelation | None:
    """Verified resource binding wins; companion labels never create explorer sessions."""
    if attribution.resources is not None and attribution.resources.session_id is not None:
        return UsageSessionCorrelation(
            session_kind="cognis", session_id=attribution.resources.session_id
        )
    if attribution.resources is not None and attribution.resources.conversation_id is not None:
        return UsageSessionCorrelation(session_kind="direct")
    labels = attribution.client_labels
    if (
        labels is None
        or labels.session_id is None
        or attribution.identity.service_id in {"intaris", "mnemory"}
    ):
        return None
    return UsageSessionCorrelation(
        session_kind="proxy",
        client_id=attribution.identity.client_id,
        client_session_id=labels.session_id,
    )


def session_dimension_key(dimensions: UsageDimensions, correlation: UsageSessionCorrelation) -> str:
    """Only this session-specific encoder can include client labels or their hash."""
    base = encode_usage_dimensions(dimensions, grain="session")
    if correlation.session_kind == "direct":
        if base.dimensions.session_id is not None or base.dimensions.conversation_id is None:
            raise ValueError("Direct usage requires a conversation and no session")
        return base.dimension_key
    if correlation.session_kind == "cognis":
        if base.dimensions.session_id != correlation.session_id:
            raise ValueError("Session attribution differs from canonical resource binding")
        return base.dimension_key
    if base.dimensions.session_id is not None:
        raise ValueError("A proxy session cannot replace a Cognis session")
    encoded = json.dumps(
        {
            "dimensions": json.loads(base.encoded_json),
            "session": correlation.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
