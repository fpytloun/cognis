from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, field_serializer

from cognis.core.agent_loop import StreamAccumulator
from cognis.providers.llm.errors import ToolArgumentParseFailure
from cognis.providers.llm.responses_bridge import (
    messages_to_responses_input,
    response_model_dump,
    responses_request_kwargs,
    responses_stream_to_chat_chunks,
    responses_to_chat_response,
    split_messages_for_responses,
    split_system_messages_for_responses,
)


class _WarningModel(BaseModel):
    usage: dict[str, int]

    @field_serializer("usage")
    def _warn_usage(self, value: dict[str, int]) -> dict[str, int]:
        import warnings

        warnings.warn(
            "PydanticSerializationUnexpectedValue(Expected `ResponseAPIUsage`)",
            UserWarning,
            stacklevel=2,
        )
        return value


def test_response_model_dump_disables_pydantic_warnings_when_supported() -> None:
    class ResponseLike:
        def __init__(self) -> None:
            self.seen_warnings: bool | None = None

        def model_dump(self, *, warnings: bool = True) -> dict[str, object]:
            self.seen_warnings = warnings
            return {"usage": {"completion_tokens": 1}}

    response = ResponseLike()

    assert response_model_dump(response) == {"usage": {"completion_tokens": 1}}
    assert response.seen_warnings is False


def test_response_model_dump_suppresses_pydantic_usage_warning() -> None:
    response = _WarningModel(usage={"completion_tokens": 1})

    assert response_model_dump(response) == {"usage": {"completion_tokens": 1}}


@pytest.mark.asyncio
async def test_responses_stream_emits_provider_liveness_for_status_events() -> None:
    async def _stream():
        yield {"type": "response.created"}

    chunks = [
        chunk
        async for chunk in responses_stream_to_chat_chunks(
            _stream(),
            dedupe_output_text_delta=True,
        )
    ]

    assert chunks == [
        {
            "provider_event": "responses",
            "provider_event_type": "response.created",
        }
    ]


@pytest.mark.asyncio
async def test_responses_stream_failed_event_includes_safe_error_details() -> None:
    async def _stream():
        yield {
            "type": "response.failed",
            "response": {"id": "resp_123", "status": "failed"},
            "error": {
                "type": "server_error",
                "code": "internal_error",
                "message": "Something went wrong",
                "details": "x" * 600,
            },
        }

    chunks = [
        chunk
        async for chunk in responses_stream_to_chat_chunks(
            _stream(),
            dedupe_output_text_delta=True,
        )
    ]

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["mid_stream_failure"] is True
    assert chunk["error"] == "Something went wrong"
    assert chunk["response_error"]["category"] == "provider_5xx"
    assert chunk["response_error"]["response_id"] == "resp_123"
    assert chunk["response_error"]["response_status"] == "failed"
    assert chunk["response_error"]["code"] == "internal_error"
    assert "<truncated" in chunk["response_error"]["details"]["details"]


@pytest.mark.asyncio
async def test_responses_stream_failed_event_preserves_prior_error_event() -> None:
    async def _stream():
        yield {
            "type": "error",
            "code": "internal_error",
            "message": "backend stream crashed",
            "param": "tools",
            "details": "tool schema exploded",
        }
        yield {
            "type": "response.failed",
            "response": {"id": "resp_123", "status": "failed"},
        }

    chunks = [
        chunk
        async for chunk in responses_stream_to_chat_chunks(
            _stream(),
            dedupe_output_text_delta=True,
        )
    ]

    assert len(chunks) == 2
    assert chunks[0]["provider_event_type"] == "error"
    failure_chunk = chunks[1]
    assert failure_chunk["mid_stream_failure"] is True
    assert failure_chunk["error"] == "backend stream crashed"
    response_error = failure_chunk["response_error"]
    assert response_error["category"] == "provider_5xx"
    assert response_error["code"] == "internal_error"
    assert response_error["response_id"] == "resp_123"
    assert response_error["details"]["previous_error_event"] == {
        "event_type": "error",
        "code": "internal_error",
        "message": "backend stream crashed",
        "param": "tools",
        "details": "tool schema exploded",
    }


@pytest.mark.asyncio
async def test_responses_stream_suppresses_unbound_text_delta() -> None:
    async def _stream():
        yield {"type": "response.output_text.delta", "delta": "hello"}

    chunks = [
        chunk
        async for chunk in responses_stream_to_chat_chunks(
            _stream(),
            dedupe_output_text_delta=True,
        )
    ]
    assert chunks[0]["suppressed_output_text_delta"] is True
    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_responses_stream_accumulates_custom_apply_patch_input() -> None:
    patch_text = "*** Begin Patch\n*** Add File: /tmp/a.txt\n+hello\n*** End Patch\n"

    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "custom_tool_call",
                "id": "item_patch",
                "call_id": "call_patch",
                "name": "apply_patch",
                "input": "",
            },
        }
        yield {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "item_patch",
            "delta": patch_text[:24],
        }
        yield {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "item_patch",
            "delta": patch_text[24:],
        }
        yield {
            "type": "response.custom_tool_call_input.done",
            "item_id": "item_patch",
            "input": patch_text,
        }
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "id": "item_patch",
                "call_id": "call_patch",
                "name": "apply_patch",
                "input": patch_text,
            },
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "apply_patch"
    assert calls[0].arguments == {"patchText": patch_text}


@pytest.mark.asyncio
async def test_responses_stream_accumulates_generic_custom_tool_input() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "custom_tool_call",
                "id": "item_custom",
                "call_id": "call_custom",
                "name": "custom_tool",
                "input": "",
            },
        }
        yield {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "item_custom",
            "delta": "abc",
        }
        yield {
            "type": "response.custom_tool_call_input.done",
            "item_id": "item_custom",
            "input": "abc",
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "custom_tool"
    assert calls[0].arguments == {"input": "abc"}


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
                },
            ],
        }
    ]


def test_messages_to_responses_input_downgrades_non_user_multimodal_blocks() -> None:
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "Continue the interrupted turn."},
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
            "role": "system",
            "content": [
                {"type": "input_text", "text": "Continue the interrupted turn."},
                {
                    "type": "input_text",
                    "text": (
                        "[Image attachment omitted from non-user message: "
                        "https://example.com/image.jpg]"
                    ),
                },
                {
                    "type": "input_text",
                    "text": (
                        "[File attachment omitted from non-user message: "
                        "report.pdf, https://example.com/report.pdf]"
                    ),
                },
            ],
        }
    ]


def test_messages_to_responses_input_downgrades_developer_multimodal_blocks() -> None:
    result = messages_to_responses_input(
        [
            {
                "role": "developer",
                "content": [
                    {"type": "text", "text": "Internal note."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/internal.png"},
                    },
                ],
            }
        ]
    )

    assert result == [
        {
            "role": "developer",
            "content": [
                {"type": "input_text", "text": "Internal note."},
                {
                    "type": "input_text",
                    "text": (
                        "[Image attachment omitted from non-user message: "
                        "https://example.com/internal.png]"
                    ),
                },
            ],
        }
    ]


def test_messages_to_responses_input_keeps_call_id_on_function_call_items() -> None:
    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
    )

    assert result == [
        {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def test_messages_to_responses_input_replays_raw_responses_output_items() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "encrypted",
        "summary": [],
    }
    message_item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "phase": "commentary",
        "content": [{"type": "output_text", "text": "I'll inspect that now."}],
    }
    call_item = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "read",
        "arguments": "{}",
    }

    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "content": "I'll inspect that now.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
                "_responses_output_items": [reasoning_item, message_item, call_item],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
    )

    assert result == [
        reasoning_item,
        message_item,
        call_item,
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def test_messages_to_responses_input_pairs_call_id_and_item_id_aliases() -> None:
    call_item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "bash",
        "arguments": '{"command":"touch /tmp/x"}',
    }

    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"touch /tmp/x"}',
                        },
                    }
                ],
                "_responses_output_items": [call_item],
            },
            # Interrupted/recovered paths can persist the output under the raw
            # Responses item id instead of the function call_id.
            {"role": "tool", "tool_call_id": "fc_1", "content": "ok"},
        ]
    )

    assert result == [
        call_item,
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def test_messages_to_responses_input_alias_match_does_not_complete_parallel_call() -> None:
    completed = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read",
        "arguments": "{}",
    }
    interrupted = {
        "type": "function_call",
        "id": "fc_2",
        "call_id": "call_2",
        "name": "grep",
        "arguments": "{}",
    }

    result = messages_to_responses_input(
        [
            {"role": "assistant", "_responses_output_items": [completed, interrupted]},
            {"role": "tool", "tool_call_id": "fc_1", "content": "ok"},
        ]
    )

    assert result == [
        completed,
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def test_messages_to_responses_input_drops_unmaterialized_raw_calls() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_partial",
        "encrypted_content": "encrypted",
    }
    completed_call = {
        "type": "function_call",
        "call_id": "call_completed",
        "name": "read",
        "arguments": "{}",
    }
    interrupted_call = {
        "type": "function_call",
        "call_id": "call_interrupted",
        "name": "grep",
        "arguments": "{}",
    }
    pending_call = {
        "type": "function_call",
        "call_id": "call_pending",
        "name": "glob",
        "arguments": "{}",
    }

    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "content": "I will inspect the repository.",
                "tool_calls": [
                    {
                        "id": "call_completed",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
                "_responses_output_items": [
                    reasoning_item,
                    completed_call,
                    interrupted_call,
                    pending_call,
                ],
            },
            {"role": "tool", "tool_call_id": "call_completed", "content": "ok"},
        ]
    )

    assert result == [
        reasoning_item,
        {"role": "assistant", "content": "I will inspect the repository."},
        completed_call,
        {"type": "function_call_output", "call_id": "call_completed", "output": "ok"},
    ]


def test_messages_to_responses_input_drops_raw_calls_without_any_outputs() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_interrupted",
        "encrypted_content": "encrypted",
    }

    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "content": "Starting the requested work.",
                "_responses_output_items": [
                    reasoning_item,
                    {
                        "type": "function_call",
                        "call_id": "call_first",
                        "name": "read",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_second",
                        "name": "grep",
                        "arguments": "{}",
                    },
                    {
                        "type": "custom_tool_call",
                        "call_id": "call_custom",
                        "name": "apply_patch",
                        "input": "*** Begin Patch\n*** End Patch\n",
                    },
                    {
                        "type": "apply_patch_call",
                        "call_id": "call_patch",
                        "status": "completed",
                        "operation": {
                            "type": "update_file",
                            "path": "/tmp/a.txt",
                            "diff": "@@\n-old\n+new\n",
                        },
                    },
                ],
            }
        ]
    )

    assert result == [
        reasoning_item,
        {"role": "assistant", "content": "Starting the requested work."},
    ]


def test_messages_to_responses_input_drops_reconstructed_call_without_output() -> None:
    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "content": "Starting inspection.",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            }
        ]
    )

    assert result == [{"role": "assistant", "content": "Starting inspection."}]


def test_messages_to_responses_input_does_not_pair_output_before_call() -> None:
    result = messages_to_responses_input(
        [
            {"role": "tool", "tool_call_id": "call_read", "content": "stale output"},
            {
                "role": "assistant",
                "content": "Starting inspection.",
                "_responses_output_items": [
                    {
                        "type": "function_call",
                        "call_id": "call_read",
                        "name": "read",
                        "arguments": "{}",
                    }
                ],
            },
        ]
    )

    assert result == [{"role": "assistant", "content": "Starting inspection."}]


def test_messages_to_responses_input_pairs_only_latest_duplicate_call_id() -> None:
    first_call = {
        "type": "function_call",
        "call_id": "call_reused",
        "name": "read",
        "arguments": '{"file_path":"old.py"}',
    }
    second_call = {
        "type": "function_call",
        "call_id": "call_reused",
        "name": "read",
        "arguments": '{"file_path":"new.py"}',
    }

    result = messages_to_responses_input(
        [
            {"role": "assistant", "_responses_output_items": [first_call]},
            {"role": "tool", "tool_call_id": "call_reused", "content": "old output"},
            {"role": "assistant", "_responses_output_items": [second_call]},
            {"role": "tool", "tool_call_id": "call_reused", "content": "new output"},
        ]
    )

    assert result == [
        second_call,
        {
            "type": "function_call_output",
            "call_id": "call_reused",
            "output": "new output",
        },
    ]


def test_messages_to_responses_input_keeps_completed_raw_multi_call_response() -> None:
    first_call = {
        "type": "function_call",
        "call_id": "call_first",
        "name": "read",
        "arguments": "{}",
    }
    second_call = {
        "type": "function_call",
        "call_id": "call_second",
        "name": "grep",
        "arguments": "{}",
    }

    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "_responses_output_items": [first_call, second_call],
            },
            {"role": "tool", "tool_call_id": "call_first", "content": "read result"},
            {"role": "tool", "tool_call_id": "call_second", "content": "grep result"},
        ]
    )

    assert result == [
        first_call,
        second_call,
        {
            "type": "function_call_output",
            "call_id": "call_first",
            "output": "read result",
        },
        {
            "type": "function_call_output",
            "call_id": "call_second",
            "output": "grep result",
        },
    ]


def test_messages_to_responses_input_reconstructs_durable_payload_snapshot() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "I will inspect the file.",
            "tool_calls": [
                {
                    "id": "call_read",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"file_path": "a.py"}'},
                }
            ],
            "_responses_output_items": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "opaque-ciphertext",
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_read",
                    "name": "read",
                    "arguments": '{"file_path": "a.py"}',
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_read", "content": "line 1"},
    ]

    assert messages_to_responses_input(messages) == [
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "opaque-ciphertext",
        },
        {"role": "assistant", "content": "I will inspect the file."},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_read",
            "name": "read",
            "arguments": '{"file_path": "a.py"}',
        },
        {"type": "function_call_output", "call_id": "call_read", "output": "line 1"},
    ]


def test_messages_to_responses_input_backfills_missing_raw_tool_call_snapshot() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "Calling grep.",
            "tool_calls": [
                {
                    "id": "call_grep",
                    "type": "function",
                    "function": {"name": "grep", "arguments": '{"pattern": "x"}'},
                }
            ],
            "_responses_output_items": [
                {"type": "reasoning", "id": "rs_2", "encrypted_content": "opaque"}
            ],
        },
        {"role": "tool", "tool_call_id": "call_grep", "content": "match"},
    ]

    assert messages_to_responses_input(messages) == [
        {"type": "reasoning", "id": "rs_2", "encrypted_content": "opaque"},
        {"role": "assistant", "content": "Calling grep."},
        {
            "type": "function_call",
            "call_id": "call_grep",
            "name": "grep",
            "arguments": '{"pattern": "x"}',
        },
        {"type": "function_call_output", "call_id": "call_grep", "output": "match"},
    ]


def test_messages_to_responses_input_backfills_missing_native_apply_patch_snapshot() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_patch",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": (
                            '{"operation":{"type":"update_file","path":"/tmp/a.txt","diff":"@@\\n-x\\n+y\\n"}}'
                        ),
                    },
                }
            ],
            "_responses_output_items": [
                {"type": "reasoning", "id": "rs_patch", "encrypted_content": "opaque"}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_patch",
            "content": "Updated /tmp/a.txt",
            "_tool_name": "apply_patch",
        },
    ]

    assert messages_to_responses_input(messages) == [
        {"type": "reasoning", "id": "rs_patch", "encrypted_content": "opaque"},
        {
            "type": "apply_patch_call",
            "call_id": "call_patch",
            "status": "completed",
            "operation": {"type": "update_file", "path": "/tmp/a.txt", "diff": "@@\n-x\n+y\n"},
        },
        {
            "type": "apply_patch_call_output",
            "call_id": "call_patch",
            "status": "completed",
            "output": "Updated /tmp/a.txt",
        },
    ]


def test_messages_to_responses_input_replays_custom_tool_output_items() -> None:
    call_item = {
        "type": "custom_tool_call",
        "call_id": "call_custom",
        "name": "apply_patch",
        "input": "*** Begin Patch\n*** End Patch\n",
    }

    result = messages_to_responses_input(
        [
            {
                "role": "assistant",
                "_responses_output_items": [call_item],
            },
            {"role": "tool", "tool_call_id": "call_custom", "content": "ok"},
        ],
    )

    assert result == [
        call_item,
        {"type": "custom_tool_call_output", "call_id": "call_custom", "output": "ok"},
    ]


def test_messages_to_responses_input_drops_orphan_tool_output() -> None:
    result = messages_to_responses_input(
        [
            {"role": "assistant", "content": "saved work"},
            {"role": "tool", "tool_call_id": "call_missing", "content": "ok"},
        ],
    )

    assert result == [{"role": "assistant", "content": "saved work"}]


def test_messages_to_responses_input_preserves_system_role() -> None:
    messages = [{"role": "system", "content": "Follow system instructions."}]

    result = messages_to_responses_input(messages)

    assert result == [{"role": "system", "content": "Follow system instructions."}]


def test_messages_to_responses_input_drops_filename_when_file_id_present() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "file",
                    "file": {
                        "file_id": "file-123",
                        "filename": "report.pdf",
                        "file_url": "https://example.com/report.pdf",
                    },
                }
            ],
        }
    ]

    result = messages_to_responses_input(messages)

    assert result == [{"role": "user", "content": [{"type": "input_file", "file_id": "file-123"}]}]


def test_messages_to_responses_input_normalizes_existing_input_file_with_file_id() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "file_id": "file-123",
                    "filename": "report.pdf",
                    "file_url": "https://example.com/report.pdf",
                }
            ],
        }
    ]

    result = messages_to_responses_input(messages)

    assert result == [{"role": "user", "content": [{"type": "input_file", "file_id": "file-123"}]}]


def test_messages_to_responses_input_drops_filename_for_existing_input_file_url() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "file_url": "https://example.com/report.pdf",
                    "filename": "report.pdf",
                }
            ],
        }
    ]

    result = messages_to_responses_input(messages)

    assert result == [
        {
            "role": "user",
            "content": [{"type": "input_file", "file_url": "https://example.com/report.pdf"}],
        }
    ]


def test_split_messages_for_responses_extracts_prefix_into_instructions() -> None:
    messages = [
        {"role": "system", "content": "Immutable persona block.", "_immutable_prefix": True},
        {"role": "system", "content": "Project AGENTS.md"},
        {"role": "user", "content": "Hello"},
    ]

    instructions, tail = split_messages_for_responses(messages, cache_breakpoint_index=0)

    assert instructions == "Immutable persona block."
    assert tail == [
        {"role": "system", "content": "Project AGENTS.md"},
        {"role": "user", "content": "Hello"},
    ]


def test_split_messages_for_responses_accepts_cache_breakpoint_list() -> None:
    messages = [
        {"role": "system", "content": "Immutable persona block."},
        {"role": "user", "content": "Earlier history"},
        {"role": "system", "content": "Mutable reminder"},
        {"role": "user", "content": "Hello"},
    ]

    instructions, tail = split_messages_for_responses(
        messages,
        cache_breakpoint_index=[
            {"index": 0, "ttl": "5m"},
            {"index": 2, "ttl": "5m"},
            {"index": 3, "ttl": "5m"},
        ],
    )

    assert instructions == "Immutable persona block."
    assert tail == [
        {"role": "user", "content": "Earlier history"},
        {"role": "system", "content": "Mutable reminder"},
        {"role": "user", "content": "Hello"},
    ]


def test_split_messages_for_responses_joins_multiple_prefix_messages() -> None:
    messages = [
        {"role": "system", "content": "Block A"},
        {"role": "system", "content": "Block B"},
        {"role": "user", "content": "Hello"},
    ]

    instructions, tail = split_messages_for_responses(messages, cache_breakpoint_index=1)

    assert instructions == "Block A\n\nBlock B"
    assert tail == [{"role": "user", "content": "Hello"}]


def test_split_messages_for_responses_handles_content_blocks() -> None:
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "Block A"},
                {"type": "text", "text": "Block B"},
            ],
        },
        {"role": "user", "content": "Hello"},
    ]

    instructions, tail = split_messages_for_responses(messages, cache_breakpoint_index=0)

    assert instructions == "Block A\n\nBlock B"
    assert tail == [{"role": "user", "content": "Hello"}]


def test_split_messages_for_responses_returns_none_without_breakpoint() -> None:
    messages = [
        {"role": "system", "content": "Hello"},
        {"role": "user", "content": "Hi"},
    ]

    instructions, tail = split_messages_for_responses(messages, cache_breakpoint_index=None)

    assert instructions is None
    assert tail == messages


def test_split_system_messages_for_responses_extracts_leading_system_prefix() -> None:
    messages = [
        {"role": "system", "content": "Persona"},
        {"role": "system", "content": [{"type": "text", "text": "Project"}]},
        {"role": "user", "content": "Hi"},
    ]

    instructions, tail = split_system_messages_for_responses(messages)

    assert instructions == "Persona\n\nProject"
    assert tail == [{"role": "user", "content": "Hi"}]


def test_split_system_messages_for_responses_preserves_later_system_messages() -> None:
    messages = [
        {"role": "system", "content": "Persona"},
        {"role": "user", "content": "Hi"},
        {"role": "system", "content": "Late system note"},
    ]

    instructions, tail = split_system_messages_for_responses(messages)

    assert instructions == "Persona"
    assert tail == [
        {"role": "user", "content": "Hi"},
        {"role": "system", "content": "Late system note"},
    ]


def test_split_messages_for_responses_refuses_non_system_prefix() -> None:
    messages = [
        {"role": "system", "content": "Persona"},
        {"role": "user", "content": "Mixed"},
        {"role": "user", "content": "Tail"},
    ]

    instructions, tail = split_messages_for_responses(messages, cache_breakpoint_index=1)

    assert instructions is None
    assert tail == messages


def test_split_messages_for_responses_returns_none_for_empty_prefix() -> None:
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": "Hi"},
    ]

    instructions, tail = split_messages_for_responses(messages, cache_breakpoint_index=0)

    assert instructions is None
    assert tail == messages


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


def test_responses_to_chat_response_does_not_fallback_to_top_level_output_text() -> None:
    payload = {
        "status": "completed",
        "output_text": "Need inspect implementation.",
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


def test_responses_to_chat_response_normalizes_apply_patch_call() -> None:
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "apply_patch_call",
                "id": "apc_1",
                "call_id": "call_patch",
                "operation": {"type": "update_file", "path": "/tmp/a.txt", "diff": "@@\n-x\n+y\n"},
            }
        ],
    }

    result = responses_to_chat_response(payload)

    tool_call = result["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "call_patch"
    assert tool_call["function"]["name"] == "apply_patch"
    assert tool_call["function"]["arguments"] == "[patch body omitted]"


def test_responses_to_chat_response_deduplicates_output_and_choices_tool_call_by_id() -> None:
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "item_123",
                "call_id": "call_123",
                "name": "delegate",
                "arguments": '{"task":"inspect"}',
            }
        ],
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "function": {
                                "name": "incorrect_compatibility_call",
                                "arguments": '{"task":"incorrect"}',
                            },
                        },
                        {
                            "id": "call_456",
                            "function": {
                                "name": "delegate",
                                "arguments": '{"task":"other"}',
                            },
                        },
                    ]
                }
            }
        ],
    }

    result = responses_to_chat_response(payload)

    assert result["choices"][0]["message"]["tool_calls"] == [
        {
            "id": "call_123",
            "type": "function",
            "function": {"name": "delegate", "arguments": '{"task":"inspect"}'},
        },
        {
            "id": "call_456",
            "type": "function",
            "function": {"name": "delegate", "arguments": '{"task":"other"}'},
        },
    ]


def test_responses_to_chat_response_drops_incomplete_apply_patch_arguments() -> None:
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "apply_patch_call",
                "id": "apc_1",
                "call_id": "call_patch",
                "operation": {"type": "update_file", "path": "", "diff": "@@\n-x\n+y\n"},
            }
        ],
    }

    result = responses_to_chat_response(payload)

    tool_call = result["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "apply_patch"
    assert tool_call["function"]["arguments"] == "{}"


def test_responses_to_chat_response_maps_native_create_file_apply_patch_call() -> None:
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "apply_patch_call",
                "id": "apc_1",
                "call_id": "call_patch",
                "operation": {"type": "create_file", "path": "/tmp/a.txt", "content": "hi\n"},
            }
        ],
    }

    result = responses_to_chat_response(payload)

    tool_call = result["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "apply_patch"
    assert json.loads(tool_call["function"]["arguments"]) == {
        "patchText": "*** Begin Patch\n*** Add File: /tmp/a.txt\n+hi\n*** End Patch\n"
    }


def test_messages_to_responses_input_drops_tool_messages_without_tool_call_id() -> None:
    result = messages_to_responses_input(
        [
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "content": "hello"},
        ]
    )

    assert result == [{"role": "assistant", "content": ""}]


def test_messages_to_responses_input_maps_native_apply_patch_roundtrip() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_patch",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": (
                            '{"operation":{"type":"update_file","path":"/tmp/a.txt","diff":"@@\\n-x\\n+y\\n"}}'
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_patch",
            "content": "Updated /tmp/a.txt",
            "_tool_name": "apply_patch",
            "_tool_is_error": False,
        },
    ]

    result = messages_to_responses_input(messages)

    assert result == [
        {
            "type": "apply_patch_call",
            "call_id": "call_patch",
            "status": "completed",
            "operation": {"type": "update_file", "path": "/tmp/a.txt", "diff": "@@\n-x\n+y\n"},
        },
        {
            "type": "apply_patch_call_output",
            "call_id": "call_patch",
            "status": "completed",
            "output": "Updated /tmp/a.txt",
        },
    ]


def test_messages_to_responses_input_does_not_replay_invalid_native_apply_patch() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_patch",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": '{"operation":{"type":"update_file","path":"","diff":"@@\\n-x\\n+y\\n"}}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_patch",
            "content": "Native apply_patch operation requires a path.",
            "_tool_name": "apply_patch",
            "_tool_is_error": True,
        },
    ]

    result = messages_to_responses_input(messages)

    assert result[0] == {
        "type": "function_call",
        "call_id": "call_patch",
        "name": "apply_patch",
        "arguments": '{"operation":{"type":"update_file","path":"","diff":"@@\\n-x\\n+y\\n"}}',
    }
    assert result[1] == {
        "type": "function_call_output",
        "call_id": "call_patch",
        "output": "Native apply_patch operation requires a path.",
    }


def test_messages_to_responses_input_marks_failed_native_apply_patch_output() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_patch",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": (
                            '{"operation":{"type":"update_file","path":"/tmp/a.txt","diff":"@@\\n-x\\n+y\\n"}}'
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_patch",
            "content": "Native apply_patch update_file operation requires a diff.",
            "_tool_name": "apply_patch",
            "_tool_is_error": True,
        },
    ]

    result = messages_to_responses_input(messages)

    assert result[0]["type"] == "apply_patch_call"
    assert result[0]["status"] == "completed"
    assert result[1] == {
        "type": "apply_patch_call_output",
        "call_id": "call_patch",
        "status": "failed",
        "output": "Native apply_patch update_file operation requires a diff.",
    }


def test_responses_request_kwargs_ignores_unsupported_string_response_format() -> None:
    result = responses_request_kwargs({"response_format": "xml"})

    assert "text" not in result


def test_responses_request_kwargs_maps_reasoning_effort_to_reasoning_summary_auto() -> None:
    result = responses_request_kwargs({"reasoning_effort": "low"})

    assert "reasoning_effort" not in result
    assert result["reasoning"] == {"effort": "low", "summary": "auto"}
    assert result["include"] == ["reasoning.encrypted_content"]


def test_responses_request_kwargs_omits_disabled_reasoning_summary() -> None:
    result = responses_request_kwargs({"reasoning_effort": "low"}, default_reasoning_summary="none")

    assert result["reasoning"] == {"effort": "low"}
    assert result["include"] == ["reasoning.encrypted_content"]


def test_responses_request_kwargs_preserves_explicit_reasoning_summary() -> None:
    result = responses_request_kwargs(
        {"reasoning_effort": "medium", "reasoning": {"summary": "detailed"}}
    )

    assert result["reasoning"] == {"effort": "medium", "summary": "detailed"}
    assert result["include"] == ["reasoning.encrypted_content"]


def test_responses_request_kwargs_can_include_encrypted_reasoning_without_reasoning_override() -> (
    None
):
    result = responses_request_kwargs({}, include_encrypted_reasoning=True)

    assert result["include"] == ["reasoning.encrypted_content"]
    assert "reasoning" not in result


def test_responses_request_kwargs_applies_default_text_verbosity() -> None:
    result = responses_request_kwargs({}, default_text_verbosity="low")

    assert result["text"] == {"verbosity": "low"}


def test_responses_request_kwargs_preserves_explicit_text_verbosity() -> None:
    result = responses_request_kwargs(
        {"text": {"verbosity": "high", "format": {"type": "text"}}},
        default_text_verbosity="low",
    )

    assert result["text"] == {"verbosity": "high", "format": {"type": "text"}}


def test_responses_request_kwargs_defaults_apply_patch_to_freeform_tool() -> None:
    result = responses_request_kwargs(
        {"tools": [{"type": "apply_patch"}, {"type": "function", "function": {"name": "read"}}]}
    )

    assert result["tools"][0]["type"] == "custom"
    assert result["tools"][0]["name"] == "apply_patch"
    assert result["tools"][0]["format"]["type"] == "grammar"


def test_responses_request_kwargs_maps_freeform_apply_patch_to_custom_tool() -> None:
    result = responses_request_kwargs(
        {
            "cognis_openai_apply_patch_tool_type": "freeform",
            "tools": [{"type": "apply_patch"}],
        }
    )

    assert result["tools"][0]["type"] == "custom"
    assert result["tools"][0]["name"] == "apply_patch"
    assert result["tools"][0]["format"]["type"] == "grammar"
    assert "Begin Patch" in result["tools"][0]["format"]["definition"]


def test_responses_to_chat_response_prefers_input_output_usage_fields() -> None:
    payload = {
        "status": "completed",
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "input_tokens": 7,
            "output_tokens": 8,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    }

    result = responses_to_chat_response(payload)

    assert result["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 8,
        "total_tokens": 15,
        "input_tokens": 7,
        "output_tokens": 8,
        "input_tokens_details": {"cached_tokens": 5},
        "output_tokens_details": {"reasoning_tokens": 3},
        "cached_tokens": 5,
        "reasoning_tokens": 3,
    }


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
async def test_responses_stream_usage_total_tokens_falls_back_to_input_plus_output() -> None:
    async def _stream():
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 7, "output_tokens": 8},
            },
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[-1]["usage"]["total_tokens"] == 15


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
async def test_responses_stream_to_chat_chunks_does_not_fallback_to_completed_output_text() -> None:
    async def _stream():
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output_text": "Need inspect implementation.",
                "usage": {"total_tokens": 5},
            },
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_suppresses_unbound_output_text_delta() -> None:
    async def _stream():
        yield {"type": "response.output_text.delta", "delta": "Need inspect implementation."}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert chunks[0]["suppressed_output_text_delta"] is True
    assert chunks[0]["suppressed_output_text_delta_chars"] == len("Need inspect implementation.")
    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_suppresses_unbound_output_text_done() -> None:
    async def _stream():
        yield {"type": "response.output_text.delta", "delta": "Need inspect implementation."}
        yield {"type": "response.output_text.done", "text": "Need inspect implementation."}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert sum(1 for chunk in chunks if chunk.get("suppressed_output_text_delta") is True) == 2
    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_preserves_message_bound_output_text_delta() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": "I'll inspect that now.",
        }
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item_read",
                "call_id": "call_read",
                "name": "read",
                "arguments": "{}",
            },
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    content_deltas = [
        (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert content_deltas == ["I'll inspect that now."]
    message_chunks = [
        chunk
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert message_chunks[0]["response_item_id"] == "msg_1"
    assert message_chunks[0]["response_item_type"] == "message"
    assert message_chunks[0]["content_source"] == "response.output_text.delta"
    assert any(
        (choice.get("delta") or {}).get("tool_calls")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_preserves_message_phase_metadata() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "message",
                "id": "msg_commentary",
                "phase": "commentary",
                "content": [],
            },
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_commentary",
            "delta": "I found the cause.",
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    message_chunks = [
        chunk
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert len(message_chunks) == 1
    assert message_chunks[0]["response_item_id"] == "msg_commentary"
    assert message_chunks[0]["response_item_type"] == "message"
    assert message_chunks[0]["response_message_phase"] == "commentary"
    assert message_chunks[0]["content_source"] == "response.output_text.delta"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_keeps_standard_duplicate_output_text_delta() -> None:
    repeated_delta = (
        "This is a long assistant response segment that is large enough to be "
        "classified as a replayed provider delta rather than intentional prose."
    )

    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": repeated_delta,
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": repeated_delta,
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    content_deltas = [
        (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert content_deltas == [repeated_delta, repeated_delta]


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_can_dedupe_cumulative_output_text_delta() -> None:
    repeated_delta = (
        "This is a long assistant response segment that is large enough to be "
        "classified as a replayed provider delta rather than intentional prose."
    )

    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": repeated_delta,
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": repeated_delta,
        }

    chunks = [
        chunk
        async for chunk in responses_stream_to_chat_chunks(
            _stream(),
            dedupe_output_text_delta=True,
        )
    ]
    content_deltas = [
        choice["delta"]["content"]
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if choice.get("delta", {}).get("content")
    ]
    assert content_deltas == [repeated_delta]
    assert sum(1 for chunk in chunks if chunk.get("suppressed_output_text_delta") is True) == 1


@pytest.mark.asyncio
async def test_response_completed_tool_fallback_emits_raw_function_call_item() -> None:
    raw_call = {
        "type": "function_call",
        "id": "fc_done",
        "call_id": "call_done",
        "name": "read",
        "arguments": '{"file_path": "a.py"}',
    }

    async def _stream():
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "output": [raw_call]},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    raw_items = [
        chunk["responses_output_item"] for chunk in chunks if "responses_output_item" in chunk
    ]
    assert raw_items == [
        {
            "type": "function_call",
            "id": "call_done",
            "call_id": "call_done",
            "name": "read",
            "arguments": '{"file_path": "a.py"}',
        }
    ]
    assert any(
        choice.get("finish_reason") == "tool_calls"
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_response_completed_tool_fallback_dedupes_streamed_raw_item() -> None:
    raw_call = {
        "type": "function_call",
        "id": "fc_streamed",
        "call_id": "call_streamed",
        "name": "read",
        "arguments": '{"file_path": "a.py"}',
    }

    async def _stream():
        yield {"type": "response.output_item.done", "item": raw_call}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "output": [raw_call]},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    raw_items = [
        chunk["responses_output_item"] for chunk in chunks if "responses_output_item" in chunk
    ]

    assert raw_items == [raw_call]


@pytest.mark.asyncio
async def test_response_completed_reasoning_fallback_emits_raw_reasoning_item() -> None:
    raw_reasoning = {
        "type": "reasoning",
        "id": "rs_done",
        "encrypted_content": "opaque-ciphertext",
    }

    async def _stream():
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [raw_reasoning, {"type": "message", "id": "msg_done"}],
            },
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert any(chunk.get("responses_output_item") == raw_reasoning for chunk in chunks)


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_emits_only_suffix_for_cumulative_delta() -> None:
    first_delta = (
        "The first part of the answer is already long enough to distinguish a "
        "provider snapshot from normal repeated wording in the assistant text."
    )
    suffix = " Only this suffix should be emitted."

    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": first_delta,
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": first_delta + suffix,
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [
        chunk
        async for chunk in responses_stream_to_chat_chunks(
            _stream(),
            dedupe_output_text_delta=True,
        )
    ]

    content_deltas = [
        (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert content_deltas == [first_delta, suffix]


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_emits_only_suffix_for_overlapping_delta() -> None:
    first_delta = (
        "The assistant has already produced a substantial paragraph with enough "
        "content to make an overlapping replay detectable before more text arrives."
    )
    overlap = first_delta[-96:]
    suffix = " This new sentence should survive overlap trimming."

    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": first_delta,
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": overlap + suffix,
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [
        chunk
        async for chunk in responses_stream_to_chat_chunks(
            _stream(),
            dedupe_output_text_delta=True,
        )
    ]

    content_deltas = [
        (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert content_deltas == [first_delta, suffix]


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_preserves_short_repeated_output_text_delta() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": "ha",
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": "ha",
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    content_deltas = [
        (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert content_deltas == ["ha", "ha"]


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_preserves_message_bound_output_text_done_suffix() -> (
    None
):
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": "I'll inspect",
        }
        yield {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "text": "I'll inspect that now.",
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    content_deltas = [
        (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert content_deltas == ["I'll inspect", " that now."]


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_dedupes_repeated_terminal_text_snapshots() -> None:
    report = (
        "Fixed and committed.\n\n"
        "Commit:\n"
        "- `0529726c fix(chat): preserve Chat v2 message timestamps`\n\n"
        "Validation:\n"
        "- Backend focused tests passed\n"
        "- UI focused tests passed\n"
    )

    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": "Done.",
        }
        yield {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "text": report,
        }
        yield {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "part": {"type": "output_text", "text": report},
        }
        yield {
            "type": "response.content_part.done",
            "item_id": "msg_1",
            "part": {"type": "output_text", "text": report},
        }
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "msg_1",
                "content": [{"type": "output_text", "text": report}],
            },
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    content = acc.get_content()
    assert content == f"Done.{report}"
    assert content.count("Fixed and committed.") == 1


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_dedupes_short_terminal_text_snapshots() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": "Done. ",
        }
        yield {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "text": "Saved.",
        }
        yield {
            "type": "response.content_part.done",
            "item_id": "msg_1",
            "part": {"type": "output_text", "text": "Saved."},
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    assert acc.get_content() == "Done. Saved."


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_suppresses_unbound_content_part_text() -> None:
    async def _stream():
        yield {
            "type": "response.content_part.added",
            "part": {"type": "output_text", "text": "Need inspect implementation."},
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert any(chunk.get("suppressed_content_part_text") is True for chunk in chunks)
    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_preserves_message_bound_content_part_text() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "part": {"type": "output_text", "text": "I'll inspect that now."},
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    content_deltas = [
        (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if (choice.get("delta") or {}).get("content")
    ]
    assert content_deltas == ["I'll inspect that now."]


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_suppresses_tool_bound_content_part_text() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item_read",
                "call_id": "call_read",
                "name": "read",
                "arguments": "{}",
            },
        }
        yield {
            "type": "response.content_part.done",
            "item_id": "item_read",
            "part": {"type": "output_text", "text": "Need inspect implementation."},
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert any(chunk.get("suppressed_content_part_text") is True for chunk in chunks)
    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_suppresses_tool_bound_output_text_delta() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item_read",
                "call_id": "call_read",
                "name": "read",
                "arguments": "{}",
            },
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "item_read",
            "delta": "Need inspect implementation.",
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert any(chunk.get("suppressed_output_text_delta") is True for chunk in chunks)
    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_suppresses_tool_bound_output_text_done() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item_read",
                "call_id": "call_read",
                "name": "read",
                "arguments": "{}",
            },
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "item_read",
            "delta": "Need inspect implementation.",
        }
        yield {
            "type": "response.output_text.done",
            "item_id": "item_read",
            "text": "Need inspect implementation.",
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]

    assert sum(1 for chunk in chunks if chunk.get("suppressed_output_text_delta") is True) == 2
    assert all(
        not (choice.get("delta") or {}).get("content")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )


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
async def test_responses_stream_to_chat_chunks_emits_raw_output_items_for_continuation() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "encrypted",
        "summary": [],
    }
    call_item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read",
        "arguments": "{}",
    }

    async def _stream():
        yield {"type": "response.output_item.done", "item": reasoning_item}
        yield {"type": "response.output_item.done", "item": call_item}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 9}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    raw_items = [
        chunk["responses_output_item"] for chunk in chunks if "responses_output_item" in chunk
    ]

    assert raw_items == [reasoning_item, call_item]


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
async def test_responses_stream_to_chat_chunks_emits_apply_patch_call() -> None:
    async def _stream():
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "apply_patch_call",
                "id": "apc_done",
                "call_id": "call_patch",
                "operation": {"type": "update_file", "path": "/tmp/a.txt", "diff": "@@\n-x\n+y\n"},
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
    assert calls[0].name == "apply_patch"
    assert calls[0].call_id == "call_patch"
    assert isinstance(calls[0], ToolArgumentParseFailure)
    assert calls[0].raw == "[patch body omitted]"


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_emits_custom_apply_patch_call() -> None:
    patch_text = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch\n"

    async def _stream():
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": patch_text,
                "call_id": "call_patch",
            },
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "apply_patch"
    assert calls[0].call_id == "call_patch"
    assert calls[0].arguments == {"patchText": patch_text}


@pytest.mark.asyncio
async def test_responses_stream_to_chat_chunks_emits_custom_apply_patch_progress() -> None:
    patch_text = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch\n"

    async def _stream():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "",
                "call_id": "call_patch",
            },
        }
        yield {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "call_patch",
            "delta": patch_text[:20],
        }
        yield {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "call_patch",
            "delta": patch_text[20:],
        }
        yield {
            "type": "response.custom_tool_call_input.done",
            "item_id": "call_patch",
            "input": patch_text,
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}

    progress_events: list[dict[str, object]] = []
    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)
        delta = chunk.get("choices", [{}])[0].get("delta", {})
        if isinstance(delta, dict) and isinstance(delta.get("tool_progress"), dict):
            progress_events.append(delta["tool_progress"])

    assert progress_events
    assert progress_events[-1]["name"] == "apply_patch"
    assert progress_events[-1]["input_chars"] == len(patch_text)
    assert progress_events[-1]["input_lines"] == patch_text.count("\n") + 1
    assert progress_events[-1]["complete"] is True
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].arguments == {"patchText": patch_text}


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
                    '{"todos":[content":"Find the Cognis Todoist project and '
                    'appropriate section","status":"in_progress"}]}'
                    '{"todos":[{"content":"Find the Cognis Todoist project and '
                    'appropriate section","status":"in_progress"},{"content":'
                    '"Create the Todoist task for Monday in the Cognis project",'
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
                "content": "Find the Cognis Todoist project and appropriate section",
                "status": "in_progress",
            },
            {
                "content": "Create the Todoist task for Monday in the Cognis project",
                "status": "pending",
            },
        ]
    }


@pytest.mark.asyncio
async def test_reasoning_delta_streams_repeated_short_token() -> None:
    """A short delta matching the emitted suffix must not be swallowed (A1)."""

    async def _stream():
        yield {"type": "response.reasoning_text.delta", "delta": "I am very"}
        yield {"type": "response.reasoning_text.delta", "delta": " very"}
        yield {"type": "response.reasoning_text.delta", "delta": " sure."}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    text = "".join(
        chunk["choices"][0]["delta"].get("reasoning_content") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    assert text == "I am very very sure."


@pytest.mark.asyncio
async def test_reasoning_summary_delta_streams_repeated_short_token() -> None:
    async def _stream():
        yield {"type": "response.reasoning_summary_text.delta", "delta": "step by"}
        yield {"type": "response.reasoning_summary_text.delta", "delta": " step"}
        yield {"type": "response.reasoning_summary_text.delta", "delta": " step"}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    text = "".join(
        chunk["choices"][0]["delta"].get("reasoning") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    assert text == "step by step step"


@pytest.mark.asyncio
async def test_identical_repeated_reasoning_summary_parts_both_emitted() -> None:
    """Two summary parts with identical done-only text must both survive (A2)."""

    async def _stream():
        yield {"type": "response.reasoning_summary_part.added", "part": {}}
        yield {"type": "response.reasoning_summary_text.done", "text": "Analyzing the code"}
        yield {"type": "response.reasoning_summary_part.done", "part": {}}
        yield {"type": "response.reasoning_summary_part.added", "part": {}}
        yield {"type": "response.reasoning_summary_text.done", "text": "Analyzing the code"}
        yield {"type": "response.reasoning_summary_part.done", "part": {}}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    summary_texts = [
        chunk["choices"][0]["delta"].get("reasoning")
        for chunk in chunks
        if chunk.get("choices") and chunk["choices"][0]["delta"].get("reasoning")
    ]
    assert summary_texts == ["Analyzing the code", "Analyzing the code"]


@pytest.mark.asyncio
async def test_reasoning_part_boundary_emitted_before_carried_text() -> None:
    """Text carried on part.added must arrive after the boundary marker (A3)."""

    async def _stream():
        yield {"type": "response.reasoning_summary_part.added", "part": {"text": "First part"}}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    kinds: list[str] = []
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_part_boundary"):
                kinds.append("boundary")
            if delta.get("reasoning"):
                kinds.append("text")
    assert kinds[:2] == ["boundary", "text"]


@pytest.mark.asyncio
async def test_summary_done_after_deltas_does_not_duplicate() -> None:
    """A .done replay of already-streamed part text stays deduplicated."""

    async def _stream():
        yield {"type": "response.reasoning_summary_part.added", "part": {}}
        yield {"type": "response.reasoning_summary_text.delta", "delta": "Plan "}
        yield {"type": "response.reasoning_summary_text.delta", "delta": "steps"}
        yield {"type": "response.reasoning_summary_text.done", "text": "Plan steps"}
        yield {"type": "response.reasoning_summary_part.done", "part": {}}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    text = "".join(
        chunk["choices"][0]["delta"].get("reasoning") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    assert text == "Plan steps"


@pytest.mark.asyncio
async def test_completed_fallback_dedups_against_all_emitted_parts() -> None:
    """Completed fallback must not re-emit text already streamed across parts."""

    async def _stream():
        yield {"type": "response.reasoning_summary_part.added", "part": {}}
        yield {"type": "response.reasoning_summary_text.done", "text": "Part one"}
        yield {"type": "response.reasoning_summary_part.done", "part": {}}
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Part one"}],
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        }

    chunks = [chunk async for chunk in responses_stream_to_chat_chunks(_stream())]
    summary_texts = [
        chunk["choices"][0]["delta"].get("reasoning")
        for chunk in chunks
        if chunk.get("choices") and chunk["choices"][0]["delta"].get("reasoning")
    ]
    assert summary_texts == ["Part one"]


@pytest.mark.asyncio
async def test_function_call_arguments_done_recovers_dropped_deltas() -> None:
    """The real API sends item_id+arguments (no nested item) on .done.

    When argument deltas were dropped before item registration, the done
    event must recover the full arguments instead of being a no-op.
    """

    async def _stream():
        # No output_item.added and no argument deltas — everything before the
        # done event was lost/suppressed by the transport.
        yield {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_123",
            "arguments": '{"path": "/tmp/foo.txt"}',
        }
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_123",
                "call_id": "call_123",
                "name": "read",
                "arguments": '{"path": "/tmp/foo.txt"}',
            },
        }
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"total_tokens": 5}},
        }

    acc = StreamAccumulator()
    async for chunk in responses_stream_to_chat_chunks(_stream()):
        acc.feed(chunk)

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "read"
    assert calls[0].arguments == {"path": "/tmp/foo.txt"}
