"""Host-action search: vault FTS + SearchCallResult; fail-closed vs empty SERP."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from hyperresearch.core.runs import init_run, load_manifest, set_status
from hyperresearch.pipeline.host_actions import HostExecutor
from hyperresearch.runtime.errors import SearchBlockError
from hyperresearch.runtime.types import HostAction
from hyperresearch.web.searxng import RUN_TRIP_LOG_NAME, load_search_call


def _action(query: str = "python async", limit: int = 10) -> HostAction:
    return HostAction(kind="search", args={"query": query, "limit": limit}, reason="find")


def _cfg(vault, tmp_path, monkeypatch, url: str = "http://127.0.0.1:8888") -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    vault.config.search_provider = "searxng"
    vault.config.searxng_url = url
    vault.config.searxng_trip_log = str(tmp_path / "ws-trip.jsonl")
    vault.config.web_provider = "crawl4ai"


def _ex(vault, tag: str, handler) -> HostExecutor:
    return HostExecutor(
        vault=vault,
        workspace_root=vault.root,
        run_tag=tag,
        httpx_transport=httpx.MockTransport(handler),
    )


def test_search_fn_still_short_circuits(tmp_vault, tmp_path, monkeypatch) -> None:
    _cfg(tmp_vault, tmp_path, monkeypatch)
    calls: list[str] = []

    def search_fn(query, limit=10):
        calls.append(query)
        return [{"id": "n1"}]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not run when search_fn is injected")

    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        search_fn=search_fn,
        httpx_transport=httpx.MockTransport(handler),
    )
    result = ex.execute(_action("ion trap"), task_id="t0")
    assert result["ok"] is True
    assert result["results"] == [{"id": "n1"}]
    assert calls == ["ion trap"]


def test_none_is_vault_fts_only_does_not_call_crawl4ai(seeded_vault, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    seeded_vault.config.search_provider = "none"
    seeded_vault.config.web_provider = "crawl4ai"
    prov = MagicMock()
    monkeypatch.setattr("hyperresearch.web.base.get_provider", lambda *a, **k: prov)
    ex = HostExecutor(vault=seeded_vault, workspace_root=seeded_vault.root)
    result = ex.execute(_action("python async"), task_id="t-none")
    assert result["ok"] is True
    assert result["results"]["web_hits"] == []
    assert result["results"]["web_error"] is None
    assert result["results"]["unresponsive_engines"] == []
    assert any("error" not in (h if isinstance(h, dict) else {}) for h in result["results"]["vault_hits"])
    prov.search.assert_not_called()


def test_searxng_does_not_call_fetch_provider_search(seeded_vault, tmp_path, monkeypatch) -> None:
    _cfg(seeded_vault, tmp_path, monkeypatch)
    init_run(seeded_vault, "r-ns")
    prov = MagicMock()
    monkeypatch.setattr("hyperresearch.web.base.get_provider", lambda *a, **k: prov)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"url": "https://example.com/a", "title": "A", "content": "b"}]},
        )

    result = _ex(seeded_vault, "r-ns", handler).execute(_action(), task_id="t-ns")
    assert result["ok"] is True
    assert result["results"]["web_hits"][0]["url"] == "https://example.com/a"
    assert result["results"]["vault_hits"]
    prov.search.assert_not_called()


def test_empty_serp_continues_and_writes_trip_row(tmp_vault, tmp_path, monkeypatch) -> None:
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "r-empty")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    result = _ex(tmp_vault, "r-empty", handler).execute(_action("no-such-topic-xyz"), task_id="ha-empty")
    assert result["ok"] is True
    assert result["results"]["web_hits"] == []
    assert load_manifest(tmp_vault, "r-empty")["status"] == "running"
    rows = (tmp_vault.run_dir("r-empty") / RUN_TRIP_LOG_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    import json

    row = json.loads(rows[0])
    assert row["event_id"] == "r-empty:ha-empty"
    assert row["hits"] == 0


def test_empty_serp_all_unresponsive_continues(tmp_vault, tmp_path, monkeypatch) -> None:
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "r-unr")
    engines = [["google", "CAPTCHA"], ["brave", "timeout"]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "unresponsive_engines": engines})

    result = _ex(tmp_vault, "r-unr", handler).execute(_action("q"), task_id="ha-unr")
    assert result["ok"] is True
    assert result["results"]["unresponsive_engines"] == engines
    assert load_manifest(tmp_vault, "r-unr")["blocked_on"] is None


def test_zero_hits_still_carry_unresponsive_engines(tmp_vault, tmp_path, monkeypatch) -> None:
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "r-z")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]},
        )

    result = _ex(tmp_vault, "r-z", handler).execute(_action("q"), task_id="ha-z")
    assert result["results"]["web_hits"] == []
    assert result["results"]["unresponsive_engines"] == [["google", "CAPTCHA"]]


def test_http_failure_blocks_even_with_vault_hits(seeded_vault, tmp_path, monkeypatch) -> None:
    _cfg(seeded_vault, tmp_path, monkeypatch)
    init_run(seeded_vault, "r-fail")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    with pytest.raises(SearchBlockError) as ei:
        _ex(seeded_vault, "r-fail", handler).execute(_action("python async"), task_id="ha-fail")
    assert ei.value.blocked_reason == "searxng_http"
    m = load_manifest(seeded_vault, "r-fail")
    assert m["blocked_on"] == "search"
    assert m["blocked_reason"] == "searxng_http"
    assert load_search_call(seeded_vault.run_dir("r-fail"), "ha-fail") is None
    trip = seeded_vault.run_dir("r-fail") / RUN_TRIP_LOG_NAME
    assert not trip.exists() or trip.read_text(encoding="utf-8").strip() == ""


def test_null_title_kept_as_empty_string_on_host_payload(tmp_vault, tmp_path, monkeypatch) -> None:
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "r-null")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"url": "https://example.com/k", "title": None, "content": "body"}]},
        )

    result = _ex(tmp_vault, "r-null", handler).execute(_action("q"), task_id="ha-null")
    hit = result["results"]["web_hits"][0]
    assert hit["url"] == "https://example.com/k"
    assert hit["title"] == ""
    assert hit["snippet"] == "body"


def test_search_does_not_widen_fetch_allow_private_hosts(tmp_vault, tmp_path, monkeypatch) -> None:
    _cfg(tmp_vault, tmp_path, monkeypatch, url="http://10.0.0.5:8080")
    init_run(tmp_vault, "r-ssrf")
    assert tmp_vault.config.fetch.allow_private_hosts == ()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    _ex(tmp_vault, "r-ssrf", handler).execute(_action("q"), task_id="ha-ssrf")
    assert tmp_vault.config.fetch.allow_private_hosts == ()


def test_fetch_still_refuses_private_model_url(tmp_vault) -> None:
    ex = HostExecutor(vault=tmp_vault, workspace_root=tmp_vault.root)
    result = ex.execute(
        HostAction(kind="fetch", args={"url": "http://127.0.0.1/secret"}, reason="x"),
        task_id="f1",
    )
    assert result["ok"] is False


def test_search_http_does_not_overwrite_budget_block(tmp_vault, tmp_path, monkeypatch) -> None:
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "r-bud")
    set_status(tmp_vault, "r-bud", "blocked", blocked_on="budget")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    with pytest.raises(SearchBlockError):
        _ex(tmp_vault, "r-bud", handler).execute(_action("q"), task_id="ha-bud")
    assert load_manifest(tmp_vault, "r-bud")["blocked_on"] == "budget"
