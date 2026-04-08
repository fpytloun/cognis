"""Built-in datetime tool definitions and handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta

from cognis.models.tool import ToolDefinition, ToolSource
from cognis.tools.registry import ToolExecutionContext

_BUILTIN_SOURCE = ToolSource(type="builtin")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

GET_CURRENT_DATETIME_TOOL = ToolDefinition(
    name="get_current_datetime",
    description=(
        "Get the current date and time. Returns ISO 8601 timestamp, "
        "human-readable string, date, time, day of week, and UNIX epoch."
    ),
    parameters={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": (
                    "IANA timezone name (e.g. 'Europe/Prague', 'America/New_York', 'UTC'). "
                    "Defaults to UTC."
                ),
            },
        },
    },
    source=_BUILTIN_SOURCE,
    category="datetime",
    read_only=True,
    timeout_seconds=5,
)

CONVERT_TIMEZONE_TOOL = ToolDefinition(
    name="convert_timezone",
    description=(
        "Convert a datetime from one timezone to another. Accepts ISO 8601 datetime strings."
    ),
    parameters={
        "type": "object",
        "properties": {
            "datetime": {
                "type": "string",
                "description": "ISO 8601 datetime string (e.g. '2025-06-15T14:30:00').",
            },
            "from_timezone": {
                "type": "string",
                "description": "Source IANA timezone name (e.g. 'UTC', 'Europe/London').",
            },
            "to_timezone": {
                "type": "string",
                "description": "Target IANA timezone name (e.g. 'America/New_York').",
            },
        },
        "required": ["datetime", "from_timezone", "to_timezone"],
    },
    source=_BUILTIN_SOURCE,
    category="datetime",
    read_only=True,
    timeout_seconds=5,
)

DATE_ARITHMETIC_TOOL = ToolDefinition(
    name="date_arithmetic",
    description=(
        "Add or subtract a duration from a datetime. Supports years, months, "
        "weeks, days, hours, minutes, and seconds. Uses calendar-aware arithmetic "
        "for months and years (e.g. Jan 31 + 1 month = Feb 28)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "datetime": {
                "type": "string",
                "description": "ISO 8601 datetime string (e.g. '2025-06-15T14:30:00').",
            },
            "operation": {
                "type": "string",
                "enum": ["add", "subtract"],
                "description": "Whether to add or subtract the duration.",
            },
            "years": {"type": "integer", "description": "Number of years."},
            "months": {"type": "integer", "description": "Number of months."},
            "weeks": {"type": "integer", "description": "Number of weeks."},
            "days": {"type": "integer", "description": "Number of days."},
            "hours": {"type": "integer", "description": "Number of hours."},
            "minutes": {"type": "integer", "description": "Number of minutes."},
            "seconds": {"type": "integer", "description": "Number of seconds."},
            "timezone": {
                "type": "string",
                "description": (
                    "IANA timezone to interpret the input datetime in, if it has no "
                    "timezone info. Defaults to UTC."
                ),
            },
        },
        "required": ["datetime", "operation"],
    },
    source=_BUILTIN_SOURCE,
    category="datetime",
    read_only=True,
    timeout_seconds=5,
)

FORMAT_DATETIME_TOOL = ToolDefinition(
    name="format_datetime",
    description=(
        "Format a datetime string into various representations. "
        "Supports ISO 8601, human-readable, date-only, time-only, and custom strftime patterns."
    ),
    parameters={
        "type": "object",
        "properties": {
            "datetime": {
                "type": "string",
                "description": "ISO 8601 datetime string (e.g. '2025-06-15T14:30:00+02:00').",
            },
            "format": {
                "type": "string",
                "enum": ["iso", "human", "date_only", "time_only", "custom"],
                "description": "Output format. Use 'custom' with custom_pattern for strftime.",
            },
            "custom_pattern": {
                "type": "string",
                "description": "Python strftime pattern (e.g. '%Y/%m/%d %H:%M'). Required when format is 'custom'.",
            },
            "timezone": {
                "type": "string",
                "description": (
                    "IANA timezone to convert to before formatting. "
                    "If omitted, uses the timezone from the input datetime (or UTC if naive)."
                ),
            },
        },
        "required": ["datetime", "format"],
    },
    source=_BUILTIN_SOURCE,
    category="datetime",
    read_only=True,
    timeout_seconds=5,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_tz(name: str | None) -> ZoneInfo:
    """Resolve an IANA timezone name, defaulting to UTC."""
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise ValueError(f"Unknown timezone: {name!r}") from exc


def _parse_dt(raw: str, tz_fallback: ZoneInfo | None = None) -> datetime:
    """Parse an ISO 8601 datetime string.

    If the parsed datetime is naive, attach *tz_fallback* (default UTC).
    """
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid datetime string: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_fallback or UTC)
    return dt


def _dt_response(dt: datetime) -> dict[str, Any]:
    """Build a rich datetime response dict."""
    return {
        "iso": dt.isoformat(),
        "human": dt.strftime("%A, %B %d, %Y at %H:%M:%S %Z"),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
        "day_of_week": dt.strftime("%A"),
        "timezone": str(dt.tzinfo),
        "utc_offset": dt.strftime("%z"),
        "epoch": int(dt.timestamp()),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_get_current_datetime(
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> dict[str, Any]:
    """Return the current date and time."""
    del context
    tz = _resolve_tz(arguments.get("timezone"))
    now = datetime.now(tz=tz)
    return _dt_response(now)


async def handle_convert_timezone(
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> dict[str, Any]:
    """Convert a datetime between timezones."""
    del context
    from_tz = _resolve_tz(arguments.get("from_timezone"))
    to_tz = _resolve_tz(arguments.get("to_timezone"))
    dt = _parse_dt(arguments["datetime"], tz_fallback=from_tz)
    # If the input already has tzinfo, localize to from_tz first
    if dt.tzinfo != from_tz:
        dt = dt.astimezone(from_tz)
    converted = dt.astimezone(to_tz)
    return {
        "original": _dt_response(dt),
        "converted": _dt_response(converted),
    }


async def handle_date_arithmetic(
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> dict[str, Any]:
    """Add or subtract a duration from a datetime."""
    del context
    tz = _resolve_tz(arguments.get("timezone"))
    dt = _parse_dt(arguments["datetime"], tz_fallback=tz)
    operation = arguments.get("operation", "add")
    if operation not in ("add", "subtract"):
        raise ValueError(f"Invalid operation: {operation!r}. Must be 'add' or 'subtract'.")

    delta = relativedelta(
        years=arguments.get("years", 0),
        months=arguments.get("months", 0),
        weeks=arguments.get("weeks", 0),
        days=arguments.get("days", 0),
        hours=arguments.get("hours", 0),
        minutes=arguments.get("minutes", 0),
        seconds=arguments.get("seconds", 0),
    )

    result = dt + delta if operation == "add" else dt - delta
    return {
        "original": _dt_response(dt),
        "result": _dt_response(result),
        "operation": operation,
    }


async def handle_format_datetime(
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> dict[str, Any]:
    """Format a datetime string."""
    del context
    tz = _resolve_tz(arguments.get("timezone"))
    dt = _parse_dt(arguments["datetime"])
    if arguments.get("timezone"):
        dt = dt.astimezone(tz)

    fmt = arguments.get("format", "iso")
    if fmt == "iso":
        formatted = dt.isoformat()
    elif fmt == "human":
        formatted = dt.strftime("%A, %B %d, %Y at %H:%M:%S %Z")
    elif fmt == "date_only":
        formatted = dt.strftime("%Y-%m-%d")
    elif fmt == "time_only":
        formatted = dt.strftime("%H:%M:%S %Z").strip()
    elif fmt == "custom":
        pattern = arguments.get("custom_pattern")
        if not pattern:
            raise ValueError("custom_pattern is required when format is 'custom'.")
        formatted = dt.strftime(pattern)
    else:
        raise ValueError(f"Unknown format: {fmt!r}")

    return {
        "formatted": formatted,
        "details": _dt_response(dt),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_ALL_DATETIME_TOOLS = [
    GET_CURRENT_DATETIME_TOOL,
    CONVERT_TIMEZONE_TOOL,
    DATE_ARITHMETIC_TOOL,
    FORMAT_DATETIME_TOOL,
]

_HANDLER_MAP: dict[str, Any] = {
    GET_CURRENT_DATETIME_TOOL.name: handle_get_current_datetime,
    CONVERT_TIMEZONE_TOOL.name: handle_convert_timezone,
    DATE_ARITHMETIC_TOOL.name: handle_date_arithmetic,
    FORMAT_DATETIME_TOOL.name: handle_format_datetime,
}


def datetime_tools() -> list[ToolDefinition]:
    """Return built-in datetime tool definitions."""
    return list(_ALL_DATETIME_TOOLS)


def build_datetime_tool_handlers() -> dict[str, Any]:
    """Return handler map for datetime tools."""
    return dict(_HANDLER_MAP)
