"""Application-owned step graph. hpr run / resume execute host steps, not Claude Skills."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

from hyperresearch.core.citecheck import write_pairs_file
from hyperresearch.core.patterns import WIKI_LINK_RE
from hyperresearch.core.profiles import Profile, resolve_profile
from hyperresearch.core.runs import (
    init_run,
    load_manifest,
    set_status,
    set_step,
    verify_run,
)
from hyperresearch.core.vault import Vault
from hyperresearch.pipeline.checkpoints import UNCERTAIN_REMOTE, TaskLog, dump_agent_result
from hyperresearch.pipeline.host_actions import (
    BudgetExhaustedError,
    HostBudget,
    HostExecutor,
    SpendLedger,
    budget_from_profile,
    evidence_content_hash,
    load_evidence,
    run_host_action_loop,
)
from hyperresearch.pipeline.patch import PatchOp, PatchSet, apply_patch_set, content_hash
from hyperresearch.pipeline.prompts import (
    evidence_extra,
    inline_artifacts,
    report_extra,
    role_payload,
)
from hyperresearch.runtime.errors import BrowserUnsupported, RuntimeFailure, UncertainSubmission
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
INDEPENDENCE_ARTIFACT = "independence.json"
CRITIC_NAMES = ("dialectic", "depth", "width", "instruction")
_ROLE_MODELS = {
    "decompose": "synthesizer",
    "width": "fetcher",
    "contradiction": "source_analyst",
    "loci": "loci_analyst",
    "investigator": "depth_investigator",
    "reconcile": "source_analyst",
    "tensions": "source_analyst",
    "corpus_critic": "corpus_critic",
    "digest": "source_analyst",
    "draft": "draft_orchestrator",
    "synthesizer": "synthesizer",
    "gap_fetch": "fetcher",
    "patcher": "patcher",
    "cite_checker": "cite_checker",
    "polish": "polish_auditor",
    "readability": "readability_recommender",
}


def mint_run_tag(query: str) -> str:
    slug = _SLUG_RE.sub("-", query.lower()).strip("-")[:24] or "run"
    if slug[0].isdigit():
        slug = f"r-{slug}"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def report_path(vault: Vault, tag: str) -> Path:
    return vault.root / "research" / "notes" / f"final_report_{tag}.md"


def _payload(role: str, context: ResearchContext, extra: str = "") -> str:
    return role_payload(
        role,
        context.canonical_query,
        extra=extra,
        declared_tier=context.tier,
    )


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


def _role_model(profile: Profile | None, role: str) -> str:
    if profile is None:
        return "default"
    if role.startswith("critic_"):
        return profile.models.critics
    attr = _ROLE_MODELS.get(role, "synthesizer")
    return str(getattr(profile.models, attr))


def _draft_letter(index: int) -> str:
    return chr(ord("a") + index)


async def run_many_retry(
    runtime: AgentRuntime,
    tasks: list[AgentTask],
    context: ResearchContext,
    concurrency: int,
    log: TaskLog,
    ledger: SpendLedger | None = None,
) -> list[AgentResult]:
    """Retry only known-safe failed members. Do not auto-POST uncertain transport."""
    results: dict[str, AgentResult] = {}
    pending = [t for t in tasks if not log.is_success(t.task_id)]
    for t in tasks:
        if log.is_success(t.task_id):
            results[t.task_id] = log.result_agent(t.task_id, t.model)

    sem = asyncio.Semaphore(max(1, concurrency))
    failed: list[AgentTask] = []
    uncertain: list[str] = []

    async def one(t: AgentTask) -> None:
        decision = log.resume_model(t.task_id)
        if decision == "skip":
            results[t.task_id] = log.result_agent(t.task_id, t.model)
            return
        if decision == UNCERTAIN_REMOTE:
            raise RuntimeError(f"uncertain_remote:{t.task_id}")
        async with sem:
            cost = 0.0
            if ledger is not None:
                cost = ledger.proposed()
                if not ledger.reserve(cost):
                    ledger.block()
                    raise BudgetExhaustedError(t.task_id)
            log.begin(t.task_id, "model", {"role": t.role})
            try:
                r = await runtime.run(t, context)
            except UncertainSubmission:
                if ledger is not None:
                    ledger.release(cost)
                log.mark_uncertain_remote(t.task_id)
                uncertain.append(t.task_id)
                return
            except RuntimeFailure:
                if ledger is not None:
                    ledger.release(cost)
                log.fail(t.task_id, "run failed")
                failed.append(t)
                return
            except Exception:
                if ledger is not None:
                    ledger.release(cost)
                raise
            if ledger is not None:
                ledger.settle(r, reserved=cost)
            log.succeed(t.task_id, dump_agent_result(r))
            results[t.task_id] = r

    await asyncio.gather(*[one(t) for t in pending])
    if uncertain:
        raise RuntimeError(f"uncertain_remote:{uncertain[0]}")
    for t in list(failed):
        decision = log.resume_model(t.task_id)
        if decision == UNCERTAIN_REMOTE:
            raise RuntimeError(f"uncertain_remote:{t.task_id}")
        cost = 0.0
        if ledger is not None:
            cost = ledger.proposed()
            if not ledger.reserve(cost):
                ledger.block()
                raise BudgetExhaustedError(t.task_id)
        log.begin(t.task_id, "model", {"role": t.role, "retry": True})
        try:
            r = await runtime.run(t, context)
        except UncertainSubmission:
            if ledger is not None:
                ledger.release(cost)
            log.mark_uncertain_remote(t.task_id)
            raise RuntimeError(f"uncertain_remote:{t.task_id}") from None
        except Exception:
            if ledger is not None:
                ledger.release(cost)
            raise
        if ledger is not None:
            ledger.settle(r, reserved=cost)
        log.succeed(t.task_id, dump_agent_result(r))
        results[t.task_id] = r
    return [results[t.task_id] for t in tasks]


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return content_hash(path.read_text(encoding="utf-8-sig"))


def _report_hash(vault: Vault, tag: str) -> str:
    return _file_hash(report_path(vault, tag))


def _evidence_hash(vault: Vault, tag: str) -> str:
    return evidence_content_hash(vault, tag)


_CITE_VERDICTS = frozenset({"unsupported", "partially-supported", "wrong-source"})
_CITE_SEVERITIES = frozenset({"critical", "major"})


def _cite_finding_valid(item: Any) -> str | None:
    if not isinstance(item, dict):
        return "MalformedStructuredOutput"
    source = item.get("cited_note_id") or item.get("source")
    if item.get("verdict") not in _CITE_VERDICTS:
        return "invalid-finding"
    if item.get("severity") not in _CITE_SEVERITIES:
        return "invalid-finding"
    if not isinstance(item.get("sentence"), str) or not str(item.get("sentence") or "").strip():
        return "invalid-finding"
    if not isinstance(item.get("evidence"), str) or not str(item.get("evidence") or "").strip():
        return "invalid-finding"
    if not isinstance(source, str) or not source.strip():
        return "invalid-finding"
    return None


def _parse_cite_output(raw: str) -> tuple[dict[str, Any], bool]:
    if not raw or not raw.strip():
        return {"findings": [], "error": "empty"}, False
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {"findings": [], "error": "malformed"}, False
    if isinstance(parsed, list):
        findings: list[Any] = parsed
        data: dict[str, Any] = {"findings": findings}
    elif isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
        findings = parsed["findings"]
        data = dict(parsed)
    else:
        return {"findings": [], "error": "malformed"}, False
    for item in findings:
        err = _cite_finding_valid(item)
        if err is not None:
            data["error"] = err
            return data, False
    return data, True


def _write_cite_findings(
    vault: Vault,
    tag: str,
    raw: str,
    *,
    dangling: list[Any] | None = None,
) -> bool:
    data, valid = _parse_cite_output(raw)
    if dangling:
        valid = False
        data["dangling_blocked"] = True
        data["dangling_count"] = len(dangling)
    data["ok"] = valid
    data["report_hash"] = _report_hash(vault, tag)
    data["evidence_hash"] = _evidence_hash(vault, tag)
    (vault.run_dir(tag) / CITE_FINDINGS).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
    return valid


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
    if data.get("ok") is not True:
        return False
    return (
        data.get("report_hash") == _report_hash(vault, tag)
        and data.get("evidence_hash") == _evidence_hash(vault, tag)
    )


def _cite_check_examined_current(vault: Vault, tag: str) -> bool:
    """True when findings are for the current report+evidence hashes (pass or fail)."""
    path = vault.run_dir(tag) / CITE_FINDINGS
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    report_h = data.get("report_hash") or ""
    evidence_h = data.get("evidence_hash") or ""
    if not report_h or not evidence_h:
        return False
    return report_h == _report_hash(vault, tag) and evidence_h == _evidence_hash(vault, tag)


def _write_independence(vault: Vault, tag: str, summary: dict[str, Any]) -> None:
    data = {
        "evidence_hash": _evidence_hash(vault, tag),
        "scored": summary.get("scored", 0),
        "clusters": list(summary.get("clusters") or []),
        "audited": list(summary.get("audited") or []),
    }
    (vault.run_dir(tag) / INDEPENDENCE_ARTIFACT).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def _cited_note_ids(vault: Vault, tag: str) -> set[str]:
    path = report_path(vault, tag)
    if not path.exists():
        return set()
    return {m.group(1).strip() for m in WIKI_LINK_RE.finditer(path.read_text(encoding="utf-8-sig"))}


def _independence_bound(vault: Vault, tag: str) -> bool:
    path = vault.run_dir(tag) / INDEPENDENCE_ARTIFACT
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("evidence_hash") != _evidence_hash(vault, tag):
        return False
    cited = _cited_note_ids(vault, tag)
    if not cited:
        return True
    evidence_ids = {str(s.get("note_id") or "") for s in load_evidence(vault, tag)}
    evidence_ids.discard("")
    if not cited <= evidence_ids:
        return False
    audited = {str(x) for x in (data.get("audited") or [])}
    return cited <= audited


def _block_ship(
    vault: Vault,
    tag: str,
    result: dict[str, Any],
    *,
    blocked_on: str,
    name: str,
    detail: str,
) -> dict[str, Any]:
    set_status(vault, tag, "blocked", blocked_on=blocked_on)
    failed = dict(result)
    failed["passed"] = False
    failed["checks"] = [*result["checks"], {"name": name, "ok": False, "detail": detail}]
    return failed


async def _model(
    runtime: AgentRuntime,
    context: ResearchContext,
    log: TaskLog,
    task: AgentTask,
    ledger: SpendLedger | None = None,
) -> AgentResult:
    decision = log.resume_model(task.task_id)
    if decision == "skip":
        return log.result_agent(task.task_id, task.model)
    if decision == UNCERTAIN_REMOTE:
        raise RuntimeError(f"uncertain_remote:{task.task_id}")
    cost = 0.0
    if ledger is not None:
        cost = ledger.proposed()
        if not ledger.reserve(cost):
            ledger.block()
            raise BudgetExhaustedError(task.task_id)
    log.begin(task.task_id, "model", {"role": task.role})
    try:
        result = await runtime.run(task, context)
    except UncertainSubmission:
        if ledger is not None:
            ledger.release(cost)
        log.mark_uncertain_remote(task.task_id)
        raise RuntimeError(f"uncertain_remote:{task.task_id}") from None
    except Exception:
        if ledger is not None:
            ledger.release(cost)
        raise
    if ledger is not None:
        ledger.settle(result, reserved=cost)
    log.succeed(task.task_id, dump_agent_result(result))
    return result


def _evidence_blob(vault: Vault, tag: str) -> str:
    run_dir = vault.run_dir(tag)
    return evidence_extra(
        vault.notes_dir,
        run_dir / "prompt-decomposition.json",
        sources=load_evidence(vault, tag),
    )


def _cite_payload(vault: Vault, tag: str, context: ResearchContext, pairs: dict[str, Any]) -> str:
    report = ""
    path = report_path(vault, tag)
    if path.exists():
        report = path.read_text(encoding="utf-8-sig")
    excerpts = _evidence_blob(vault, tag)
    extra = (
        "Report to audit:\n"
        + report
        + "\n\nCite-check pairs:\n"
        + json.dumps(pairs, ensure_ascii=False, indent=2)
        + "\n\nSource excerpts:\n"
        + excerpts
    )
    return _payload("cite_checker", context, extra=extra)


async def _run_cite_check(
    vault: Vault,
    tag: str,
    runtime: AgentRuntime,
    context: ResearchContext,
    log: TaskLog,
    model: str,
    task_id: str,
    ledger: SpendLedger | None = None,
) -> None:
    path = report_path(vault, tag)
    pairs: dict[str, Any] = {}
    if path.exists():
        pairs = write_pairs_file(vault, tag, path)
    result = await _model(
        runtime,
        context,
        log,
        AgentTask(
            task_id=task_id,
            role="cite_checker",
            payload=_cite_payload(vault, tag, context, pairs),
            model=model,
            allowed_actions=(),
        ),
        ledger,
    )
    dangling = list(pairs.get("dangling") or [])
    _write_cite_findings(vault, tag, result.text, dangling=dangling)


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
    """Claude-in-Chrome is unsupported. Abandon queued items; do not crash or block."""
    try:
        from hyperresearch.core.escalation import list_items, queue_stats, resolve

        stats = queue_stats(vault.db, vault_tag=tag)
    except Exception:
        return
    if not stats or not (
        stats.get("queued") or stats.get("needs_human") or stats.get("in_progress")
    ):
        return
    for status in ("queued", "in_progress", "needs_human"):
        try:
            items = list_items(vault.db, status=status, vault_tag=tag)
        except Exception:
            continue
        for item in items:
            try:
                resolve(
                    vault.db,
                    int(item["id"]),
                    "abandoned",
                    detail="Spec 1: Claude-in-Chrome unsupported; skip fetch, continue run",
                )
            except Exception:
                continue


def _write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def execute_step(
    vault: Vault,
    tag: str,
    step: str,
    runtime: AgentRuntime,
    context: ResearchContext,
    log: TaskLog,
    executor: HostExecutor,
    budget: HostBudget,
    ledger: SpendLedger | None = None,
) -> None:
    set_step(vault, tag, step, "running")
    run_dir = vault.run_dir(tag)
    profile: Profile | None = None
    try:
        profile = resolve_profile(context.profile, vault.config_path)
    except Exception:
        profile = None

    def model_for(role: str) -> str:
        return _role_model(profile, role)

    if step == "1":
        role = "decompose"
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id="step-1",
                role=role,
                payload=_payload(role, context),
                model=model_for(role),
                output_schema=dict,
                allowed_actions=(),
            ),
            ledger,
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
        parts = [_evidence_blob(vault, tag)]
        if step == "5":
            parts.append(inline_artifacts([("loci", run_dir / "loci.json")]))
        elif step == "8":
            parts.append(
                inline_artifacts(
                    [
                        ("loci", run_dir / "loci.json"),
                        ("comparisons", run_dir / "comparisons.md"),
                        ("source-tensions", run_dir / "source-tensions.json"),
                    ]
                )
            )
        elif step == "13":
            parts.append(
                inline_artifacts(
                    [
                        (f"critic-findings-{name}.json", run_dir / f"critic-findings-{name}.json")
                        for name in CRITIC_NAMES
                    ]
                )
            )
        extra = "\n".join(parts)
        await run_host_action_loop(
            runtime,
            AgentTask(
                task_id=f"step-{step}",
                role=role,
                payload=_payload(role, context, extra=extra),
                model=model_for(role),
                output_schema=dict,
                allowed_actions=allowed,
            ),
            context,
            executor,
            budget,
            log,
            task_id_prefix=f"step-{step}",
            ledger=ledger,
        )
    elif step == "10":
        extra = _evidence_blob(vault, tag) + "\n" + inline_artifacts(
            [
                ("evidence-digest", run_dir / EVIDENCE_DIGEST),
                ("contradiction-graph", run_dir / "contradiction-graph.md"),
                ("loci", run_dir / "loci.json"),
                ("comparisons", run_dir / "comparisons.md"),
                ("source-tensions", run_dir / "source-tensions.json"),
            ]
        )
        if context.tier == "light" or (profile is not None and profile.draft_count <= 1):
            result = await _model(
                runtime,
                context,
                log,
                AgentTask(
                    task_id="step-10",
                    role="draft",
                    payload=_payload("draft", context, extra=extra),
                    model=model_for("draft"),
                    allowed_actions=(),
                ),
                ledger,
            )
            text = result.text.strip()
            if not text:
                set_status(vault, tag, "blocked", blocked_on="empty-draft")
                set_step(vault, tag, step, "done")
                return
            _write_report(report_path(vault, tag), text)
        else:
            n = profile.draft_count if profile is not None else 3
            (run_dir / "temp").mkdir(parents=True, exist_ok=True)
            tasks = [
                AgentTask(
                    task_id=f"step-10-draft-{_draft_letter(i)}",
                    role="draft",
                    payload=_payload(
                        "draft",
                        context,
                        extra=extra + f"\nDraft id: {_draft_letter(i)}\n",
                    ),
                    model=model_for("draft"),
                    allowed_actions=(),
                )
                for i in range(n)
            ]
            results = await run_many_retry(
                runtime, tasks, context, concurrency=n, log=log, ledger=ledger
            )
            for i, result in enumerate(results):
                text = result.text.strip()
                if not text:
                    set_status(vault, tag, "blocked", blocked_on="empty-draft")
                    set_step(vault, tag, step, "done")
                    return
                (run_dir / "temp" / f"draft-{_draft_letter(i)}.md").write_text(
                    result.text, encoding="utf-8"
                )
    elif step == "11":
        draft_paths = sorted((run_dir / "temp").glob("draft-*.md"))
        drafts = inline_artifacts([(p.name, p) for p in draft_paths])
        extra = _evidence_blob(vault, tag) + "\n" + drafts
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id="step-11",
                role="synthesizer",
                payload=_payload("synthesizer", context, extra=extra),
                model=model_for("synthesizer"),
                allowed_actions=(),
            ),
            ledger,
        )
        text = result.text.strip()
        if not text:
            set_status(vault, tag, "blocked", blocked_on="empty-draft")
            set_step(vault, tag, step, "done")
            return
        _write_report(report_path(vault, tag), text)
    elif step in {"3", "4", "6", "7", "9"}:
        role = {
            "3": "contradiction",
            "4": "loci",
            "6": "reconcile",
            "7": "tensions",
            "9": "digest",
        }[step]
        artifact = {
            "3": "contradiction-graph.md",
            "4": "loci.json",
            "6": "comparisons.md",
            "7": "source-tensions.json",
            "9": "evidence-digest.md",
        }[step]
        extras = [
            ("decomposition", run_dir / "prompt-decomposition.json"),
            ("contradiction-graph", run_dir / "contradiction-graph.md"),
            ("loci", run_dir / "loci.json"),
            ("comparisons", run_dir / "comparisons.md"),
            ("source-tensions", run_dir / "source-tensions.json"),
            ("evidence-digest", run_dir / EVIDENCE_DIGEST),
        ]
        extra = _evidence_blob(vault, tag) + "\n" + inline_artifacts(extras)
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id=f"step-{step}",
                role=role,
                payload=_payload(role, context, extra=extra),
                model=model_for(role),
                allowed_actions=(),
            ),
            ledger,
        )
        body = result.text if not artifact.endswith(".json") else (result.text or "[]")
        (run_dir / artifact).write_text(body, encoding="utf-8")
    elif step == "12":
        rpath = report_path(vault, tag)
        extras = [
            ("report", rpath),
            ("decomposition", run_dir / "prompt-decomposition.json"),
            ("evidence-digest", run_dir / EVIDENCE_DIGEST),
        ]
        extra = inline_artifacts(extras)
        tasks = [
            AgentTask(
                task_id=f"step-12-{name}",
                role=f"critic_{name}",
                payload=_payload(f"critic_{name}", context, extra=extra),
                model=model_for(f"critic_{name}"),
                allowed_actions=(),
            )
            for name in CRITIC_NAMES
        ]
        results = await run_many_retry(
            runtime, tasks, context, concurrency=len(tasks), log=log, ledger=ledger
        )
        for name, result in zip(CRITIC_NAMES, results, strict=True):
            (run_dir / f"critic-findings-{name}.json").write_text(
                result.text or '{"findings": []}', encoding="utf-8"
            )
    elif step == "14":
        path = report_path(vault, tag)
        current = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        critic_files = [
            (f"critic-findings-{name}.json", run_dir / f"critic-findings-{name}.json")
            for name in CRITIC_NAMES
        ]
        extra = report_extra(current, content_hash(current)) + "\n" + inline_artifacts(critic_files)
        result = await _model(
            runtime,
            context,
            log,
            AgentTask(
                task_id="step-14",
                role="patcher",
                payload=_payload("patcher", context, extra=extra),
                model=model_for("patcher"),
                output_schema=dict,
                allowed_actions=(),
            ),
            ledger,
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
        await _run_cite_check(
            vault, tag, runtime, context, log, model_for("cite_checker"), "step-14.5", ledger
        )
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
                payload=_payload(
                    role,
                    context,
                    extra=report_extra(
                        path.read_text(encoding="utf-8-sig") if path.exists() else "",
                        before,
                    ),
                ),
                model=model_for(role),
                output_schema=dict,
                allowed_actions=(),
            ),
            ledger,
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
                vault,
                tag,
                runtime,
                context,
                log,
                model_for("cite_checker"),
                f"cite-check-{after[:16]}",
                ledger,
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


def _invalidate_cite_check(vault: Vault, tag: str) -> None:
    """A report mutation unbinds cite-check. Never restamp an old ok hash."""
    cc_path = vault.run_dir(tag) / CITE_FINDINGS
    if not cc_path.exists():
        return
    try:
        data = json.loads(cc_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    data["ok"] = False
    data["report_hash"] = ""
    cc_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _unquote_unmatched_report(vault: Vault, tag: str, log: TaskLog | None = None) -> int:
    """Host patch: unquote unmatched spans; invalidate cite-check bind."""
    from hyperresearch.cli.lint import unmatched_quote_ops

    path = report_path(vault, tag)
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8-sig")
    raw = unmatched_quote_ops(vault.db, text)
    if not raw:
        return 0
    ops = tuple(PatchOp(old_text=old, new_text=new) for old, new in raw)
    apply_patch_set(
        path,
        PatchSet(base_report_hash=content_hash(text), ops=ops),
        workspace_root=vault.root,
        state_path=vault.run_dir(tag) / "patch-state.json",
        task_id=f"unquote-{content_hash(text)[:12]}",
        log=log,
    )
    _invalidate_cite_check(vault, tag)
    return len(ops)


def _ship(vault: Vault, tag: str, tier: str) -> dict[str, Any]:
    live = load_manifest(vault, tag)
    if live.get("status") == "blocked" and live.get("blocked_on") != "verify":
        result = verify_run(vault, tag)
        failed = dict(result)
        failed["passed"] = False
        return failed
    _unquote_unmatched_report(vault, tag)
    result = verify_run(vault, tag)
    if not result["passed"]:
        set_status(vault, tag, "blocked", blocked_on="verify")
        return result
    if tier == "light":
        set_status(vault, tag, "completed")
        return result
    try:
        from hyperresearch.core.independence import compute_independence

        ids = [str(s.get("note_id") or "") for s in load_evidence(vault, tag)]
        ids = [i for i in ids if i]
        summary = compute_independence(vault, note_ids=ids)
        path = vault.run_dir(tag) / INDEPENDENCE_ARTIFACT
        if not path.exists():
            _write_independence(vault, tag, summary)
    except Exception as exc:
        return _block_ship(
            vault, tag, result,
            blocked_on="independence",
            name="independence-final",
            detail=str(exc),
        )
    if not _independence_bound(vault, tag):
        return _block_ship(
            vault, tag, result,
            blocked_on="independence",
            name="independence-final",
            detail=f"unbound evidence={_evidence_hash(vault, tag)[:12]}",
        )
    if not _cite_check_bound(vault, tag):
        return _block_ship(
            vault, tag, result,
            blocked_on="cite-check",
            name="cite-check-final",
            detail=(
                f"unbound report={_report_hash(vault, tag)[:12]} "
                f"evidence={_evidence_hash(vault, tag)[:12]}"
            ),
        )
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
    ledger = SpendLedger(vault=vault, tag=run_tag)
    executor = HostExecutor(vault=vault, workspace_root=vault.root, run_tag=run_tag)

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
        try:
            await execute_step(
                vault, run_tag, step, runtime, context, log, executor, budget, ledger
            )
        except BudgetExhaustedError:
            break
        if load_manifest(vault, run_tag).get("status") == "blocked":
            break
        if step == "1":
            tier = _declared_tier(run_dir, tier)
            context = context_for(vault, run_tag, runtime.name, query, profile, tier)
            steps = step_ids_for(tier, profile, vault.config_path)

    _unquote_unmatched_report(vault, run_tag, log=log)
    live_tail = load_manifest(vault, run_tag)
    step_145_done = live_tail.get("steps", {}).get("14.5", {}).get("status") == "done"
    findings_exist = (run_dir / CITE_FINDINGS).exists()
    if (
        context.tier != "light"
        and (step_145_done or findings_exist)
        and not _cite_check_examined_current(vault, run_tag)
    ):
        try:
            await _run_cite_check(
                vault,
                run_tag,
                runtime,
                context,
                log,
                _role_model(resolved, "cite_checker"),
                f"cite-check-after-unquote-{_report_hash(vault, run_tag)[:16]}",
                ledger,
            )
        except BudgetExhaustedError:
            pass
        except RuntimeError as exc:
            if not str(exc).startswith("uncertain_remote:"):
                raise
    result = _ship(vault, run_tag, context.tier)
    manifest = load_manifest(vault, run_tag)
    return {"manifest": manifest, "verify": result, "tag": run_tag}


async def resume_run(vault: Vault, tag: str, runtime: AgentRuntime) -> dict[str, Any]:
    query = _query_text(vault, tag)
    profile = load_manifest(vault, tag).get("profile", "light")
    return await execute_run(vault, query, runtime, profile=profile, tag=tag, resume=True)
