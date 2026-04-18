from __future__ import annotations

import pytest

from cognis.core.agent_loop import StreamAccumulator
from cognis.providers.llm.responses_bridge import (
    messages_to_responses_input,
    responses_request_kwargs,
    responses_stream_to_chat_chunks,
    responses_to_chat_response,
)


def test_messages_to_responses_input_normalizes_multimodal_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/image.jpg", "detail": "high"},
                },
                {
                    "type": "file",
                    "file": {
                        "file_url": "https://example.com/report.pdf",
                        "filename": "report.pdf",
                    },
                },
            ],
        }
    ]

    result = messages_to_responses_input(messages)

    assert result == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is in this image?"},
                {
                    "type": "input_image",
                    "image_url": "https://example.com/image.jpg",
                    "detail": "high",
                },
                {
                    "type": "input_file",
                    "file_url": "https://example.com/report.pdf",
                    "filename": "report.pdf",
                },
            ],
        }
    ]


def test_messages_to_responses_input_preserves_system_role() -> None:
    messages = [{"role": "system", "content": "Follow system instructions."}]

    result = messages_to_responses_input(messages)

    assert result == [{"role": "system", "content": "Follow system instructions."}]


def test_responses_to_chat_response_preserves_reasoning_only_payload() -> None:
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "content": [
                    {
                        "type": "reasoning_text",
                        "text": '{"decision":"revise","reasoning":"tests missing"}',
                    }
                ],
                "summary": [{"type": "summary", "text": "Need tests"}],
            }
        ],
        "usage": {"output_tokens": 10, "total_tokens": 15},
    }

    result = responses_to_chat_response(payload)

    message = result["choices"][0]["message"]
    assert message["content"] is None
    assert message["reasoning_content"] == '{"decision":"revise","reasoning":"tests missing"}'
    assert message["reasoning"] == "Need tests"


def test_responses_to_chat_response_keeps_refusal_separate_from_content() -> None:
    payload = {
        "status": "completed",
        "output": [{"type": "refusal", "refusal": {"text": "Cannot comply"}}],
    }

    result = responses_to_chat_response(payload)

    message = result["choices"][0]["message"]
    assert message["content"] is None
    assert message["refusal"] == "Cannot comply"


def test_responses_to_chat_response_does_not_promote_reasoning_blocks_to_content() -> None:
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "reasoning_text", "text": "internal reasoning"}],
            }
        ],
    }

    result = responses_to_chat_response(payload)

    assert result["choices"][0]["message"]["content"] is None


def test_responses_to_chat_response_marks_incomplete_as_length() -> None:
    payload = {
        "status": "incomplete",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "{"}]}],
    }

    result = responses_to_chat_response(payload)

    assert result["choices"][0]["finish_reason"] == "length"
    assert result["response_status"] == "incomplete"


def test_messages_to_responses_input_drops_tool_messages_without_tool_call_id() -> None:
    result = messages_to_responses_input(
        [
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "content": "hello"},
        ]
    )

    assert result == [{"role": "assistant", "content": ""}]


def test_responses_request_kwargs_ignores_unsupported_string_response_format() -> None:
    result = responses_request_kwargs({"response_format": "xml"})

    assert "text" not in result


def test_responses_to_chat_response_prefers_input_output_usage_fields() -> None:
    payload = {
        "status": "completed",
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "input_tokens": 7,
            "output_tokens": 8,
            "total_tokens": 15,
        },
    }

    result = responses_to_chat_response(payload)

    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15}


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_emits_reasoning_content() -> None:
    async def _stream():
        yield {
            "type": "response.reasoning_text.delta",
            "delta": '{"decision":"revise"}',
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == '{"decision":"revise"}'
    assert chunks[-1]["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_keeps_reasoning_summary_separate() -> None:
    async def _stream():
        yield {"type": "response.reasoning_text.delta", "delta": '{"decision":"revise"}'}
        yield {"type": "response.reasoning_summary_text.delta", "delta": "Need tests"}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == '{"decision":"revise"}'
    assert chunks[1]["choices"][0]["delta"]["reasoning"] == "Need tests"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_falls_back_to_reasoning_on_completion() -> None:
    async def _stream():
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": '{"decision":"revise"}'}],
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == '{"decision":"revise"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_falls_back_to_reasoning_summary_on_completion() -> (
    None
):
    async def _stream():
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary", "text": "Need tests"}],
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[0]["choices"][0]["delta"]["reasoning"] == "Need tests"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_backfills_remaining_summary_fields() -> None:
    async def _stream():
        yield {"type": "response.reasoning_text.delta", "delta": '{"decision":"revise"}'}
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": '{"decision":"revise"}'}],
                        "summary": [{"type": "summary", "text": "Need tests"}],
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == '{"decision":"revise"}'
    assert chunks[1]["choices"][0]["delta"]["reasoning"] == "Need tests"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_emits_refusal() -> None:
    async def _stream():
        yield {"type": "response.refusal.delta", "delta": "Cannot comply"}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 3}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[0]["choices"][0]["delta"]["refusal"] == "Cannot comply"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_dedupes_replayed_function_call_events() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "step_todo_write",
            },
        }
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"todos":[{"content":"Load `daily-brief`',
        }
        # Provider replay re-adds the same item and restarts from an overlapping prefix.
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "step_todo_write",
            },
        }
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": 'Load `daily-brief` skill","status":"completed"}]}',
        }
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "step_todo_write",
                "arguments": (
                    '{"todos":[{"content":"Load `daily-brief` skill","status":"completed"}]}'
                ),
            },
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 9}},
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "step_todo_write"
    assert calls[0].arguments == {
        "todos": [{"content": "Load `daily-brief` skill", "status": "completed"}]
    }


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_emits_done_only_function_call() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_done",
                "call_id": "call_done",
                "name": "step_complete",
                "arguments": '{"summary":"done"}',
            },
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 4}},
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "step_complete"
    assert calls[0].call_id == "call_done"
    assert calls[0].arguments == {"summary": "done"}


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_backfills_completed_only_function_call() -> None:
    async def _stream():
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_completed",
                        "call_id": "call_completed",
                        "name": "step_complete",
                        "arguments": '{"summary":"done from completed"}',
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "step_complete"
    assert calls[0].call_id == "call_completed"
    assert calls[0].arguments == {"summary": "done from completed"}


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_dedupes_replay_when_item_id_changes() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_original",
                "call_id": "call_shared",
                "name": "step_complete",
            },
        }
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_original",
            "delta": '{"summary":"done',
        }
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_retry",
                "call_id": "call_shared",
                "name": "step_complete",
            },
        }
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_retry",
                "call_id": "call_shared",
                "name": "step_complete",
                "arguments": '{"summary":"done"}',
            },
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 6}},
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].call_id == "call_shared"
    assert calls[0].arguments == {"summary": "done"}


@pytest.mark.asyncio
async def test_responses_stream_recovers_trailing_valid_object_suffix() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_suffix",
                "call_id": "call_suffix",
                "name": "step_todo_write",
                "arguments": (
                    '{"todos":[content":"Find the Lumilens Todoist project and '
                    'appropriate section","status":"in_progress"}]}'
                    '{"todos":[{"content":"Find the Lumilens Todoist project and '
                    'appropriate section","status":"in_progress"},{"content":'
                    '"Create the Todoist task for Monday in the Lumilens project",'
                    '"status":"pending"}]}'
                ),
            },
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 7}},
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "step_todo_write"
    assert calls[0].arguments == {
        "todos": [
            {
                "content": "Find the Lumilens Todoist project and appropriate section",
                "status": "in_progress",
            },
            {
                "content": "Create the Todoist task for Monday in the Lumilens project",
                "status": "pending",
            },
        ]
    }
