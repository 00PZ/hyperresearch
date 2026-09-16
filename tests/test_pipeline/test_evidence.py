"""Run evidence manifest: selected sources only; mutation invalidates gates."""

from __future__ import annotations

from hyperresearch.core.note import write_note
from hyperresearch.core.runs import load_manifest
from hyperresearch.pipeline.host_actions import (
    HostBudget,
    HostExecutor,
    load_evidence,
    run_host_action_loop,
)
from hyperresearch.pipeline.orchestrator import (
    EVIDENCE_DIGEST,
    _ship,
    execute_run,
    report_path,
)
from hyperresearch.runtime import AgentResult, AgentTask, FakeRuntime, ResearchContext
from tests.test_pipeline.test_full import SRC_BODY, _rt, plant_src, run


def test_fetched_and_reused_evidence_in_audit(tmp_vault):
    tag = "ev-fetch"
    plant_src(tmp_vault, tag, note_id="reused-note")
    note = tmp_vault.notes_dir / "fetched-note.md"
    note.write_text("# fetched\nbody\n", encoding="utf-8")

    def fetch_fn(url, tags=None):
        return {"note_id": "fetched-note", "url": url}

    script = {
        "loop-model-0": AgentResult(
            text="fetch",
            structured={"kind": "fetch", "args": {"url": "https://example.com/f"}, "reason": "x"},
            requested_model="m",
        ),
        "loop-model-1": AgentResult(
            text="read",
            structured={"kind": "evidence_read", "args": {"note_id": "reused-note"}, "reason": "x"},
            requested_model="m",
        ),
        "loop-model-2": AgentResult(
            text="done",
            structured={"kind": "complete", "args": {}, "reason": "enough"},
            requested_model="m",
        ),
    }
    ex = HostExecutor(
        vault=tmp_vault,
        workspace_root=tmp_vault.root,
        fetch_fn=fetch_fn,
        run_tag=tag,
    )
    run(
        run_host_action_loop(
            FakeRuntime(responses=script),
            AgentTask(
                task_id="loop",
                role="width",
                payload="q",
                model="m",
                allowed_actions=("search", "fetch", "evidence_read", "complete"),
            ),
            ResearchContext(
                run_id=tag,
                canonical_query="q",
                tier="full",
                profile="full",
                runtime_name="fake",
                workspace_root=tmp_vault.root,
            ),
            ex,
            HostBudget(max_iterations=8, max_seconds=30, max_cost_usd=99),
            task_id_prefix="loop",
        )
    )
    sources = load_evidence(tmp_vault, tag)
    ids = {s["note_id"] for s in sources}
    assert "fetched-note" in ids
    assert "reused-note" in ids
    origins = {s["note_id"]: s["origin"] for s in sources}
    assert origins["fetched-note"] == "fetched"
    assert origins["reused-note"] == "reused"


def test_unrelated_notes_not_implicit(tmp_vault):
    write_note(
        tmp_vault.notes_dir,
        "Unrelated",
        body="should not appear in evidence extra",
        note_id="unrelated-note",
        source="https://example.com/other",
        tier="institutional",
        content_type="article",
    )
    tmp_vault.auto_sync()
    plant_src(tmp_vault, "ev-unrel")
    rt = _rt()
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="ev-unrel"))
    draft_payloads = [c.payload for c in rt.calls if c.role in {"draft", "digest", "cite_checker"}]
    blob = "\n".join(draft_payloads)
    assert "unrelated-note" not in blob
    assert "should not appear in evidence extra" not in blob
    ids = {s["note_id"] for s in load_evidence(tmp_vault, "ev-unrel")}
    assert "unrelated-note" not in ids


def test_mutating_selected_source_invalidates_when_digest_unchanged(tmp_vault):
    plant_src(tmp_vault, "ev-mut")
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag="ev-mut"))
    assert result["manifest"]["status"] == "verified"
    digest = tmp_vault.run_dir("ev-mut") / EVIDENCE_DIGEST
    digest_text = digest.read_text(encoding="utf-8")
    npath = tmp_vault.notes_dir / "src-note.md"
    npath.write_text(npath.read_text(encoding="utf-8") + "\nMUTATED BYTES\n", encoding="utf-8")
    assert digest.read_text(encoding="utf-8") == digest_text
    _ship(tmp_vault, "ev-mut", "full")
    assert load_manifest(tmp_vault, "ev-mut")["status"] != "verified"


def test_cited_source_omitted_from_independence_cannot_pass(tmp_vault):
    # Note exists (not dangling) but is not tagged for this run and not scored.
    write_note(
        tmp_vault.notes_dir,
        "Source Note",
        body=SRC_BODY,
        note_id="src-note",
        tags=[],
        source="https://example.com/src-note",
        tier="institutional",
        content_type="article",
    )
    tmp_vault.auto_sync()
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag="ev-omit"))
    assert result["manifest"]["status"] == "blocked"
    assert result["manifest"]["status"] != "verified"
    assert report_path(tmp_vault, "ev-omit").exists()
