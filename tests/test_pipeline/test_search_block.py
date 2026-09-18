"""Orchestrator: blocked_on=search survives host-error handler; resume HTTP rules."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hyperresearch.core.runs import init_run, load_manifest, set_status
from hyperresearch.pipeline.checkpoints import TaskLog
from hyperresearch.pipeline.host_actions import HostBudget, HostExecutor, run_host_action_loop
from hyperresearch.pipeline.orchestrator import execute_run
from hyperresearch.runtime import AgentResult, AgentTask, FakeRuntime, ResearchContext
from hyperresearch.runtime.errors import SearchBlockError
from hyperresearch.runtime.types import HostAction

DECOMP = json.dumps({"pipeline_tier": "light", "required_section_headings": ["Findings"]})
COMPLETE = {"kind": "complete", "args": {}, "reason": "done"}
REPORT = "## Findings\n\n" + ("Substantive sentence with real evidence attached. " * 80)


def run(coro):
    return asyncio.run(coro)


def _rt(**overrides):
    responses = {
        "decompose": DECOMP,
        "width": COMPLETE,
        "draft": REPORT,
        "polish": '{"applied": []}',
        "readability": '{"applied": []}',
    }
    responses.update(overrides)
    return FakeRuntime(responses=responses)


def test_start_gate_unconfigured_does_not_start_steps(tmp_vault, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = ""
    rt = _rt()
    result = run(execute_run(tmp_vault, "What is X?", rt, profile="light", tag="gate-1"))
    m = result["manifest"]
    assert m["status"] == "blocked"
    assert m["blocked_on"] == "search"
    assert m["blocked_reason"] == "searxng_unconfigured"
    assert m.get("steps") in ({}, None) or "1" not in m.get("steps", {})
    assert rt.calls == []


def test_absent_search_provider_run_starts(tmp_vault, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    assert tmp_vault.config.search_provider == "none"
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="light", tag="gate-ok"))
    assert result["manifest"]["status"] in ("completed", "blocked")
    assert result["manifest"].get("blocked_on") != "search"


def test_typed_search_exception_through_execute_run(tmp_vault, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    from hyperresearch.pipeline import orchestrator as orch

    async def boom(*args, **kwargs):
        raise SearchBlockError("searxng_http")

    monkeypatch.setattr(orch, "execute_step", boom)
    with pytest.raises(SearchBlockError):
        run(execute_run(tmp_vault, "What is X?", _rt(), profile="light", tag="sb-ex"))
    m = load_manifest(tmp_vault, "sb-ex")
    assert m["status"] == "blocked"
    assert m["blocked_on"] == "search"
    assert m["blocked_reason"] == "searxng_http"


def test_resume_unconfigured_clears_after_url_repair(tmp_vault, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = ""
    run(execute_run(tmp_vault, "What is X?", _rt(), profile="light", tag="fix-url"))
    assert load_manifest(tmp_vault, "fix-url")["blocked_reason"] == "searxng_unconfigured"

    tmp_vault.config.searxng_url = "http://127.0.0.1:8888"
    tmp_vault.config.searxng_trip_log = str(tmp_vault.root / "ws.jsonl")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    from hyperresearch.pipeline import orchestrator as orch

    orig = orch.HostExecutor

    def factory(*args, **kwargs):
        kwargs.setdefault("httpx_transport", httpx.MockTransport(handler))
        if len(args) >= 1:
            return orig(*args, **kwargs)
        return orig(**kwargs)

    monkeypatch.setattr(orch, "HostExecutor", factory)
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="light", tag="fix-url", resume=True))
    assert result["manifest"]["blocked_on"] != "search" or result["manifest"]["status"] != "blocked"


def test_resume_after_http_failure_requeries_once(tmp_vault, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = "http://127.0.0.1:8888"
    tmp_vault.config.searxng_trip_log = str(tmp_path / "ws.jsonl")
    init_run(tmp_vault, "r-re")
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(500, text="down")
        return httpx.Response(200, json={"results": [{"url": "https://example.com/a", "title": "A", "content": "b"}]})

    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="r-re",
        httpx_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SearchBlockError):
        ex.execute(HostAction(kind="search", args={"query": "q"}, reason="x"), task_id="ha-re")
    assert seen == [1]
    result = ex.execute(HostAction(kind="search", args={"query": "q"}, reason="x"), task_id="ha-re")
    assert result["ok"] is True
    assert seen == [1, 1]
    assert result["results"]["web_hits"][0]["url"] == "https://example.com/a"


def test_completed_fetch_not_replayed_on_search_resume(tmp_vault, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = "http://127.0.0.1:8888"
    tmp_vault.config.searxng_trip_log = str(tmp_path / "ws.jsonl")
    tag = "r-loop"
    init_run(tmp_vault, tag)
    fetches: list[str] = []
    searches: list[int] = []

    def fetch_fn(url, tags=None):
        fetches.append(url)
        return {"note_id": "n1", "url": url}

    def handler(request: httpx.Request) -> httpx.Response:
        searches.append(1)
        if len(searches) == 1:
            return httpx.Response(500, text="down")
        return httpx.Response(200, json={"results": []})

    def search_then_fetch():
        return AgentResult(
            text="acts",
            structured={
                "actions": [
                    {"kind": "fetch", "args": {"url": "https://example.com/a"}, "reason": "f"},
                    {"kind": "search", "args": {"query": "q"}, "reason": "s"},
                ]
            },
            requested_model="m",
        )

    log = TaskLog(tmp_vault.run_dir(tag) / "task_log.jsonl")
    ctx = ResearchContext(
        run_id=tag,
        canonical_query="q",
        tier="light",
        profile="light",
        runtime_name="fake",
        workspace_root=tmp_vault.root,
    )
    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag=tag,
        fetch_fn=fetch_fn,
        httpx_transport=httpx.MockTransport(handler),
    )
    rt = FakeRuntime(
        responses={
            "loop-model-0": search_then_fetch(),
            "loop-model-1": AgentResult(text="done", structured=COMPLETE, requested_model="m"),
        }
    )
    task = AgentTask(
        task_id="loop",
        role="width",
        payload="q",
        model="m",
        allowed_actions=("search", "fetch", "evidence_read", "complete"),
    )
    with pytest.raises(SearchBlockError):
        run(
            run_host_action_loop(
                rt,
                task,
                ctx,
                ex,
                HostBudget(max_iterations=8, max_seconds=30, max_cost_usd=10),
                log,
                task_id_prefix="loop",
            )
        )
    assert fetches == ["https://example.com/a"]
    assert searches == [1]
    assert log.is_success("loop-ha-0-0")
    assert not log.is_success("loop-ha-0-1")

    rt2 = FakeRuntime(
        responses={
            "loop-model-0": search_then_fetch(),
            "loop-model-1": AgentResult(text="done", structured=COMPLETE, requested_model="m"),
        }
    )
    run(
        run_host_action_loop(
            rt2,
            task,
            ctx,
            ex,
            HostBudget(max_iterations=8, max_seconds=30, max_cost_usd=10),
            log,
            task_id_prefix="loop",
        )
    )
    assert fetches == ["https://example.com/a"]
    assert searches == [1, 1]


def test_block_if_still_running_does_not_overwrite_verify(tmp_vault) -> None:
    from hyperresearch.pipeline.orchestrator import _block_if_still_running

    init_run(tmp_vault, "r-ver")
    set_status(tmp_vault, "r-ver", "blocked", blocked_on="verify")
    _block_if_still_running(tmp_vault, "r-ver", blocked_on="search", blocked_reason="searxng_http")
    assert load_manifest(tmp_vault, "r-ver")["blocked_on"] == "verify"


def test_execute_run_start_gate_preserves_budget_block(tmp_vault, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = ""
    init_run(tmp_vault, "r-bud")
    set_status(tmp_vault, "r-bud", "blocked", blocked_on="budget")
    result = run(
        execute_run(tmp_vault, "What is X?", _rt(), profile="light", tag="r-bud", resume=True)
    )
    assert result["manifest"]["blocked_on"] == "budget"
    assert load_manifest(tmp_vault, "r-bud")["blocked_on"] == "budget"
