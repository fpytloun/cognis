from __future__ import annotations

import json

from cognis.testing.mock_llm.policy import PTC_POLICY_MODEL, build_ptc_policy_assessment

ASSESSMENT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "policy_assessment",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "mutation": {"enum": ["read_only", "mutating", "unclear"]},
                "relationship": {
                    "enum": [
                        "directly_serves_intent",
                        "supports_intent",
                        "unrelated",
                        "conflicts_with_intent",
                        "unclear",
                    ]
                },
                "risk": {"enum": ["low", "medium", "high", "critical", "unknown"]},
                "authorization": {"enum": ["explicit", "implicit", "absent", "unclear"]},
                "operation": {"enum": ["read", "write", "unknown"]},
                "environment": {"enum": ["development", "production", "unknown"]},
                "reversibility": {"enum": ["reversible", "irreversible", "unknown"]},
                "side_effect_scope": {"enum": ["bounded", "unbounded", "unknown"]},
                "authorization_source": {
                    "enum": ["latest_user_message", "session_intention", "none"]
                },
                "authorization_scope": {"enum": ["within_scope", "outside_scope", "unclear"]},
                "effects": {
                    "type": "array",
                    "items": {
                        "enum": ["read", "write", "execute", "transmit", "delete", "unknown"]
                    },
                },
            },
            "required": [
                "mutation",
                "relationship",
                "risk",
                "authorization",
                "operation",
                "environment",
                "reversibility",
                "side_effect_scope",
                "authorization_source",
                "authorization_scope",
                "effects",
            ],
            "additionalProperties": False,
        },
    },
}


def test_named_ptc_policy_model_classifies_the_read_and_write_fixture_cells() -> None:
    messages = [
        {
            "role": "user",
            "content": (
                '{"tool":"python_execute","arguments":'
                '{"description":"PTC-QUALIFY-01: local state and nested lookup"}}'
            ),
        }
    ]

    content, trace = build_ptc_policy_assessment(messages, ASSESSMENT_SCHEMA)
    assessment = json.loads(content)

    assert trace["test_model"] == PTC_POLICY_MODEL
    assert trace["qualified_action"] is True
    assert assessment["relationship"] == "directly_serves_intent"
    assert assessment["risk"] == "low"
    assert assessment["authorization"] == "explicit"
    assert assessment["authorization_scope"] == "within_scope"
    assert assessment["effects"] == ["execute", "read", "write"]


def test_named_ptc_policy_model_qualifies_only_the_scoped_mutation_marker() -> None:
    content, trace = build_ptc_policy_assessment(
        [
            {
                "role": "user",
                "content": (
                    '{"tool":"python_execute","arguments":'
                    '{"description":"PTC-MUTATION-01: create one test artifact"}}'
                ),
            }
        ],
        ASSESSMENT_SCHEMA,
    )
    assessment = json.loads(content)

    assert trace["qualified_action"] is True
    assert assessment["mutation"] == "mutating"
    assert assessment["operation"] == "write"
    assert assessment["authorization"] == "explicit"
    assert assessment["environment"] == "development"


def test_ptc_policy_model_returns_unknown_for_unrecognized_actions() -> None:
    content, trace = build_ptc_policy_assessment(
        [
            {
                "role": "user",
                "content": '{"tool":"python_execute","arguments":{"code":"open(\\"/tmp/x\\",\\"w\\")"}}',
            }
        ],
        ASSESSMENT_SCHEMA,
    )
    assessment = json.loads(content)

    assert trace["qualified_action"] is False
    assert assessment["relationship"] == "unclear"
    assert assessment["risk"] == "unknown"
    assert assessment["authorization"] == "unclear"
    assert assessment["authorization_scope"] == "unclear"
    assert assessment["effects"] == ["unknown"]
