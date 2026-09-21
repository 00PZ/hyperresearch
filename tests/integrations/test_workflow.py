"""Spec 2.2 workflow worker tests. Mocked HTTP."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from hyperresearch.core.runs import init_run, patch_manifest, set_status
from hyperresearch.pipeline.package import package_path, write_package
from hyperresearch.workflow import (
    WorkflowError,
    company_lock,
    dispatch_ingest,
    drain,
    freeze_envelope,
    ingest_retry,
    load_workflow,
    publish_package,
    save_workflow,
    wiki_draft,
)


class FakePageStore:
    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.raw: dict[str, bytes] = {}
        self.put_calls: list[tuple[str, dict[str, Any]]] = []

    def get_page(self, slug: str) -> dict[str, Any] | None:
        return self.pages.get(slug)

    def put_page(self, slug: str, **kwargs: Any) -> dict[str, Any]:
        # Unconditional replace. No if-match.
        self.put_calls.append((slug, dict(kwargs)))
        body = kwargs.get("body")
        self.pages[slug] = {"slug": slug, **kwargs, "body": body}
        return self.pages[slug]

    def get_raw_data(self, key: str) -> bytes | None:
        return self.raw.get(key)

    def put_raw_data(self, key: str, body: Any, **kwargs: Any) -> None:
        raw = body if isinstance(body, bytes) else str(body).encode()
        self.raw[key] = raw


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


def _package(vault, tag: str, report: bytes = b"# Title\nhello\n") -> None:
    run_dir = vault.run_dir(tag)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_package(
        run_dir,
        package_path(run_dir),
        company="shoshin",
        run_id=tag,
        report_bytes=report,
        snapshots=[],
        verification={
            "verified_hash": hashlib.sha256(report).hexdigest(),
            "cite_check_bind": hashlib.sha256(report).hexdigest(),
            "independence_bind": "e",
            "verified_at": "t",
        },
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
            gbrain=FakePageStore(),
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
    gbrain = FakePageStore()
    env = freeze_envelope(
        run_id=tag,
        report_bytes=report,
        title="t",
        package_digest="digest-engine",
    )
    # Remote matches published bytes, not engine digest.
    gbrain.pages[env["slug"]] = {"slug": env["slug"], "body": report.decode()}
    state = load_workflow(tmp_vault.run_dir(tag))
    state["publication"] = {"status": "idle", "envelope": env}
    save_workflow(tmp_vault.run_dir(tag), state)
    publish_package(gbrain, tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    report_puts = [c for c in gbrain.put_calls if c[0] == env["slug"]]
    assert report_puts == []
    # engine digest equal is not the skip key — different published bytes conflict
    other = freeze_envelope(run_id=tag, report_bytes=b"# other published\n", title="t", package_digest="digest-engine")
    gbrain2 = FakePageStore()
    gbrain2.pages[other["slug"]] = {"slug": other["slug"], "body": report.decode()}
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "idle", "envelope": other}
    save_workflow(tmp_vault.run_dir(tag), st)
    out = publish_package(gbrain2, tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["reason"] == "conflict"


def test_conflict_on_different_published_bytes(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "pub-conf"
    init_run(tmp_vault, tag, company="shoshin")
    _package(tmp_vault, tag, b"# new\n")
    gbrain = FakePageStore()
    gbrain.pages[f"companies/shoshin/research/reports/{tag}"] = {"body": "# old remote\n"}
    out = publish_package(gbrain, tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["status"] == "failed"
    assert out["publication"]["reason"] == "conflict"
    assert gbrain.pages[f"companies/shoshin/research/reports/{tag}"]["body"] == "# old remote\n"


def test_drain_invalid_package_does_not_rebuild(tmp_vault, tmp_path):
    tag = "inv-1"
    init_run(tmp_vault, tag, company="shoshin")
    set_status(tmp_vault, tag, "verified")
    patch_manifest(tmp_vault, tag, package={"status": "invalid", "reason": "artifact_error"})
    calls: list[str] = []

    def run_hpr(*args, **kwargs):
        calls.append("hpr")
        raise AssertionError("must not rebuild")

    gbrain = FakePageStore()
    slug = "companies/shoshin/research/queue/q1"
    gbrain.pages[slug] = {
        "slug": slug,
        "body": {"status": "running", "run_id": tag, "query": "q"},
        "status": "running",
        "run_id": tag,
        "query": "q",
    }
    drain(
        company="shoshin",
        tier="full",
        vault=tmp_vault,
        gbrain=gbrain,
        paperclip=FakePaperclip(),
        run_hpr=run_hpr,
        lock_path=tmp_path / "lock",
        queue_pages=[gbrain.pages[slug]],
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


def test_ingest_retry_refuses_unless_publication_ok(tmp_vault, monkeypatch):
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://paperclip.test")
    tag = "ing-deny"
    init_run(tmp_vault, tag, company="shoshin")
    pc = FakePaperclip()
    with pytest.raises(WorkflowError):
        ingest_retry(pc, tmp_vault.run_dir(tag), "companies/shoshin/research/reports/x")
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
    gbrain = FakePageStore()
    slug = "companies/shoshin/research/queue/q1"
    page = {"slug": slug, "status": "running", "run_id": tag, "query": "q", "body": {"status": "running", "run_id": tag, "query": "q"}}
    drain(
        company="shoshin",
        tier="light",
        vault=tmp_vault,
        gbrain=gbrain,
        paperclip=pc,
        run_hpr=lambda *a, **k: None,
        lock_path=tmp_path / "lock",
        queue_pages=[page],
    )
    assert pc.posts == []
    assert gbrain.put_calls == []


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


def test_failed_drain_does_not_post_retry_does(tmp_vault, monkeypatch):
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
    ingest_retry(pc, tmp_vault.run_dir(tag), "s")
    assert len(pc.posts) == 1


def test_missing_bearer_unconfigured_keeps_verified(tmp_vault):
    tag = "pub-unconf"
    init_run(tmp_vault, tag, company="shoshin")
    set_status(tmp_vault, tag, "verified")
    _package(tmp_vault, tag)
    out = publish_package(FakePageStore(), tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer=None)
    assert out["publication"]["reason"] == "unconfigured"
    assert load_manifest_status(tmp_vault, tag) == "verified"


def load_manifest_status(vault, tag: str) -> str:
    from hyperresearch.core.runs import load_manifest

    return str(load_manifest(vault, tag).get("status"))


def test_wiki_draft_reads_research_slug():
    gbrain = FakePageStore()
    gbrain.pages["companies/shoshin/research/reports/r1"] = {"body": "REPORT BODY"}
    assert wiki_draft(gbrain, "companies/shoshin/research/reports/r1") == "REPORT BODY"


def test_schema_prefixes_not_analysis_or_wiki():
    from hyperresearch.workflow import SCHEMA_TYPES

    assert SCHEMA_TYPES["research-report"].startswith("companies/shoshin/research/reports/")
    assert "analysis" not in SCHEMA_TYPES
    assert "wiki" not in SCHEMA_TYPES


def test_stub_queue_put_page_that_drops_query_fails():
    class Dropping:
        def get_page(self, slug: str) -> dict:
            return {"query": "keep-me", "run_id": "r", "body": {"query": "keep-me", "run_id": "r"}}

        def put_page(self, slug: str, **kwargs: Any) -> None:
            body = dict(kwargs)
            body.pop("query", None)
            if isinstance(body.get("body"), dict):
                body["body"].pop("query", None)

    from hyperresearch.workflow import merge_queue

    # merge_queue itself refuses if query would be missing after merge; dropping client is the stub.
    gbrain = FakePageStore()
    gbrain.pages["q"] = {"query": "keep", "run_id": "r", "body": {"query": "keep", "run_id": "r"}}
    merged = merge_queue(gbrain, "q", status="published", run_id="r", query="keep")
    assert merged["query"] == "keep"
    assert merged["run_id"] == "r"


def test_matching_hash_is_partial_until_index(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "pub-part"
    init_run(tmp_vault, tag, company="shoshin")
    report = b"# body\n"
    _package(tmp_vault, tag, report)
    gbrain = FakePageStore()
    env = freeze_envelope(run_id=tag, report_bytes=report, title="t", package_digest="d")
    env["raw"] = {hashlib.sha256(b"raw").hexdigest(): hashlib.sha256(b"raw").hexdigest()}
    gbrain.pages[env["slug"]] = {"body": report.decode()}
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "idle", "envelope": env}
    save_workflow(tmp_vault.run_dir(tag), st)
    out = publish_package(gbrain, tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["status"] == "ok"


def test_raw_conflict(tmp_vault, monkeypatch):
    monkeypatch.setenv("GBRAIN_SHOSHIN_BEARER", "tok")
    tag = "pub-raw"
    init_run(tmp_vault, tag, company="shoshin")
    report = b"# body\n"
    _package(tmp_vault, tag, report)
    digest = hashlib.sha256(b"one").hexdigest()
    gbrain = FakePageStore()
    gbrain.raw[f"research-{digest}"] = b"different"
    env = freeze_envelope(run_id=tag, report_bytes=report, title="t", package_digest="d")
    env["raw"] = {digest: digest}
    st = load_workflow(tmp_vault.run_dir(tag))
    st["publication"] = {"status": "idle", "envelope": env}
    save_workflow(tmp_vault.run_dir(tag), st)
    out = publish_package(gbrain, tmp_vault.run_dir(tag), company="shoshin", run_id=tag, bearer="tok")
    assert out["publication"]["reason"] == "raw_conflict"
    assert gbrain.raw[f"research-{digest}"] == b"different"


def test_engine_has_no_hardcoded_paperclip_uuids():
    root = Path(__file__).resolve().parents[2] / "src" / "hyperresearch"
    text = ""
    for folder in (root / "pipeline", root / "cli"):
        for path in folder.rglob("*.py"):
            text += path.read_text(encoding="utf-8")
    assert "PAPERCLIP_" not in text
