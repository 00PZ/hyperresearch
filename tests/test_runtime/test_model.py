"""ModelRuntime: no tools on the wire, refuse tools, unexpected_tool_response."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from hyperresearch.runtime import (
    AgentTask,
    ModelRuntime,
    ResearchContext,
    ToolsRequired,
    UnexpectedToolResponse,
)
from hyperresearch.runtime.model import capabilities_require_tools, chat_completions_url


def _ctx(tmp_path: Path) -> ResearchContext:
    return ResearchContext(
        run_id="run-1",
        canonical_query="q",
        tier="light",
        profile="light",
        runtime_name="model",
        workspace_root=tmp_path,
    )


def _task(**kwargs: object) -> AgentTask:
    base = dict(task_id="t1", role="draft", payload="write it", model="test-model")
    base.update(kwargs)
    return AgentTask(**base)  # type: ignore[arg-type]


def run(coro):
    return asyncio.run(coro)


def _runtime(handler, **kwargs) -> ModelRuntime:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return ModelRuntime(
        base_url="https://example.test/v1",
        api_key="sk-test",
        default_model="test-model",
        client=client,
        **kwargs,
    )


class TestModelRuntime:
    def test_no_tools_in_request(self, tmp_path):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "model": "echo-test-model",
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        rt = _runtime(handler)
        result = run(rt.run(_task(), _ctx(tmp_path)))
        assert captured[0]["model"] == "test-model"
        assert "tools" not in captured[0]
        assert "tool_choice" not in captured[0]
        assert "parallel_tool_calls" not in captured[0]
        assert result.actual_model is None
        assert result.reported_model == "echo-test-model"
        assert result.text == "hi"
        assert result.usage["prompt_tokens"] == 1

    def test_missing_usage_tolerated(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        rt = _runtime(handler)
        result = run(rt.run(_task(), _ctx(tmp_path)))
        assert result.usage == {}
        assert result.actual_model is None

    def test_refuse_if_capabilities_require_tools(self):
        with pytest.raises(ToolsRequired):
            ModelRuntime(
                base_url="https://example.test/v1",
                api_key="x",
                default_model="m",
                capabilities={"tools": True},
            )
        with pytest.raises(ToolsRequired):
            ModelRuntime(
                base_url="https://example.test/v1",
                api_key="x",
                default_model="m",
                tools_required=True,
            )
        assert capabilities_require_tools({"supported_features": ["tool_calls"]})

    def test_unexpected_tool_response_not_executed(self, tmp_path):
        executed = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": None,
                            "tool_calls": [{
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "fetch", "arguments": "{\"url\":\"https://x\"}"},
                            }],
                        }
                    }]
                },
            )

        rt = _runtime(handler)
        with pytest.raises(UnexpectedToolResponse):
            run(rt.run(_task(), _ctx(tmp_path)))
        assert executed == []

    def test_same_contract_one_task_and_parallel(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": body["messages"][0]["content"]}}]},
            )

        rt = _runtime(handler)
        ctx = _ctx(tmp_path)
        one = run(rt.run(_task(payload="solo"), ctx))
        assert one.text == "solo"
        many = run(rt.run_many([
            _task(task_id="a", payload="A"),
            _task(task_id="b", payload="B"),
        ], ctx, concurrency=2))
        assert [r.text for r in many] == ["A", "B"]
        assert all(r.actual_model is None for r in many)

    def test_chat_url(self):
        assert chat_completions_url("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
        assert chat_completions_url("https://x/v1/chat/completions") == "https://x/v1/chat/completions"
