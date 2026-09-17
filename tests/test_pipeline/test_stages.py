"""Full graph: upstream artifacts, draft multiplicity, distinct critics."""

from __future__ import annotations

from hyperresearch.core.profiles import resolve_profile
from hyperresearch.pipeline.orchestrator import CRITIC_NAMES, execute_run
from tests.test_pipeline.test_full import _rt, plant_src, run


def test_task_payloads_include_upstream_artifacts(tmp_vault):
    plant_src(tmp_vault, "st-art")
    rt = _rt()
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="st-art"))
    by_role: dict[str, str] = {}
    for task in rt.calls:
        by_role.setdefault(task.role, task.payload)
    assert "Decomposition" in by_role["contradiction"] or "prompt-decomposition" in by_role["contradiction"]
    assert "graph" in by_role["loci"] or "contradiction-graph" in by_role["loci"]
    assert "comparisons" in by_role["tensions"].lower() or "loci" in by_role["tensions"].lower()
    assert "Evidence notes" in by_role["digest"]
    assert "draft-a.md" in by_role["synthesizer"]
    assert "critic-findings-dialectic.json" in by_role["patcher"]


def test_configured_draft_count_and_four_distinct_critics(tmp_vault):
    plant_src(tmp_vault, "st-multi")
    rt = _rt()
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="st-multi"))
    profile = resolve_profile("full", tmp_vault.config_path)
    draft_ids = [c.task_id for c in rt.calls if c.role == "draft"]
    assert len(draft_ids) == profile.draft_count
    assert len(set(draft_ids)) == profile.draft_count
    critic_roles = [c.role for c in rt.calls if c.role.startswith("critic_")]
    assert sorted(critic_roles) == sorted(f"critic_{n}" for n in CRITIC_NAMES)
    for name in CRITIC_NAMES:
        path = tmp_vault.run_dir("st-multi") / f"critic-findings-{name}.json"
        assert path.exists()
        assert path.read_text(encoding="utf-8").strip()


def test_critic_finding_reaches_patcher_payload(tmp_vault):
    plant_src(tmp_vault, "st-crit")
    finding = '{"findings": [{"severity": "major", "issue": "UNIQUE_CRITIC_TOKEN", "suggestion": "fix"}]}'
    rt = _rt(critic_dialectic=finding)
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="st-crit"))
    patcher = [c for c in rt.calls if c.role == "patcher"]
    assert patcher
    assert "UNIQUE_CRITIC_TOKEN" in patcher[0].payload


def test_draft_reaches_synthesis_payload(tmp_vault):
    plant_src(tmp_vault, "st-draft")
    marker = "DRAFT_ANGLE_A_UNIQUE"
    rt = _rt(draft=marker + "\n## Findings\n\n" + ("word " * 80))
    run(execute_run(tmp_vault, "What is X?", rt, profile="full", tag="st-draft"))
    synth = [c for c in rt.calls if c.role == "synthesizer"]
    assert synth
    assert marker in synth[0].payload
    assert "draft-a.md" in synth[0].payload
