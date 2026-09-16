"""Resume: skip completed task_ids; uncertain_remote no auto-POST; run_many retries failed only."""

from __future__ import annotations

import asyncio
from pathlib import Path

from hyperresearch.pipeline.checkpoints import UNCERTAIN_REMOTE, TaskLog
from hyperresearch.pipeline.orchestrator import run_many_retry
from hyperresearch.runtime import AgentTask, FakeRuntime, ResearchContext, RuntimeFailure


def _ctx(tmp_path: Path) -> ResearchContext:
    return ResearchContext(
        run_id="r",
        canonical_query="q",
        tier="light",
        profile="light",
        runtime_name="fake",
        workspace_root=tmp_path,
    )


def _task(tid: str) -> AgentTask:
    return AgentTask(task_id=tid, role="r", payload="p", model="m")


def test_completed_task_id_not_replayed(tmp_path):
    log = TaskLog(tmp_path / "task_log.jsonl")
    log.begin("a", "model")
    log.succeed("a", {"text": "done"})
    assert log.resume_model("a") == "skip"
    assert log.resume_model("missing") == "launch"


def test_uncertain_remote_does_not_auto_post(tmp_path):
    log = TaskLog(tmp_path / "task_log.jsonl")
    log.begin("m1", "model", {"payload": "hi"})
    assert log.resume_model("m1") == UNCERTAIN_REMOTE
    assert log.get("m1").status == UNCERTAIN_REMOTE
    assert log.resume_model("m1") == UNCERTAIN_REMOTE


def test_run_many_retries_only_failed_member(tmp_path):
    calls: list[str] = []

    class Flaky(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.fail_once = {"b": True}

        async def run(self, task, context):
            calls.append(task.task_id)
            if task.task_id == "b" and self.fail_once.get("b"):
                self.fail_once["b"] = False
                raise RuntimeFailure("b-fail")
            return await super().run(task, context)

    rt = Flaky()
    rt.default = "ok"
    log = TaskLog(tmp_path / "t.jsonl")
    tasks = [_task("a"), _task("b"), _task("c")]
    results = asyncio.run(run_many_retry(rt, tasks, _ctx(tmp_path), concurrency=3, log=log))
    assert [r.text for r in results] == ["ok", "ok", "ok"]
    assert calls.count("a") == 1
    assert calls.count("c") == 1
    assert calls.count("b") == 2


def test_run_many_skip_already_successful(tmp_path):
    log = TaskLog(tmp_path / "t.jsonl")
    log.begin("a", "model")
    log.succeed("a", {"text": "cached"})
    calls: list[str] = []

    class Spy(FakeRuntime):
        async def run(self, task, context):
            calls.append(task.task_id)
            return await super().run(task, context)

    rt = Spy(default="fresh")
    results = asyncio.run(run_many_retry(rt, [_task("a"), _task("b")], _ctx(tmp_path), 2, log))
    assert results[0].text == "cached"
    assert results[1].text == "fresh"
    assert "a" not in calls
    assert calls == ["b"]
