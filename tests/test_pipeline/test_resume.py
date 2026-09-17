"""Resume: skip completed task_ids; uncertain_remote no auto-POST; run_many retries failed only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from hyperresearch.pipeline.checkpoints import UNCERTAIN_REMOTE, TaskLog, dump_agent_result
from hyperresearch.pipeline.host_actions import HostBudget, HostExecutor, run_host_action_loop
from hyperresearch.pipeline.orchestrator import (
    _maybe_patches,
    execute_run,
    run_many_retry,
    task_log_for,
)
from hyperresearch.pipeline.patch import PatchState, apply_patch_set, content_hash
from hyperresearch.runtime import (
    AgentResult,
    AgentTask,
    FakeRuntime,
    ModelRuntime,
    ResearchContext,
    RuntimeFailure,
    RuntimeTimeout,
)
from tests.test_pipeline.test_full import _rt, plant_src, run


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


def test_run_many_timeout_does_not_retry_task_id(tmp_path):
    calls: list[str] = []

    class Boom(FakeRuntime):
        async def run(self, task, context):
            calls.append(task.task_id)
            if task.task_id == "b":
                raise RuntimeTimeout("t")
            return await super().run(task, context)

    rt = Boom()
    rt.default = "ok"
    log = TaskLog(tmp_path / "t.jsonl")
    with pytest.raises(RuntimeError, match="uncertain_remote:b"):
        asyncio.run(run_many_retry(rt, [_task("a"), _task("b"), _task("c")], _ctx(tmp_path), 3, log))
    assert calls.count("b") == 1
    assert calls.count("a") == 1
    assert calls.count("c") == 1
    assert log.get("b") is not None
    assert log.get("b").status == UNCERTAIN_REMOTE
    assert log.is_success("a")
    assert log.is_success("c")


def _ok_chat(content: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _model_runtime(handler) -> ModelRuntime:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return ModelRuntime(
        base_url="https://example.test/v1",
        api_key="sk-test",
        default_model="m",
        client=client,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: httpx.ReadError("connection reset after provider accepted request"),
        lambda: httpx.RemoteProtocolError("peer closed connection"),
        lambda: httpx.WriteError("broken pipe"),
        lambda: httpx.TransportError("generic transport failure"),
    ],
)
def test_run_many_uncertain_transport_one_post(tmp_path, factory):
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = body["messages"][0]["content"]
        posts.append(text)
        if text == "B":
            raise factory()
        return _ok_chat("ok")

    rt = _model_runtime(handler)
    log = TaskLog(tmp_path / "t.jsonl")
    tasks = [
        AgentTask(task_id="a", role="r", payload="A", model="m"),
        AgentTask(task_id="b", role="r", payload="B", model="m"),
        AgentTask(task_id="c", role="r", payload="C", model="m"),
    ]
    with pytest.raises(RuntimeError, match="uncertain_remote:b"):
        asyncio.run(run_many_retry(rt, tasks, _ctx(tmp_path), 3, log))
    assert posts.count("B") == 1
    assert posts.count("A") == 1
    assert posts.count("C") == 1
    assert log.get("b") is not None
    assert log.get("b").status == UNCERTAIN_REMOTE
    assert log.is_success("a")
    assert log.is_success("c")


def test_run_many_connect_error_retries(tmp_path):
    posts: list[str] = []
    fail_once = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_once
        body = json.loads(request.content)
        text = body["messages"][0]["content"]
        posts.append(text)
        if text == "B" and fail_once:
            fail_once = False
            raise httpx.ConnectError("connection refused")
        return _ok_chat("ok")

    rt = _model_runtime(handler)
    log = TaskLog(tmp_path / "t.jsonl")
    tasks = [
        AgentTask(task_id="a", role="r", payload="A", model="m"),
        AgentTask(task_id="b", role="r", payload="B", model="m"),
        AgentTask(task_id="c", role="r", payload="C", model="m"),
    ]
    results = asyncio.run(run_many_retry(rt, tasks, _ctx(tmp_path), 3, log))
    assert [r.text for r in results] == ["ok", "ok", "ok"]
    assert posts.count("B") == 2
    assert posts.count("A") == 1
    assert posts.count("C") == 1
    assert log.is_success("a")
    assert log.is_success("b")
    assert log.is_success("c")


def test_critic_timeout_one_dispatch_uncertain_remote(tmp_vault):
    plant_src(tmp_vault, "to-crit")
    rt = _rt(critic_depth=RuntimeTimeout("t"))
    with pytest.raises(RuntimeError, match="uncertain_remote"):
        run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="to-crit"))
    depth = [c for c in rt.calls if c.role == "critic_depth"]
    assert len(depth) == 1
    log = task_log_for(tmp_vault, "to-crit")
    rec = log.get("step-12-depth")
    assert rec is not None
    assert rec.status == UNCERTAIN_REMOTE
    assert log.is_success("step-12-dialectic")


def test_crash_between_model_success_and_first_action_still_fetches(tmp_vault):
    tag = "r-crash1"
    log = TaskLog(tmp_vault.run_dir(tag) / "task_log.jsonl")
    tmp_vault.run_dir(tag).mkdir(parents=True, exist_ok=True)
    result = AgentResult(
        text="fetch",
        structured={"kind": "fetch", "args": {"url": "https://example.com/a"}, "reason": "x"},
        requested_model="m",
    )
    log.begin("loop-model-0", "model")
    log.succeed("loop-model-0", dump_agent_result(result))
    fetches: list[str] = []

    def fetch_fn(url, tags=None):
        fetches.append(url)
        return {"note_id": "n1", "url": url}

    rt = FakeRuntime(default="should-not-rerun-model")
    run_host_action_loop_sync = asyncio.run
    out = run_host_action_loop_sync(
        run_host_action_loop(
            rt,
            AgentTask(
                task_id="loop",
                role="width",
                payload="q",
                model="m",
                allowed_actions=("search", "fetch", "evidence_read", "complete"),
            ),
            _ctx(tmp_vault.root),
            HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root, fetch_fn=fetch_fn),
            HostBudget(max_iterations=1, max_seconds=30, max_cost_usd=99),
            log,
            task_id_prefix="loop",
        )
    )
    assert fetches == ["https://example.com/a"]
    assert rt.calls == []
    assert out.structured["kind"] == "fetch"


def test_crash_between_two_actions(tmp_vault):
    tag = "r-crash2"
    log = TaskLog(tmp_vault.run_dir(tag) / "task_log.jsonl")
    tmp_vault.run_dir(tag).mkdir(parents=True, exist_ok=True)
    structured = {
        "actions": [
            {"kind": "fetch", "args": {"url": "https://example.com/a"}, "reason": "one"},
            {"kind": "fetch", "args": {"url": "https://example.com/b"}, "reason": "two"},
        ]
    }
    log.begin("loop-model-0", "model")
    log.succeed(
        "loop-model-0",
        dump_agent_result(AgentResult(text="two", structured=structured, requested_model="m")),
    )
    log.begin("loop-ha-0-0", "host_action", {"kind": "fetch"})
    log.succeed("loop-ha-0-0", {"ok": True, "url": "https://example.com/a", "note_id": "a"})
    fetches: list[str] = []

    def fetch_fn(url, tags=None):
        fetches.append(url)
        return {"note_id": "b", "url": url}

    asyncio.run(
        run_host_action_loop(
            FakeRuntime(default="no"),
            AgentTask(
                task_id="loop",
                role="width",
                payload="q",
                model="m",
                allowed_actions=("search", "fetch", "evidence_read", "complete"),
            ),
            _ctx(tmp_vault.root),
            HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root, fetch_fn=fetch_fn),
            HostBudget(max_iterations=1, max_seconds=30, max_cost_usd=99),
            log,
            task_id_prefix="loop",
        )
    )
    assert fetches == ["https://example.com/b"]


def test_restore_4000_char_draft_byte_for_byte(tmp_path):
    log = TaskLog(tmp_path / "t.jsonl")
    draft = "D" * 4000
    log.begin("step-10", "model")
    log.succeed("step-10", dump_agent_result(AgentResult(text=draft, requested_model="m")))
    restored = log.result_agent("step-10", "m")
    assert restored.text == draft
    assert len(restored.text) == 4000


def test_restore_and_apply_structured_patch(tmp_vault):
    path = tmp_vault.root / "research" / "notes" / "final_report_r.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("alpha beta", encoding="utf-8")
    h = content_hash("alpha beta")
    structured = {
        "base_report_hash": h,
        "ops": [{"old_text": "alpha", "new_text": "ALPHA"}],
    }
    log = TaskLog(tmp_vault.root / "research" / "runs" / "r" / "task_log.jsonl")
    log.path.parent.mkdir(parents=True, exist_ok=True)
    log.begin("step-14", "model")
    log.succeed(
        "step-14",
        dump_agent_result(AgentResult(text="{}", structured=structured, requested_model="m")),
    )
    restored = log.result_agent("step-14", "m")
    patch_set = _maybe_patches(restored, h)
    assert patch_set is not None
    apply_patch_set(
        path,
        patch_set,
        workspace_root=tmp_vault.root,
        state_path=tmp_vault.root / "research" / "runs" / "r" / "patch-state.json",
        task_id="step-14-apply",
        log=log,
    )
    assert path.read_text(encoding="utf-8") == "ALPHA beta"
    assert PatchState.load(tmp_vault.root / "research" / "runs" / "r" / "patch-state.json").cumulative_bytes > 0


def test_resumed_context_includes_earlier_evidence(tmp_vault):
    tag = "r-ev"
    log = TaskLog(tmp_vault.run_dir(tag) / "task_log.jsonl")
    tmp_vault.run_dir(tag).mkdir(parents=True, exist_ok=True)
    log.begin("loop-model-0", "model")
    log.succeed(
        "loop-model-0",
        dump_agent_result(
            AgentResult(
                text="fetch",
                structured={"kind": "fetch", "args": {"url": "https://example.com/ev"}, "reason": "x"},
                requested_model="m",
            )
        ),
    )
    seen: list[str] = []

    def fetch_fn(url, tags=None):
        return {"note_id": "ev1", "url": url, "excerpt": "EVIDENCE_TOKEN"}

    class Spy(FakeRuntime):
        async def run(self, task, context):
            seen.append(task.payload)
            return AgentResult(
                text="done",
                structured={"kind": "complete", "args": {}, "reason": "ok"},
                requested_model="m",
            )

    asyncio.run(
        run_host_action_loop(
            Spy(default="x"),
            AgentTask(
                task_id="loop",
                role="width",
                payload="CANONICAL",
                model="m",
                allowed_actions=("search", "fetch", "evidence_read", "complete"),
            ),
            _ctx(tmp_vault.root),
            HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root, fetch_fn=fetch_fn, run_tag=tag),
            HostBudget(max_iterations=4, max_seconds=30, max_cost_usd=99),
            log,
            task_id_prefix="loop",
        )
    )
    assert seen
    assert "https://example.com/ev" in seen[0]
    assert "host-results" in seen[0]
