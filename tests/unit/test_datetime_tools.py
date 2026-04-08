"""Unit tests for built-in datetime tools."""

from __future__ import annotations

from datetime import datetime

import pytest

from cognis.models.tool import ExecutorHandle
from cognis.tools.builtin.datetime_tools import (
    _ALL_DATETIME_TOOLS,
    build_datetime_tool_handlers,
    datetime_tools,
    handle_convert_timezone,
    handle_date_arithmetic,
    handle_format_datetime,
    handle_get_current_datetime,
)
from cognis.tools.registry import ToolExecutionContext

_DUMMY_CONTEXT = ToolExecutionContext(
    executor_handle=ExecutorHandle(
        executor_id="test",
        executor_type="in_process",
    )
)


# ---------------------------------------------------------------------------
# Definition tests
# ---------------------------------------------------------------------------


class TestDatetimeToolDefinitions:
    """Test datetime tool definitions."""

    def test_datetime_tools_count(self) -> None:
        defs = datetime_tools()
        assert len(defs) == 4

    def test_all_have_builtin_source(self) -> None:
        for tool in _ALL_DATETIME_TOOLS:
            assert tool.source.type == "builtin"

    def test_all_have_datetime_category(self) -> None:
        for tool in _ALL_DATETIME_TOOLS:
            assert tool.category == "datetime"

    def test_all_are_read_only(self) -> None:
        for tool in _ALL_DATETIME_TOOLS:
            assert tool.read_only is True

    def test_tool_names(self) -> None:
        names = {t.name for t in _ALL_DATETIME_TOOLS}
        assert names == {
            "get_current_datetime",
            "convert_timezone",
            "date_arithmetic",
            "format_datetime",
        }

    def test_handler_map_covers_all_tools(self) -> None:
        handlers = build_datetime_tool_handlers()
        for tool in _ALL_DATETIME_TOOLS:
            assert tool.name in handlers


# ---------------------------------------------------------------------------
# get_current_datetime
# ---------------------------------------------------------------------------


class TestGetCurrentDatetime:
    async def test_utc_default(self) -> None:
        result = await handle_get_current_datetime({}, _DUMMY_CONTEXT)
        assert result["timezone"] == "UTC"
        assert "iso" in result
        assert "epoch" in result
        assert "day_of_week" in result

    async def test_specific_timezone(self) -> None:
        result = await handle_get_current_datetime({"timezone": "Europe/Prague"}, _DUMMY_CONTEXT)
        assert "Europe/Prague" in result["timezone"]
        # Verify the returned time is actually in Prague timezone
        dt = datetime.fromisoformat(result["iso"])
        assert dt.tzinfo is not None

    async def test_invalid_timezone(self) -> None:
        with pytest.raises(ValueError, match="Unknown timezone"):
            await handle_get_current_datetime({"timezone": "Invalid/Zone"}, _DUMMY_CONTEXT)

    async def test_response_fields(self) -> None:
        result = await handle_get_current_datetime({}, _DUMMY_CONTEXT)
        expected_keys = {
            "iso",
            "human",
            "date",
            "time",
            "day_of_week",
            "timezone",
            "utc_offset",
            "epoch",
        }
        assert expected_keys == set(result.keys())


# ---------------------------------------------------------------------------
# convert_timezone
# ---------------------------------------------------------------------------


class TestConvertTimezone:
    async def test_utc_to_prague(self) -> None:
        result = await handle_convert_timezone(
            {
                "datetime": "2025-06-15T12:00:00",
                "from_timezone": "UTC",
                "to_timezone": "Europe/Prague",
            },
            _DUMMY_CONTEXT,
        )
        assert "original" in result
        assert "converted" in result
        # Prague is UTC+2 in summer (CEST)
        converted_dt = datetime.fromisoformat(result["converted"]["iso"])
        assert converted_dt.hour == 14

    async def test_new_york_to_tokyo(self) -> None:
        result = await handle_convert_timezone(
            {
                "datetime": "2025-01-15T10:00:00",
                "from_timezone": "America/New_York",
                "to_timezone": "Asia/Tokyo",
            },
            _DUMMY_CONTEXT,
        )
        converted_dt = datetime.fromisoformat(result["converted"]["iso"])
        # New York EST is UTC-5, Tokyo is UTC+9, so +14 hours
        assert converted_dt.day == 16
        assert converted_dt.hour == 0

    async def test_invalid_from_timezone(self) -> None:
        with pytest.raises(ValueError, match="Unknown timezone"):
            await handle_convert_timezone(
                {
                    "datetime": "2025-06-15T12:00:00",
                    "from_timezone": "Bogus/Zone",
                    "to_timezone": "UTC",
                },
                _DUMMY_CONTEXT,
            )

    async def test_invalid_datetime_string(self) -> None:
        with pytest.raises(ValueError, match="Invalid datetime"):
            await handle_convert_timezone(
                {
                    "datetime": "not-a-date",
                    "from_timezone": "UTC",
                    "to_timezone": "UTC",
                },
                _DUMMY_CONTEXT,
            )

    async def test_datetime_with_existing_tz(self) -> None:
        """Input with explicit tz offset should be respected."""
        result = await handle_convert_timezone(
            {
                "datetime": "2025-06-15T14:00:00+02:00",
                "from_timezone": "Europe/Prague",
                "to_timezone": "UTC",
            },
            _DUMMY_CONTEXT,
        )
        converted_dt = datetime.fromisoformat(result["converted"]["iso"])
        assert converted_dt.hour == 12


# ---------------------------------------------------------------------------
# date_arithmetic
# ---------------------------------------------------------------------------


class TestDateArithmetic:
    async def test_add_days(self) -> None:
        result = await handle_date_arithmetic(
            {"datetime": "2025-06-15T12:00:00", "operation": "add", "days": 10},
            _DUMMY_CONTEXT,
        )
        result_dt = datetime.fromisoformat(result["result"]["iso"])
        assert result_dt.day == 25
        assert result["operation"] == "add"

    async def test_subtract_hours(self) -> None:
        result = await handle_date_arithmetic(
            {"datetime": "2025-06-15T12:00:00", "operation": "subtract", "hours": 5},
            _DUMMY_CONTEXT,
        )
        result_dt = datetime.fromisoformat(result["result"]["iso"])
        assert result_dt.hour == 7

    async def test_add_months_calendar_aware(self) -> None:
        """Jan 31 + 1 month should give Feb 28 (non-leap year)."""
        result = await handle_date_arithmetic(
            {"datetime": "2025-01-31T12:00:00", "operation": "add", "months": 1},
            _DUMMY_CONTEXT,
        )
        result_dt = datetime.fromisoformat(result["result"]["iso"])
        assert result_dt.month == 2
        assert result_dt.day == 28

    async def test_add_months_leap_year(self) -> None:
        """Jan 31 + 1 month in a leap year should give Feb 29."""
        result = await handle_date_arithmetic(
            {"datetime": "2024-01-31T12:00:00", "operation": "add", "months": 1},
            _DUMMY_CONTEXT,
        )
        result_dt = datetime.fromisoformat(result["result"]["iso"])
        assert result_dt.month == 2
        assert result_dt.day == 29

    async def test_add_years(self) -> None:
        result = await handle_date_arithmetic(
            {"datetime": "2025-06-15T12:00:00", "operation": "add", "years": 2},
            _DUMMY_CONTEXT,
        )
        result_dt = datetime.fromisoformat(result["result"]["iso"])
        assert result_dt.year == 2027

    async def test_subtract_weeks(self) -> None:
        result = await handle_date_arithmetic(
            {"datetime": "2025-06-15T12:00:00", "operation": "subtract", "weeks": 2},
            _DUMMY_CONTEXT,
        )
        result_dt = datetime.fromisoformat(result["result"]["iso"])
        assert result_dt.day == 1

    async def test_combined_duration(self) -> None:
        result = await handle_date_arithmetic(
            {
                "datetime": "2025-06-15T12:00:00",
                "operation": "add",
                "days": 5,
                "hours": 3,
                "minutes": 30,
            },
            _DUMMY_CONTEXT,
        )
        result_dt = datetime.fromisoformat(result["result"]["iso"])
        assert result_dt.day == 20
        assert result_dt.hour == 15
        assert result_dt.minute == 30

    async def test_invalid_operation(self) -> None:
        with pytest.raises(ValueError, match="Invalid operation"):
            await handle_date_arithmetic(
                {"datetime": "2025-06-15T12:00:00", "operation": "multiply"},
                _DUMMY_CONTEXT,
            )

    async def test_with_timezone(self) -> None:
        result = await handle_date_arithmetic(
            {
                "datetime": "2025-06-15T12:00:00",
                "operation": "add",
                "days": 1,
                "timezone": "Europe/Prague",
            },
            _DUMMY_CONTEXT,
        )
        assert "Europe/Prague" in result["result"]["timezone"]


# ---------------------------------------------------------------------------
# format_datetime
# ---------------------------------------------------------------------------


class TestFormatDatetime:
    async def test_iso_format(self) -> None:
        result = await handle_format_datetime(
            {"datetime": "2025-06-15T14:30:00+02:00", "format": "iso"},
            _DUMMY_CONTEXT,
        )
        assert result["formatted"] == "2025-06-15T14:30:00+02:00"

    async def test_human_format(self) -> None:
        result = await handle_format_datetime(
            {"datetime": "2025-06-15T14:30:00+00:00", "format": "human"},
            _DUMMY_CONTEXT,
        )
        assert "Sunday" in result["formatted"]
        assert "June" in result["formatted"]
        assert "2025" in result["formatted"]

    async def test_date_only(self) -> None:
        result = await handle_format_datetime(
            {"datetime": "2025-06-15T14:30:00", "format": "date_only"},
            _DUMMY_CONTEXT,
        )
        assert result["formatted"] == "2025-06-15"

    async def test_time_only(self) -> None:
        result = await handle_format_datetime(
            {"datetime": "2025-06-15T14:30:00+00:00", "format": "time_only"},
            _DUMMY_CONTEXT,
        )
        assert "14:30:00" in result["formatted"]

    async def test_custom_format(self) -> None:
        result = await handle_format_datetime(
            {
                "datetime": "2025-06-15T14:30:00",
                "format": "custom",
                "custom_pattern": "%Y/%m/%d %H:%M",
            },
            _DUMMY_CONTEXT,
        )
        assert result["formatted"] == "2025/06/15 14:30"

    async def test_custom_format_missing_pattern(self) -> None:
        with pytest.raises(ValueError, match="custom_pattern is required"):
            await handle_format_datetime(
                {"datetime": "2025-06-15T14:30:00", "format": "custom"},
                _DUMMY_CONTEXT,
            )

    async def test_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="Unknown format"):
            await handle_format_datetime(
                {"datetime": "2025-06-15T14:30:00", "format": "xml"},
                _DUMMY_CONTEXT,
            )

    async def test_with_timezone_conversion(self) -> None:
        result = await handle_format_datetime(
            {
                "datetime": "2025-06-15T12:00:00+00:00",
                "format": "iso",
                "timezone": "America/New_York",
            },
            _DUMMY_CONTEXT,
        )
        dt = datetime.fromisoformat(result["formatted"])
        assert dt.hour == 8  # UTC-4 in summer (EDT)

    async def test_details_included(self) -> None:
        result = await handle_format_datetime(
            {"datetime": "2025-06-15T14:30:00+00:00", "format": "iso"},
            _DUMMY_CONTEXT,
        )
        assert "details" in result
        assert "epoch" in result["details"]
        assert "day_of_week" in result["details"]
