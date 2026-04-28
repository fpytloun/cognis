"""Strict conditional gate expression evaluation."""

from __future__ import annotations

import ast
from typing import Any

from cognis.models.workflow import Workflow

_ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Attribute,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)


def evaluate_gate_conditions(
    expressions: list[str],
    *,
    step_outputs: dict[str, dict[str, Any]],
    thresholds: dict[str, Any] | None = None,
) -> bool:
    """Return true when any condition expression evaluates true."""

    if not expressions:
        return True
    context = _build_context(step_outputs=step_outputs, thresholds=thresholds or {})
    return any(bool(_eval_expression(expr, context)) for expr in expressions)


def validate_gate_conditions(workflow: Workflow) -> None:
    """Validate conditional gate syntax and references within a workflow."""

    seen_steps: set[str] = set()
    metadata_fields: dict[str, set[str]] = {}
    for step in workflow.steps:
        if step.gate is not None:
            for condition in step.gate.conditions:
                tree = _parse(condition.expression)
                for namespace, step_name, field_name in _references(tree):
                    if namespace in {"metadata", "outputs"} and step_name not in seen_steps:
                        raise ValueError(
                            f"Gate {step.name!r} condition references unknown/later step {step_name!r}"
                        )
                    if namespace == "metadata" and field_name not in metadata_fields.get(
                        step_name, set()
                    ):
                        raise ValueError(
                            f"Gate {step.name!r} condition references undeclared metadata "
                            f"{step_name}.{field_name}"
                        )
        contract = step.metadata_contract
        metadata_fields[step.name] = {
            field.name for field in (contract.fields if contract is not None else [])
        }
        seen_steps.add(step.name)


def _build_context(
    *, step_outputs: dict[str, dict[str, Any]], thresholds: dict[str, Any]
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    for step_name, raw in step_outputs.items():
        metadata[step_name] = raw.get("metadata", {}) if isinstance(raw, dict) else {}
        outputs[step_name] = raw.get("outputs", {}) if isinstance(raw, dict) else {}
    return {"metadata": metadata, "outputs": outputs, "thresholds": thresholds}


def _eval_expression(expression: str, context: dict[str, Any]) -> Any:
    return _eval_node(_parse(expression).body, context)


def _parse(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid gate condition syntax: {expression!r}") from exc
    _validate_ast(tree)
    return tree


def _validate_ast(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, _ALLOWED_AST_NODES):
            continue
        raise ValueError(f"Unsupported gate condition syntax: {type(node).__name__}")


def _eval_node(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List | ast.Tuple):
        return [_eval_node(item, context) for item in node.elts]
    if isinstance(node, ast.Name):
        if node.id in {"true", "false"}:
            return node.id == "true"
        raise ValueError(f"Unsupported gate condition identifier: {node.id}")
    if isinstance(node, ast.Attribute):
        return _resolve_attribute(node, context)
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(value, context) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not bool(_eval_node(node.operand, context))
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, context)
            if not _compare(left, op, right):
                return False
            left = right
        return True
    raise ValueError(f"Unsupported gate condition syntax: {type(node).__name__}")


def _compare(left: Any, op: ast.cmpop, right: Any) -> bool:
    try:
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.In):
            return left in right
        if isinstance(op, ast.NotIn):
            return left not in right
    except TypeError as exc:
        raise ValueError("Gate condition compared incompatible values") from exc
    raise ValueError(f"Unsupported gate comparison: {type(op).__name__}")


def _resolve_attribute(node: ast.Attribute, context: dict[str, Any]) -> Any:
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        raise ValueError("Unsupported gate condition reference")
    parts.append(current.id)
    parts.reverse()
    if parts[0] not in {"metadata", "outputs", "thresholds"}:
        raise ValueError(f"Unsupported gate condition reference: {'.'.join(parts)}")
    value: Any = context
    for part in parts:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _references(tree: ast.AST) -> list[tuple[str, str, str]]:
    refs: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts = _attribute_parts(node)
        if len(parts) >= 3 and parts[0] in {"metadata", "outputs"}:
            refs.append((parts[0], parts[1], parts[2]))
    return refs


def _attribute_parts(node: ast.Attribute) -> list[str]:
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    parts.reverse()
    return parts
