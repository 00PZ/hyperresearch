"""Full path: cite-check binds (final report hash, evidence snapshot)."""

from __future__ import annotations

import asyncio
import json

from hyperresearch.core.note import write_note
from hyperresearch.core.runs import load_manifest
from hyperresearch.pipeline.host_actions import record_evidence
from hyperresearch.pipeline.orchestrator import (
    CITE_FINDINGS,
    EVIDENCE_DIGEST,
    INDEPENDENCE_ARTIFACT,
    _evidence_hash,
    _ship,
    _unquote_unmatched_report,
    execute_run,
    report_path,
    resume_run,
)
from hyperresearch.pipeline.patch import PatchState, content_hash
from hyperresearch.runtime import AgentResult, FakeRuntime
from hyperresearch.runtime.errors import UncertainSubmission

REPORT = "## Findings\n\n" + (
    "Substantive sentence with real evidence attached [[src-note]]. " * 80
)
DECOMP = json.dumps({"pipeline_tier": "full", "required_section_headings": ["Findings"]})
COMPLETE = {"kind": "complete", "args": {}, "reason": "done"}
EMPTY = '{"applied": []}'
DIGEST = "evidence snapshot one\n"
CITE_OK = '{"findings": []}'
CRITIC_EMPTY = '{"findings": []}'
SRC_BODY = "Substantive sentence with real evidence attached. Primary source excerpt.\n"


def _rt(**overrides):
    responses = {
        "decompose": DECOMP,
        "width": COMPLETE,
        "contradiction": "graph",
        "loci": "[]",
        "investigator": COMPLETE,
        "reconcile": "ok",
        "tensions": "[]",
        "corpus_critic": COMPLETE,
        "digest": DIGEST,
        "draft": REPORT,
        "synthesizer": REPORT,
        "critic_dialectic": CRITIC_EMPTY,
        "critic_depth": CRITIC_EMPTY,
        "critic_width": CRITIC_EMPTY,
        "critic_instruction": CRITIC_EMPTY,
        "gap_fetch": COMPLETE,
        "patcher": EMPTY,
        "cite_checker": CITE_OK,
        "polish": EMPTY,
        "readability": EMPTY,
    }
    responses.update(overrides)
    return FakeRuntime(responses=responses)


def run(coro):
    return asyncio.run(coro)


def plant_src(vault, tag: str, *, note_id: str = "src-note", tagged: bool = True) -> None:
    tags = [tag] if tagged else []
    write_note(
        vault.notes_dir,
        "Source Note",
        body=SRC_BODY,
        note_id=note_id,
        tags=tags,
        source=f"https://example.com/{note_id}",
        tier="institutional",
        content_type="article",
    )
    vault.auto_sync()
    vault.run_dir(tag).mkdir(parents=True, exist_ok=True)
    npath = vault.notes_dir / f"{note_id}.md"
    record_evidence(
        vault,
        tag,
        note_id,
        f"https://example.com/{note_id}",
        content_hash(npath.read_text(encoding="utf-8-sig")),
        "reused",
    )


def test_stale_cite_check_hash_is_not_verified(tmp_vault):
    plant_src(tmp_vault, "fl-old")
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag="fl-old"))
    assert result["manifest"]["status"] == "verified"
    findings_path = tmp_vault.run_dir("fl-old") / CITE_FINDINGS
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    assert data["report_hash"] == content_hash(
        report_path(tmp_vault, "fl-old").read_text(encoding="utf-8-sig")
    )
    data["report_hash"] = "0" * 64
    findings_path.write_text(json.dumps(data), encoding="utf-8")
    _ship(tmp_vault, "fl-old", "full")
    manifest = load_manifest(tmp_vault, "fl-old")
    assert manifest["status"] == "blocked"
    assert manifest["status"] != "verified"
    assert findings_path.exists()


def test_polish_that_changes_hash_reruns_cite_check(tmp_vault):
    token = "UNIQTOKEN"
    body = REPORT + f"\n{token}\n"
    plant_src(tmp_vault, "fl-polish")
    polish = AgentResult(
        text=EMPTY,
        structured={
            "base_report_hash": "late",
            "ops": [{"old_text": token, "new_text": "patched"}],
        },
        requested_model="m",
    )

    class PolishRuntime(FakeRuntime):
        async def run(self, task, context):
            result = await super().run(task, context)
            if task.role == "polish" and isinstance(result.structured, dict):
                path = report_path(tmp_vault, "fl-polish")
                current = path.read_text(encoding="utf-8")
                structured = dict(result.structured)
                structured["base_report_hash"] = content_hash(current)
                return AgentResult(
                    text=result.text,
                    structured=structured,
                    usage=result.usage,
                    requested_model=result.requested_model,
                )
            return result

    rt = PolishRuntime(responses=_rt(draft=body, synthesizer=body, polish=polish).responses)
    result = run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="fl-polish"))
    text = report_path(tmp_vault, "fl-polish").read_text(encoding="utf-8")
    assert "patched" in text
    assert token not in text
    assert result["manifest"]["status"] == "verified"
    findings = json.loads(
        (tmp_vault.run_dir("fl-polish") / CITE_FINDINGS).read_text(encoding="utf-8")
    )
    assert findings["report_hash"] == content_hash(text)
    cite_calls = [c for c in rt.calls if c.role == "cite_checker"]
    assert len(cite_calls) == 2


def test_full_verified_writes_bound_independence_artifact(tmp_vault):
    plant_src(tmp_vault, "fl-ind")
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag="fl-ind"))
    assert result["manifest"]["status"] == "verified"
    path = tmp_vault.run_dir("fl-ind") / INDEPENDENCE_ARTIFACT
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["evidence_hash"] == _evidence_hash(tmp_vault, "fl-ind")
    assert "scored" in data
    assert "clusters" in data
    assert (tmp_vault.run_dir("fl-ind") / EVIDENCE_DIGEST).exists()


def test_stale_independence_evidence_hash_is_not_verified(tmp_vault):
    plant_src(tmp_vault, "fl-ind-stale")
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag="fl-ind-stale"))
    assert result["manifest"]["status"] == "verified"
    path = tmp_vault.run_dir("fl-ind-stale") / INDEPENDENCE_ARTIFACT
    data = json.loads(path.read_text(encoding="utf-8"))
    data["evidence_hash"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    _ship(tmp_vault, "fl-ind-stale", "full")
    manifest = load_manifest(tmp_vault, "fl-ind-stale")
    assert manifest["status"] == "blocked"
    assert manifest["status"] != "verified"


def test_investigator_payload_contains_loci_marker(tmp_vault):
    marker = "LOCI_MARKER_ZX91"
    plant_src(tmp_vault, "pay-loci")
    rt = _rt(loci=json.dumps({"loci": [{"id": marker}]}))
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="pay-loci"))
    inv = [c for c in rt.calls if c.role == "investigator"]
    assert inv
    assert marker in inv[0].payload


def test_gap_fetch_payload_contains_critic_finding_marker(tmp_vault):
    marker = "CRITIC_MARKER_QW17"
    plant_src(tmp_vault, "pay-gap")
    finding = json.dumps({"findings": [{"severity": "major", "issue": marker, "suggestion": "fix"}]})
    rt = _rt(critic_dialectic=finding)
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="pay-gap"))
    gap = [c for c in rt.calls if c.role == "gap_fetch"]
    assert gap
    assert marker in gap[0].payload


def test_draft_payload_contains_digest_analysis_marker(tmp_vault):
    marker = "DIGEST_MARKER_AB42"
    plant_src(tmp_vault, "pay-draft")
    rt = _rt(digest=f"preceding analysis {marker}\n")
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="pay-draft"))
    drafts = [c for c in rt.calls if c.role == "draft"]
    assert drafts
    assert marker in drafts[0].payload

def test_ship_unquote_cannot_restamp_stale_cite_check(tmp_vault):
    """Review reproduction: unmatched quote + extra assertion must not inherit old audit."""
    tag = "fl-unquote-stale"
    plant_src(tmp_vault, tag)
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag=tag))
    assert result["manifest"]["status"] == "verified"
    findings_path = tmp_vault.run_dir(tag) / CITE_FINDINGS
    before = json.loads(findings_path.read_text(encoding="utf-8"))
    assert before.get("ok") is True
    cite_before = before.get("report_hash")
    path = report_path(tmp_vault, tag)
    extra = (
        "\nThe source states \u201cthis product guarantees unlimited profit forever\u201d "
        "[[src-note]]. Unrelated assertion about purple sky.\n"
    )
    path.write_text(path.read_text(encoding="utf-8-sig") + extra, encoding="utf-8")
    state_before = PatchState.load(tmp_vault.run_dir(tag) / "patch-state.json")
    shipped = _ship(tmp_vault, tag, "full")
    manifest = load_manifest(tmp_vault, tag)
    assert manifest["status"] != "verified"
    assert shipped.get("passed") is not True
    after = json.loads(findings_path.read_text(encoding="utf-8"))
    report = path.read_text(encoding="utf-8-sig")
    assert after.get("ok") is not True
    assert after.get("report_hash") != content_hash(report)
    assert cite_before != content_hash(report)
    state_after = PatchState.load(tmp_vault.run_dir(tag) / "patch-state.json")
    assert state_after.cumulative_hunks >= state_before.cumulative_hunks
    assert "this product guarantees unlimited profit forever" in report


def test_ship_unquote_counts_patch_accounting(tmp_vault):
    tag = "fl-unquote-acct"
    plant_src(tmp_vault, tag)
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag=tag))
    assert result["manifest"]["status"] == "verified"
    path = report_path(tmp_vault, tag)
    path.write_text(
        path.read_text(encoding="utf-8-sig")
        + "\nnot the same as \u201cofficial materials do not specify\u201d here.\n",
        encoding="utf-8",
    )
    before = PatchState.load(tmp_vault.run_dir(tag) / "patch-state.json")
    _ship(tmp_vault, tag, "full")
    after = PatchState.load(tmp_vault.run_dir(tag) / "patch-state.json")
    assert after.cumulative_bytes > before.cumulative_bytes
    assert after.cumulative_hunks > before.cumulative_hunks
    manifest = load_manifest(tmp_vault, tag)
    assert manifest["status"] != "verified"


def test_resume_runs_pending_final_cite_check_after_cleanup(tmp_vault):
    """Crash after unquote persist, before audit: resume runs the audit once."""
    tag = "fl-unquote-resume"
    plant_src(tmp_vault, tag)
    first = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag=tag))
    assert first["manifest"]["status"] == "verified"
    path = report_path(tmp_vault, tag)
    path.write_text(
        path.read_text(encoding="utf-8-sig")
        + "\nnot the same as \u201cofficial materials do not specify\u201d here.\n",
        encoding="utf-8",
    )
    assert _unquote_unmatched_report(tmp_vault, tag) >= 1
    rt2 = _rt()
    second = run(resume_run(tmp_vault, tag, rt2))
    cite = [c for c in rt2.calls if c.role == "cite_checker"]
    assert len(cite) == 1
    assert second["manifest"]["status"] == "verified"


def test_resume_uncertain_final_cite_check_does_not_repost(tmp_vault):
    tag = "fl-unquote-unc"
    plant_src(tmp_vault, tag)
    first = run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag=tag))
    assert first["manifest"]["status"] == "verified"
    path = report_path(tmp_vault, tag)
    path.write_text(
        path.read_text(encoding="utf-8-sig")
        + "\nnot the same as \u201cofficial materials do not specify\u201d here.\n",
        encoding="utf-8",
    )
    assert _unquote_unmatched_report(tmp_vault, tag) >= 1

    class Unc(FakeRuntime):
        posts = 0

        async def run(self, task, context):
            if task.role == "cite_checker":
                type(self).posts += 1
                raise UncertainSubmission("read reset")
            return await super().run(task, context)

    Unc.posts = 0
    rt = Unc(responses=_rt().responses)
    second = run(resume_run(tmp_vault, tag, rt))
    assert Unc.posts == 1
    assert load_manifest(tmp_vault, tag)["status"] != "verified"
    run(resume_run(tmp_vault, tag, rt))
    assert Unc.posts == 1
    assert load_manifest(tmp_vault, tag)["status"] != "verified"
    assert second["manifest"]["status"] != "verified"


def test_duplicate_unmatched_quotes_are_unquoted(tmp_vault):
    """Same unmatched span twice: cleanup uses occurrence, run continues."""
    tag = "fl-dup-quote"
    plant_src(tmp_vault, tag)
    phrase = "a framing phrase with five words"
    quoted = f"\u201c{phrase}\u201d"
    body = REPORT + f"\n{quoted}\n{quoted}\n"
    result = run(execute_run(
        tmp_vault, "What is X?", _rt(draft=body, synthesizer=body), profile="full", tag=tag,
    ))
    text = report_path(tmp_vault, tag).read_text(encoding="utf-8-sig")
    assert quoted not in text
    assert phrase in text
    assert result["manifest"]["status"] == "verified"


def test_finalization_exception_persists_blocked_not_running(tmp_vault, monkeypatch):
    """Unexpected ship failure must not leave the manifest running."""
    tag = "fl-final-err"
    plant_src(tmp_vault, tag)

    def boom(*_a, **_k):
        raise RuntimeError("finalization boom")

    monkeypatch.setattr("hyperresearch.pipeline.orchestrator._ship", boom)
    try:
        run(execute_run(tmp_vault, "What is X?", _rt(), profile="full", tag=tag))
    except RuntimeError as exc:
        assert "finalization boom" in str(exc)
    else:
        raise AssertionError("expected finalization boom")
    manifest = load_manifest(tmp_vault, tag)
    assert manifest["status"] == "blocked"
    assert manifest.get("blocked_on") == "host-error"
