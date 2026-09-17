"""Host-action loop: search / fetch / evidence_read / complete."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hyperresearch.core.profiles import Profile
from hyperresearch.core.runs import add_spend, load_manifest, set_status
from hyperresearch.core.vault import Vault
from hyperresearch.pipeline.checkpoints import TaskLog, dump_agent_result
from hyperresearch.pipeline.patch import content_hash
from hyperresearch.runtime.errors import IllegalHostAction, UncertainSubmission
from hyperresearch.runtime.parse import assert_action_allowed, iter_host_actions
from hyperresearch.runtime.types import (
    AgentResult,
    AgentRuntime,
    AgentTask,
    HostAction,
    ResearchContext,
)

_NOTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SAFE_QUERY_MAX = 2000
EVIDENCE_MANIFEST = "evidence.json"
# Token-only usage (no cost_usd) reserves this many USD per model dispatch.
DEFAULT_CALL_COST_USD = 1.0


class BudgetExhaustedError(Exception):
    """Run-wide spend ceiling hit. Do not dispatch."""


@dataclass(frozen=True)
class HostBudget:
    max_iterations: int
    max_seconds: float
    max_cost_usd: float


def budget_from_profile(profile: Profile, manifest: dict[str, Any]) -> HostBudget:
    cost = manifest.get("budget_usd")
    max_cost = float(cost) if cost is not None else float("inf")
    time_s = manifest.get("time_budget_s")
    max_seconds = float(time_s) if time_s is not None else float("inf")
    return HostBudget(
        max_iterations=max(1, int(profile.investigator_max)),
        max_seconds=max_seconds,
        max_cost_usd=max_cost,
    )


@dataclass
class SpendLedger:
    """Durable settled spend. Pending reservations are in-memory only."""

    vault: Vault
    tag: str
    default_call_cost: float = DEFAULT_CALL_COST_USD
    pending: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _last_actual: float | None = None

    def spent(self) -> float:
        manifest = load_manifest(self.vault, self.tag)
        return float((manifest.get("spend") or {}).get("estimated_usd") or 0.0)

    def ceiling(self) -> float | None:
        budget = load_manifest(self.vault, self.tag).get("budget_usd")
        return None if budget is None else float(budget)

    def proposed(self, amount: float | None = None) -> float:
        """Justified reservation: explicit amount, else last actual, else default."""
        if amount is not None:
            return float(amount)
        with self._lock:
            if self._last_actual is not None:
                return self._last_actual
            return self.default_call_cost

    def can_dispatch(self, amount: float | None = None) -> bool:
        cost = self.proposed(amount)
        with self._lock:
            return self._fits_unlocked(cost)

    def reserve(self, amount: float | None = None) -> bool:
        cost = self.proposed(amount)
        with self._lock:
            if not self._fits_unlocked(cost):
                return False
            self.pending = round(self.pending + cost, 4)
            return True

    def release(self, amount: float | None = None) -> None:
        cost = self.default_call_cost if amount is None else float(amount)
        with self._lock:
            self.pending = max(0.0, round(self.pending - cost, 4))

    def settle(self, result: AgentResult, reserved: float | None = None) -> None:
        actual = _cost_of(result)
        if actual <= 0:
            actual = self.default_call_cost
        self.release(self.default_call_cost if reserved is None else reserved)
        with self._lock:
            self._last_actual = actual
        add_spend(self.vault, self.tag, estimated_usd=actual, agents_spawned=1)

    def block(self) -> None:
        set_status(self.vault, self.tag, "blocked", blocked_on="budget")

    def _fits_unlocked(self, amount: float) -> bool:
        ceil = self.ceiling()
        if ceil is None:
            return True
        return round(self.spent() + self.pending + amount, 4) <= round(ceil, 4)


def evidence_path(vault: Vault, tag: str) -> Path:
    return vault.run_dir(tag) / EVIDENCE_MANIFEST


def load_evidence(vault: Vault, tag: str) -> list[dict[str, Any]]:
    path = evidence_path(vault, tag)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    sources = data.get("sources")
    return list(sources) if isinstance(sources, list) else []


def record_evidence(
    vault: Vault,
    tag: str,
    note_id: str,
    url: str,
    body_hash: str,
    origin: str,
) -> None:
    sources = load_evidence(vault, tag)
    entry = {
        "note_id": note_id,
        "url": url,
        "content_hash": body_hash,
        "origin": origin,
    }
    by_id: dict[str, dict[str, Any]] = {}
    for src in sources:
        nid = str(src.get("note_id") or "")
        if nid:
            by_id[nid] = src
    by_id[note_id] = entry
    path = evidence_path(vault, tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sources": list(by_id.values())}, indent=2) + "\n",
        encoding="utf-8",
    )


def evidence_content_hash(vault: Vault, tag: str) -> str:
    """Identity of selected source bytes, not of evidence-digest.md."""
    lines: list[str] = []
    for src in load_evidence(vault, tag):
        note_id = str(src.get("note_id") or "")
        npath = vault.notes_dir / f"{note_id}.md"
        body = npath.read_text(encoding="utf-8-sig") if npath.exists() else ""
        lines.append(f"{note_id}:{content_hash(body)}")
    return content_hash("\n".join(sorted(lines)))


def _note_hash(vault: Vault, note_id: str) -> str:
    npath = vault.notes_dir / f"{note_id}.md"
    if not npath.exists():
        return content_hash("")
    return content_hash(npath.read_text(encoding="utf-8-sig"))


@dataclass
class HostExecutor:
    vault: Vault
    workspace_root: Path
    search_fn: Callable[..., Any] | None = None
    fetch_fn: Callable[..., Any] | None = None
    spent_usd: float = 0.0
    fetches: list[str] = field(default_factory=list)
    run_tag: str = ""

    def execute(self, action: HostAction, *, task_id: str) -> dict[str, Any]:
        if action.kind == "complete":
            return {"task_id": task_id, "kind": "complete", "ok": True}
        if action.kind == "search":
            return self._search(action, task_id)
        if action.kind == "fetch":
            return self._fetch(action, task_id)
        if action.kind == "evidence_read":
            return self._read(action, task_id)
        raise IllegalHostAction(f"unknown kind {action.kind!r}")

    def _search(self, action: HostAction, task_id: str) -> dict[str, Any]:
        query = str(action.args.get("query") or "").strip()
        if not query or len(query) > _SAFE_QUERY_MAX:
            raise IllegalHostAction("search query missing or too long")
        limit = int(action.args.get("limit") or 10)
        limit = max(1, min(limit, 50))
        if self.search_fn is not None:
            hits = self.search_fn(query, limit=limit)
            digest = hashlib.sha256(repr(hits).encode()).hexdigest()[:16]
            return {
                "task_id": task_id,
                "kind": "search",
                "ok": True,
                "query": query,
                "results": hits,
                "content_hash": digest,
            }
        vault_hits: list[Any] = []
        try:
            from hyperresearch.search.fts import search_fts

            vault_hits = list(search_fts(self.vault.db, query, limit=limit) or [])
        except Exception as e:
            vault_hits = [{"error": str(e)}]
        web_hits: list[Any] = []
        web_error: str | None = None
        try:
            from hyperresearch.pipeline.prompts import web_hit
            from hyperresearch.web.base import get_provider

            provider_name = getattr(getattr(self.vault, "config", None), "web_provider", None)
            prov = get_provider(provider_name)
            web_hits = [web_hit(item) for item in prov.search(query, max_results=limit)]
        except NotImplementedError as e:
            web_error = str(e)
        except Exception as e:
            web_error = str(e)
        payload = {
            "vault_hits": vault_hits,
            "web_hits": web_hits,
            "web_error": web_error,
            "hint": None
            if web_hits
            else "Provider cannot web-search. Propose fetch actions with https URLs.",
        }
        digest = hashlib.sha256(repr(payload).encode()).hexdigest()[:16]
        return {
            "task_id": task_id,
            "kind": "search",
            "ok": bool(web_hits or vault_hits) or web_error is None,
            "query": query,
            "results": payload,
            "content_hash": digest,
        }

    def _fetch(self, action: HostAction, task_id: str) -> dict[str, Any]:
        url = str(action.args.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise IllegalHostAction(f"refusing fetch url {url!r}")
        if parsed.username or parsed.password:
            raise IllegalHostAction("refusing URL with credentials")
        tags = action.args.get("tags") or []
        if not isinstance(tags, list):
            raise IllegalHostAction("fetch tags must be a list")
        tags = [str(t) for t in tags]
        if self.run_tag and self.run_tag not in tags:
            tags.append(self.run_tag)
        try:
            if self.fetch_fn is not None:
                result = self.fetch_fn(url, tags=tags)
            else:
                from hyperresearch.core.fetcher import fetch_and_save

                try:
                    result = fetch_and_save(self.vault, url, tags=list(tags))
                except ValueError as e:
                    # Idempotent: already in vault.
                    from hyperresearch.core.fetcher import existing_live_note_for_url

                    row = existing_live_note_for_url(self.vault.db, url)
                    result = {
                        "ok": True,
                        "reconciled": True,
                        "note_id": row["note_id"] if row else None,
                        "url": url,
                        "detail": str(e),
                    }
        except IllegalHostAction:
            raise
        except Exception as e:
            return {"task_id": task_id, "kind": "fetch", "ok": False, "url": url, "error": str(e)}
        self.fetches.append(url)
        note_id = result.get("note_id") if isinstance(result, dict) else None
        if self.run_tag and note_id:
            record_evidence(
                self.vault,
                self.run_tag,
                str(note_id),
                url,
                _note_hash(self.vault, str(note_id)),
                "fetched",
            )
        body = repr(result)
        return {
            "task_id": task_id,
            "kind": "fetch",
            "ok": True,
            "url": url,
            "note_id": note_id,
            "result": result,
            "content_hash": hashlib.sha256(body.encode()).hexdigest()[:16],
        }

    def _read(self, action: HostAction, task_id: str) -> dict[str, Any]:
        note_id = str(action.args.get("note_id") or action.args.get("id") or "").strip()
        if not _NOTE_ID_RE.fullmatch(note_id):
            raise IllegalHostAction(f"illegal note id {note_id!r}")
        rel = action.args.get("path")
        if rel:
            path = _safe_path(self.workspace_root, str(rel))
        else:
            path = self.vault.notes_dir / f"{note_id}.md"
            try:
                path = _safe_path(self.vault.notes_dir, f"{note_id}.md")
            except IllegalHostAction:
                raise IllegalHostAction(f"note path escapes workspace: {note_id}")
        if not path.exists():
            return {
                "task_id": task_id,
                "kind": "evidence_read",
                "ok": False,
                "error": "note_not_found",
                "note_id": note_id,
            }
        text = path.read_text(encoding="utf-8-sig")
        source = ""
        try:
            from hyperresearch.core.frontmatter import parse_frontmatter
            from hyperresearch.core.untrusted import is_untrusted, wrap_body

            meta, body = parse_frontmatter(text)
            source = str(meta.source or "")
            note_type = meta.type
            if is_untrusted(source, note_type):
                text = wrap_body(body, source)
        except IllegalHostAction:
            raise
        except Exception:
            pass
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.run_tag:
            record_evidence(self.vault, self.run_tag, note_id, source, digest, "reused")
        return {
            "task_id": task_id,
            "kind": "evidence_read",
            "ok": True,
            "note_id": note_id,
            "path": str(path),
            "body": text,
            "content_hash": digest,
            "source": source,
        }


def _safe_path(root: Path, raw: str) -> Path:
    root = root.resolve()
    candidate = (root / raw).resolve()
    if not candidate.is_relative_to(root):
        raise IllegalHostAction(f"path escapes workspace: {raw}")
    return candidate


def _cost_of(result: AgentResult) -> float:
    usage = result.usage or {}
    for key in ("cost_usd", "estimated_usd"):
        if key in usage:
            try:
                return float(usage[key])
            except (TypeError, ValueError):
                pass
    return 0.0


def _block_budget(ledger: SpendLedger | None) -> None:
    if ledger is not None:
        ledger.block()
    raise BudgetExhaustedError("budget exhausted")


async def run_host_action_loop(
    runtime: AgentRuntime,
    task: AgentTask,
    context: ResearchContext,
    executor: HostExecutor,
    budget: HostBudget,
    log: TaskLog | None = None,
    *,
    task_id_prefix: str | None = None,
    ledger: SpendLedger | None = None,
) -> AgentResult:
    """Repeat until complete or budget exhausted. Host validates and executes."""
    prefix = task_id_prefix or task.task_id
    payload = task.payload
    last = AgentResult(text="", requested_model=task.model)
    started = time.monotonic()
    spent_local = 0.0
    for i in range(budget.max_iterations):
        if time.monotonic() - started > budget.max_seconds:
            break
        run_spent = ledger.spent() if ledger is not None else spent_local
        if run_spent >= budget.max_cost_usd:
            if ledger is not None:
                ledger.block()
            break
        if ledger is not None and not ledger.can_dispatch():
            _block_budget(ledger)
        model_id = f"{prefix}-model-{i}"
        skipped = False
        if log is not None:
            decision = log.resume_model(model_id)
            if decision == "skip":
                last = log.result_agent(model_id, task.model)
                skipped = True
            elif decision == "uncertain_remote":
                raise RuntimeError(f"uncertain_remote:{model_id}")
        if not skipped:
            if log is not None:
                log.begin(model_id, "model", {"role": task.role})
            cost = 0.0
            if ledger is not None:
                cost = ledger.proposed()
                if not ledger.reserve(cost):
                    _block_budget(ledger)
            step_task = AgentTask(
                task_id=model_id,
                role=task.role,
                payload=payload,
                model=task.model,
                output_schema=task.output_schema,
                allowed_actions=task.allowed_actions,
            )
            try:
                last = await runtime.run(step_task, context)
            except UncertainSubmission:
                if ledger is not None:
                    ledger.release(cost)
                if log is not None:
                    log.mark_uncertain_remote(model_id)
                raise RuntimeError(f"uncertain_remote:{model_id}") from None
            except Exception:
                if ledger is not None:
                    ledger.release(cost)
                raise
            spent_local += _cost_of(last) or (ledger.default_call_cost if ledger else 0.0)
            if ledger is not None:
                ledger.settle(last, reserved=cost)
            if log is not None:
                log.succeed(model_id, dump_agent_result(last))
        actions = iter_host_actions(last.structured)
        if not actions:
            if any(k in task.allowed_actions for k in ("search", "fetch")):
                payload = (
                    payload
                    + "\n\n<host-error>No host actions parsed. "
                    + "Emit JSON {\"actions\":[{\"kind\":\"fetch\",\"args\":{\"url\":\"https://...\"},\"reason\":\"...\"}]} "
                    + "or kind complete after fetching.</host-error>\n"
                )
                continue
            break
        provenance: list[dict[str, Any]] = []
        stop = False
        for j, action in enumerate(actions):
            assert_action_allowed(action, task.allowed_actions)
            ha_id = f"{prefix}-ha-{i}-{j}"
            if log is not None and log.is_success(ha_id):
                rec_result = log.result_payload(ha_id)
                provenance.append(rec_result or {"task_id": ha_id, "ok": True, "reconciled": True})
                if action.kind == "complete":
                    stop = True
                continue
            if log is not None:
                log.begin(ha_id, "host_action", {"kind": action.kind, "args": action.args})
            result = executor.execute(action, task_id=ha_id)
            if log is not None:
                log.succeed(ha_id, result)
            provenance.append(result)
            if action.kind == "complete":
                stop = True
        if stop:
            return last
        payload = task.payload + "\n\n<host-results>\n" + _format_prov(provenance) + "\n</host-results>\n"
    return last


def _format_prov(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False, indent=2)
