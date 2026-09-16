"""Persistent run-wide budget: zero blocks dispatch; resume keeps spend."""

from __future__ import annotations

from hyperresearch.core.runs import load_manifest
from hyperresearch.pipeline.orchestrator import execute_run, resume_run
from hyperresearch.runtime import FakeRuntime
from tests.test_pipeline.test_full import _rt, plant_src, run


def test_budget_usd_zero_prevents_paid_model_dispatch(tmp_vault):
    plant_src(tmp_vault, "bud-zero")
    rt = _rt()
    result = run(
        execute_run(
            tmp_vault,
            "What is X?",
            rt,
            profile="full",
            tag="bud-zero",
            budget_usd=0,
        )
    )
    assert rt.calls == []
    assert result["manifest"]["status"] == "blocked"
    assert result["manifest"]["blocked_on"] == "budget"
    assert result["manifest"]["status"] != "verified"
    assert result["manifest"]["status"] != "completed"


def test_stages_cannot_each_consume_entire_budget(tmp_vault):
    plant_src(tmp_vault, "bud-share")
    rt = _rt()
    result = run(
        execute_run(
            tmp_vault,
            "What is X?",
            rt,
            profile="full",
            tag="bud-share",
            budget_usd=2,
        )
    )
    # Default reserve is 1 USD per dispatch. Two calls, then blocked.
    assert len(rt.calls) == 2
    assert result["manifest"]["status"] == "blocked"
    assert result["manifest"]["blocked_on"] == "budget"
    spent = result["manifest"]["spend"]["estimated_usd"]
    assert spent >= 2


def test_resume_preserves_spend(tmp_vault):
    plant_src(tmp_vault, "bud-resume")
    rt = _rt()
    first = run(
        execute_run(
            tmp_vault,
            "What is X?",
            rt,
            profile="full",
            tag="bud-resume",
            budget_usd=2,
        )
    )
    spent = first["manifest"]["spend"]["estimated_usd"]
    assert spent > 0
    rt2 = FakeRuntime(default="should-not-reset")
    second = run(resume_run(tmp_vault, "bud-resume", rt2))
    assert second["manifest"]["spend"]["estimated_usd"] == spent
    assert load_manifest(tmp_vault, "bud-resume")["spend"]["estimated_usd"] == spent


def test_exhaustion_cannot_produce_verified(tmp_vault):
    plant_src(tmp_vault, "bud-exh")
    result = run(
        execute_run(
            tmp_vault,
            "What is X?",
            _rt(),
            profile="full",
            tag="bud-exh",
            budget_usd=1,
        )
    )
    assert result["manifest"]["status"] == "blocked"
    assert result["manifest"]["status"] != "verified"
    assert result["manifest"]["status"] != "completed"
