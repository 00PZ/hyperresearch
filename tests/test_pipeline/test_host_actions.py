"""Host-action loop: propose → validate → execute → provenance; illegal; budget."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hyperresearch.pipeline.checkpoints import TaskLog
from hyperresearch.pipeline.host_actions import HostBudget, HostExecutor, run_host_action_loop
from hyperresearch.runtime import (
    AgentResult,
    AgentTask,
    FakeRuntime,
    HostAction,
    IllegalHostAction,
    ResearchContext,
)
from hyperresearch.runtime.parse import assert_action_allowed


def run(coro):
    return asyncio.run(coro)


def _ctx(root: Path) -> ResearchContext:
    return ResearchContext(
        run_id="r1",
        canonical_query="q",
        tier="light",
        profile="light",
        runtime_name="fake",
        workspace_root=root,
    )


def test_search_fetch_read_complete_roundtrip(tmp_vault):
    searches = []
    fetches = []

    def search_fn(query, limit=10):
        searches.append(query)
        return [{"id": "n1", "title": "hit"}]

    def fetch_fn(url, tags=None):
        fetches.append(url)
        return {"note_id": "n1", "url": url}

    note = tmp_vault.notes_dir / "n1.md"
    note.write_text("# n1\nbody\n", encoding="utf-8")

    script = {
        "loop-model-0": AgentResult(
            text="search",
            structured={"kind": "search", "args": {"query": "ion trap"}, "reason": "find"},
            requested_model="m",
        ),
        "loop-model-1": AgentResult(
            text="fetch",
            structured={"kind": "fetch", "args": {"url": "https://example.com/a"}, "reason": "read"},
            requested_model="m",
        ),
        "loop-model-2": AgentResult(
            text="read",
            structured={"kind": "evidence_read", "args": {"note_id": "n1"}, "reason": "quote"},
            requested_model="m",
        ),
        "loop-model-3": AgentResult(
            text="done",
            structured={"kind": "complete", "args": {}, "reason": "enough"},
            requested_model="m",
        ),
    }
    rt = FakeRuntime(responses=script)
    ex = HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root, search_fn=search_fn, fetch_fn=fetch_fn)
    log = TaskLog(tmp_vault.run_dir("r1") / "task_log.jsonl")
    tmp_vault.run_dir("r1").mkdir(parents=True, exist_ok=True)
    result = run(run_host_action_loop(
        rt,
        AgentTask(
            task_id="loop",
            role="investigator",
            payload="q",
            model="m",
            allowed_actions=("search", "fetch", "evidence_read", "complete"),
        ),
        _ctx(tmp_vault.root),
        ex,
        HostBudget(max_iterations=8, max_seconds=30, max_cost_usd=10),
        log,
        task_id_prefix="loop",
    ))
    assert result.text == "done"
    assert searches == ["ion trap"]
    assert fetches == ["https://example.com/a"]
    assert log.is_success("loop-ha-0-0")
    assert log.is_success("loop-ha-1-0")


def test_illegal_action_rejected(tmp_vault):
    rt = FakeRuntime(default=AgentResult(
        text="no",
        structured={"kind": "fetch", "args": {"url": "https://x"}, "reason": "x"},
        requested_model="m",
    ))
    ex = HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root, fetch_fn=lambda **k: (_ for _ in ()).throw(AssertionError("executed")))
    with pytest.raises(IllegalHostAction):
        run(run_host_action_loop(
            rt,
            AgentTask(task_id="x", role="critic", payload="q", model="m", allowed_actions=()),
            _ctx(tmp_vault.root),
            ex,
            HostBudget(max_iterations=3, max_seconds=5, max_cost_usd=1),
            task_id_prefix="x",
        ))


def test_budget_stops_iterations(tmp_vault):
    n = {"i": 0}

    def forever(task, context):
        n["i"] += 1
        return AgentResult(
            text="search",
            structured={"kind": "search", "args": {"query": f"q{n['i']}"}, "reason": "more"},
            requested_model="m",
        )

    rt = FakeRuntime(default=forever)
    calls = []
    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        search_fn=lambda query, limit=10: calls.append(query) or [],
    )
    run(run_host_action_loop(
        rt,
        AgentTask(
            task_id="b",
            role="investigator",
            payload="q",
            model="m",
            allowed_actions=("search", "fetch", "evidence_read", "complete"),
        ),
        _ctx(tmp_vault.root),
        ex,
        HostBudget(max_iterations=3, max_seconds=30, max_cost_usd=99),
        task_id_prefix="b",
    ))
    assert len(calls) == 3


def test_path_traversal_rejected(tmp_vault):
    ex = HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root)
    with pytest.raises(IllegalHostAction, match="escapes"):
        ex.execute(
            HostAction(kind="evidence_read", args={"note_id": "x", "path": "../../etc/passwd"}, reason="x"),
            task_id="t",
        )
    with pytest.raises(IllegalHostAction):
        assert_action_allowed(
            HostAction(kind="fetch", args={"url": "https://x"}, reason="x"),
            (),
        )


def test_missing_actions_retries_then_complete(tmp_vault):
    script = {
        "loop-model-0": AgentResult(
            text='{"report": "nope"}',
            structured={"report": "nope"},
            requested_model="m",
        ),
        "loop-model-1": AgentResult(
            text="done",
            structured={"kind": "complete", "args": {}, "reason": "ok"},
            requested_model="m",
        ),
    }
    rt = FakeRuntime(responses=script)
    ex = HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root)
    log = TaskLog(tmp_vault.run_dir("r-retry") / "task_log.jsonl")
    tmp_vault.run_dir("r-retry").mkdir(parents=True, exist_ok=True)
    result = run(run_host_action_loop(
        rt,
        AgentTask(
            task_id="loop",
            role="investigator",
            payload="q",
            model="m",
            allowed_actions=("search", "fetch", "evidence_read", "complete"),
        ),
        _ctx(tmp_vault.root),
        ex,
        HostBudget(max_iterations=8, max_seconds=30, max_cost_usd=10),
        log,
        task_id_prefix="loop",
    ))
    assert result.text == "done"
    assert any("No host actions parsed" in t.payload for t in rt.calls[1:])


def test_missing_note_read_returns_note_not_found(tmp_vault):
    ex = HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root)
    result = ex.execute(
        HostAction(kind="evidence_read", args={"note_id": "world-mobile-airnodes"}, reason="x"),
        task_id="t-missing",
    )
    assert result["ok"] is False
    assert result["error"] == "note_not_found"
    assert result["note_id"] == "world-mobile-airnodes"


def test_missing_note_read_then_valid_action_continues(tmp_vault):
    fetches: list[str] = []

    def fetch_fn(url, tags=None):
        fetches.append(url)
        return {"note_id": "n1", "url": url}

    (tmp_vault.notes_dir / "n1.md").write_text("# n1\nbody\n", encoding="utf-8")
    script = {
        "loop-model-0": AgentResult(
            text="read missing",
            structured={
                "kind": "evidence_read",
                "args": {"note_id": "world-mobile-airnodes"},
                "reason": "guess",
            },
            requested_model="m",
        ),
        "loop-model-1": AgentResult(
            text="fetch",
            structured={
                "kind": "fetch",
                "args": {"url": "https://example.com/a"},
                "reason": "recover",
            },
            requested_model="m",
        ),
        "loop-model-2": AgentResult(
            text="done",
            structured={"kind": "complete", "args": {}, "reason": "done"},
            requested_model="m",
        ),
    }
    rt = FakeRuntime(responses=script)
    log = TaskLog(tmp_vault.root / "tl.jsonl")
    result = run(run_host_action_loop(
        rt,
        AgentTask(
            task_id="loop",
            role="investigator",
            payload="q",
            model="m",
            allowed_actions=("search", "fetch", "evidence_read", "complete"),
        ),
        _ctx(tmp_vault.root),
        HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root, fetch_fn=fetch_fn),
        HostBudget(max_iterations=8, max_seconds=30, max_cost_usd=10),
        log,
        task_id_prefix="loop",
    ))
    assert result.text == "done"
    assert fetches == ["https://example.com/a"]
    miss = log.result_payload("loop-ha-0-0")
    assert miss is not None
    assert miss.get("ok") is False
    assert miss.get("error") == "note_not_found"
    assert miss.get("note_id") == "world-mobile-airnodes"
