"""Application-owned step graph. hpr run / resume execute host steps, not Claude Skills."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

from hyperresearch.core.citecheck import write_pairs_file
from hyperresearch.core.profiles import resolve_profile
from hyperresearch.core.runs import (
    init_run,
    load_manifest,
    set_status,
    set_step,
    verify_run,
)
from hyperresearch.core.vault import Vault
from hyperresearch.pipeline.checkpoints import UNCERTAIN_REMOTE, TaskLog
from hyperresearch.pipeline.host_actions import (
    HostBudget,
    HostExecutor,
    budget_from_profile,
    run_host_action_loop,
)
from hyperresearch.pipeline.patch import PatchOp, PatchSet, apply_patch_set, content_hash
from hyperresearch.runtime.errors import BrowserUnsupported
from hyperresearch.runtime.types import (
    AgentResult,
    AgentRuntime,
    AgentTask,
    ResearchContext,
)

LIGHT_STEPS = ("1", "2", "10", "15", "16")
FULL_STEPS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "14.5", "15", "16")
_INVESTIGATE = ("search", "fetch", "evidence_read", "complete")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
CITE_FINDINGS = "cite-check-findings.json"
EVIDENCE_DIGEST = "evidence-digest.md"


def mint_run_tag(query: str) -> str:
    slug = _SLUG_RE.sub("-", query.lower()).strip("-")[:24] or "run"
    if slug[0].isdigit():
        slug = f"r-{slug}"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def report_path(vault: Vault, tag: str) -> Path:
    return vault.root / "research" / "notes" / f"final_report_{tag}.md"


def task_log_for(vault: Vault, tag: str) -> TaskLog:
    return TaskLog(vault.run_dir(tag) / "task_log.jsonl")


def context_for(
    vault: Vault, tag: str, runtime_name: str, query: str, profile: str, tier: str
) -> ResearchContext:
    return ResearchContext(
        run_id=tag,
        canonical_query=query,
        tier=tier,
        profile=profile,
        runtime_name=runtime_name,
        workspace_root=vault.root,
    )


def _query_text(vault: Vault, tag: str, fallback: str = "") -> str:
    qpath = vault.run_dir(tag) / "query.md"
    if qpath.exists():
        return qpath.read_text(encoding="utf-8-sig")
    return fallback


def _declared_tier(run_dir: Path, default: str) -> str:
    decomp = run_dir / "prompt-decomposition.json"
    if not decomp.exists():
        return default
    try:
        data = json.loads(decomp.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return default
    tier = data.get("pipeline_tier")
    return tier if isinstance(tier, str) and tier.strip() else default


def step_ids_for(tier: str, profile_name: str, config_path: Path | None) -> tuple[str, ...]:
    if tier == "light":
        return LIGHT_STEPS
    if tier == "full":
        return FULL_STEPS
    resolved = resolve_profile(profile_name, config_path)
    return tuple(str(s) for s in resolved.steps)


async def run_many_retry(
    runtime: AgentRuntime,
    tasks: list[AgentTask],
    context: ResearchContext,
    concurrency: int,
    log: TaskLog,
) -> list[AgentResult]:
    """Retry only failed members. Successful siblings are not launched again."""
    results: dict[str, AgentResult] = {}
    pending = [t for t in tasks if not log.is_success(t.task_id)]
    for t in tasks:
        if log.is_success(t.task_id):
            results[t.task_id] = AgentResult(
                text=log.result_text(t.task_id), requested_model=t.model
            )

    sem = asyncio.Semaphore(max(1, concurrency))
    failed: list[AgentTask] = []

    async def one(t: AgentTask) -> None:
        decision = log.resume_model(t.task_id)
        if decision == "skip":
            results[t.task_id] = AgentResult(
                text=log.result_text(t.task_id), requested_model=t.model
            )
            return
        if decision == UNCERTAIN_REMOTE:
            raise RuntimeError(f"uncertain_remote:{t.task_id}")
        async with sem:
            log.begin(t.task_id, "model", {"role": t.role})
            try:
                r = await runtime.run(t, context)
            except Exception:
                log.fail(t.task_id, "run failed")
                failed.append(t)
                return
            log.succeed(t.task_id, {"text": r.text[:2000]})
            results[t.task_id] = r

    await asyncio.gather(*[one(t) for t in pending])
    for t in list(failed):
        decision = log.resume_model(t.task_id)
        if decision == UNCERTAIN_REMOTE:
            raise RuntimeError(f"uncertain_remote:{t.task_id}")
        log.begin(t.task_id, "model", {"role": t.role, "retry": True})
        r = await runtime.run(t, context)
        log.succeed(t.task_id, {"text": r.text[:2000]})
        results[t.task_id] = r
    return [results[t.task_id] for t in tasks]


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return content_hash(path.read_text(encoding="utf-8-sig"))


def _report_hash(vault: Vault, tag: str) -> str:
    return _file_hash(report_path(vault, tag))


def _evidence_hash(vault: Vault, tag: str) -> str:
    return _file_hash(vault.run_dir(tag) / EVIDENCE_DIGEST)


def _write_cite_findings(vault: Vault, tag: str, raw: str) -> None:
    try:
        data: Any = json.loads(raw) if raw.strip() else {"findings": []}
    except json.JSONDecodeError:
        data = {"findings": []}
    if isinstance(data, list):
        data = {"findings": data}
    if not isinstance(data, dict):
        data = {"findings": []}
    data["report_hash"] = _report_hash(vault, tag)
    data["evidence_hash"] = _evidence_hash(vault, tag)
    (vault.run_dir(tag) / CITE_FINDINGS).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def _cite_check_bound(vault: Vault, tag: str) -> bool:
    path = vault.run_dir(tag) / CITE_FINDINGS
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    return (
        data.get("report_hash") == _report_hash(vault, tag)
        and data.get("evidence_hash") == _evidence_hash(vault, tag)
    )


async def _model(
    runtime: AgentRuntime,
    context: ResearchContext,
    log: TaskLog,
    task: AgentTask,
) -> AgentResult:
    decision = log.resume_model(task.task_id)
    if decision == "skip":
        return AgentResult(text=log.result_text(task.task_id), requested_model=task.model)
    if decision == UNCERTAIN_REMOTE:
        raise RuntimeError(f"uncertain_remote:{task.task_id}")
    log.begin(task.task_id, "model", {"role": task.role})
    result = await runtime.run(task, context)
    log.succeed(task.task_id, {"text": result.text[:2000]})
    return result


async def _run_cite_check(
    vault: Vault,
    tag: str,
    runtime: AgentRuntime,
    context: ResearchContext,
    log: TaskLog,
    model: str,
    task_id: str,
) -> None:
    path = report_path(vault, tag)
    if path.exists():
        write_pairs_file(vault, tag, path)
    result = await _model(
        runtime,
        context,
        log,
        AgentTask(
            task_id=task_id,
            role="cite_checker",
            payload=context.canonical_query,
            model=model,
            allowed_actions=(),
        ),
    )
    _write_cite_findings(vault, tag, result.text)


def _maybe_patches(result: AgentResult, current_hash: str) -> PatchSet | None:
    structured = result.structured
    if not isinstance(structured, dict):
        return None
    ops_raw = structured.get("ops") or structured.get("patches")
    if not isinstance(ops_raw, list) or not ops_raw:
        return None
    ops = []
    for item in ops_raw:
        if not isinstance(item, dict):
            continue
        occ = item.get("occurrence")
        ops.append(
            PatchOp(
                old_text=str(item.get("old_text") or ""),
                new_text=str(item.get("new_text") or ""),
                occurrence=int(occ) if occ is not None else None,
                path=str(item.get("path") or ""),
            )
        )
    base = str(structured.get("base_report_hash") or current_hash)
    return PatchSet(base_report_hash=base, ops=tuple(ops))


def _assert_no_chrome(vault: Vault, tag: str) -> None:
    try:
        from hyperresearch.core.escalation import queue_stats

        stats = queue_stats(vault.db, vault_tag=tag)
    except Exception:
        return
    if stats and (stats.get("queued") or stats.get("needs_human")):
        raise BrowserUnsupported(
            "Claude-in-Chrome browser escalation is unsupported in Spec 1; "
            "queued escalations are not silent-skipped"
        )


async def execute_step(
    vault: Vault,
    tag: str,
    step: str,
    runtime: AgentRuntime,
    context: ResearchContext,
    log: TaskLog,
    executor: HostExecutor,
    budget: HostBudget,
) -> None:
    set_step(vault, tag, step, "running")
    run_dir = vault.run_dir(tag)
    model = "default"
    try:
        profile = resolve_profile(context.profile, vault.config_path)
        model = profile.models.synthesizer
    except Exception:
        pass

    if step == "1":
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id="step-1",
                role="decompose",
                payload=context.canonical_query,
                model=model,
                output_schema=dict,
                allowed_actions=(),
            ),
        )
        data = result.structured if isinstance(result.structured, dict) else json.loads(result.text)
        if "pipeline_tier" not in data:
            data["pipeline_tier"] = context.tier
        (run_dir / "prompt-decomposition.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
    elif step in {"2", "5", "8", "13"}:
        _assert_no_chrome(vault, tag)
        allowed = _INVESTIGATE
        role = {"2": "width", "5": "investigator", "8": "corpus_critic", "13": "gap_fetch"}[step]
        await run_host_action_loop(
            runtime,
            AgentTask(
                task_id=f"step-{step}",
                role=role,
                payload=context.canonical_query,
                model=model,
                output_schema=dict,
                allowed_actions=allowed,
            ),
            context,
            executor,
            budget,
            log,
            task_id_prefix=f"step-{step}",
        )
    elif step in {"10", "11"}:
        role = "draft" if step == "10" else "synthesizer"
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id=f"step-{step}",
                role=role,
                payload=context.canonical_query,
                model=model,
                allowed_actions=(),
            ),
        )
        text = result.text.strip()
        if not text:
            set_status(vault, tag, "blocked", blocked_on="empty-draft")
            set_step(vault, tag, step, "done")
            return
        path = report_path(vault, tag)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    elif step in {"3", "4", "6", "7", "9", "12"}:
        role = {
            "3": "contradiction",
            "4": "loci",
            "6": "reconcile",
            "7": "tensions",
            "9": "digest",
            "12": "critic",
        }[step]
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id=f"step-{step}",
                role=role,
                payload=context.canonical_query,
                model=model,
                allowed_actions=(),
            ),
        )
        artifact = {
            "3": "contradiction-graph.md",
            "4": "loci.json",
            "6": "comparisons.md",
            "7": "source-tensions.json",
            "9": "evidence-digest.md",
            "12": "critic-findings-dialectic.json",
        }[step]
        body = result.text if not artifact.endswith(".json") else (result.text or "[]")
        if step == "12":
            for name in (
                "critic-findings-dialectic.json",
                "critic-findings-depth.json",
                "critic-findings-width.json",
                "critic-findings-instruction.json",
            ):
                if not (run_dir / name).exists():
                    (run_dir / name).write_text(result.text or "[]", encoding="utf-8")
        else:
            (run_dir / artifact).write_text(body, encoding="utf-8")
    elif step == "14":
        path = report_path(vault, tag)
        current = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id="step-14",
                role="patcher",
                payload=context.canonical_query,
                model=model,
                output_schema=dict,
                allowed_actions=(),
            ),
        )
        patch_set = _maybe_patches(result, content_hash(current))
        if patch_set:
            apply_patch_set(
                path,
                patch_set,
                workspace_root=vault.root,
                state_path=run_dir / "patch-state.json",
                task_id="step-14-apply",
                log=log,
            )
        (run_dir / "patch-log.json").write_text(result.text or "[]", encoding="utf-8")
    elif step == "14.5":
        await _run_cite_check(vault, tag, runtime, context, log, model, "step-14.5")
    elif step in {"15", "16"}:
        path = report_path(vault, tag)
        before = _file_hash(path)
        role = "polish" if step == "15" else "readability"
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id=f"step-{step}",
                role=role,
                payload=context.canonical_query,
                model=model,
                output_schema=dict,
                allowed_actions=(),
            ),
        )
        patch_set = _maybe_patches(result, before)
        if patch_set:
            apply_patch_set(
                path,
                patch_set,
                workspace_root=vault.root,
                state_path=run_dir / "patch-state.json",
                task_id=f"step-{step}-apply",
                log=log,
            )
        after = _file_hash(path)
        if after != before and context.tier != "light":
            await _run_cite_check(
                vault, tag, runtime, context, log, model, f"cite-check-{after[:16]}"
            )
        if step == "15":
            (run_dir / "polish-log.json").write_text(
                result.text or '{"applied": []}', encoding="utf-8"
            )
        else:
            (run_dir / "readability-log.json").write_text(
                result.text or '{"applied": []}', encoding="utf-8"
            )
    else:
        raise BrowserUnsupported(f"step {step} is not implemented as a host step")

    set_step(vault, tag, step, "done")


def _ship(vault: Vault, tag: str, tier: str) -> dict[str, Any]:
    result = verify_run(vault, tag)
    if not result["passed"]:
        set_status(vault, tag, "blocked", blocked_on="verify")
        return result
    if tier == "light":
        set_status(vault, tag, "completed")
        return result
    from hyperresearch.core.independence import compute_independence

    compute_independence(vault, tag)
    if not _cite_check_bound(vault, tag):
        set_status(vault, tag, "blocked", blocked_on="cite-check")
        failed = dict(result)
        failed["passed"] = False
        failed["checks"] = [
            *result["checks"],
            {
                "name": "cite-check-final",
                "ok": False,
                "detail": (
                    f"unbound report={_report_hash(vault, tag)[:12]} "
                    f"evidence={_evidence_hash(vault, tag)[:12]}"
                ),
            },
        ]
        return failed
    set_status(vault, tag, "verified")
    return result


async def execute_run(
    vault: Vault,
    query: str,
    runtime: AgentRuntime,
    *,
    profile: str = "light",
    tag: str | None = None,
    resume: bool = False,
    budget_usd: float | None = None,
) -> dict[str, Any]:
    if resume:
        if not tag:
            raise ValueError("resume requires a run tag")
        run_tag = tag
        manifest = load_manifest(vault, run_tag)
        query = _query_text(vault, run_tag, query)
    else:
        run_tag = tag or mint_run_tag(query)
        manifest = init_run(vault, run_tag, profile=profile, budget_usd=budget_usd, query=query)

    run_dir = vault.run_dir(run_tag)
    qpath = run_dir / "query.md"
    if not qpath.exists():
        qpath.write_text(query, encoding="utf-8")
    query = qpath.read_text(encoding="utf-8-sig")

    tier = _declared_tier(run_dir, "light" if profile == "light" else "full")
    if profile == "light":
        tier = _declared_tier(run_dir, "light")
    context = context_for(vault, run_tag, runtime.name, query, profile, tier)
    log = task_log_for(vault, run_tag)
    resolved = resolve_profile(profile, vault.config_path)
    budget = budget_from_profile(resolved, manifest)
    executor = HostExecutor(vault=vault, workspace_root=vault.root)

    steps = step_ids_for(
        tier if (run_dir / "prompt-decomposition.json").exists() else (
            "light" if profile == "light" else "full"
        ),
        profile,
        vault.config_path,
    )

    for step in steps:
        live = load_manifest(vault, run_tag)
        if live.get("status") == "blocked":
            break
        status = live.get("steps", {}).get(step, {}).get("status")
        if status in ("done", "skipped"):
            continue
        await execute_step(vault, run_tag, step, runtime, context, log, executor, budget)
        if load_manifest(vault, run_tag).get("status") == "blocked":
            break
        if step == "1":
            tier = _declared_tier(run_dir, tier)
            context = context_for(vault, run_tag, runtime.name, query, profile, tier)
            steps = step_ids_for(tier, profile, vault.config_path)

    result = _ship(vault, run_tag, context.tier)
    manifest = load_manifest(vault, run_tag)
    return {"manifest": manifest, "verify": result, "tag": run_tag}


async def resume_run(vault: Vault, tag: str, runtime: AgentRuntime) -> dict[str, Any]:
    query = _query_text(vault, tag)
    profile = load_manifest(vault, tag).get("profile", "light")
    return await execute_run(vault, query, runtime, profile=profile, tag=tag, resume=True)
