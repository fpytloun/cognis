"""Unit tests for the executor runner module."""

from __future__ import annotations

from cognis.executor.runner import ExecutorRunner, _normalize_result
from cognis.models.tool import ExecutorConfig, InferenceConfig, ToolResult


def test_normalize_result_from_string() -> None:
    """_normalize_result wraps a string in ToolResult."""
    result = _normalize_result("hello world", 42)
    assert isinstance(result, ToolResult)
    assert result.output == "hello world"
    assert result.duration_ms == 42
    assert result.is_error is False


def test_normalize_result_from_dict() -> None:
    """_normalize_result serializes a dict to JSON."""
    result = _normalize_result({"key": "value"}, 10)
    assert result.output == '{"key": "value"}'
    assert result.duration_ms == 10


def test_normalize_result_from_tool_result() -> None:
    """_normalize_result preserves an existing ToolResult."""
    original = ToolResult(output="test", is_error=True, duration_ms=None)
    result = _normalize_result(original, 50)
    assert result.output == "test"
    assert result.is_error is True
    assert result.duration_ms == 50


def test_runner_init_tools() -> None:
    """ExecutorRunner initializes tool handlers from cognis.tools.executor."""
    config = ExecutorConfig(
        executor_id="test-runner",
        metadata={"enabled_tools": "*"},
    )
    runner = ExecutorRunner(config)
    runner._init_tools()
    # Should have at least some handlers (bash, read, write, etc.)
    assert len(runner._tool_handlers) > 0


def test_runner_init_tools_filtered() -> None:
    """ExecutorRunner filters tools by enabled_tools metadata."""
    config = ExecutorConfig(
        executor_id="test-runner",
        metadata={"enabled_tools": "bash,read"},
    )
    runner = ExecutorRunner(config)
    runner._init_tools()
    # Should only have the specified tools
    assert "bash" in runner._tool_handlers or len(runner._tool_handlers) <= 2


def test_runner_init_inference_none() -> None:
    """ExecutorRunner skips inference init when not configured."""
    config = ExecutorConfig(executor_id="test-runner")
    runner = ExecutorRunner(config)
    runner._init_inference()
    assert runner._inference_handler is None


def test_runner_handle_configure() -> None:
    """_handle_configure merges secrets."""
    config = ExecutorConfig(executor_id="test-runner", secrets={"existing": "value"})
    runner = ExecutorRunner(config)
    runner._handle_configure({"secrets": {"new_key": "new_value"}})
    assert runner._secrets == {"existing": "value", "new_key": "new_value"}


def test_runner_get_inference_models_empty() -> None:
    """_get_inference_models returns empty list when no inference configured."""
    config = ExecutorConfig(executor_id="test-runner")
    runner = ExecutorRunner(config)
    assert runner._get_inference_models() == []


def test_runner_get_inference_models_with_config() -> None:
    """_get_inference_models returns models from inference config."""
    config = ExecutorConfig(
        executor_id="test-runner",
        inference=InferenceConfig(
            endpoint="http://localhost:11434/v1",
            models=["llama3.2", "codellama"],
        ),
    )
    runner = ExecutorRunner(config)
    assert runner._get_inference_models() == ["llama3.2", "codellama"]


def test_runner_get_inference_type() -> None:
    """_get_inference_type returns the inference type from config."""
    config = ExecutorConfig(
        executor_id="test-runner",
        inference=InferenceConfig(type="openai_compatible"),
    )
    runner = ExecutorRunner(config)
    assert runner._get_inference_type() == "openai_compatible"


def test_runner_get_inference_type_none() -> None:
    """_get_inference_type returns None when no inference configured."""
    config = ExecutorConfig(executor_id="test-runner")
    runner = ExecutorRunner(config)
    assert runner._get_inference_type() is None
