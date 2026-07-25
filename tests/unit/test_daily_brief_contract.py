from __future__ import annotations

from copy import deepcopy

from cognis.core.daily_brief_contract import (
    daily_brief_contract_required,
    daily_brief_contract_version,
    persisted_daily_brief_is_valid,
    resolve_daily_brief_contract,
    tool_call_fingerprint,
    validate_daily_brief_deliverable,
)
from cognis.models.deliverable import (
    PULSE_DAILY_SKELETON,
    Deliverable,
    normalize_required_rich_payload,
)


def _valid_daily_brief() -> dict:
    url = "https://news.example.test/article"
    return {
        "blocks": [
            {
                "type": "accordion",
                "items": [
                    {
                        "type": "card",
                        "title": "Verified article",
                        "href": url,
                        "content": f"[Read the source article]({url})",
                        "source_ids": ["article-1"],
                        "media": {
                            "ref": "att_article_1",
                            "alt": "Article source photograph",
                            "source_url": url,
                        },
                    }
                ],
            }
        ],
        "assets": [],
        "sources": [{"id": "article-1", "url": url, "title": "Verified article"}],
        "datasets": [],
        "exports": [],
        "metadata": {
            "presentation": "pulse",
            "pulse_variant": "daily",
            "pulse_version": 2,
        },
    }


def _valid_full_daily_brief() -> dict:
    payload = deepcopy(PULSE_DAILY_SKELETON)
    source_urls = {source["id"]: source["url"] for source in payload["sources"]}
    article_number = 0
    for accordion in payload["blocks"][4]["blocks"]:
        for article in accordion["items"]:
            article_number += 1
            source_id = article["source_id"]
            article["media"] = {
                "ref": f"att_article_{article_number}",
                "alt": f"Source image for {article['title']}",
                "source_url": source_urls[source_id],
            }
    return payload


def _materialization_evidence(payload: dict) -> dict[str, str]:
    evidence: dict[str, str] = {}
    stack = list(payload.get("blocks", []))
    while stack:
        block = stack.pop()
        if not isinstance(block, dict):
            continue
        media = block.get("media")
        if isinstance(media, dict):
            artifact_id = media.get("ref")
            source_url = media.get("source_url")
            if isinstance(artifact_id, str) and isinstance(source_url, str):
                evidence[artifact_id] = source_url
        for key in ("blocks", "children", "items"):
            nested = block.get(key)
            if isinstance(nested, list):
                stack.extend(nested)
    return evidence


def test_daily_brief_scope_is_narrow_and_deterministic() -> None:
    assert daily_brief_contract_required(
        task_title="Daily Brief",
        task_description="",
        task_expected_output=None,
        loaded_skill_names=[],
    )
    assert daily_brief_contract_required(
        task_title="Morning report",
        task_description="",
        task_expected_output=None,
        loaded_skill_names=["daily-brief"],
    )
    assert not daily_brief_contract_required(
        task_title="Generic rich report",
        task_description="",
        task_expected_output=None,
        loaded_skill_names=["Cognis Rich Deliverable"],
    )


def test_daily_brief_contract_activation_preserves_loaded_v12() -> None:
    activation = resolve_daily_brief_contract(
        task_title="Daily Brief",
        task_description="Use daily_brief_v13.",
        task_expected_output=None,
        loaded_skill_snapshots={
            "skill_daily": {
                "skill_id": "skill_daily",
                "name": "daily-brief",
                "version_id": "sv_v12",
                "version_number": 7,
                "content_hash": "hash-v12",
                "contract_version": 12,
            }
        },
    )

    assert activation is not None
    assert activation.version == 12
    assert activation.skill_version_id == "sv_v12"
    assert (
        daily_brief_contract_version(
            name="daily-brief",
            prompt_templates={"daily_brief_v13": "Current contract"},
        )
        == 13
    )


def test_generic_daily_brief_fails_deterministically() -> None:
    issues = validate_daily_brief_deliverable(
        action="write_deliverable",
        format_name="markdown",
        rich=None,
        validation_fingerprint_present=False,
        executed_tool_names=[],
    )

    assert "action must be rich:pulse" in issues
    assert "format must be rich" in issues
    assert "the exact write_deliverable payload must pass validate_tool_call first" in issues


def test_valid_daily_brief_pulse_passes() -> None:
    payload = _valid_daily_brief()
    assert (
        validate_daily_brief_deliverable(
            action="rich:pulse",
            format_name="rich",
            rich=payload,
            validation_fingerprint_present=True,
            executed_tool_names=["artifact_read"],
            materialized_artifact_evidence=_materialization_evidence(payload),
        )
        == []
    )


def test_daily_brief_rejects_missing_successful_artifact_read_evidence() -> None:
    issues = validate_daily_brief_deliverable(
        action="rich:pulse",
        format_name="rich",
        rich=_valid_daily_brief(),
        validation_fingerprint_present=True,
        executed_tool_names=[],
    )

    assert any("distinct artifact_read result" in issue for issue in issues)


def test_daily_brief_rejects_unrelated_or_reused_materialization_evidence() -> None:
    payload = _valid_full_daily_brief()
    evidence = _materialization_evidence(payload)
    first_artifact_id, first_source_url = next(iter(evidence.items()))
    articles = [
        article for accordion in payload["blocks"][4]["blocks"] for article in accordion["items"]
    ]
    for article in articles:
        article["media"]["ref"] = first_artifact_id
        article["media"]["source_url"] = first_source_url

    issues = validate_daily_brief_deliverable(
        action="rich:pulse",
        format_name="rich",
        rich=payload,
        validation_fingerprint_present=True,
        executed_tool_names=["artifact_read"] * len(articles),
        materialized_artifact_evidence=evidence,
    )

    assert any("distinct artifact_read result" in issue for issue in issues)


def test_valid_canonical_pulse_passes_daily_gate_and_rich_normalization() -> None:
    payload = _valid_full_daily_brief()

    assert (
        validate_daily_brief_deliverable(
            action="rich:pulse",
            format_name="rich",
            rich=payload,
            validation_fingerprint_present=True,
            executed_tool_names=["artifact_read", "artifact_read", "artifact_read"],
            materialized_artifact_evidence=_materialization_evidence(payload),
        )
        == []
    )
    normalized, warnings = normalize_required_rich_payload(payload)
    assert normalized["metadata"]["pulse_version"] == 2
    assert warnings == []


def test_daily_brief_rejects_lazy_media_diesel_and_image_edit() -> None:
    payload = deepcopy(_valid_daily_brief())
    payload["blocks"][0]["items"][0]["title"] = "Diesel update"
    payload["blocks"][0]["items"][0]["media"]["ref"] = "tool_artifact:call_real:media:1"

    issues = validate_daily_brief_deliverable(
        action="rich:pulse",
        format_name="rich",
        rich=payload,
        validation_fingerprint_present=True,
        executed_tool_names=["image_edit", "image_generate", "image_generate"],
    )

    assert "diesel content is forbidden" in issues
    assert "article media must use materialized artifact IDs" in issues
    assert "image_edit is forbidden" in issues
    assert "image_generate may be used at most once" in issues


def test_validation_fingerprint_changes_with_payload() -> None:
    original = {"action": "rich:pulse", "content": "A"}
    changed = {"action": "rich:pulse", "content": "B"}
    assert tool_call_fingerprint("write_deliverable", original) != tool_call_fingerprint(
        "write_deliverable", changed
    )


def test_persisted_generic_deliverable_cannot_complete_daily_brief() -> None:
    generic = Deliverable(
        deliverable_id="dlv_generic",
        version=1,
        content="fallback",
        format="markdown",
    )
    valid = Deliverable(
        deliverable_id="dlv_pulse",
        version=1,
        content="fallback",
        format="rich",
        render_metadata={
            "pulse_valid": True,
            "pulse_schema": "cognis.rich.pulse.v2",
            "pulse_variant": "daily",
            "daily_brief_contract": {
                "version": 13,
                "validated_payload_fingerprint": "sha256",
            },
            "pulse_quality": {
                "article_count": 1,
                "article_media_count": 1,
                "article_citation_count": 1,
            },
        },
    )

    assert not persisted_daily_brief_is_valid(generic)
    assert persisted_daily_brief_is_valid(valid)
    assert not persisted_daily_brief_is_valid(valid, contract_version=12)
