"""Spec 2.2 workflow worker tests. Mocked HTTP."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from hyperresearch.core.frontmatter import FRONTMATTER_RE
from hyperresearch.core.runs import init_run, patch_manifest, set_status
from hyperresearch.pipeline.package import (
    package_path,
    validate_package,
    verification_of,
    write_package,
)
from hyperresearch.workflow import (
    WorkflowError,
    company_lock,
    dispatch_ingest,
    drain,
    freeze_envelope,
    harvest_gaps,
    ingest_retry,
    load_workflow,
    publish_package,
    save_workflow,
    wiki_draft,
)

INDEX_SLUG = "companies/shoshin/research/index"



def _md(meta: dict[str, Any], body: str = "") -> str:
    return f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n{body}"


def _page_from_content(slug: str, content: str) -> dict[str, Any]:
    match = FRONTMATTER_RE.match(content)
    fm: dict[str, Any] = {}
    rest = content
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            fm = loaded
        rest = content[match.end() :]
    return {
        "id": 1,
        "slug": slug,
        "type": fm.get("type"),
        "title": fm.get("title"),
        "compiled_truth": rest,
        "frontmatter": fm,
        "content": content,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "source_id": "shoshin",
        "tags": [],
    }


def _sse(rpc_id: int, payload: Any, *, is_error: bool = False) -> httpx.Response:
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        result["isError"] = True
    envelope = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    body = f"event: message\ndata: {json.dumps(envelope, separators=(',', ':'))}\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _fixture_payload(name: str) -> Any:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "gbrain_wire" / name
    raw = path.read_text(encoding="utf-8")
    if name.endswith(".json"):
        text = json.loads(raw)["result"]["content"][0]["text"]
        return json.loads(text)
    for line in raw.splitlines():
        if line.startswith("data:"):
            msg = json.loads(line[5:].strip())
            return json.loads(msg["result"]["content"][0]["text"])
    raise AssertionError(f"no data in {name}")


class McpWire:
    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.raw: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.raw_error = False

    def client(self) -> Any:
        from hyperresearch.knowledge.gbrain import GBrainClient

        return GBrainClient("http://gbrain.test/mcp", "tok", transport=httpx.MockTransport(self))

    def seed_markdown(self, slug: str, content: str) -> None:
        self.pages[slug] = _page_from_content(slug, content)

    def seed_page(self, page: dict[str, Any]) -> None:
        self.pages[str(page["slug"])] = page

    def tool_args(self, name: str) -> list[dict[str, Any]]:
        return [c["params"]["arguments"] for c in self.calls if c["params"]["name"] == name]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.calls.append(payload)
        rpc_id = payload["id"]
        name = payload["params"]["name"]
        args = payload["params"]["arguments"]
        if name == "get_page":
            slug = str(args.get("slug") or "")
            page = self.pages.get(slug)
            if page is None:
                return _sse(
                    rpc_id,
                    {"error": "page_not_found", "message": f"Page not found: {slug}"},
                    is_error=True,
                )
            body = dict(page)
            if not args.get("include_content"):
                body = {k: v for k, v in body.items() if k != "content"}
            return _sse(rpc_id, body)
        if name == "put_page":
            slug = str(args["slug"])
            self.pages[slug] = _page_from_content(slug, str(args.get("content") or ""))
            return _sse(rpc_id, {"slug": slug, "status": "created_or_updated"})
        if name == "get_raw_data":
            if self.raw_error:
                return httpx.Response(500, text="nope")
            slug = str(args.get("slug") or "")
            source = args.get("source")
            if source is None:
                rows = [
                    {"source": src, "data": data}
                    for (s, src), data in self.raw.items()
                    if s == slug
                ]
            else:
                data = self.raw.get((slug, str(source)))
                rows = [] if data is None else [{"source": source, "data": data}]
            return _sse(rpc_id, rows)
        if name == "put_raw_data":
            slug = str(args["slug"])
            source = str(args["source"])
            data = args["data"]
            self.raw[(slug, source)] = data if isinstance(data, dict) else {"body": data}
            return _sse(rpc_id, {"ok": True})
        if name == "list_pages":
            typ = args.get("type")
            rows = []
            for slug, page in self.pages.items():
                if typ and page.get("type") != typ:
                    continue
                rows.append(
                    {
                        "slug": slug,
                        "source_id": page.get("source_id") or "shoshin",
                        "type": page.get("type"),
                        "title": page.get("title") or slug,
                        "updated_at": "2026-09-28T00:00:00.000Z",
                    }
                )
            offset = int(args.get("offset") or 0)
            limit = int(args.get("limit") or 100)
            return _sse(rpc_id, rows[offset : offset + limit])
        if name == "search":
            return _sse(rpc_id, [])
        return _sse(rpc_id, {"error": "invalid_params", "message": name}, is_error=True)


def _assert_live_args(wire: McpWire) -> None:
    for call in wire.calls:
        args = call["params"]["arguments"]
        assert "prefix" not in args
        assert "key" not in args
        assert "body" not in args


class FakePaperclip:
    def __init__(self, post_status: int = 201, post_json: dict[str, Any] | None = None) -> None:
        self.post_status = post_status
        self.post_json = post_json if post_json is not None else {"id": "iss-1"}
        self.posts: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any] | None = None, headers: dict | None = None) -> Any:
        self.posts.append(json or {})
        payload = json or {}

        class R:
            status_code = self.post_status
            content = b"{}" if self.post_json else b""
            payload = self.post_json

            def json(self) -> Any:
                return self.payload

        if self.post_status == 201 and self.post_json and self.post_json.get("id"):
            desc = str(payload.get("description") or "")
            self.issues.append({"id": self.post_json["id"], "description": desc})
        return R()

    def get(self, url: str, params: dict | None = None) -> Any:
        issues = list(self.issues)

        class R:
            status_code = 200

            def json(self) -> Any:
                return {"issues": issues, "pages": 1}

        return R()


def _package(vault, tag: str, report: bytes = b"# Title\nhello\n", snapshots: list | None = None) -> None:
    run_dir = vault.run_dir(tag)
    run_dir.mkdir(parents=True, exist_ok=True)
    snaps = snapshots or []
    write_package(
        run_dir,
        package_path(run_dir),
        company="shoshin",
        run_id=tag,
        report_bytes=report,
        snapshots=snaps,
        verification=verification_of(report, snaps, verified_at="t"),
    )


def test_two_workers_lock(tmp_vault, tmp_path):
    lock = tmp_path / "co.lock"
    held = []

    def run_hpr(*args, **kwargs):
        held.append("hpr")
        raise AssertionError("second worker must not start hpr")

    with company_lock(lock), pytest.raises(WorkflowError, match="lock held"):
        drain(
            company="shoshin",
            tier="full",
            vault=tmp_vault,
            gbrain=McpWire().client(),
            paperclip=FakePaperclip(),
            run_hpr=run_hpr,
            lock_path=lock,
            queue_pages=[{"slug": "companies/shoshin/research/queue/q1", "status": "pending", "query": "q"}],
        )
    assert held == []


def test_skip_put_page_uses_envelope_published_hash(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "pub-skip"
    init_run(tmp_vault, tag, company="shoshin")
    report = b"# published body\n"
    _package(tmp_vault, tag, report)
    env = freeze_envelope(run_id=tag, report_bytes=report, title="t", package_digest="digest-engine")
    wire = McpWire()
    wire.seed_markdown(
        env["slug"],
        _md({"type": "research-report", "published_sha256": env["published_report_sha256"]}, report.decode()),
    )
    state = load_workflow(tmp_vault.run_dir(tag))
    state["publication"] = {"status": "idle", "envelope": env}
    save_workflow(tmp_vault.run_dir(tag), state)
    publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert [a for a in wire.tool_args("put_page") if a["slug"] == env["slug"]] == []
    other = freeze_envelope(run_id=tag, report_bytes=b"# other published\n", title="t", package_digest="digest-engine")
    wire2 = McpWire()
    wire2.seed_markdown(
        other["slug"],
        _md({"type": "research-report", "published_sha256": env["published_report_sha256"]}, report.decode()),
    )
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "idle", "envelope": other}
    save_workflow(tmp_vault.run_dir(tag), st)
    out = publish_package(wire2.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["reason"] == "conflict"
    assert wire2.tool_args("put_page") == []


def test_conflict_on_different_published_bytes(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "pub-conf"
    init_run(tmp_vault, tag, company="shoshin")
    _package(tmp_vault, tag, b"# new\n")
    slug = f"companies/shoshin/research/reports/{tag}"
    wire = McpWire()
    original = _md({"type": "research-report", "published_sha256": "old-hash"}, "# old remote\n")
    wire.seed_markdown(slug, original)
    out = publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["status"] == "failed"
    assert out["publication"]["reason"] == "conflict"
    assert wire.pages[slug]["content"] == original
    assert wire.tool_args("put_page") == []


def test_drain_invalid_package_does_not_rebuild(tmp_vault, tmp_path):
    tag = "inv-1"
    init_run(tmp_vault, tag, company="shoshin")
    set_status(tmp_vault, tag, "verified")
    patch_manifest(tmp_vault, tag, package={"status": "invalid", "reason": "artifact_error"})
    calls: list[str] = []

    def run_hpr(*args, **kwargs):
        calls.append("hpr")
        raise AssertionError("must not rebuild")

    slug = "companies/shoshin/research/queue/q1"
    drain(
        company="shoshin",
        tier="full",
        vault=tmp_vault,
        gbrain=McpWire().client(),
        paperclip=FakePaperclip(),
        run_hpr=run_hpr,
        lock_path=tmp_path / "lock",
        queue_pages=[{"slug": slug, "status": "running", "run_id": tag, "query": "q"}],
    )
    assert calls == []


def test_paperclip_500_uncertain_no_extra_post(tmp_vault, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co")
    monkeypatch.setenv("PAPERCLIP_LIBRARIAN_AGENT_ID", "lib")
    tag = "ing-500"
    init_run(tmp_vault, tag, company="shoshin")
    state = load_workflow(tmp_vault.run_dir(tag))
    state["publication"] = {"status": "ok"}
    save_workflow(tmp_vault.run_dir(tag), state)
    pc = FakePaperclip(post_status=500, post_json={})
    dispatch_ingest(pc, tmp_vault.run_dir(tag), research_slug="companies/shoshin/research/reports/ing-500")
    assert load_workflow(tmp_vault.run_dir(tag))["ingest"]["status"] == "uncertain"
    assert len(pc.posts) == 1
    dispatch_ingest(pc, tmp_vault.run_dir(tag), research_slug="companies/shoshin/research/reports/ing-500")
    assert len(pc.posts) == 1
    assert load_workflow(tmp_vault.run_dir(tag))["ingest"]["status"] == "uncertain"


def test_ingest_retry_refuses_unless_publication_ok(tmp_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    tag = "ing-deny"
    init_run(tmp_vault, tag, company="shoshin")
    pc = FakePaperclip()
    with pytest.raises(WorkflowError):
        ingest_retry(pc, tmp_vault.run_dir(tag), "companies/shoshin/research/reports/x", tmp_path / "lock")
    assert pc.posts == []


def test_light_tier_completed_does_not_post_librarian(tmp_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co")
    monkeypatch.setenv("PAPERCLIP_LIBRARIAN_AGENT_ID", "lib")
    tag = "light-1"
    init_run(tmp_vault, tag, profile="light", company="shoshin")
    set_status(tmp_vault, tag, "completed")
    pc = FakePaperclip()
    wire = McpWire()
    slug = "companies/shoshin/research/queue/q1"
    drain(
        company="shoshin",
        tier="light",
        vault=tmp_vault,
        gbrain=wire.client(),
        paperclip=pc,
        run_hpr=lambda *a, **k: None,
        lock_path=tmp_path / "lock",
        queue_pages=[{"slug": slug, "status": "running", "run_id": tag, "query": "q"}],
    )
    assert pc.posts == []
    assert wire.tool_args("put_page") == []


def test_crash_after_publish_idle_first_post(tmp_vault, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co")
    monkeypatch.setenv("PAPERCLIP_LIBRARIAN_AGENT_ID", "lib")
    tag = "ing-idle"
    init_run(tmp_vault, tag, company="shoshin")
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "ok"}
    st["ingest"] = {"status": "idle"}
    save_workflow(tmp_vault.run_dir(tag), st)
    pc = FakePaperclip()
    dispatch_ingest(pc, tmp_vault.run_dir(tag), research_slug="companies/shoshin/research/reports/ing-idle")
    assert len(pc.posts) == 1
    assert "research_slug=" in pc.posts[0]["description"]
    assert "seed" in pc.posts[0]["description"]


def test_in_flight_uncertain_reconcile_only(tmp_vault, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    tag = "ing-rec"
    init_run(tmp_vault, tag, company="shoshin")
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "ok"}
    st["ingest"] = {"status": "in_flight"}
    save_workflow(tmp_vault.run_dir(tag), st)
    pc = FakePaperclip()
    dispatch_ingest(pc, tmp_vault.run_dir(tag), research_slug="slug-x")
    assert pc.posts == []


def test_failed_drain_does_not_post_retry_does(tmp_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co")
    monkeypatch.setenv("PAPERCLIP_LIBRARIAN_AGENT_ID", "lib")
    tag = "ing-fail"
    init_run(tmp_vault, tag, company="shoshin")
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "ok"}
    st["ingest"] = {"status": "failed"}
    save_workflow(tmp_vault.run_dir(tag), st)
    pc = FakePaperclip()
    dispatch_ingest(pc, tmp_vault.run_dir(tag), research_slug="s")
    assert pc.posts == []
    ingest_retry(pc, tmp_vault.run_dir(tag), "s", tmp_path / "lock")
    assert len(pc.posts) == 1


def test_missing_bearer_unconfigured_keeps_verified(tmp_vault):
    tag = "pub-unconf"
    init_run(tmp_vault, tag, company="shoshin")
    set_status(tmp_vault, tag, "verified")
    _package(tmp_vault, tag)
    out = publish_package(McpWire().client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer=None)
    assert out["publication"]["reason"] == "unconfigured"
    assert load_manifest_status(tmp_vault, tag) == "verified"


def load_manifest_status(vault, tag: str) -> str:
    from hyperresearch.core.runs import load_manifest

    return str(load_manifest(vault, tag).get("status"))


def test_wiki_draft_reads_research_slug():
    wire = McpWire()
    slug = "companies/shoshin/research/reports/r1"
    wire.seed_markdown(slug, _md({"type": "research-report"}, "REPORT BODY"))
    assert wiki_draft(wire.client(), slug) == "REPORT BODY"


def test_schema_prefixes_not_analysis_or_wiki():
    from hyperresearch.workflow import SCHEMA_TYPES

    assert SCHEMA_TYPES["research-report"].startswith("companies/shoshin/research/reports/")
    assert "analysis" not in SCHEMA_TYPES
    assert "wiki" not in SCHEMA_TYPES


def test_stub_queue_put_page_that_drops_query_fails():
    from hyperresearch.workflow import merge_queue

    wire = McpWire()
    wire.seed_markdown("q", _md({"type": "research-queue", "query": "keep", "run_id": "r"}))
    merged = merge_queue(wire.client(), "q", status="published", run_id="r", query="keep")
    assert merged["query"] == "keep"
    assert merged["run_id"] == "r"
    content = wire.tool_args("put_page")[-1]["content"]
    assert "query: keep" in content or "query:keep" in content


def test_matching_hash_is_partial_until_index(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "pub-part"
    init_run(tmp_vault, tag, company="shoshin")
    report = b"# body\n"
    _package(tmp_vault, tag, report)
    env = freeze_envelope(run_id=tag, report_bytes=report, title="t", package_digest="d")
    env["raw"] = []
    wire = McpWire()
    wire.seed_markdown(
        env["slug"],
        _md({"type": "research-report", "published_sha256": env["published_report_sha256"]}, report.decode()),
    )
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "idle", "envelope": env}
    save_workflow(tmp_vault.run_dir(tag), st)
    out = publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["status"] == "ok"
    assert [a for a in wire.tool_args("put_page") if a["slug"] == env["slug"]] == []


def test_raw_conflict(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "pub-raw"
    init_run(tmp_vault, tag, company="shoshin")
    report = b"# body\n"
    _package(tmp_vault, tag, report)
    digest = hashlib.sha256(b"one").hexdigest()
    env = freeze_envelope(run_id=tag, report_bytes=report, title="t", package_digest="d")
    env["raw"] = [{"sha256": digest, "body": "one"}]
    wire = McpWire()
    wire.seed_markdown(
        env["slug"],
        _md({"type": "research-report", "published_sha256": env["published_report_sha256"]}, report.decode()),
    )
    wire.raw[(env["slug"], f"research-{digest}")] = {"sha256": "different", "body": "different"}
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "idle", "envelope": env}
    save_workflow(tmp_vault.run_dir(tag), st)
    out = publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["reason"] == "raw_conflict"
    assert wire.raw[(env["slug"], f"research-{digest}")]["body"] == "different"
    assert wire.tool_args("put_raw_data") == []


def test_engine_has_no_hardcoded_paperclip_uuids():
    root = Path(__file__).resolve().parents[2] / "src" / "hyperresearch"
    text = ""
    for folder in (root / "pipeline", root / "cli"):
        for path in folder.rglob("*.py"):
            text += path.read_text(encoding="utf-8")
    assert "PAPERCLIP_" not in text


def _queue_items(wire: McpWire) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for slug, page in wire.pages.items():
        if slug.startswith("companies/shoshin/research/queue/"):
            items.append(page.get("frontmatter") or {})
    return items


def test_harvest_two_open_gaps_lines(tmp_path: Path) -> None:
    wire = McpWire()
    wire.seed_page(_fixture_payload("get_page_wiki.sse"))
    harvest_gaps(company="shoshin", gbrain=wire.client(), lock_path=tmp_path / "co.lock")
    items = _queue_items(wire)
    assert len(items) == 2
    assert {i["gap_text"] for i in items} == {"first known hole", "second known hole"}
    assert all(i["origin"] == "wiki-gap" and i["status"] == "pending" for i in items)
    _assert_live_args(wire)
    list_calls = wire.tool_args("list_pages")
    assert any("type" not in a for a in list_calls)
    assert any(a.get("type") == "research-queue" for a in list_calls)


def test_harvest_reharvest_adds_none(tmp_path: Path) -> None:
    wire = McpWire()
    wire.seed_page(_fixture_payload("get_page_wiki.sse"))
    lock = tmp_path / "co.lock"
    harvest_gaps(company="shoshin", gbrain=wire.client(), lock_path=lock)
    puts = len(wire.tool_args("put_page"))
    harvest_gaps(company="shoshin", gbrain=wire.client(), lock_path=lock)
    assert len(_queue_items(wire)) == 2
    assert len(wire.tool_args("put_page")) == puts


def test_harvest_empty_gaps_zero(tmp_path: Path) -> None:
    wire = McpWire()
    wire.seed_markdown(
        "companies/shoshin/knowledge/wiki/topic",
        _md({"type": "concept", "title": "t"}, "## Gaps\n\n## Other\n- not harvested\n"),
    )
    harvest_gaps(company="shoshin", gbrain=wire.client(), lock_path=tmp_path / "co.lock")
    assert _queue_items(wire) == []


def test_harvest_ignores_pages_outside_prefix(tmp_path: Path) -> None:
    wire = McpWire()
    mixed = _fixture_payload("list_pages_mixed.sse")
    for row in mixed:
        wire.pages[row["slug"]] = {**row, "frontmatter": {}, "compiled_truth": "", "content": _md({"type": row["type"]})}
    wire.seed_page(_fixture_payload("get_page_wiki.sse"))
    harvest_gaps(company="shoshin", gbrain=wire.client(), lock_path=tmp_path / "co.lock")
    assert all(
        not s.startswith("companies/stark-industries/")
        for s in wire.pages
        if s.startswith("companies/shoshin/research/queue/")
    )
    list_calls = wire.tool_args("list_pages")
    assert all("prefix" not in a for a in list_calls)


def test_drain_cli_passes_configured_runtime_and_reader(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from hyperresearch.core.vault import Vault
    from hyperresearch.knowledge.gbrain import GBrainReader
    from hyperresearch.runtime.fake import FakeRuntime
    from hyperresearch.workflow import cli as wfcli

    captured: dict[str, Any] = {}

    class Sentinel:
        name = "model"

    async def fake_execute_run(vault, query, runtime, **kwargs):
        captured["runtime"] = runtime
        captured["knowledge_backend"] = kwargs.get("knowledge_backend")
        captured["knowledge_reader"] = kwargs.get("knowledge_reader")
        return {"manifest": {"status": "running"}, "verify": {}, "tag": "t"}

    root = tmp_path / "shoshin"
    Vault.init(root, name="Shoshin")
    monkeypatch.setenv("HYPERRESEARCH_SHOSHIN_VAULT", str(root))
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    monkeypatch.setenv("GBRAIN_MCP_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("HYPERRESEARCH_KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.setattr(wfcli, "_make_runtime", lambda name: Sentinel() if name != "fake" else FakeRuntime())
    monkeypatch.setattr("hyperresearch.pipeline.orchestrator.execute_run", fake_execute_run)

    wire = McpWire()
    wire.seed_markdown(
        "companies/shoshin/research/queue/q1",
        _md({"type": "research-queue", "status": "pending", "query": "q"}),
    )
    monkeypatch.setattr(wfcli, "_gbrain", wire.client)
    result = CliRunner().invoke(wfcli.app, ["drain", "--company", "shoshin"])
    assert result.exit_code == 0, result.output
    assert captured["knowledge_backend"] == "gbrain"
    assert captured["runtime"].name == "model"
    assert not isinstance(captured["runtime"], FakeRuntime)
    assert isinstance(captured["knowledge_reader"], GBrainReader)


def test_hr_workflow_drain_fails_closed_without_vault_env(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from hyperresearch.core.vault import Vault
    from hyperresearch.workflow.cli import app

    monkeypatch.delenv("HYPERRESEARCH_SHOSHIN_VAULT", raising=False)
    Vault.init(tmp_path / "ambient", name="Ambient")
    monkeypatch.chdir(tmp_path / "ambient")
    result = CliRunner().invoke(app, ["drain", "--company", "shoshin"])
    assert result.exit_code != 0
    assert "HYPERRESEARCH_SHOSHIN_VAULT" in result.output


def test_successful_new_raw_write_and_retry_skip(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "raw-ok"
    init_run(tmp_vault, tag, company="shoshin")
    evidence = b"EVIDENCE-BODY"
    _package(
        tmp_vault,
        tag,
        b"# Title\nhello\n",
        snapshots=[
            {
                "ref": "d1",
                "document_id": "d1",
                "namespace": "shoshin",
                "type": "wiki",
                "body": evidence.decode(),
                "provenance": [],
            }
        ],
    )
    wire = McpWire()
    first = publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert first["publication"]["status"] == "ok"
    source = f"research-{hashlib.sha256(evidence).hexdigest()}"
    slug = f"companies/shoshin/research/reports/{tag}"
    raw_puts = wire.tool_args("put_raw_data")
    assert raw_puts == [
        {
            "slug": slug,
            "source": source,
            "data": {
                "sha256": hashlib.sha256(evidence).hexdigest(),
                "document_id": "d1",
                "namespace": "shoshin",
                "type": "wiki",
                "provenance": [],
                "body": evidence.decode(),
            },
        }
    ]
    report_put = wire.tool_args("put_page")[0]
    assert set(report_put) == {"slug", "content"}
    assert "type: research-report" in report_put["content"]
    assert "published_sha256:" in report_put["content"]
    _assert_live_args(wire)
    second = publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert second["publication"]["status"] == "ok"
    assert wire.tool_args("put_raw_data") == raw_puts


def test_get_raw_data_exception_does_not_overwrite(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "raw-exc"
    init_run(tmp_vault, tag, company="shoshin")
    evidence = b"EVIDENCE-BODY"
    _package(
        tmp_vault,
        tag,
        b"# Title\nhello\n",
        snapshots=[
            {
                "ref": "d1",
                "document_id": "d1",
                "body": evidence.decode(),
            }
        ],
    )
    wire = McpWire()
    wire.raw_error = True
    out = publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["reason"] == "http"
    assert wire.tool_args("put_raw_data") == []


def test_index_merge_keeps_existing_yaml_row(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "idx-md"
    init_run(tmp_vault, tag, company="shoshin")
    _package(tmp_vault, tag, b"# Title\nhello\n")
    wire = McpWire()
    wire.seed_page(_fixture_payload("get_page_index_include_content.sse"))
    publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    puts = [a for a in wire.tool_args("put_page") if a["slug"] == INDEX_SLUG]
    assert puts
    body = puts[-1]["content"]
    assert "r0" in body
    assert tag in body
    assert "Human prose kept" in body


def test_overlapping_ingest_retries_second_fails_immediately(tmp_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co")
    monkeypatch.setenv("PAPERCLIP_LIBRARIAN_AGENT_ID", "lib")
    tag = "ing-lock"
    init_run(tmp_vault, tag, company="shoshin")
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "ok"}
    st["ingest"] = {"status": "failed"}
    save_workflow(tmp_vault.run_dir(tag), st)
    lock = tmp_path / "co.lock"
    pc = FakePaperclip()
    with company_lock(lock), pytest.raises(WorkflowError, match="lock held"):
        ingest_retry(pc, tmp_vault.run_dir(tag), "s", lock)
    assert pc.posts == []


def test_ingest_retry_vs_drain_serializes(tmp_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "co")
    monkeypatch.setenv("PAPERCLIP_LIBRARIAN_AGENT_ID", "lib")
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "ing-drain"
    init_run(tmp_vault, tag, company="shoshin")
    set_status(tmp_vault, tag, "verified")
    _package(tmp_vault, tag)
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "ok"}
    st["ingest"] = {"status": "failed"}
    save_workflow(tmp_vault.run_dir(tag), st)
    lock = tmp_path / "co.lock"
    pc = FakePaperclip()
    slug = "companies/shoshin/research/queue/q1"
    page = {"slug": slug, "status": "running", "run_id": tag, "query": "q"}
    with company_lock(lock):
        with pytest.raises(WorkflowError, match="lock held"):
            ingest_retry(pc, tmp_vault.run_dir(tag), "s", lock)
        with pytest.raises(WorkflowError, match="lock held"):
            drain(
                company="shoshin",
                tier="full",
                vault=tmp_vault,
                gbrain=McpWire().client(),
                paperclip=pc,
                run_hpr=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no hpr")),
                lock_path=lock,
                queue_pages=[page],
            )
    assert pc.posts == []


def test_interrupted_save_workflow_retains_previous(tmp_path):
    run_dir = tmp_path / "run"
    save_workflow(run_dir, {"publication": {"status": "ok"}, "n": 1})
    dest = run_dir / "workflow.json"
    assert json.loads(dest.read_text(encoding="utf-8"))["n"] == 1

    def boom_replace(src: str, dst: str) -> None:
        raise OSError("interrupt")

    import os

    real = os.replace

    def guarded(src: str, dst: str) -> None:
        if str(dst).endswith("workflow.json"):
            boom_replace(src, dst)
        else:
            real(src, dst)

    try:
        os.replace = guarded  # type: ignore[method-assign]
        with pytest.raises(OSError):
            save_workflow(run_dir, {"publication": {"status": "ok"}, "n": 2})
    finally:
        os.replace = real  # type: ignore[method-assign]
    assert json.loads(dest.read_text(encoding="utf-8"))["n"] == 1
    assert dest.read_text(encoding="utf-8")[0] != "{" or json.loads(dest.read_text(encoding="utf-8"))["n"] == 1


def test_drain_runtime_recovers_missing_package(tmp_vault, tmp_path):
    import asyncio
    import shutil

    from hyperresearch.pipeline.orchestrator import execute_run
    from hyperresearch.runtime import FakeRuntime
    from tests.test_pipeline.test_full import _rt, plant_src, run

    tag = "drain-rec"
    plant_src(tmp_vault, tag)
    result = run(
        execute_run(
            tmp_vault,
            "q",
            _rt(),
            profile="full",
            tag=tag,
            company="shoshin",
        )
    )
    assert result["manifest"]["status"] == "verified"
    dest = package_path(tmp_vault.run_dir(tag))
    shutil.rmtree(dest)

    class BoomRuntime(FakeRuntime):
        async def run(self, task, context):  # type: ignore[no-untyped-def]
            raise AssertionError("ModelRuntime must not run")

    def run_hpr(query: str, run_id: str, resume: bool = False) -> Any:
        return asyncio.run(
            execute_run(
                tmp_vault,
                query,
                BoomRuntime(),
                profile="full",
                tag=run_id,
                resume=True,
                company="shoshin",
            )
        )

    slug = "companies/shoshin/research/queue/q-rec"
    page = {"slug": slug, "status": "running", "run_id": tag, "query": "q"}
    drain(
        company="shoshin",
        tier="full",
        vault=tmp_vault,
        gbrain=McpWire().client(),
        paperclip=None,
        run_hpr=run_hpr,
        lock_path=tmp_path / "lock",
        queue_pages=[page],
        runtime=object(),
    )
    assert dest.is_dir()
    validate_package(dest)


def test_index_complete_markdown_keeps_old_row(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "idx-complete"
    init_run(tmp_vault, tag, company="shoshin")
    _package(tmp_vault, tag, b"# Title\nhello\n")
    original = (
        "---\nreports:\n  - run_id: old\n    slug: old\n"
        "---\n# Research catalog\nPublished reports for Shoshin.\n"
    )
    wire = McpWire()
    wire.seed_markdown(INDEX_SLUG, original)
    publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    puts = [a for a in wire.tool_args("put_page") if a["slug"] == INDEX_SLUG]
    assert puts
    body = puts[-1]["content"]
    assert "run_id: old" in body or "old" in body
    assert tag in body
    assert "Research catalog" in body


def test_unparseable_index_fails_remote_unchanged(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "idx-bad"
    init_run(tmp_vault, tag, company="shoshin")
    _package(tmp_vault, tag, b"# Title\nhello\n")
    original = "---\nreports: [\n---\n# Research catalog\n"
    wire = McpWire()
    wire.pages[INDEX_SLUG] = {
        "slug": INDEX_SLUG,
        "type": "research-index",
        "compiled_truth": "",
        "frontmatter": {},
        "content": original,
    }
    out = publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["status"] == "failed"
    assert out["publication"]["reason"] == "conflict"
    assert wire.pages[INDEX_SLUG]["content"] == original
    assert not any(a["slug"] == INDEX_SLUG for a in wire.tool_args("put_page"))


def test_worker_request_shapes(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "shape-1"
    init_run(tmp_vault, tag, company="shoshin")
    evidence = b"EVIDENCE-BODY"
    _package(
        tmp_vault,
        tag,
        b"# Title\nhello\n",
        snapshots=[{"ref": "d1", "document_id": "d1", "body": evidence.decode()}],
    )
    wire = McpWire()
    publish_package(wire.client(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    _assert_live_args(wire)
    put_page = wire.tool_args("put_page")[0]
    assert set(put_page) == {"slug", "content"}
    assert "type: research-report" in put_page["content"]
    assert "title:" in put_page["content"]
    assert "published_sha256:" in put_page["content"]
    raw = wire.tool_args("put_raw_data")[0]
    assert set(raw) == {"slug", "source", "data"}
    assert isinstance(raw["data"], dict)
    got = wire.tool_args("get_raw_data")[0]
    assert set(got) == {"slug", "source"}
    get_index = next(a for a in wire.tool_args("get_page") if a["slug"] == INDEX_SLUG)
    assert get_index.get("include_content") is True
