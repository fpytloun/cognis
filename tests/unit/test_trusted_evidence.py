from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from cognis.config import load_config
from cognis.core.remember_queue import RememberRetryQueue
from cognis.core.trusted_evidence import (
    EVIDENCE_OUTCOMES_TOTAL,
    EVIDENCE_QUEUE_KIND,
    ORDINARY_USER_QUEUE_KIND,
    EvidenceOrigin,
    authenticated_direct_user_origin,
    build_evidence_admission,
    build_evidence_event_binding,
    build_marker,
    canonical_last_assistant_event,
    classify_origin,
    deserialize_evidence_admission,
    deserialize_evidence_origin,
    deterministic_queue_id,
    event_hash,
    evidence_admission_authorizes,
    is_eligible_user_event,
    is_evidence_enabled_for_owner,
    marker_is_valid,
    marker_matches_event,
    observe_queue_age,
    serialize_evidence_admission,
    serialize_evidence_origin,
    sha256_hex,
    trusted_evidence_policy_fingerprint,
)
from cognis.models.session import SessionEvent

ADMISSION_KEY = b"trusted-evidence-unit-test-key"


def _event_binding(
    *,
    content: str = "Příliš žluťoučký",
    owner_id: str = "owner@example.com",
    turn_id: str = "turn-1",
) -> dict[str, object]:
    return build_evidence_event_binding(
        intaris_session_id="intaris-1",
        cognis_session_id="session-1",
        conversation_id="conversation-1",
        turn_id=turn_id,
        user_id="user@example.com",
        owner_id=owner_id,
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        attachment_refs_value=[{"artifact_id": "artifact-1", "mime_type": "text/plain"}],
    )


def test_eligibility_requires_direct_user_visible_input() -> None:
    base = {
        "content": "hello",
        "user_visible_content": "hello",
        "source": "user_input",
        "role": "user",
        "prompt_visibility": "user_visible",
        "prompt_provenance": {"kind": "user_authored"},
        "system_initiated": False,
        "is_retry": False,
        "internal_workflow_prompt": False,
        "delegated": False,
        "profile_switch_reentry": False,
    }
    assert (
        is_eligible_user_event(
            **base,
            origin=EvidenceOrigin(
                authenticated=True,
                source="user_input",
                role="user",
                prompt_visibility="user_visible",
                prompt_provenance="user_authored",
                memory_eligible=True,
            ),
        )
        is True
    )
    for field in (
        "system_initiated",
        "is_retry",
        "internal_workflow_prompt",
        "delegated",
        "profile_switch_reentry",
    ):
        candidate = dict(base)
        candidate[field] = True
        assert is_eligible_user_event(**candidate) is False
    for field, value in (
        ("source", "tool_output"),
        ("role", "assistant"),
        ("prompt_visibility", "model_only"),
        ("user_visible_content", ""),
        ("content", " "),
    ):
        candidate = dict(base)
        candidate[field] = value
        assert is_eligible_user_event(**candidate) is False


def test_evidence_origin_durable_encoding_is_strict_and_round_trips() -> None:
    origin = authenticated_direct_user_origin()
    encoded = serialize_evidence_origin(origin)

    assert deserialize_evidence_origin(encoded) == origin
    assert deserialize_evidence_origin({**encoded, "untrusted": True}) is None
    assert (
        deserialize_evidence_origin(
            {key: value for key, value in encoded.items() if key != "source"}
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authenticated", "true"),
        ("authenticated", 1),
        ("memory_eligible", "true"),
        ("system_initiated", 0),
        ("source", "USER_INPUT"),
        ("role", "USER"),
        ("prompt_visibility", "USER_VISIBLE"),
        ("prompt_provenance", "USER_AUTHORED"),
    ],
)
def test_evidence_origin_recovery_rejects_coercion_and_non_wire_enums(
    field: str, value: object
) -> None:
    encoded = serialize_evidence_origin(authenticated_direct_user_origin())
    encoded[field] = value

    assert deserialize_evidence_origin(encoded) is None


def test_trusted_evidence_policy_fingerprint_is_keyed_and_allowlist_sensitive() -> None:
    common = {
        "key": b"shared-cognis-secrets-key",
        "enabled": True,
        "max_attempts": 8,
        "max_age_seconds": 3600,
    }
    first = trusted_evidence_policy_fingerprint(
        **common,
        owner_allowlist=("owner@example.com", "admin@example.com"),
    )
    normalized = trusted_evidence_policy_fingerprint(
        **common,
        owner_allowlist=(" ADMIN@example.com ", "owner@example.com"),
    )
    changed = trusted_evidence_policy_fingerprint(
        **common,
        owner_allowlist=("owner@example.com", "other-owner@example.com"),
    )
    different_key = trusted_evidence_policy_fingerprint(
        **{**common, "key": b"another-shared-cognis-key"},
        owner_allowlist=("owner@example.com", "admin@example.com"),
    )

    assert first == normalized
    assert first != changed
    assert first != different_key
    assert "owner@example.com" not in first


@pytest.mark.parametrize(
    "changes",
    [
        {"authenticated": False},
        {"memory_eligible": False},
        {"private_controller_instruction": True},
        {"source": "controller_instruction"},
        {"prompt_visibility": "model_only"},
        {"prompt_provenance": "internal_workflow"},
        {"system_initiated": True},
        {"retry": True},
        {"delegated": True},
        {"workflow": True},
        {"managed": True},
        {"forked": True},
        {"channel_controller": True},
    ],
)
def test_origin_admission_matrix_never_marks_non_direct_input(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "authenticated": True,
        "source": "user_input",
        "role": "user",
        "prompt_visibility": "user_visible",
        "prompt_provenance": "user_authored",
        "memory_eligible": True,
        "system_initiated": False,
        "retry": False,
        "delegated": False,
        "workflow": False,
        "managed": False,
        "forked": False,
        "channel_controller": False,
    }
    values.update(changes)
    origin = EvidenceOrigin(
        authenticated=bool(values["authenticated"]),
        source=str(values["source"]),
        role="user",
        prompt_visibility=str(values["prompt_visibility"]),
        prompt_provenance=str(values["prompt_provenance"]),
        memory_eligible=bool(values["memory_eligible"]),
        private_controller_instruction=bool(values.get("private_controller_instruction", False)),
        system_initiated=bool(values["system_initiated"]),
        retry=bool(values["retry"]),
        delegated=bool(values["delegated"]),
        workflow=bool(values["workflow"]),
        managed=bool(values["managed"]),
        forked=bool(values["forked"]),
        channel_controller=bool(values["channel_controller"]),
    )
    assert (
        is_eligible_user_event(
            content="hello",
            user_visible_content="hello",
            source=origin.source,
            role=origin.role,
            prompt_visibility=origin.prompt_visibility,
            prompt_provenance={"kind": origin.prompt_provenance},
            system_initiated=origin.system_initiated,
            is_retry=origin.retry,
            internal_workflow_prompt=origin.workflow,
            delegated=origin.delegated,
            profile_switch_reentry=False,
            origin=origin,
        )
        is False
    )


def test_origin_classification_preserves_controller_metadata() -> None:
    origin = classify_origin(
        {
            "source": "controller_instruction",
            "private_controller_instruction": True,
            "memory_eligible": False,
        },
        system_initiated=False,
        is_retry=False,
        delegated=False,
        workflow=False,
        managed=True,
        forked=False,
        channel_controller=True,
    )
    assert origin.authenticated is False
    assert origin.memory_eligible is False
    assert origin.private_controller_instruction is True


def test_marker_is_versioned_and_does_not_store_content() -> None:
    admission = build_evidence_admission(
        key=ADMISSION_KEY,
        admitted=True,
        owner_id="owner@example.com",
        policy_fingerprint="0123456789abcdef",
        event_binding=_event_binding(),
    )
    marker = build_marker(
        content="Příliš žluťoučký",
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        user_id="user@example.com",
        owner_id="owner@example.com",
        intaris_session_id="intaris-1",
        cognis_session_id="session-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        attachment_refs_value=[{"artifact_id": "artifact-1", "mime_type": "text/plain"}],
        admission=admission,
        admission_key=ADMISSION_KEY,
    )
    assert marker_is_valid(marker) is True
    assert marker_is_valid(marker, admission_key=ADMISSION_KEY) is True
    forged_marker = {
        **marker,
        "trusted_evidence_admission": {
            **marker["trusted_evidence_admission"],
            "owner_id": "other@example.com",
        },
    }
    assert marker_is_valid(forged_marker, admission_key=ADMISSION_KEY) is False
    assert "content" not in marker
    assert marker["content_hash"]
    assert marker["marker_hash"]
    assert marker["trusted_evidence_admission"] == serialize_evidence_admission(admission)


def test_admission_mac_allows_exact_replay_but_rejects_event_substitution() -> None:
    admission = build_evidence_admission(
        key=ADMISSION_KEY,
        admitted=True,
        owner_id="owner@example.com",
        policy_fingerprint="0123456789abcdef",
        event_binding=_event_binding(content="bound content"),
    )
    marker = build_marker(
        content="bound content",
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        user_id="user@example.com",
        owner_id="owner@example.com",
        intaris_session_id="intaris-1",
        cognis_session_id="session-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        attachment_refs_value=[{"artifact_id": "artifact-1", "mime_type": "text/plain"}],
        admission=admission,
        admission_key=ADMISSION_KEY,
    )
    exact_event = {
        "source": "user_input",
        "role": "user",
        "prompt_visibility": "user_visible",
        "prompt_provenance": {"kind": "user_authored"},
        "user_visible_content": "bound content",
        "attachments": [{"artifact_id": "artifact-1", "mime_type": "text/plain"}],
        "turn_id": "turn-1",
    }

    assert marker_matches_event(
        marker,
        admission_key=ADMISSION_KEY,
        intaris_session_id="intaris-1",
        event_data=exact_event,
    )
    assert not marker_matches_event(
        marker,
        admission_key=ADMISSION_KEY,
        intaris_session_id="intaris-1",
        event_data={**exact_event, "turn_id": "turn-2"},
    )
    assert not marker_matches_event(
        marker,
        admission_key=ADMISSION_KEY,
        intaris_session_id="intaris-1",
        event_data={**exact_event, "user_visible_content": "substituted content"},
    )
    for substitution in (
        {
            "turn_id": "turn-2",
            "content_hash": hashlib.sha256(b"substituted content").hexdigest(),
            "content": "substituted content",
        },
        {"owner_id": "other@example.com", "content": "bound content"},
    ):
        copied_marker = {
            **marker,
            **{key: value for key, value in substitution.items() if key != "content"},
        }
        copied_without_hash = {
            key: value for key, value in copied_marker.items() if key != "marker_hash"
        }
        copied_marker["marker_hash"] = sha256_hex(
            {**copied_without_hash, "content": substitution["content"]}
        )
        assert marker_is_valid(copied_marker, admission_key=ADMISSION_KEY) is False
    for admitted_at in (None, "2099-01-01T00:00:00+00:00"):
        changed_marker = {**marker}
        if admitted_at is None:
            changed_marker.pop("admitted_at")
        else:
            changed_marker["admitted_at"] = admitted_at
        changed_without_hash = {
            key: value for key, value in changed_marker.items() if key != "marker_hash"
        }
        changed_marker["marker_hash"] = sha256_hex(
            {**changed_without_hash, "content": "bound content"}
        )
        assert marker_is_valid(changed_marker, admission_key=ADMISSION_KEY) is False
    with pytest.raises(ValueError, match="positive frozen admission"):
        build_marker(
            content="bound content",
            source="user_input",
            role="user",
            prompt_visibility="user_visible",
            prompt_provenance={"kind": "user_authored"},
            user_id="user@example.com",
            owner_id="other@example.com",
            intaris_session_id="intaris-1",
            cognis_session_id="session-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            attachment_refs_value=[{"artifact_id": "artifact-1", "mime_type": "text/plain"}],
            admission=admission,
            admission_key=ADMISSION_KEY,
        )


def test_evidence_admission_is_strict_and_fail_closed() -> None:
    admitted = build_evidence_admission(
        key=ADMISSION_KEY,
        admitted=True,
        owner_id=" Owner@Example.com ",
        policy_fingerprint="0123456789abcdef",
        event_binding=_event_binding(),
    )
    encoded = serialize_evidence_admission(admitted)

    assert admitted.owner_id == "owner@example.com"
    assert deserialize_evidence_admission(encoded) == admitted
    assert deserialize_evidence_admission({**encoded, "admitted": "true"}) is None
    assert deserialize_evidence_admission({**encoded, "version": 2}) is None
    assert deserialize_evidence_admission({**encoded, "owner_id": "other@example.com"}) != admitted
    assert deserialize_evidence_admission({**encoded, "extra": True}) is None
    assert evidence_admission_authorizes(
        admitted,
        key=ADMISSION_KEY,
        owner_id="owner@example.com",
    )
    assert not evidence_admission_authorizes(
        {**encoded, "owner_id": "other@example.com"},
        key=ADMISSION_KEY,
        owner_id="other@example.com",
    )
    assert not evidence_admission_authorizes(
        admitted,
        key=b"another-key",
        owner_id="owner@example.com",
    )


def test_negative_evidence_admission_cannot_select_an_owner() -> None:
    negative = build_evidence_admission(
        key=ADMISSION_KEY,
        admitted=False,
        owner_id="owner@example.com",
        policy_fingerprint="",
        event_binding=_event_binding(),
    )

    assert negative.owner_id is None
    assert deserialize_evidence_admission(serialize_evidence_admission(negative)) == negative


def test_event_hash_and_queue_ids_are_deterministic() -> None:
    event = SessionEvent(
        type="user_message",
        data={"role": "user", "content": "hello", "trusted_evidence": {"version": 1}},
    )
    first = event_hash("intaris-1", 7, event)
    second = event_hash("intaris-1", 7, event.model_dump(mode="json"))
    assert first == second
    assert deterministic_queue_id(EVIDENCE_QUEUE_KIND, first) == deterministic_queue_id(
        EVIDENCE_QUEUE_KIND, first
    )
    assert deterministic_queue_id(EVIDENCE_QUEUE_KIND, first) != deterministic_queue_id(
        ORDINARY_USER_QUEUE_KIND, first
    )
    assert deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, first, "assistant-a") != (
        deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, first, "assistant-b")
    )


def test_canonical_last_assistant_event_matches_live_identity() -> None:
    events = [
        {"type": "user_message", "seq": 100, "data": {"turn_id": "turn-1"}},
        {"type": "assistant_message", "seq": 101, "data": {"turn_id": "turn-1"}},
        {"type": "assistant_message", "seq": 102, "data": {"turn_id": "turn-1"}},
    ]
    selected = canonical_last_assistant_event(events, "turn-1")
    assert isinstance(selected, dict)
    assert selected["seq"] == 102
    assert deterministic_queue_id(
        "ordinary_assistant",
        "user-hash",
        event_hash("intaris-1", selected["seq"], selected),
    ) == deterministic_queue_id(
        "ordinary_assistant",
        "user-hash",
        event_hash("intaris-1", 102, events[2]),
    )


def test_queue_durable_payload_drops_content_messages() -> None:
    payload = {
        "item_id": "rq_evidence-1",
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "messages": [{"role": "user", "content": "secret content"}],
    }
    assert "messages" not in RememberRetryQueue._durable_payload(payload)


def test_queue_age_is_nonnegative() -> None:
    now = datetime.now(UTC)
    age = observe_queue_age(now - timedelta(seconds=3), now=now)
    assert 2.9 <= age <= 3.1


def test_evidence_config_is_disabled_and_bounded_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COGNIS_TRUSTED_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("COGNIS_TRUSTED_EVIDENCE_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("COGNIS_TRUSTED_EVIDENCE_MAX_AGE_SECONDS", raising=False)
    config = load_config()
    assert config.trusted_evidence_enabled is False
    assert config.trusted_evidence_owner_allowlist == ()
    assert config.trusted_evidence_max_attempts == 8
    assert config.trusted_evidence_max_age_seconds == 3600


def test_evidence_config_normalizes_owner_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COGNIS_TRUSTED_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv(
        "COGNIS_TRUSTED_EVIDENCE_OWNER_ALLOWLIST",
        " Owner@Example.COM, SECOND@Example.com ",
    )
    config = load_config()
    assert config.trusted_evidence_owner_allowlist == (
        "owner@example.com",
        "second@example.com",
    )


def test_evidence_config_rejects_empty_owner_allowlist_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "COGNIS_TRUSTED_EVIDENCE_OWNER_ALLOWLIST", "owner@example.com, ,second@example.com"
    )
    with pytest.raises(ValueError, match="COGNIS_TRUSTED_EVIDENCE_OWNER_ALLOWLIST"):
        load_config()


def test_evidence_owner_selection_uses_owner_not_user() -> None:
    assert is_evidence_enabled_for_owner(
        enabled=True,
        owner_id="Owner@Example.com",
        owner_allowlist=("owner@example.com",),
    )
    assert not is_evidence_enabled_for_owner(
        enabled=True,
        owner_id="other@example.com",
        owner_allowlist=("owner@example.com",),
    )
    assert not is_evidence_enabled_for_owner(
        enabled=True,
        owner_id=None,
        owner_allowlist=("owner@example.com",),
    )
    assert not is_evidence_enabled_for_owner(
        enabled=False,
        owner_id="owner@example.com",
        owner_allowlist=("owner@example.com",),
    )


def test_evidence_metrics_have_no_identity_labels() -> None:
    assert EVIDENCE_OUTCOMES_TOTAL._labelnames == ("outcome",)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COGNIS_TRUSTED_EVIDENCE_MAX_ATTEMPTS", "0"),
        ("COGNIS_TRUSTED_EVIDENCE_MAX_AGE_SECONDS", "1"),
    ],
)
def test_evidence_config_rejects_unsafe_bounds(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.delenv("COGNIS_TRUSTED_EVIDENCE_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("COGNIS_TRUSTED_EVIDENCE_MAX_AGE_SECONDS", raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_config()
