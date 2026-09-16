"""Full path: cite-check binds (final report hash, evidence snapshot)."""

from __future__ import annotations

import asyncio
import json

from hyperresearch.core.runs import load_manifest
from hyperresearch.pipeline.orchestrator import CITE_FINDINGS, _ship, execute_run, report_path
from hyperresearch.pipeline.patch import content_hash
from hyperresearch.runtime import AgentResult, FakeRuntime

REPORT = "## Findings\n\n" + (
    "Substantive sentence with real evidence attached [[src-note]]. " * 80
)
DECOMP = json.dumps({"pipeline_tier": "full", "required_section_headings": ["Findings"]})
COMPLETE = {"kind": "complete", "args": {}, "reason": "done"}
EMPTY = '{"applied": []}'
DIGEST = "evidence snapshot one\n"


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
        "critic": "[]",
        "gap_fetch": COMPLETE,
        "patcher": EMPTY,
        "cite_checker": '{"findings": []}',
        "polish": EMPTY,
        "readability": EMPTY,
    }
    responses.update(overrides)
    return FakeRuntime(responses=responses)


def run(coro):
    return asyncio.run(coro)


def test_stale_cite_check_hash_is_not_verified(tmp_vault):
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
