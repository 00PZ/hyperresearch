"""Light path: ship-check on final report → completed, never verified."""

from __future__ import annotations

import asyncio
import json

from hyperresearch.core.runs import load_manifest, verify_run
from hyperresearch.pipeline.orchestrator import execute_run, report_path
from hyperresearch.pipeline.patch import content_hash
from hyperresearch.runtime import AgentResult, FakeRuntime

REPORT = "## Findings\n\n" + (
    "Substantive sentence with real evidence attached [[src-note]]. " * 80
)
DECOMP = json.dumps({"pipeline_tier": "light", "required_section_headings": ["Findings"]})
COMPLETE = {"kind": "complete", "args": {}, "reason": "done"}
EMPTY = '{"applied": []}'


def _rt(**overrides):
    responses = {
        "decompose": DECOMP,
        "width": COMPLETE,
        "draft": REPORT,
        "polish": EMPTY,
        "readability": EMPTY,
    }
    responses.update(overrides)
    return FakeRuntime(responses=responses)


def run(coro):
    return asyncio.run(coro)


def test_light_ship_pass_is_completed_not_verified(tmp_vault):
    result = run(execute_run(tmp_vault, "What is X?", _rt(), profile="light", tag="lt-01"))
    manifest = result["manifest"]
    assert result["verify"]["passed"] is True
    assert manifest["status"] == "completed"
    assert manifest["status"] != "verified"
    assert manifest["status"] != "done"


def test_light_ship_fail_blocks(tmp_vault):
    bad = FakeRuntime(responses={
        "decompose": DECOMP,
        "width": COMPLETE,
        "draft": "too short, no citations, no heading",
        "polish": EMPTY,
        "readability": EMPTY,
    })
    result = run(execute_run(tmp_vault, "What is X?", bad, profile="light", tag="lt-02"))
    assert result["verify"]["passed"] is False
    assert result["manifest"]["status"] == "blocked"
    assert result["manifest"]["blocked_on"] == "verify"


def test_light_polish_that_changes_report_reruns_ship_check(tmp_vault):
    """Polish mutation invalidates a prior passing ship-check."""
    # First produce a passing report, then polish replaces a unique token.
    token = "UNIQTOKEN"
    body = "## Findings\n\n" + (
        "Substantive sentence with real evidence attached [[src-note]]. " * 80
    ) + f"\n{token}\n"
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
                path = report_path(tmp_vault, "lt-03")
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

    rt = PolishRuntime(responses={
        "decompose": DECOMP,
        "width": COMPLETE,
        "draft": body,
        "polish": polish,
        "readability": EMPTY,
    })
    result = run(execute_run(tmp_vault, "What is X?", rt, profile="light", tag="lt-03"))
    text = report_path(tmp_vault, "lt-03").read_text(encoding="utf-8")
    assert "patched" in text
    assert token not in text
    # Gate ran on the patched bytes.
    assert result["verify"]["passed"] is True
    assert load_manifest(tmp_vault, "lt-03")["status"] == "completed"
    assert verify_run(tmp_vault, "lt-03")["passed"] is True
