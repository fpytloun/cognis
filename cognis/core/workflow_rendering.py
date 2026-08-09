"""Constrained rendering primitives for deterministic workflow definitions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from jinja2 import StrictUndefined, nodes
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment, SecurityError

from cognis.models.workflow import DeterministicOutputConfig, StepOutput

MAX_RENDER_INPUT_BYTES = 256_000
MAX_RENDER_OUTPUT_BYTES = 256_000
MAX_METADATA_BYTES = 64_000
MAX_AUDIT_BYTES = 16_000
MAX_DETERMINISTIC_JUMPS = 100
MAX_CONTEXT_STRING_BYTES = 32_000
MAX_TEMPLATE_NODES = 256

_EXACT_EXPRESSION = re.compile(r"^\s*\{\{\s*(.*?)\s*\}\}\s*$", re.DOTALL)
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|credential|authorization|api[_-]?key|cookie|session)",
    re.IGNORECASE,
)
_FORBIDDEN_NODES = (
    nodes.Import,
    nodes.FromImport,
    nodes.Include,
    nodes.Extends,
    nodes.Macro,
    nodes.CallBlock,
    nodes.Block,
    nodes.For,
    nodes.Assign,
    nodes.AssignBlock,
    nodes.Mul,
    nodes.Pow,
    nodes.Mod,
    nodes.Concat,
)
_JSON_SCALARS = (str, int, float, bool, type(None))


class WorkflowRenderError(ValueError):
    """A bounded, context-free workflow rendering failure."""


def _json_size(value: Any) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkflowRenderError("workflow render data must be JSON-serializable") from exc


def _validate_json_like(value: Any, *, path: str = "$", depth: int = 0) -> Any:
    if depth > 32:
        raise WorkflowRenderError("workflow render data exceeds maximum nesting depth")
    if isinstance(value, _JSON_SCALARS):
        if isinstance(value, str) and len(value.encode()) > MAX_CONTEXT_STRING_BYTES:
            raise WorkflowRenderError(f"workflow render string exceeds size limit at {path}")
        if isinstance(value, float) and not (-float("inf") < value < float("inf")):
            raise WorkflowRenderError(f"workflow render data contains non-finite number at {path}")
        return value
    if isinstance(value, list):
        return [
            _validate_json_like(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowRenderError(f"workflow render data has non-string key at {path}")
            result[key] = _validate_json_like(item, path=f"{path}.{key}", depth=depth + 1)
        return result
    raise WorkflowRenderError(f"workflow render data contains unsafe value at {path}")


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise WorkflowRenderError("invalid IANA timezone") from exc


def _datetime(value: str, *, fallback_timezone: str = "UTC") -> datetime:
    if not isinstance(value, str):
        raise WorkflowRenderError("datetime helper input must be an ISO 8601 string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowRenderError("invalid ISO 8601 datetime") from exc
    return (
        result if result.tzinfo is not None else result.replace(tzinfo=_timezone(fallback_timezone))
    )


def _now(timezone: str = "UTC") -> str:
    return datetime.now(tz=_timezone(timezone)).isoformat()


def _date_delta(value: str, *, subtract: bool, **parts: int) -> str:
    allowed = {"years", "months", "weeks", "days", "hours", "minutes", "seconds"}
    if set(parts) - allowed or any(not isinstance(amount, int) for amount in parts.values()):
        raise WorkflowRenderError("date helper duration contains unsupported values")
    delta = relativedelta(**parts)
    result = _datetime(value) - delta if subtract else _datetime(value) + delta
    return result.isoformat()


def _convert_timezone(value: str, from_timezone: str, to_timezone: str) -> str:
    source = _timezone(from_timezone)
    target = _timezone(to_timezone)
    parsed = _datetime(value, fallback_timezone=from_timezone)
    return parsed.astimezone(source).astimezone(target).isoformat()


def _format_datetime(value: str, format: str = "iso", timezone: str | None = None) -> str:
    parsed = _datetime(value)
    if timezone is not None:
        parsed = parsed.astimezone(_timezone(timezone))
    formats = {
        "iso": lambda: parsed.isoformat(),
        "human": lambda: parsed.strftime("%A, %B %d, %Y at %H:%M:%S %Z"),
        "date_only": lambda: parsed.strftime("%Y-%m-%d"),
        "time_only": lambda: parsed.strftime("%H:%M:%S %Z").strip(),
    }
    if format not in formats:
        raise WorkflowRenderError("unsupported datetime format")
    return formats[format]()


_HELPERS = {
    "now": _now,
    "date_add": lambda value, **parts: _date_delta(value, subtract=False, **parts),
    "date_sub": lambda value, **parts: _date_delta(value, subtract=True, **parts),
    "convert_timezone": _convert_timezone,
    "format_datetime": _format_datetime,
}


class _WorkflowSandbox(SandboxedEnvironment):
    def getattr(self, obj: Any, attribute: str) -> Any:
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return self.undefined(obj=obj, name=attribute)

    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        del value
        return isinstance(obj, dict) and attr in obj

    def is_safe_callable(self, obj: Any) -> bool:
        return obj in _HELPERS.values()


class WorkflowRenderer:
    """Render workflow templates against an immutable JSON-like context."""

    def __init__(self) -> None:
        self._environment = _WorkflowSandbox(
            undefined=StrictUndefined,
            autoescape=False,
            loader=None,
        )
        self._environment.filters.clear()
        self._environment.filters["length"] = len
        self._environment.globals.clear()
        self._environment.globals.update(_HELPERS)

    def _prepare(self, source: str, context: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(source, str):
            raise WorkflowRenderError("workflow template must be a string")
        safe_context = _validate_json_like(dict(context))
        if _json_size({"template": source, "context": safe_context}) > MAX_RENDER_INPUT_BYTES:
            raise WorkflowRenderError("workflow render input exceeds size limit")
        try:
            parsed = self._environment.parse(source)
        except (TemplateError, RecursionError) as exc:
            raise WorkflowRenderError("invalid workflow template") from exc
        if any(parsed.find_all(_FORBIDDEN_NODES)):
            raise WorkflowRenderError("workflow template uses a forbidden construct")
        if sum(1 for _ in parsed.find_all(nodes.Node)) > MAX_TEMPLATE_NODES:
            raise WorkflowRenderError("workflow template exceeds complexity limit")
        return safe_context

    def _bounded(self, value: Any, *, limit: int | None = None) -> Any:
        safe = _validate_json_like(value)
        if _json_size(safe) > (MAX_RENDER_OUTPUT_BYTES if limit is None else limit):
            raise WorkflowRenderError("workflow rendered output exceeds size limit")
        return safe

    def render_text(self, source: str, context: Mapping[str, Any]) -> str:
        safe_context = self._prepare(source, context)
        try:
            rendered = self._environment.from_string(source).render(safe_context)
        except (
            TemplateError,
            SecurityError,
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
        ) as exc:
            raise WorkflowRenderError("workflow text rendering failed") from exc
        return self._bounded(rendered)

    def render_expression(self, source: str, context: Mapping[str, Any]) -> bool:
        match = _EXACT_EXPRESSION.fullmatch(source)
        expression = match.group(1) if match else source
        safe_context = self._prepare(f"{{{{ {expression} }}}}", context)
        try:
            value = self._environment.compile_expression(expression, undefined_to_none=False)(
                **safe_context
            )
        except (
            TemplateError,
            SecurityError,
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
        ) as exc:
            raise WorkflowRenderError("workflow expression rendering failed") from exc
        if type(value) is not bool:
            raise WorkflowRenderError("workflow expression must evaluate to a boolean")
        return value

    def render_native(self, value: Any, context: Mapping[str, Any]) -> Any:
        safe_context = _validate_json_like(dict(context))
        if _json_size({"value": value, "context": safe_context}) > MAX_RENDER_INPUT_BYTES:
            raise WorkflowRenderError("workflow render input exceeds size limit")
        return self._bounded(self._render_native_value(value, safe_context))

    def _render_native_value(self, value: Any, context: Mapping[str, Any]) -> Any:
        if isinstance(value, str):
            match = _EXACT_EXPRESSION.fullmatch(value)
            if match:
                expression = match.group(1)
                self._prepare(value, context)
                try:
                    rendered = self._environment.compile_expression(
                        expression, undefined_to_none=False
                    )(**context)
                except (
                    TemplateError,
                    SecurityError,
                    TypeError,
                    ValueError,
                    OverflowError,
                    RecursionError,
                ) as exc:
                    raise WorkflowRenderError("workflow native rendering failed") from exc
                return _validate_json_like(rendered)
            return self.render_text(value, context)
        if isinstance(value, list):
            return [self._render_native_value(item, context) for item in value]
        if isinstance(value, dict):
            return {key: self._render_native_value(item, context) for key, item in value.items()}
        return _validate_json_like(value)


def normalize_deterministic_output(
    config: DeterministicOutputConfig,
    renderer: WorkflowRenderer,
    context: Mapping[str, Any],
    *,
    step_type: str,
) -> StepOutput:
    """Render a deterministic output into the existing StepOutput contract."""

    metadata = renderer.render_native(config.metadata, context)
    metadata = {**metadata, "deterministic_step": True, "step_type": step_type}
    if _json_size(metadata) > MAX_METADATA_BYTES:
        raise WorkflowRenderError("workflow output metadata exceeds size limit")
    return StepOutput(
        summary=renderer.render_text(config.summary, context),
        content=renderer.render_text(config.content, context) if config.content is not None else "",
        outputs=renderer.render_native(config.outputs, context),
        metadata=metadata,
    )


def build_render_audit_record(
    *,
    template: Any,
    rendered: Any,
    redact_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Build a bounded redacted audit summary without retaining raw context."""

    explicit = {key.lower() for key in (redact_keys or set())}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    "[redacted]"
                    if _SENSITIVE_KEY.search(str(key)) or str(key).lower() in explicit
                    else redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    record = {
        "template_digest": hashlib.sha256(
            json.dumps(template, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "rendered": redact(_validate_json_like(rendered)),
    }
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) <= MAX_AUDIT_BYTES:
        return record
    return {
        "template_digest": record["template_digest"],
        "rendered_digest": hashlib.sha256(encoded.encode()).hexdigest(),
        "truncated": True,
    }
