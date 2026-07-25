"""Deterministic acceptance contract for Daily Brief deliverables."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cognis.models.deliverable import pulse_quality_metadata

_DAILY_BRIEF_TITLE_RE = re.compile(r"^\s*daily[\s_-]+brief\s*$", re.IGNORECASE)
_FORBIDDEN_FUEL_RE = re.compile(r"\b(?:diesel|nafta)\b", re.IGNORECASE)
_VISIBLE_AVAILABILITY_KEYS = frozenset({"availability", "available", "degraded_data"})
_DAILY_BRIEF_VERSION_RE = re.compile(r"daily[\s_-]*brief[\s_-]*v(\d+)", re.IGNORECASE)
CURRENT_DAILY_BRIEF_CONTRACT_VERSION = 13


@dataclass(frozen=True, slots=True)
class DailyBriefContractActivation:
    """Concrete Daily Brief contract selected for one runtime step."""

    version: int
    source: str
    skill_id: str | None = None
    skill_version_id: str | None = None
    skill_version_number: int | None = None
    skill_content_hash: str | None = None


def tool_call_fingerprint(tool_name: str, arguments: object) -> str:
    """Return a stable fingerprint for one validated tool invocation."""

    serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{tool_name}\n{serialized}".encode()).hexdigest()


def daily_brief_contract_required(
    *,
    task_title: str,
    task_description: str,
    task_expected_output: str | None,
    loaded_skill_names: Iterable[str],
) -> bool:
    """Return whether the current step is governed by the Daily Brief contract."""

    return (
        resolve_daily_brief_contract(
            task_title=task_title,
            task_description=task_description,
            task_expected_output=task_expected_output,
            loaded_skill_names=loaded_skill_names,
        )
        is not None
    )


def daily_brief_contract_version(
    *,
    name: str,
    prompt_templates: Mapping[str, Any] | None = None,
    instructions: str | None = None,
) -> int | None:
    """Extract an explicit Daily Brief contract version from immutable skill content."""

    if name.strip().lower() != "daily-brief":
        return None
    candidates = [instructions or ""]
    for key, value in (prompt_templates or {}).items():
        candidates.extend((str(key), str(value)))
    versions = {
        int(match.group(1))
        for candidate in candidates
        for match in _DAILY_BRIEF_VERSION_RE.finditer(candidate)
    }
    return max(versions) if versions else None


def resolve_daily_brief_contract(
    *,
    task_title: str,
    task_description: str,
    task_expected_output: str | None,
    loaded_skill_names: Iterable[str] = (),
    loaded_skill_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
) -> DailyBriefContractActivation | None:
    """Resolve the exact contract version without upgrading older loaded skills."""

    snapshots = loaded_skill_snapshots or {}
    daily_snapshots = [
        snapshot
        for snapshot in snapshots.values()
        if str(snapshot.get("name") or "").strip().lower() == "daily-brief"
    ]
    if daily_snapshots:
        snapshot = daily_snapshots[-1]
        raw_version = snapshot.get("contract_version")
        if isinstance(raw_version, int) and raw_version > 0:
            return DailyBriefContractActivation(
                version=raw_version,
                source="loaded_skill",
                skill_id=_optional_string(snapshot.get("skill_id")),
                skill_version_id=_optional_string(snapshot.get("version_id")),
                skill_version_number=(
                    int(snapshot["version_number"])
                    if isinstance(snapshot.get("version_number"), int)
                    else None
                ),
                skill_content_hash=_optional_string(snapshot.get("content_hash")),
            )

    contract_text = f"{task_description}\n{task_expected_output or ''}"
    versions = {int(match.group(1)) for match in _DAILY_BRIEF_VERSION_RE.finditer(contract_text)}
    if versions:
        return DailyBriefContractActivation(version=max(versions), source="task_contract")
    if any(name.strip().lower() == "daily-brief" for name in loaded_skill_names):
        return DailyBriefContractActivation(
            version=CURRENT_DAILY_BRIEF_CONTRACT_VERSION,
            source="legacy_loaded_skill",
        )
    if _DAILY_BRIEF_TITLE_RE.fullmatch(task_title or ""):
        return DailyBriefContractActivation(
            version=CURRENT_DAILY_BRIEF_CONTRACT_VERSION,
            source="task_title",
        )
    if "daily-brief skill" in contract_text.lower():
        return DailyBriefContractActivation(
            version=CURRENT_DAILY_BRIEF_CONTRACT_VERSION,
            source="legacy_task_contract",
        )
    return None


def validate_daily_brief_deliverable(
    *,
    action: object,
    format_name: str,
    rich: object,
    validation_fingerprint_present: bool,
    executed_tool_names: Iterable[str],
    materialized_artifact_evidence: Mapping[str, str] | None = None,
) -> list[str]:
    """Return deterministic rejection reasons for a Daily Brief deliverable."""

    issues: list[str] = []
    if action != "rich:pulse":
        issues.append("action must be rich:pulse")
    if format_name != "rich":
        issues.append("format must be rich")
    if not validation_fingerprint_present:
        issues.append("the exact write_deliverable payload must pass validate_tool_call first")
    if not isinstance(rich, dict):
        issues.append("rich payload is required")
        return issues

    metadata = rich.get("metadata")
    if not isinstance(metadata, dict):
        issues.append("rich.metadata is required")
    else:
        if metadata.get("presentation") != "pulse":
            issues.append("rich.metadata.presentation must be pulse")
        if metadata.get("pulse_variant") != "daily":
            issues.append("rich.metadata.pulse_variant must be daily")
        if metadata.get("pulse_version") != 2:
            issues.append("rich.metadata.pulse_version must be 2")

    tool_names = [name for name in executed_tool_names if isinstance(name, str)]
    quality = pulse_quality_metadata(rich)
    article_count = int(quality.get("article_count") or 0)
    article_media_count = int(quality.get("article_media_count") or 0)
    article_citation_count = int(quality.get("article_citation_count") or 0)
    if article_count < 1:
        issues.append("at least one rendered article is required")
    if not (article_count == article_media_count == article_citation_count):
        issues.append("article_count, article_media_count and article_citation_count must be equal")
    if not _article_media_matches_materialization_evidence(
        rich, materialized_artifact_evidence or {}
    ):
        issues.append(
            "each article must use a distinct artifact_read result whose source URL matches "
            "the cited article"
        )

    serialized = json.dumps(rich, sort_keys=True, ensure_ascii=False, default=str)
    if _FORBIDDEN_FUEL_RE.search(serialized):
        issues.append("diesel content is forbidden")
    if _contains_visible_availability_metadata(rich.get("blocks")):
        issues.append("visible availability metadata is forbidden")
    if _contains_lazy_or_remote_media(rich.get("blocks")):
        issues.append("article media must use materialized artifact IDs")

    if "image_edit" in tool_names:
        issues.append("image_edit is forbidden")
    if tool_names.count("image_generate") > 1:
        issues.append("image_generate may be used at most once")
    return issues


def persisted_daily_brief_is_valid(
    deliverable: object,
    *,
    contract_version: int = CURRENT_DAILY_BRIEF_CONTRACT_VERSION,
) -> bool:
    """Return whether a persisted deliverable carries accepted Pulse evidence."""

    if getattr(deliverable, "format", None) != "rich":
        return False
    metadata = getattr(deliverable, "render_metadata", None)
    if not isinstance(metadata, dict):
        return False
    quality = metadata.get("pulse_quality")
    if not isinstance(quality, dict):
        return False
    contract = metadata.get("daily_brief_contract")
    if not isinstance(contract, dict):
        return False
    article_count = int(quality.get("article_count") or 0)
    return (
        metadata.get("pulse_valid") is True
        and metadata.get("pulse_schema") == "cognis.rich.pulse.v2"
        and metadata.get("pulse_variant") == "daily"
        and contract.get("version") == contract_version
        and isinstance(contract.get("validated_payload_fingerprint"), str)
        and bool(contract["validated_payload_fingerprint"])
        and article_count > 0
        and article_count == int(quality.get("article_media_count") or 0)
        and article_count == int(quality.get("article_citation_count") or 0)
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _contains_visible_availability_metadata(value: object) -> bool:
    if isinstance(value, list):
        return any(_contains_visible_availability_metadata(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    if any(key in value for key in _VISIBLE_AVAILABILITY_KEYS):
        return True
    if str(value.get("status") or "").strip().lower() == "unavailable":
        return True
    return any(_contains_visible_availability_metadata(item) for item in value.values())


def _article_media_matches_materialization_evidence(
    payload: Mapping[str, object],
    evidence: Mapping[str, str],
) -> bool:
    raw_sources = payload.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    source_urls = {
        str(item.get("id")).strip(): str(item.get("url")).strip()
        for item in sources
        if isinstance(item, Mapping)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("url"), str)
    }
    articles = _article_blocks(payload.get("blocks"), inside_accordion=False)
    seen_artifact_ids: set[str] = set()
    for article in articles:
        refs = article.get("source_ids")
        source_ids = (
            [str(ref).strip() for ref in refs if isinstance(ref, str)]
            if isinstance(refs, list)
            else []
        )
        if isinstance(article.get("source_id"), str):
            source_ids.append(str(article["source_id"]).strip())
        media = article.get("media")
        if not isinstance(media, Mapping):
            return False
        artifact_id = media.get("ref") or media.get("artifact_id") or media.get("content_ref")
        if (
            not isinstance(artifact_id, str)
            or artifact_id in seen_artifact_ids
            or artifact_id not in evidence
        ):
            return False
        provenance = media.get("source_url") or media.get("provenance")
        allowed_urls = {
            source_urls[source_id] for source_id in source_ids if source_id in source_urls
        }
        evidence_url = evidence[artifact_id].strip()
        if (
            not isinstance(provenance, str)
            or provenance.strip() not in allowed_urls
            or evidence_url not in allowed_urls
            or provenance.strip() != evidence_url
        ):
            return False
        seen_artifact_ids.add(artifact_id)
    return bool(articles)


def _article_blocks(value: object, *, inside_accordion: bool) -> list[Mapping[str, object]]:
    if isinstance(value, list):
        return [
            article
            for item in value
            for article in _article_blocks(item, inside_accordion=inside_accordion)
        ]
    if not isinstance(value, Mapping):
        return []
    block_type = str(value.get("type") or "")
    nested_inside_accordion = inside_accordion or block_type == "accordion"
    articles = (
        [value] if inside_accordion and block_type in {"card", "section", "research_answer"} else []
    )
    for key in ("blocks", "children", "items"):
        articles.extend(_article_blocks(value.get(key), inside_accordion=nested_inside_accordion))
    return articles


def _contains_lazy_or_remote_media(value: object) -> bool:
    if isinstance(value, list):
        return any(_contains_lazy_or_remote_media(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    media = value.get("media")
    if isinstance(media, Mapping):
        ref = media.get("ref") or media.get("artifact_id") or media.get("content_ref")
        if isinstance(ref, str) and (
            ref.startswith("tool_artifact:")
            or ref.startswith("http://")
            or ref.startswith("https://")
        ):
            return True
    return any(_contains_lazy_or_remote_media(item) for item in value.values())
