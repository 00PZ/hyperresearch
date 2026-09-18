"""Trip-log writer: durable result first, dual JSONL, event_id dedup."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from hyperresearch.core.runs import init_run
from hyperresearch.pipeline.host_actions import HostExecutor
from hyperresearch.runtime.errors import SearchBlockError
from hyperresearch.runtime.types import HostAction
from hyperresearch.web.base import WebResult
from hyperresearch.web.searxng import (
    RUN_TRIP_LOG_NAME,
    SearchCallResult,
    append_trip_row,
    load_search_call,
    searxng_search,
    trip_row_for,
)


def _hit() -> dict:
    return {"url": "https://example.com/a", "title": "A", "content": "body"}


def test_append_dedupes_event_id(tmp_path) -> None:
    path = tmp_path / "t.jsonl"
    row = {"event_id": "r:t1", "ts": "t", "run_id": "r", "hits": 0, "unresponsive_engines": []}
    append_trip_row(path, row)
    append_trip_row(path, row)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_concurrent_appends_collapse_duplicates(tmp_path) -> None:
    path = tmp_path / "t.jsonl"
    rows = [
        {"event_id": f"r:t{i}", "ts": "t", "run_id": "r", "hits": 0, "unresponsive_engines": []}
        for i in range(12)
    ]
    payload = rows + rows

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda row: append_trip_row(path, row), payload))
    ids = [json.loads(line)["event_id"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(ids) == 12
    assert set(ids) == {f"r:t{i}" for i in range(12)}


def test_durable_then_failed_workstation_append_resume_no_http(tmp_vault, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = "http://127.0.0.1:8888"
    ws = tmp_path / "ws-dir"
    ws.mkdir()
    tmp_vault.config.searxng_trip_log = str(ws)
    init_run(tmp_vault, "r-dw")
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(200, json={"results": [_hit()]})

    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="r-dw",
        httpx_transport=httpx.MockTransport(handler),
    )
    action = HostAction(kind="search", args={"query": "q"}, reason="x")
    with pytest.raises(SearchBlockError) as ei:
        ex.execute(action, task_id="ha-dw")
    assert ei.value.blocked_reason == "searxng_trip_log"
    rec = load_search_call(tmp_vault.run_dir("r-dw"), "ha-dw")
    assert rec is not None
    assert rec["hits"][0]["url"] == "https://example.com/a"
    assert seen == [1]

    tmp_vault.config.searxng_trip_log = str(tmp_path / "ws.jsonl")
    result = ex.execute(action, task_id="ha-dw")
    assert result["ok"] is True
    assert result["results"]["web_hits"][0]["url"] == "https://example.com/a"
    assert result["results"]["web_hits"][0]["title"] == "A"
    assert seen == [1]
    ws_lines = (tmp_path / "ws.jsonl").read_text(encoding="utf-8").splitlines()
    run_lines = (tmp_vault.run_dir("r-dw") / RUN_TRIP_LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["event_id"] for x in ws_lines] == ["r-dw:ha-dw"]
    assert [json.loads(x)["event_id"] for x in run_lines] == ["r-dw:ha-dw"]


def test_crash_after_both_appends_before_checkpoint_no_http(tmp_vault, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = "http://127.0.0.1:8888"
    tmp_vault.config.searxng_trip_log = str(tmp_path / "ws.jsonl")
    init_run(tmp_vault, "r-ck")
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(200, json={"results": [_hit()]})

    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="r-ck",
        httpx_transport=httpx.MockTransport(handler),
    )
    action = HostAction(kind="search", args={"query": "q"}, reason="x")
    first = ex.execute(action, task_id="ha-ck")
    assert first["ok"] is True
    assert seen == [1]
    second = ex.execute(action, task_id="ha-ck")
    assert second["results"]["web_hits"] == first["results"]["web_hits"]
    assert seen == [1]
    run_lines = (tmp_vault.run_dir("r-ck") / RUN_TRIP_LOG_NAME).read_text(encoding="utf-8").splitlines()
    ws_lines = (tmp_path / "ws.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(run_lines) == 1
    assert len(ws_lines) == 1
    assert json.loads(run_lines[0])["event_id"] == "r-ck:ha-ck"


def test_http_failure_writes_no_durable_result(tmp_vault, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = "http://127.0.0.1:8888"
    tmp_vault.config.searxng_trip_log = str(tmp_path / "ws.jsonl")
    init_run(tmp_vault, "r-no")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="nope")

    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="r-no",
        httpx_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SearchBlockError):
        ex.execute(HostAction(kind="search", args={"query": "q"}, reason="x"), task_id="ha-no")
    assert load_search_call(tmp_vault.run_dir("r-no"), "ha-no") is None
    assert not (tmp_path / "ws.jsonl").exists()


def test_trip_row_event_id_and_engine_list() -> None:
    result = SearchCallResult(
        hits=[WebResult(url="https://example.com/a", title="A", content="b")],
        unresponsive_engines=[["google", "CAPTCHA"]],
    )
    row = trip_row_for("run1", "task9", result, ts="2026-01-01T00:00:00+00:00")
    assert row["event_id"] == "run1:task9"
    assert row["run_id"] == "run1"
    assert row["hits"] == 1
    assert row["unresponsive_engines"] == [["google", "CAPTCHA"]]


def test_searxng_search_not_used_on_http_failure_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x")

    with pytest.raises(SearchBlockError):
        searxng_search(
            "http://127.0.0.1:8888",
            "q",
            client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        )


def test_append_repairs_truncated_jsonl_tail(tmp_path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"event_id":"r:t', encoding="utf-8")
    row = {"event_id": "r:t", "ts": "t", "run_id": "r", "hits": 0, "unresponsive_engines": []}
    append_trip_row(path, row)
    parsed = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [p["event_id"] for p in parsed] == ["r:t"]


def test_truncated_trip_log_recovery_no_http(tmp_vault, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    tmp_vault.config.search_provider = "searxng"
    tmp_vault.config.searxng_url = "http://127.0.0.1:8888"
    ws = tmp_path / "ws.jsonl"
    tmp_vault.config.searxng_trip_log = str(ws)
    init_run(tmp_vault, "r-t")
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(200, json={"results": [_hit()]})

    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="r-t",
        httpx_transport=httpx.MockTransport(handler),
    )
    action = HostAction(kind="search", args={"query": "q"}, reason="x")
    first = ex.execute(action, task_id="t")
    assert first["ok"] is True
    assert seen == [1]
    ws.write_text('{"event_id":"r:t', encoding="utf-8")
    second = ex.execute(action, task_id="t")
    assert second["ok"] is True
    assert seen == [1]
    parsed = [json.loads(line) for line in ws.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [p["event_id"] for p in parsed] == ["r-t:t"]
