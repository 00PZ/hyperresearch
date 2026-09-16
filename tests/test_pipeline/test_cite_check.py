"""Cite-check fail-closed: malformed JSON and dangling pairs block."""

from __future__ import annotations

import json

from hyperresearch.pipeline.orchestrator import CITE_FINDINGS, execute_run
from tests.test_pipeline.test_full import _rt, plant_src, run


def test_invalid_json_cite_checker_blocks(tmp_vault):
    plant_src(tmp_vault, "cite-bad-json")
    result = run(
        execute_run(
            tmp_vault,
            "What is X?",
            _rt(cite_checker="This is not valid JSON..."),
            profile="full",
            tag="cite-bad-json",
        )
    )
    assert result["manifest"]["status"] == "blocked"
    assert result["manifest"]["status"] != "verified"
    data = json.loads((tmp_vault.run_dir("cite-bad-json") / CITE_FINDINGS).read_text(encoding="utf-8"))
    assert data.get("ok") is not True
    assert data.get("error") == "malformed"


def test_dangling_citations_empty_findings_blocks(tmp_vault):
    # No vault note for [[src-note]] → mechanical dangling. Empty findings must not pass.
    result = run(
        execute_run(
            tmp_vault,
            "What is X?",
            _rt(cite_checker='{"findings": []}'),
            profile="full",
            tag="cite-dangling",
        )
    )
    assert result["manifest"]["status"] == "blocked"
    assert result["manifest"]["status"] != "verified"
    data = json.loads((tmp_vault.run_dir("cite-dangling") / CITE_FINDINGS).read_text(encoding="utf-8"))
    assert data.get("ok") is not True
    assert data.get("dangling_blocked") is True
    assert data.get("dangling_count", 0) >= 1


def test_checker_payload_contains_report_claims_and_source_excerpts(tmp_vault):
    plant_src(tmp_vault, "cite-payload")
    rt = _rt()
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="cite-payload"))
    cite_calls = [c for c in rt.calls if c.role == "cite_checker"]
    assert cite_calls
    payload = cite_calls[0].payload
    assert "Substantive sentence with real evidence attached" in payload
    assert "Primary source excerpt" in payload
    assert "Cite-check pairs" in payload or "sampled_for_llm" in payload
    assert "[[src-note]]" in payload


def test_empty_cite_checker_output_blocks(tmp_vault):
    plant_src(tmp_vault, "cite-empty")
    result = run(
        execute_run(
            tmp_vault,
            "What is X?",
            _rt(cite_checker="   "),
            profile="full",
            tag="cite-empty",
        )
    )
    assert result["manifest"]["status"] == "blocked"
    data = json.loads((tmp_vault.run_dir("cite-empty") / CITE_FINDINGS).read_text(encoding="utf-8"))
    assert data.get("ok") is not True
    assert data.get("error") == "empty"
