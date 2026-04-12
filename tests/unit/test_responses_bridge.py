from __future__ import annotations

import pytest

from cognis.providers.llm.responses_bridge import (
    messages_to_responses_input,
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
