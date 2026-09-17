"""FakeRuntime contract: one task, parallel, structured, failure, timeout, malformed, usage, illegal action."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from hyperresearch.runtime import (
    AgentResult,
    AgentTask,
    FakeRuntime,
    HostAction,
    IllegalHostAction,
    MalformedStructuredOutput,
    ResearchContext,
    RuntimeFailure,
    RuntimeTimeout,
)
from hyperresearch.runtime.parse import assert_action_allowed, iter_host_actions


def _ctx(tmp_path: Path) -> ResearchContext:
    return ResearchContext(
        run_id="run-1",
        canonical_query="What is X?",
        tier="light",
        profile="light",
        runtime_name="fake",
        workspace_root=tmp_path,
    )


def _task(**kwargs: object) -> AgentTask:
    base = dict(
        task_id="t1",
        role="investigator",
        payload="hello",
        model="fake-model",
    )
    base.update(kwargs)
    return AgentTask(**base)  # type: ignore[arg-type]


def run(coro):
    return asyncio.run(coro)


class TestFakeRuntimeContract:
    def test_one_task(self, tmp_path):
        rt = FakeRuntime(default="pong")
        result = run(rt.run(_task(), _ctx(tmp_path)))
        assert result.text == "pong"
        assert result.actual_model is None
        assert result.requested_model == "fake-model"
        assert rt.calls[0].task_id == "t1"

    def test_parallel_tasks(self, tmp_path):
        rt = FakeRuntime(responses={"a": "A", "b": "B", "c": "C"})
        tasks = [
            _task(task_id="a", role="r"),
            _task(task_id="b", role="r"),
            _task(task_id="c", role="r"),
        ]
        results = run(rt.run_many(tasks, _ctx(tmp_path), concurrency=2))
        assert [r.text for r in results] == ["A", "B", "C"]

    def test_structured_output(self, tmp_path):
        @dataclass
        class Out:
            answer: str

        rt = FakeRuntime(default='{"answer": "42"}')
        result = run(rt.run(_task(output_schema=Out), _ctx(tmp_path)))
        assert result.structured == Out(answer="42")

    def test_runtime_failure(self, tmp_path):
        rt = FakeRuntime(default=RuntimeFailure("boom"))
        with pytest.raises(RuntimeFailure, match="boom"):
            run(rt.run(_task(), _ctx(tmp_path)))

    def test_timeout(self, tmp_path):
        rt = FakeRuntime(default=RuntimeTimeout("slow"))
        with pytest.raises(RuntimeTimeout, match="slow"):
            run(rt.run(_task(), _ctx(tmp_path)))

    def test_malformed_structured(self, tmp_path):
        rt = FakeRuntime(default="not-json")
        with pytest.raises(MalformedStructuredOutput):
            run(rt.run(_task(output_schema=dict), _ctx(tmp_path)))

    def test_missing_usage_tolerated(self, tmp_path):
        rt = FakeRuntime(default=AgentResult(text="ok", requested_model="fake-model"))
        result = run(rt.run(_task(), _ctx(tmp_path)))
        assert result.text == "ok"
        assert result.usage == {}

    def test_illegal_host_action_not_executed(self, tmp_path):
        illegal = HostAction(kind="fetch", args={"url": "https://evil.example"}, reason="no")
        rt = FakeRuntime(
            default=AgentResult(
                text="fetch",
                structured={"kind": "fetch", "args": {"url": "https://evil.example"}, "reason": "no"},
                requested_model="fake-model",
            )
        )
        task = _task(allowed_actions=())
        result = run(rt.run(task, _ctx(tmp_path)))
        actions = iter_host_actions(result.structured)
        assert actions[0].kind == "fetch"
        with pytest.raises(IllegalHostAction):
            assert_action_allowed(actions[0], task.allowed_actions)
        assert rt.executed_host_actions == []
        assert illegal.kind == "fetch"
