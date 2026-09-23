"""Spec 2.2 engine: KnowledgeReader, gaps, independence, isolation."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hyperresearch.core.independence import cluster_evidence_independence
from hyperresearch.core.note import write_note
from hyperresearch.core.runs import init_run
from hyperresearch.core.vault import Vault
from hyperresearch.pipeline.host_actions import (
    HostExecutor,
    persist_snapshot,
    record_evidence,
)
from hyperresearch.pipeline.knowledge import FakeKnowledgeReader, FilesReader, NoneReader
from hyperresearch.pipeline.orchestrator import INDEPENDENCE_ARTIFACT, execute_run, report_path
from hyperresearch.pipeline.patch import content_hash
from hyperresearch.runtime.errors import IllegalHostAction
from hyperresearch.runtime.types import HostAction
from tests.test_pipeline.test_full import SRC_BODY, _rt, plant_src, run


def _cfg(vault, tmp_path, monkeypatch, url: str = "http://127.0.0.1:8888") -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    vault.config.search_provider = "searxng"
    vault.config.searxng_url = url
    vault.config.web_provider = "crawl4ai"


def _ex(vault, tag: str, handler, **kwargs) -> HostExecutor:
    return HostExecutor(
        vault=vault,
        workspace_root=vault.root,
        run_tag=tag,
        httpx_transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_company_shoshin_vault_isolates_stark_fts(tmp_path):
    stark = Vault.init(tmp_path / "stark", name="Stark")
    shoshin = Vault.init(tmp_path / "shoshin", name="Shoshin")
    write_note(stark.notes_dir, "Stark secret", body="only-in-stark", note_id="stark-secret")
    stark.auto_sync()
    shoshin.auto_sync()
    from hyperresearch.search.fts import search_fts

    hits = search_fts(shoshin.db, "only-in-stark", limit=10) or []
    assert hits == []


def test_memory_search_does_not_call_searxng(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "mem-0", company="shoshin", knowledge_backend="none")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("SearXNG must not run on memory search")

    ex = _ex(tmp_vault, "mem-0", handler, company="shoshin", knowledge_reader=NoneReader())
    result = ex.execute(
        HostAction(kind="search", args={"query": "q", "mode": "memory"}, reason="m"),
        task_id="t0",
    )
    assert result["ok"] is True
    assert result["results"]["web_hits"] == []
    assert calls == []


def test_web_without_gap_id_blocks(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "gap-0", company="shoshin")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no web")

    ex = _ex(tmp_vault, "gap-0", handler, company="shoshin", knowledge_reader=NoneReader())
    with pytest.raises(IllegalHostAction):
        ex.execute(
            HostAction(kind="search", args={"query": "q", "mode": "web"}, reason="w"),
            task_id="t0",
        )


def test_web_with_durable_gap_may_call_searxng(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "gap-1", company="shoshin")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("searxng")
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    ex = _ex(tmp_vault, "gap-1", handler, company="shoshin", knowledge_reader=NoneReader())
    gap = {
        "id": "g1",
        "question": "what is missing",
        "reason": "uncovered",
        "memory_refs": [],
        "memory_search": {
            "query": "q",
            "scope": None,
            "ok": True,
            "hit_count": 0,
            "retrieved_at": "t",
        },
    }
    ex.execute(
        HostAction(kind="search", args={"query": "q", "mode": "memory", "gap": gap}, reason="m"),
        task_id="t0",
    )
    result = ex.execute(
        HostAction(kind="search", args={"query": "q", "mode": "web", "gap_id": "g1"}, reason="w"),
        task_id="t1",
    )
    assert result["ok"] is True
    assert calls == ["searxng"]


def test_host_rejects_stale_with_empty_memory_refs(tmp_vault):
    init_run(tmp_vault, "gap-stale", company="shoshin")
    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="gap-stale",
        company="shoshin",
        knowledge_reader=NoneReader(),
    )
    with pytest.raises(IllegalHostAction):
        ex.execute(
            HostAction(
                kind="search",
                args={
                    "query": "q",
                    "gap": {"id": "s", "question": "q", "reason": "stale", "memory_refs": []},
                },
                reason="x",
            ),
            task_id="t",
        )


def test_host_accepts_uncovered_with_zero_hit_memory_search(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "gap-u", company="shoshin")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("web")
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    ex = _ex(tmp_vault, "gap-u", handler, company="shoshin", knowledge_reader=NoneReader())
    gap = {
        "id": "u1",
        "question": "hole",
        "reason": "uncovered",
        "memory_search": {"query": "q", "ok": True, "hit_count": 0, "retrieved_at": "t"},
    }
    ex.execute(HostAction(kind="search", args={"query": "q", "gap": gap}, reason="m"), task_id="t0")
    ex.execute(
        HostAction(kind="search", args={"query": "q", "mode": "web", "gap_id": "u1"}, reason="w"),
        task_id="t1",
    )
    assert calls == ["web"]


def test_host_rejects_uncovered_without_memory_search(tmp_vault):
    init_run(tmp_vault, "gap-bad", company="shoshin")
    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="gap-bad",
        company="shoshin",
        knowledge_reader=NoneReader(),
    )
    with pytest.raises(IllegalHostAction):
        ex.execute(
            HostAction(
                kind="search",
                args={
                    "query": "q",
                    "gap": {"id": "u", "question": "q", "reason": "uncovered", "memory_refs": []},
                },
                reason="x",
            ),
            task_id="t",
        )


def test_host_accepts_uncovered_with_memory_refs_despite_nonzero_search(tmp_vault):
    init_run(tmp_vault, "gap-refs", company="shoshin")
    persist_snapshot(
        tmp_vault,
        "gap-refs",
        {
            "ref": "doc-a",
            "document_id": "doc-a",
            "namespace": "shoshin",
            "type": "wiki",
            "content_hash": "h",
            "body": "snap",
            "provenance": [],
        },
    )
    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="gap-refs",
        company="shoshin",
        knowledge_reader=NoneReader(),
    )
    result = ex.execute(
        HostAction(
            kind="search",
            args={
                "query": "q",
                "gap": {
                    "id": "u2",
                    "question": "q",
                    "reason": "uncovered",
                    "memory_refs": ["doc-a"],
                    "memory_search": {"query": "q", "ok": True, "hit_count": 3},
                },
            },
            reason="x",
        ),
        task_id="t",
    )
    assert result["ok"] is True


def test_emptying_hits_does_not_open_web(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "gap-empty", company="shoshin")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("web")
        return httpx.Response(200, json={"results": []})

    reader = FakeKnowledgeReader(hits=[])
    ex = _ex(tmp_vault, "gap-empty", handler, company="shoshin", knowledge_reader=reader)
    ex.execute(HostAction(kind="search", args={"query": "q"}, reason="m"), task_id="t0")
    assert calls == []


def test_fakeknowledgereader_timeout_does_not_open_searxng(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "to-0", company="shoshin")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("web")
        return httpx.Response(200, json={"results": []})

    reader = FakeKnowledgeReader(search_error="timeout")
    ex = _ex(tmp_vault, "to-0", handler, company="shoshin", knowledge_reader=reader)
    result = ex.execute(HostAction(kind="search", args={"query": "q"}, reason="m"), task_id="t0")
    assert result["ok"] is False
    with pytest.raises(IllegalHostAction):
        ex.execute(
            HostAction(
                kind="search",
                args={
                    "query": "q",
                    "gap": {
                        "id": "g",
                        "question": "q",
                        "reason": "uncovered",
                        "memory_refs": [],
                        "memory_search": result["results"]["memory_search"],
                    },
                },
                reason="x",
            ),
            task_id="t1",
        )
    assert calls == []


def test_knowledge_none_empty_success_may_uncover(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "none-u", company="shoshin", knowledge_backend="none")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("web")
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    ex = _ex(tmp_vault, "none-u", handler, company="shoshin", knowledge_reader=NoneReader())
    mem = ex.execute(HostAction(kind="search", args={"query": "q"}, reason="m"), task_id="t0")
    assert mem["ok"] is True
    assert mem["results"]["memory_search"]["hit_count"] == 0
    gap = {
        "id": "n1",
        "question": "q",
        "reason": "uncovered",
        "memory_search": mem["results"]["memory_search"],
    }
    ex.execute(HostAction(kind="search", args={"query": "q", "gap": gap}, reason="m"), task_id="t1")
    ex.execute(
        HostAction(kind="search", args={"query": "q", "mode": "web", "gap_id": "n1"}, reason="w"),
        task_id="t2",
    )
    assert calls == ["web"]


def test_gbrain_files_error_does_not(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "err-0", company="shoshin", knowledge_backend="files")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("web")
        return httpx.Response(200, json={"results": []})

    missing = FilesReader(root=tmp_path / "no-such-tree")
    ex = _ex(tmp_vault, "err-0", handler, company="shoshin", knowledge_reader=missing)
    result = ex.execute(HostAction(kind="search", args={"query": "q"}, reason="m"), task_id="t0")
    assert result["ok"] is False
    assert calls == []


def test_healthy_gbrain_files_zero_hit_may_uncover(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    tree = tmp_path / "kb"
    tree.mkdir()
    (tree / "note.md").write_text("unrelated", encoding="utf-8")
    init_run(tmp_vault, "zhit", company="shoshin", knowledge_backend="files")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("web")
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    reader = FilesReader(root=tree)
    ex = _ex(tmp_vault, "zhit", handler, company="shoshin", knowledge_reader=reader)
    mem = ex.execute(HostAction(kind="search", args={"query": "no-match-zz"}, reason="m"), task_id="t0")
    assert mem["ok"] is True
    assert mem["results"]["memory_search"]["hit_count"] == 0
    gap = {
        "id": "z1",
        "question": "q",
        "reason": "uncovered",
        "memory_search": mem["results"]["memory_search"],
    }
    ex.execute(HostAction(kind="search", args={"query": "q", "gap": gap}, reason="m"), task_id="t1")
    ex.execute(
        HostAction(kind="search", args={"query": "q", "mode": "web", "gap_id": "z1"}, reason="w"),
        task_id="t2",
    )
    assert calls == ["web"]


def test_empty_provenance_snapshots_do_not_satisfy_independence(tmp_vault):
    tag = "ind-empty"
    init_run(tmp_vault, tag, company="shoshin")
    persist_snapshot(
        tmp_vault,
        tag,
        {
            "ref": "doc-a",
            "document_id": "doc-a",
            "namespace": "shoshin",
            "type": "wiki",
            "content_hash": "h1",
            "body": "A",
            "provenance": [],
        },
    )
    persist_snapshot(
        tmp_vault,
        tag,
        {
            "ref": "doc-b",
            "document_id": "doc-b",
            "namespace": "shoshin",
            "type": "wiki",
            "content_hash": "h2",
            "body": "B",
            "provenance": [],
        },
    )
    report = "## Findings\n\n" + ("Context from [[doc-a]] and [[doc-b]]. " * 80)
    plant_src(tmp_vault, tag)
    npath = tmp_vault.notes_dir / "src-note.md"
    npath.write_text(npath.read_text(encoding="utf-8-sig").replace("https://example.com/src-note", ""), encoding="utf-8")
    tmp_vault.auto_sync()
    record_evidence(tmp_vault, tag, "src-note", "", content_hash(SRC_BODY), "reused")
    path = report_path(tmp_vault, tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = run(execute_run(tmp_vault, "q", _rt(draft=report, synthesizer=report), profile="full", tag=tag, company="shoshin"))
    assert result["manifest"]["status"] != "verified"
    data = cluster_evidence_independence(
        [
            {"id": "doc-a", "document_id": "doc-a", "provenance": []},
            {"id": "doc-b", "document_id": "doc-b", "provenance": []},
        ]
    )
    assert data["independent_source_count"] == 0


def test_known_distinct_provenance_does():
    data = cluster_evidence_independence(
        [
            {
                "id": "wiki-1",
                "document_id": "companies/shoshin/knowledge/wiki/a",
                "provenance": ["https://a.example/1"],
            },
            {
                "id": "wiki-2",
                "document_id": "companies/shoshin/knowledge/wiki/b",
                "provenance": ["https://b.example/2"],
            },
        ]
    )
    assert data["independent_source_count"] == 2


def test_shared_provenance_is_one_lineage():
    url = "https://shared.example/article"
    data = cluster_evidence_independence(
        [
            {"id": "wiki", "document_id": "wiki-page", "provenance": [url]},
            {"id": "report", "document_id": "research-report-1", "provenance": [url]},
        ]
    )
    assert data["independent_source_count"] == 1


def test_full_company_fakeruntime_cites_snapshot_known_provenance(tmp_vault):

    tag = "co-full"
    reader = FakeKnowledgeReader(
        hits=[
            {
                "ref": "mem-1",
                "namespace": "shoshin",
                "document_id": "mem-1",
                "type": "wiki",
                "content_hash": "x",
                "provenance": [],
            }
        ],
        pages={
            "mem-1": {
                "body": "company context",
                "namespace": "shoshin",
                "document_id": "mem-1",
                "type": "wiki",
                "content_hash": "x",
                "provenance": [],
            }
        },
    )
    write_note(
        tmp_vault.notes_dir,
        "Memory",
        body="company context",
        note_id="mem-1",
        tags=[tag],
        source="",
        tier="institutional",
        content_type="article",
    )
    persist_snapshot(
        tmp_vault,
        tag,
        {
            "ref": "mem-1",
            "document_id": "mem-1",
            "namespace": "shoshin",
            "type": "wiki",
            "content_hash": "x",
            "body": "company context",
            "provenance": [],
        },
    )
    plant_src(tmp_vault, tag, note_id="src-note")
    write_note(
        tmp_vault.notes_dir,
        "Other",
        body=SRC_BODY,
        note_id="web-2",
        tags=[tag],
        source="https://other.example/paper",
        tier="institutional",
        content_type="article",
    )
    tmp_vault.auto_sync()
    n2 = tmp_vault.notes_dir / "web-2.md"
    record_evidence(
        tmp_vault,
        tag,
        "web-2",
        "https://other.example/paper",
        content_hash(n2.read_text(encoding="utf-8-sig")),
        "reused",
    )
    report = "## Findings\n\n" + (
        "Substantive sentence with real evidence attached [[src-note]] [[web-2]] [[mem-1]]. " * 80
    )
    result = run(
        execute_run(
            tmp_vault,
            "q",
            _rt(draft=report, synthesizer=report),
            profile="full",
            tag=tag,
            company="shoshin",
            knowledge_reader=reader,
        )
    )
    assert result["manifest"]["status"] == "verified"
    data = json.loads((tmp_vault.run_dir(tag) / INDEPENDENCE_ARTIFACT).read_text(encoding="utf-8"))
    assert data["independent_source_count"] == 2
    assert (tmp_vault.run_dir(tag) / "verified-package" / "manifest.json").exists()


def test_fixture_change_after_snapshot_does_not_change_bound_hash(tmp_vault):
    tag = "snap-mut"
    init_run(tmp_vault, tag, company="shoshin")
    persist_snapshot(
        tmp_vault,
        tag,
        {
            "ref": "doc",
            "document_id": "doc",
            "namespace": "shoshin",
            "type": "wiki",
            "content_hash": "abc",
            "body": "frozen",
            "provenance": ["https://x.example"],
        },
    )
    from hyperresearch.pipeline.host_actions import evidence_content_hash

    before = evidence_content_hash(tmp_vault, tag)
    page = tmp_vault.run_dir(tag) / "snapshots" / "doc.json"
    live = json.loads(page.read_text(encoding="utf-8"))
    live["remote"] = "changed-later"
    # mutate a sidecar that is not the snapshot body file used in the hash? hash reads snapshot file.
    fixture = tmp_vault.root / "later.md"
    fixture.write_text("changed", encoding="utf-8")
    after = evidence_content_hash(tmp_vault, tag)
    assert before == after


def test_stark_evidence_read_prohibited(tmp_vault):
    init_run(tmp_vault, "st-0", company="shoshin")
    write_note(
        tmp_vault.notes_dir,
        "Leaked",
        body="should not leak",
        note_id="leaked",
    )
    tmp_vault.auto_sync()
    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        run_tag="st-0",
        company="shoshin",
        knowledge_reader=FakeKnowledgeReader(),
    )
    result = ex.execute(
        HostAction(
            kind="evidence_read",
            args={"ref": "companies/stark-industries/knowledge/wiki/home"},
            reason="x",
        ),
        task_id="t",
    )
    assert result["ok"] is False
    assert result["error"] == "prohibited"


def test_light_tier_company_completed_no_package(tmp_vault):
    decomp = json.dumps({"pipeline_tier": "light", "required_section_headings": ["Findings"]})
    result = run(
        execute_run(
            tmp_vault,
            "light q",
            _rt(decompose=decomp),
            profile="light",
            tag="lt-co",
            company="shoshin",
            knowledge_reader=NoneReader(),
        )
    )
    assert result["manifest"]["status"] == "completed"
    assert not (tmp_vault.run_dir("lt-co") / "verified-package").exists()

def test_hpr_imports_without_gbrain_paperclip_extras():
    import sys

    import hyperresearch.pipeline.orchestrator as orch

    assert "paperclip" not in sys.modules
    assert "hyperresearch.knowledge.gbrain" not in sys.modules
    assert orch.execute_run is execute_run


def test_grep_pipeline_cli_has_no_paperclip():
    root = Path(__file__).resolve().parents[2] / "src" / "hyperresearch"
    blobs = []
    for folder in (root / "pipeline", root / "cli"):
        for path in folder.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            blobs.append(text)
    joined = "\n".join(blobs)
    assert "Paperclip" not in joined
    assert "PAPERCLIP_" not in joined
    assert "hpr publish" not in joined
    assert "hpr queue" not in joined
    assert "hpr ingest-retry" not in joined


def test_files_reader_excludes_stark_industries_employee_secret(tmp_path):
    root = tmp_path / "kb"
    secret = root / "companies" / "stark-industries" / "employees" / "jarvis" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("jarvis-secret-token", encoding="utf-8")
    (root / "companies" / "shoshin" / "knowledge" / "wiki").mkdir(parents=True)
    (root / "companies" / "shoshin" / "knowledge" / "wiki" / "ok.md").write_text(
        "shoshin note", encoding="utf-8"
    )
    reader = FilesReader(root=root, namespace="shoshin")
    found = reader.search("secret")
    assert found["ok"] is True
    assert found["hit_count"] == 0
    assert all("stark-industries" not in h["ref"] for h in found["hits"])
    got = reader.get("companies/stark-industries/employees/jarvis/secret.md")
    assert got["ok"] is False


def test_fabricated_gap_does_not_open_web(tmp_vault, tmp_path, monkeypatch):
    _cfg(tmp_vault, tmp_path, monkeypatch)
    init_run(tmp_vault, "gap-fake", company="shoshin")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("searxng")
        return httpx.Response(200, json={"results": []})

    ex = _ex(
        tmp_vault,
        "gap-fake",
        handler,
        company="shoshin",
        knowledge_reader=FakeKnowledgeReader(),
    )
    with pytest.raises(IllegalHostAction):
        ex.execute(
            HostAction(
                kind="search",
                args={
                    "query": "q",
                    "mode": "web",
                    "gap_id": "invented",
                    "gap": {
                        "id": "invented",
                        "question": "q",
                        "reason": "stale",
                        "memory_refs": ["never-retrieved"],
                    },
                },
                reason="w",
            ),
            task_id="t0",
        )
    assert calls == []


def test_hpr_run_company_fails_closed_without_vault_env(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from hyperresearch.cli import app
    from hyperresearch.core.vault import Vault

    monkeypatch.delenv("HYPERRESEARCH_SHOSHIN_VAULT", raising=False)
    Vault.init(tmp_path / "ambient", name="Ambient")
    monkeypatch.chdir(tmp_path / "ambient")
    result = CliRunner().invoke(app, ["run", "go", "q", "--company", "shoshin"])
    assert result.exit_code != 0
    assert "HYPERRESEARCH_SHOSHIN_VAULT" in result.output


def test_gbrain_search_iserror_is_backend_error(tmp_path):
    from hyperresearch.knowledge.gbrain import GBrainClient, GBrainReader

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "Access denied"}],
                },
            },
        )

    client = GBrainClient("http://gbrain.test/mcp", "tok", transport=httpx.MockTransport(handler))
    reader = GBrainReader(client)
    out = reader.search("q")
    assert out["ok"] is False
    assert out.get("hit_count") == 0
    assert out.get("hits") == []


def test_gbrain_put_page_iserror_raises():
    from hyperresearch.knowledge.gbrain import GBrainClient, GBrainError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "Access denied"}],
                },
            },
        )

    client = GBrainClient("http://gbrain.test/mcp", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(GBrainError):
        client.put_page("companies/shoshin/research/reports/x", body="x")


def test_gbrain_put_raw_data_iserror_raises():
    from hyperresearch.knowledge.gbrain import GBrainClient, GBrainError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"isError": True, "content": [{"type": "text", "text": "no"}]},
            },
        )

    client = GBrainClient("http://gbrain.test/mcp", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(GBrainError):
        client.put_raw_data("research-abc", b"bytes")


def test_gbrain_unrecognized_search_payload_is_not_empty_success():
    from hyperresearch.knowledge.gbrain import GBrainClient, GBrainReader

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": "Access denied"}},
        )

    client = GBrainClient("http://gbrain.test/mcp", "tok", transport=httpx.MockTransport(handler))
    reader = GBrainReader(client)
    out = reader.search("q")
    assert out["ok"] is False
    assert out.get("hits") == []
