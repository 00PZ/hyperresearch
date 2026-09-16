"""Host-action loop: search / fetch / evidence_read / complete."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hyperresearch.pipeline.checkpoints import TaskLog
from hyperresearch.runtime.errors import IllegalHostAction
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


@dataclass(frozen=True)
class HostBudget:
    max_iterations: int
    max_seconds: float
    max_cost_usd: float


def budget_from_profile(profile: Any, manifest: dict[str, Any]) -> HostBudget:
    cost = manifest.get("budget_usd")
    max_cost = float(cost) if cost is not None else float("inf")
    return HostBudget(
        max_iterations=max(1, int(profile.investigator_max)),
        max_seconds=float(profile.vault_check_interval_s) * 60.0,
        max_cost_usd=max_cost,
    )


@dataclass
class HostExecutor:
    vault: Any
    workspace_root: Path
    search_fn: Callable[..., Any] | None = None
    fetch_fn: Callable[..., Any] | None = None
    spent_usd: float = 0.0
    fetches: list[str] = field(default_factory=list)

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
        else:
            from hyperresearch.search.fts import search_fts

            hits = search_fts(self.vault.db, query, limit=limit)
        digest = hashlib.sha256(repr(hits).encode()).hexdigest()[:16]
        return {
            "task_id": task_id,
            "kind": "search",
            "ok": True,
            "query": query,
            "results": hits,
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
            raise IllegalHostAction(f"no note file for {note_id}")
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


async def run_host_action_loop(
    runtime: AgentRuntime,
    task: AgentTask,
    context: ResearchContext,
    executor: HostExecutor,
    budget: HostBudget,
    log: TaskLog | None = None,
    *,
    task_id_prefix: str | None = None,
) -> AgentResult:
    """Repeat until complete or budget exhausted. Host validates and executes."""
    prefix = task_id_prefix or task.task_id
    payload = task.payload
    last = AgentResult(text="", requested_model=task.model)
    started = time.monotonic()
    spent = 0.0
    for i in range(budget.max_iterations):
        if time.monotonic() - started > budget.max_seconds:
            break
        if spent >= budget.max_cost_usd:
            break
        model_id = f"{prefix}-model-{i}"
        if log is not None:
            decision = log.resume_model(model_id)
            if decision == "skip":
                rec = log.get(model_id)
                last = AgentResult(text=(rec.result or {}).get("text", ""), requested_model=task.model)
                continue
            if decision == "uncertain_remote":
                raise RuntimeError(f"uncertain_remote:{model_id}")
            log.begin(model_id, "model", {"role": task.role})
        step_task = AgentTask(
            task_id=model_id,
            role=task.role,
            payload=payload,
            model=task.model,
            output_schema=task.output_schema,
            allowed_actions=task.allowed_actions,
        )
        last = await runtime.run(step_task, context)
        spent += _cost_of(last)
        if log is not None:
            log.succeed(model_id, {"text": last.text[:2000]})
        actions = iter_host_actions(last.structured)
        if not actions:
            break
        provenance: list[dict[str, Any]] = []
        stop = False
        for j, action in enumerate(actions):
            assert_action_allowed(action, task.allowed_actions)
            ha_id = f"{prefix}-ha-{i}-{j}"
            if log is not None and log.is_success(ha_id):
                rec = log.get(ha_id)
                provenance.append(rec.result or {"task_id": ha_id, "ok": True, "reconciled": True})
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
    import json

    return json.dumps(items, ensure_ascii=False, indent=2)
