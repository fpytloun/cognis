from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
from jsonschema import Draft7Validator

from cognis.core.tool_exposure import (
    _normalize_anthropic_tool_schema_arguments,
    reverse_tool_argument_aliases,
)
from cognis.models.tool import tool_input_schema
from cognis.providers.llm.anthropic import (
    CompiledAnthropicToolBundle,
    compile_anthropic_tool_bundle,
)
from cognis.providers.llm.anthropic.contracts import materialize_json
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL


def _plain(value: object) -> object:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}  # type: ignore[union-attr]
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def test_recursive_exposure_aliases_compile_and_reverse_end_to_end() -> None:
    schema = {
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "child[]": {
                        "anyOf": [
                            {"$ref": "#/$defs/Node"},
                            {"type": "null"},
                        ]
                    }
                },
            }
        },
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "recursive",
                "description": "recursive",
                "parameters": schema,
                "x-stable-tool-id": "builtin:recursive",
            },
        }
    ]

    normalized_tools, aliases = _normalize_anthropic_tool_schema_arguments(
        tools,
        alias_map={},
    )
    bundle = compile_anthropic_tool_bundle(
        normalized_tools,
        argument_alias_map=aliases,
        stable_id_map={"recursive": "builtin:recursive"},
    )

    restored_bundle = CompiledAnthropicToolBundle.from_dict(bundle.to_dict())
    assert restored_bundle.fingerprint == bundle.fingerprint
    binding = restored_bundle.bindings[0]
    assert "$cognis_refs" in binding.reverse_argument_aliases
    assert reverse_tool_argument_aliases(
        {"root": {"child": {"child": None}}},
        binding.reverse_argument_aliases,
    ) == {"root": {"child[]": {"child[]": None}}}
    json.dumps(bundle.to_dict())


def _tool(
    name: str,
    stable_id: str,
    parameters: dict[str, object] | None = None,
    **metadata: object,
) -> dict[str, object]:
    function: dict[str, object] = {
        "name": name,
        "description": f"{name} description",
        "parameters": parameters
        if parameters is not None
        else {
            "type": "object",
            "properties": {"command": {"type": "string"}, "description": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        "x-stable-tool-id": stable_id,
    }
    function.update(metadata)
    return {"type": "function", "function": function}


def test_compiler_preserves_order_and_uses_exact_collision_safe_wire_names() -> None:
    tools = [
        _tool("mcp/github.search", "stable-a"),
        _tool("mcp_github_search", "stable-b"),
        _tool("CaseSensitive", "stable-c"),
        _tool("casesensitive", "stable-d"),
    ]
    bundle = compile_anthropic_tool_bundle(tools)

    assert [binding.canonical_name for binding in bundle.bindings] == [
        "mcp/github.search",
        "mcp_github_search",
        "CaseSensitive",
        "casesensitive",
    ]
    wire_names = [tool["name"] for tool in bundle.wire_tools]
    assert wire_names[0].startswith("mcp_github_search_")
    assert wire_names[1:] == ["mcp_github_search", "CaseSensitive", "casesensitive"]
    assert len(set(wire_names)) == len(wire_names)
    assert bundle.fingerprint == compile_anthropic_tool_bundle(deepcopy(tools)).fingerprint


def test_compiler_keeps_native_search_server_tool_unbound_and_client_bindings_exact() -> None:
    server = {
        "type": "tool_search_tool_bm25_20251119",
        "name": "tool_search_tool_bm25",
    }
    bundle = compile_anthropic_tool_bundle([server, _tool("weather", "weather")])

    assert [tool["name"] for tool in bundle.wire_tools] == ["weather"]
    assert [_plain(tool) for tool in bundle.server_tools] == [server]
    assert [binding.wire_name for binding in bundle.bindings] == ["weather"]
    assert (
        bundle.fingerprint
        == compile_anthropic_tool_bundle(
            [deepcopy(server), _tool("weather", "weather")]
        ).fingerprint
    )
    assert (
        bundle.fingerprint
        == compile_anthropic_tool_bundle(
            [deepcopy(server), _tool("weather", "weather")],
            server_tools=[deepcopy(server)],
        ).fingerprint
    )


def test_compiler_rejects_client_collision_with_native_search_server_name() -> None:
    with pytest.raises(ValueError, match="collide"):
        compile_anthropic_tool_bundle(
            [
                {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"},
                _tool("tool_search_tool_bm25", "custom"),
            ]
        )


def test_compiler_long_names_namespace_and_binding_preflight() -> None:
    tool = _tool("x" * 180, "stable")
    bundle = compile_anthropic_tool_bundle([tool], wire_namespace="oauth")
    assert bundle.wire_tools[0]["name"].startswith("oauth_")
    assert len(bundle.wire_tools[0]["name"]) <= 128
    with pytest.raises(ValueError, match="incomplete"):
        compile_anthropic_tool_bundle(
            [{"type": "function", "function": {"name": "visible", "parameters": {}}}]
        )
    with pytest.raises(ValueError, match="one-to-one"):
        compile_anthropic_tool_bundle([_tool("a", "same"), _tool("b", "same")])


def test_compiler_preserves_alias_trees_and_metadata() -> None:
    aliases = {
        "canonical": {
            "unsafe_key": {
                "original": "unsafe key",
                "properties": {"nested": {"original": "$nested", "properties": {}}},
            }
        }
    }
    bundle = compile_anthropic_tool_bundle(
        [
            _tool(
                "visible",
                "stable",
                cache_control={"type": "ephemeral", "ttl": "5m"},
                defer_loading=True,
                input_examples=[{"command": "pwd"}],
            )
        ],
        alias_map={"visible": "canonical"},
        argument_alias_map=aliases,
    )
    assert bundle.bindings[0].canonical_name == "canonical"
    assert bundle.bindings[0].reverse_argument_aliases == aliases["canonical"]
    assert bundle.wire_tools[0]["defer_loading"] is True
    assert "cache_control" not in bundle.wire_tools[0]
    assert _plain(bundle.wire_tools[0]["input_examples"]) == [{"command": "pwd"}]


def test_strict_projection_is_provider_only_and_bash_like_schema_is_closed() -> None:
    canonical = {
        "type": "object",
        "properties": {"command": {"type": "string"}, "description": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    }
    bundle = compile_anthropic_tool_bundle([_tool("bash", "bash", canonical)])
    assert bundle.wire_tools[0]["strict"] is True
    assert _plain(bundle.wire_tools[0]["input_schema"]) == canonical
    assert canonical["additionalProperties"] is False


def test_typeless_object_root_is_normalized_before_strict_projection_and_encodes() -> None:
    canonical = {
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    }
    bundle = compile_anthropic_tool_bundle([_tool("bash", "bash", canonical)])

    tool = bundle.wire_tools[0]
    assert tool["strict"] is True
    assert _plain(tool["input_schema"]) == {"type": "object", **canonical}
    assert "type" not in canonical
    encoded = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        json={"tools": materialize_json([tool])},
    )
    assert json.loads(encoded.content)["tools"][0]["input_schema"]["type"] == "object"


def test_empty_root_schema_is_normalized_to_an_object() -> None:
    bundle = compile_anthropic_tool_bundle([_tool("empty", "empty", {})])

    assert _plain(bundle.wire_tools[0]["input_schema"]) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert bundle.wire_tools[0]["strict"] is True


def test_typeless_dynamic_map_root_is_normalized_but_remains_non_strict() -> None:
    canonical = {"additionalProperties": {"type": "string"}}
    bundle = compile_anthropic_tool_bundle([_tool("labels", "labels", canonical)])

    assert _plain(bundle.wire_tools[0]["input_schema"]) == {"type": "object", **canonical}
    assert "strict" not in bundle.wire_tools[0]
    assert "dynamic map" in bundle.strict_diagnostics[0]


def test_typeless_nested_schema_preserves_reverse_aliases_and_strict_diagnostics() -> None:
    aliases = {
        "canonical": {
            "safe_key": {
                "original": "unsafe key",
                "properties": {"nested_key": {"original": "$nested", "properties": {}}},
            }
        }
    }
    schema = {
        "properties": {
            "safe_key": {
                "properties": {"nested_key": {"type": "string"}},
            }
        },
        "additionalProperties": False,
    }
    bundle = compile_anthropic_tool_bundle(
        [_tool("visible", "stable", schema)],
        alias_map={"visible": "canonical"},
        argument_alias_map=aliases,
    )

    assert bundle.bindings[0].canonical_name == "canonical"
    assert bundle.bindings[0].reverse_argument_aliases == aliases["canonical"]
    assert _plain(bundle.wire_tools[0]["input_schema"]) == {"type": "object", **schema}
    assert "strict" not in bundle.wire_tools[0]
    assert "missing an explicit type" in bundle.strict_diagnostics[0]


def test_excessively_nested_schema_is_omitted_without_failing_bundle() -> None:
    deep_schema: dict[str, object] = {}
    node = deep_schema
    for _ in range(140):
        child: dict[str, object] = {"properties": {}}
        node["properties"] = {"child": child}
        node = child

    bundle = compile_anthropic_tool_bundle(
        [
            _tool("deep", "deep", deep_schema),
            _tool("healthy", "healthy"),
        ]
    )

    assert [tool["name"] for tool in bundle.wire_tools] == ["healthy"]
    assert [binding.canonical_name for binding in bundle.bindings] == ["healthy"]
    assert any(
        "deep: omitted:" in diagnostic and "maximum depth" in diagnostic
        for diagnostic in bundle.strict_diagnostics
    )


def test_cyclic_schema_is_omitted_without_recursion_error() -> None:
    cyclic: dict[str, object] = {"properties": {}}
    cyclic["properties"] = {"self": cyclic}

    bundle = compile_anthropic_tool_bundle(
        [
            _tool("cyclic", "cyclic", cyclic),
            _tool("healthy", "healthy"),
        ]
    )

    assert [tool["name"] for tool in bundle.wire_tools] == ["healthy"]
    assert [binding.canonical_name for binding in bundle.bindings] == ["healthy"]
    assert any(
        "cyclic: omitted:" in diagnostic and "contains a cycle" in diagnostic
        for diagnostic in bundle.strict_diagnostics
    )


@pytest.mark.parametrize("metadata_name", ["input_examples", "cache_control"])
def test_cyclic_tool_metadata_omits_only_the_affected_tool(metadata_name: str) -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    metadata: dict[str, object]
    if metadata_name == "input_examples":
        metadata = {"input_examples": [cyclic]}
    else:
        cyclic["type"] = "ephemeral"
        metadata = {"cache_control": cyclic}

    bundle = compile_anthropic_tool_bundle(
        [
            _tool("cyclic_metadata", "cyclic_metadata", **metadata),
            _tool("healthy", "healthy"),
        ]
    )

    assert [tool["name"] for tool in bundle.wire_tools] == ["healthy"]
    assert [binding.canonical_name for binding in bundle.bindings] == ["healthy"]
    assert any(
        "cyclic_metadata: omitted:" in diagnostic and metadata_name in diagnostic
        for diagnostic in bundle.strict_diagnostics
    )


def test_cyclic_reverse_aliases_omit_only_the_affected_tool() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    bundle = compile_anthropic_tool_bundle(
        [
            _tool("aliased", "aliased"),
            _tool("healthy", "healthy"),
        ],
        argument_alias_map={"aliased": cyclic},
    )

    assert [tool["name"] for tool in bundle.wire_tools] == ["healthy"]
    assert [binding.canonical_name for binding in bundle.bindings] == ["healthy"]
    assert any(
        "aliased: omitted:" in diagnostic and "reverse argument aliases" in diagnostic
        for diagnostic in bundle.strict_diagnostics
    )


def test_shared_acyclic_schema_subtree_is_not_misclassified_as_cycle() -> None:
    shared = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {"left": shared, "right": shared},
        "additionalProperties": False,
    }

    bundle = compile_anthropic_tool_bundle([_tool("shared", "shared", schema)])

    assert [tool["name"] for tool in bundle.wire_tools] == ["shared"]
    assert bundle.wire_tools[0]["strict"] is True


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ({"type": "string"}, "object root"),
        ({"type": "array", "items": {"type": "string"}}, "object root"),
        ({"type": ["object", "null"]}, "must be the string"),
        ({"type": None, "properties": {}}, "must be the string"),
        ({"anyOf": [{"type": "object"}, {"type": "string"}]}, "ambiguous root"),
        ({"anyOf": []}, "ambiguous root"),
        ({"oneOf": {"type": "object"}}, "ambiguous root"),
        ({"oneOf": [True]}, "ambiguous root"),
        (
            {"oneOf": [{"anyOf": [{"type": "object"}]}]},
            "ambiguous root",
        ),
        (
            {
                "oneOf": [
                    {"type": "object", "$ref": "#/definitions/string"},
                    {"type": "object"},
                ]
            },
            "ambiguous root",
        ),
        ({"$ref": "#/$defs/root"}, "ambiguous root"),
        ({"type": "object", "$ref": "#/$defs/root"}, "ambiguous root"),
        ({"type": "object", "$dynamicRef": "#root"}, "ambiguous root"),
        ({"properties": {}, "items": {"type": "string"}}, "contradictory"),
    ],
)
def test_invalid_or_ambiguous_root_schema_fails_tool_specific_preflight(
    schema: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValueError, match=rf"Anthropic tool 'invalid'.*{reason}"):
        compile_anthropic_tool_bundle([_tool("invalid", "invalid", schema)])


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf"])
def test_object_union_root_is_normalized_for_anthropic(keyword: str) -> None:
    bundle = compile_anthropic_tool_bundle(
        [
            _tool(
                "object_union",
                "object_union",
                {
                    keyword: [
                        {
                            "type": "object",
                            "properties": {"first": {"type": "string"}},
                            "required": ["first"],
                        },
                        {
                            "properties": {"second": {"type": "integer"}},
                            "required": ["second"],
                        },
                    ]
                },
            )
        ]
    )

    schema = bundle.wire_tools[0]["input_schema"]
    assert schema["type"] == "object"
    assert keyword not in schema
    assert "required" not in schema
    assert schema["properties"] == {"first": {}, "second": {}}
    assert "strict" not in bundle.wire_tools[0]
    assert f"top-level {keyword} lowered" in bundle.strict_diagnostics[0]


def test_explicit_object_root_lowers_composition_keywords() -> None:
    parameters = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {"first": {"type": "string"}},
            }
        ],
    }

    bundle = compile_anthropic_tool_bundle(
        [_tool("explicit_object", "explicit_object", parameters)]
    )

    assert _plain(bundle.wire_tools[0]["input_schema"]) == {
        "type": "object",
        "properties": {"first": {"type": "string"}},
    }
    assert "strict" not in bundle.wire_tools[0]
    assert "top-level oneOf lowered" in bundle.strict_diagnostics[0]


def test_all_of_object_root_is_lowered_with_required_union() -> None:
    bundle = compile_anthropic_tool_bundle(
        [
            _tool(
                "all_of_object",
                "all_of_object",
                {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {"first": {"type": "string"}},
                            "required": ["first"],
                        },
                        {
                            "type": "object",
                            "properties": {"second": {"type": "integer"}},
                            "required": ["second"],
                        },
                    ]
                },
            )
        ]
    )

    schema = bundle.wire_tools[0]["input_schema"]
    assert "allOf" not in schema
    assert schema["type"] == "object"
    assert schema["required"] == ("first", "second")
    assert schema["properties"] == {
        "first": {"type": "string"},
        "second": {"type": "integer"},
    }
    assert "top-level allOf lowered" in bundle.strict_diagnostics[0]


def test_union_lowering_does_not_constrain_properties_missing_from_a_branch() -> None:
    canonical = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"kind": {"const": "a"}},
                "required": ["kind"],
            },
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ]
    }

    bundle = compile_anthropic_tool_bundle([_tool("broad_union", "broad_union", canonical)])
    lowered = _plain(bundle.wire_tools[0]["input_schema"])

    assert isinstance(lowered, dict)
    assert lowered["properties"] == {"kind": {}, "value": {}}
    valid_for_second_branch = {"kind": "b", "value": "ok"}
    Draft7Validator(canonical).validate(valid_for_second_branch)
    Draft7Validator(lowered).validate(valid_for_second_branch)


def test_union_variant_deduplication_preserves_json_boolean_and_number() -> None:
    bundle = compile_anthropic_tool_bundle(
        [
            _tool(
                "json_types",
                "json_types",
                {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"value": {"const": True}},
                        },
                        {
                            "type": "object",
                            "properties": {"value": {"const": 1}},
                        },
                    ]
                },
            )
        ]
    )

    variants = bundle.wire_tools[0]["input_schema"]["properties"]["value"]["anyOf"]
    assert len(variants) == 2
    assert _plain(variants) == [{"const": True}, {"const": 1}]


def test_composition_lowering_rejects_malformed_required() -> None:
    with pytest.raises(ValueError, match="required must be an array of strings"):
        compile_anthropic_tool_bundle(
            [
                _tool(
                    "malformed_required",
                    "malformed_required",
                    {
                        "oneOf": [
                            {"type": "object", "required": None},
                            {"type": "object"},
                        ]
                    },
                )
            ]
        )


def test_explicit_object_root_preserves_conditionals() -> None:
    parameters = {
        "type": "object",
        "properties": {"kind": {"type": "string"}},
        "if": {"properties": {"kind": {"const": "special"}}},
        "then": {"required": ["kind"]},
    }

    bundle = compile_anthropic_tool_bundle(
        [_tool("conditional_object", "conditional_object", parameters)]
    )

    assert _plain(bundle.wire_tools[0]["input_schema"]) == parameters
    assert "strict" not in bundle.wire_tools[0]


def test_write_deliverable_object_union_compiles_for_anthropic() -> None:
    schema = tool_input_schema(WRITE_DELIVERABLE_TOOL)

    bundle = compile_anthropic_tool_bundle(
        [_tool("write_deliverable", "builtin:write_deliverable", schema)]
    )

    assert bundle.wire_tools[0]["input_schema"]["type"] == "object"
    assert not ({"allOf", "anyOf", "oneOf"} & bundle.wire_tools[0]["input_schema"].keys())
    assert bundle.wire_tools[0]["input_schema"]["required"] == ("action", "content")
    assert set(bundle.wire_tools[0]["input_schema"]["properties"]) == {
        "action",
        "content",
        "format",
        "outputs",
        "rich",
        "target",
        "title",
    }
    assert "strict" not in bundle.wire_tools[0]
    assert "top-level oneOf lowered" in bundle.strict_diagnostics[0]
    lowered_schema = _plain(bundle.wire_tools[0]["input_schema"])
    assert isinstance(lowered_schema, dict)
    Draft7Validator(lowered_schema).validate({"action": "write_deliverable", "content": "fallback"})
    Draft7Validator(lowered_schema).validate(
        {
            "action": "rich:pulse",
            "content": "fallback",
            "format": "rich",
            "rich": {
                "blocks": [{"type": "markdown", "content": "section"}] * 7,
                "metadata": {"presentation": "pulse", "pulse_version": 2},
            },
        }
    )
    assert list(Draft7Validator(schema).iter_errors({"action": "rich:pulse", "content": "x"}))
    assert not list(
        Draft7Validator(lowered_schema).iter_errors({"action": "rich:pulse", "content": "x"})
    )


def test_large_mixed_deferred_inventory_normalizes_only_custom_tools() -> None:
    server_tools = [
        {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"},
        {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"},
    ]
    custom_tools = [
        _tool(
            f"tool_{index}",
            f"stable_{index}",
            (
                {"properties": {"value": {"type": "string"}}, "additionalProperties": False}
                if index % 3 == 0
                else {}
                if index % 3 == 1
                else {"additionalProperties": {"type": "string"}}
            ),
            defer_loading=index % 2 == 0,
        )
        for index in range(96)
    ]
    bundle = compile_anthropic_tool_bundle([*server_tools, *custom_tools])

    assert len(bundle.wire_tools) == len(custom_tools)
    assert len(bundle.server_tools) == len(server_tools)
    assert all(tool["input_schema"]["type"] == "object" for tool in bundle.wire_tools)
    assert [_plain(tool) for tool in bundle.server_tools] == server_tools


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ({"type": "object", "additionalProperties": {"type": "string"}}, "dynamic map"),
        (
            {
                "type": "object",
                "properties": {"x": {"$ref": "#/$defs/x"}},
                "additionalProperties": False,
            },
            "unsupported $ref",
        ),
        (
            {
                "type": "object",
                "properties": {"x": {"type": "string", "pattern": "x"}},
                "additionalProperties": False,
            },
            "unsupported pattern",
        ),
        (
            {
                "type": "object",
                "properties": {"x": {"anyOf": [{"type": "string"}]}},
                "additionalProperties": False,
            },
            "unsupported anyOf",
        ),
    ],
)
def test_ineligible_strict_schemas_remain_canonical_and_emit_diagnostics(
    schema: dict[str, object], reason: str
) -> None:
    original = deepcopy(schema)
    bundle = compile_anthropic_tool_bundle([_tool("tool", "stable", schema)])
    assert "strict" not in bundle.wire_tools[0]
    assert _plain(bundle.wire_tools[0]["input_schema"]) == original
    assert reason in bundle.strict_diagnostics[0]


def test_strict_rejects_typeless_and_nullable_nested_schemas() -> None:
    typeless = {
        "type": "object",
        "properties": {"nested": {"properties": {"x": {"type": "string"}}}},
        "additionalProperties": False,
    }
    nullable = {
        "type": "object",
        "properties": {
            "nested": {
                "type": ["object", "null"],
                "properties": {"x": {"pattern": "unsafe"}},
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    }
    assert (
        "missing an explicit type"
        in compile_anthropic_tool_bundle(
            [_tool("typeless", "typeless", typeless)]
        ).strict_diagnostics[0]
    )
    assert (
        "unsupported pattern"
        in compile_anthropic_tool_bundle(
            [_tool("nullable", "nullable", nullable)]
        ).strict_diagnostics[0]
    )


def test_nested_reverse_aliases_must_be_bijective() -> None:
    aliases = {
        "canonical": {
            "wire_a": {"original": "same", "properties": {}},
            "wire_b": {"original": "same", "properties": {}},
        }
    }
    with pytest.raises(ValueError, match="bijective"):
        compile_anthropic_tool_bundle(
            [_tool("visible", "stable")],
            alias_map={"visible": "canonical"},
            argument_alias_map=aliases,
        )


def test_strict_budgets_are_deterministic() -> None:
    tools = [_tool(f"tool_{index}", f"stable_{index}") for index in range(21)]
    bundle = compile_anthropic_tool_bundle(tools)
    assert sum(tool.get("strict") is True for tool in bundle.wire_tools) == 20
    assert "tool budget exhausted" in bundle.strict_diagnostics[0]

    optional = {
        "type": "object",
        "properties": {f"option_{index}": {"type": "string"} for index in range(25)},
        "additionalProperties": False,
    }
    bundle = compile_anthropic_tool_bundle([_tool("optional", "optional", optional)])
    assert "optional parameter budget exhausted" in bundle.strict_diagnostics[0]
