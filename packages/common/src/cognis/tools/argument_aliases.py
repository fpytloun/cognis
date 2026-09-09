"""Provider argument-alias reversal shared by executor inference transports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_ARGUMENT_ALIAS_ANY_PROPERTY = "*"
_ARGUMENT_ALIAS_REF = "$cognis_ref"
_ARGUMENT_ALIAS_REF_DEFINITIONS = "$cognis_refs"


def reverse_tool_argument_aliases(
    arguments: Any,
    alias_tree: Mapping[str, Any],
    *,
    _ref_alias_trees: Mapping[str, Mapping[str, Any]] | None = None,
) -> Any:
    """Translate provider-facing aliased argument keys back to canonical names."""

    if _ref_alias_trees is None:
        stored_ref_alias_trees = alias_tree.get(_ARGUMENT_ALIAS_REF_DEFINITIONS)
        _ref_alias_trees = (
            stored_ref_alias_trees if isinstance(stored_ref_alias_trees, Mapping) else {}
        )
    ref_name = alias_tree.get(_ARGUMENT_ALIAS_REF)
    if isinstance(ref_name, str):
        ref_alias_tree = _ref_alias_trees.get(ref_name)
        if isinstance(ref_alias_tree, Mapping):
            effective_alias_tree = _copy_argument_alias_tree(ref_alias_tree)
            _merge_argument_alias_tree(effective_alias_tree, alias_tree)
            alias_tree = effective_alias_tree
    if isinstance(arguments, (list, tuple)):
        return [
            reverse_tool_argument_aliases(
                item,
                alias_tree,
                _ref_alias_trees=_ref_alias_trees,
            )
            for item in arguments
        ]
    if not isinstance(arguments, Mapping):
        return arguments

    translated: dict[str, Any] = {}
    wildcard_node = alias_tree.get(_ARGUMENT_ALIAS_ANY_PROPERTY)
    wildcard_children = (
        wildcard_node.get("properties", {}) if isinstance(wildcard_node, Mapping) else {}
    )
    for key, value in arguments.items():
        alias_node = alias_tree.get(key)
        if isinstance(alias_node, str):
            if alias_node in translated:
                raise ValueError("Tool argument aliases are ambiguous")
            translated[alias_node] = reverse_tool_argument_aliases(
                value,
                {},
                _ref_alias_trees=_ref_alias_trees,
            )
            continue
        if isinstance(alias_node, Mapping):
            original_key = alias_node.get("original", key)
            child_alias_tree = alias_node.get("properties", {})
            if isinstance(child_alias_tree, Mapping):
                value = reverse_tool_argument_aliases(
                    value,
                    child_alias_tree,
                    _ref_alias_trees=_ref_alias_trees,
                )
            if isinstance(original_key, str):
                if original_key in translated:
                    raise ValueError("Tool argument aliases are ambiguous")
                translated[original_key] = value
                continue
        if isinstance(wildcard_children, Mapping) and wildcard_children:
            value = reverse_tool_argument_aliases(
                value,
                wildcard_children,
                _ref_alias_trees=_ref_alias_trees,
            )
        else:
            value = reverse_tool_argument_aliases(
                value,
                {},
                _ref_alias_trees=_ref_alias_trees,
            )
        if key in translated:
            raise ValueError("Tool argument aliases are ambiguous")
        translated[key] = value
    return translated


def _copy_argument_alias_tree(alias_tree: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _copy_argument_alias_tree(value) if isinstance(value, Mapping) else value
        for key, value in alias_tree.items()
    }


def _merge_argument_alias_tree(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, source_node in source.items():
        if key not in target:
            target[key] = (
                _copy_argument_alias_tree(source_node)
                if isinstance(source_node, Mapping)
                else source_node
            )
            continue
        target_node = target[key]
        if not isinstance(target_node, dict) or not isinstance(source_node, Mapping):
            continue
        target_children = target_node.setdefault("properties", {})
        source_children = source_node.get("properties", {})
        if isinstance(target_children, dict) and isinstance(source_children, Mapping):
            _merge_argument_alias_tree(target_children, source_children)
