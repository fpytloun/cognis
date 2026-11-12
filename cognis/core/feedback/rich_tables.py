"""Bounded authored rich-table membership, independent of browser observations."""

from __future__ import annotations

from typing import Any, Literal

from cognis.core.feedback.primitives import FeedbackError
from cognis.models import feedback as dto


def unavailable(message: str) -> FeedbackError:
    return FeedbackError(422, "scope_unavailable", message)


def rich_block(
    payload: dict[str, Any] | None, block_id: str, identity: Literal["semantic", "revision_local"]
) -> tuple[dict[str, Any], str]:
    """Resolve the same globally unique identity used by authored text fields."""
    if payload is None:
        raise unavailable("A rich target requires rich content")
    matches: list[tuple[dict[str, Any], str]] = []
    queue: list[tuple[Any, str, int]] = [(payload, "", 0)]
    visited = 0
    while queue:
        value, path, depth = queue.pop()
        visited += 1
        if visited > 100_000 or depth > 64:
            raise unavailable("Rich source exceeds the identity traversal budget")
        if isinstance(value, dict):
            if (identity == "semantic" and value.get("id") == block_id) or (
                identity == "revision_local" and path == block_id
            ):
                matches.append((value, path))
            queue.extend((child, f"{path}/{key}", depth + 1) for key, child in value.items())
        elif isinstance(value, list):
            queue.extend((child, f"{path}/{index}", depth + 1) for index, child in enumerate(value))
    if len(matches) != 1:
        raise unavailable("Rich block identity is missing or ambiguous")
    return matches[0]


def authored_block_paths(payload: dict[str, Any]) -> set[str]:
    """Enumerate block positions rendered by rich container owners, not arbitrary JSON."""
    paths: set[str] = set()

    def children(block: dict[str, Any], path: str) -> list[tuple[dict[str, Any], str]]:
        key = "blocks" if isinstance(block.get("blocks"), list) else "children"
        keys = [key]
        if block.get("type", block.get("kind")) in {"tabs", "accordion", "modal", "gallery"}:
            keys.append("items")
        result: list[tuple[dict[str, Any], str]] = []
        for key in keys:
            values = block.get(key)
            if isinstance(values, list):
                result.extend(
                    (child, f"{path}/{key}/{index}")
                    for index, child in enumerate(values)
                    if isinstance(child, dict)
                )
        return result

    queue = children({"blocks": payload.get("blocks")}, "")
    while queue:
        block, path = queue.pop()
        if len(paths) >= 100_000:
            raise unavailable("Rich source exceeds the block budget")
        paths.add(path)
        type_ = block.get("type", block.get("kind"))
        if type_ in {
            "section",
            "stack",
            "columns",
            "grid",
            "card_grid",
            "modal",
            "gallery",
            "divider",
        }:
            queue.extend(children(block, path))
        elif type_ in {"tabs", "accordion"}:
            for entry, entry_path in children(block, path):
                queue.extend(children(entry, entry_path))
    return paths


def object_keys(value: dict[str, Any]) -> list[str]:
    """Match ECMAScript Object.keys ordering for inferred numeric column names."""
    indices = [
        key
        for key in value
        if len(key) <= 10
        and key.isascii()
        and key.isdecimal()
        and str(int(key)) == key
        and int(key) < 2**32 - 1
    ]
    return sorted(indices, key=int) + [key for key in value if key not in indices]


def table_membership(block: dict[str, Any]) -> tuple[list[str], dict[int, set[str]]]:
    """Mirror tableRows/tableColumns provenance without interpreting arbitrary paths."""
    if block.get("type", block.get("kind")) not in {
        "table",
        "comparison_matrix",
        "decision_matrix",
    }:
        raise unavailable("Select an authored table data grid")
    rows = block.get("rows")
    if rows is None:
        rows = block.get("data")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise unavailable("Table requires between 1 and 1000 authored rows")
    columns = block.get("columns")
    declared: list[str] = []
    if isinstance(columns, list):
        for column in columns:
            key = column
            if isinstance(column, dict):
                key = next(
                    (
                        column[name]
                        for name in ("key", "id", "label")
                        if column.get(name) is not None
                    ),
                    "",
                )
            if not isinstance(key, str) or not key:
                raise unavailable("Table column identity is unavailable")
            declared.append(key)
    if len(declared) > 128 or len(set(declared)) != len(declared):
        raise unavailable("Table columns are ambiguous or exceed the 128 column budget")
    membership: dict[int, set[str]] = {}
    first_keys: list[str] | None = None
    for ordinal, row in enumerate(rows):
        values: dict[str, Any] = {}
        if isinstance(row, list):
            keys = [declared[i] if i < len(declared) else str(i) for i in range(len(row))]
            if len(set(keys)) != len(keys):
                raise unavailable("Table cell membership is ambiguous")
            values = dict(zip(keys, row, strict=True))
        elif isinstance(row, dict):
            if isinstance(row.get("values"), list):
                values = {
                    declared[i] if i < len(declared) else str(i): cell
                    for i, cell in enumerate(row["values"])
                }
                if len(values) != len(row["values"]):
                    raise unavailable("Table cell membership is ambiguous")
            values.update(row)
        else:
            continue
        membership[ordinal] = set(values)
        if first_keys is None:
            first_keys = object_keys(values)
    keys = declared if isinstance(columns, list) else first_keys or []
    if not 1 <= len(keys) <= 128 or len(set(keys)) != len(keys):
        raise unavailable("Table columns are missing, ambiguous or oversized")
    return keys, membership


def validate_table_range(payload: dict[str, Any] | None, locator: dto.RichLocator) -> None:
    """Validate all selected membership after source/grant/context/digest authorization."""
    selection = locator.table_range
    assert selection is not None
    block, path = rich_block(payload, locator.block_id, locator.identity)
    if payload is None or path not in authored_block_paths(payload):
        raise unavailable("Table is not an authored rendered block")
    columns, membership = table_membership(block)
    if selection.column_end > len(columns):
        raise unavailable("Selected columns lie outside the authored table")
    if selection.scope == "rows" and (
        selection.column_start != 0 or selection.column_end != len(columns)
    ):
        raise unavailable("Whole rows require every authored data column")
    if selection.scope == "columns" and selection.rows != sorted(membership):
        raise unavailable("Whole columns require every authored data row")
    selected_columns = columns[selection.column_start : selection.column_end]
    for ordinal in selection.rows:
        if ordinal not in membership or not all(
            key in membership[ordinal] for key in selected_columns
        ):
            raise unavailable("Selected cells lack exact authored membership")
