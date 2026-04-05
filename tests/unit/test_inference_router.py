from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.providers.llm.inference_router import InferenceRouter


class _Connection:
    async def llm_complete_stream(self, **_: object):
        yield {"content": "Hello", "tool_calls": None, "reasoning_content": None, "index": 0}
        yield {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "search_tools", "arguments": '{"query":"docs"}'},
                }
            ],
            "reasoning_content": None,
            "index": 1,
        }
        yield {"done": True, "usage": {"total_tokens": 9}, "finish_reason": "stop"}


class _Provider:
    def __init__(self) -> None:
        self.connection = _Connection()

    async def list_active(self):
        return [SimpleNamespace(executor_id="exec-1", metadata={"labels": {"location": "local"}})]

    async def get_executor(self, handle: SimpleNamespace):
        assert handle.executor_id == "exec-1"
        return self.connection


@pytest.mark.asyncio
async def test_inference_router_route_generate_reconstructs_normalized_response() -> None:
    router = InferenceRouter(_Provider())

    result = await router.route_generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.4",
        executor_labels={"location": "local"},
        request_kwargs={"cognis_llm_api": "responses"},
    )

    assert result["choices"][0]["message"]["content"] == "Hello"
    assert result["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"
    assert result["usage"]["total_tokens"] == 9
