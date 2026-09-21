"""Durable memory-gap list for company runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GAPS_NAME = "gaps.json"
MEMORY_SEARCHES_NAME = "memory-searches.json"
REASONS = frozenset({"stale", "uncovered", "contradicted"})


def gaps_path(run_dir: Path) -> Path:
    return run_dir / GAPS_NAME


def load_gaps(run_dir: Path) -> list[dict[str, Any]]:
    path = gaps_path(run_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [g for g in data if isinstance(g, dict)]
    if isinstance(data, dict) and isinstance(data.get("gaps"), list):
        return [g for g in data["gaps"] if isinstance(g, dict)]
    return []


def save_gaps(run_dir: Path, gaps: list[dict[str, Any]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    gaps_path(run_dir).write_text(
        json.dumps({"gaps": gaps}, indent=2) + "\n", encoding="utf-8"
    )


def memory_searches_path(run_dir: Path) -> Path:
    return run_dir / MEMORY_SEARCHES_NAME


def load_memory_searches(run_dir: Path) -> list[dict[str, Any]]:
    path = memory_searches_path(run_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("searches"), list):
        return [r for r in data["searches"] if isinstance(r, dict)]
    return []


def record_memory_search(run_dir: Path, record: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = load_memory_searches(run_dir)
    rows.append(record)
    memory_searches_path(run_dir).write_text(
        json.dumps({"searches": rows}, indent=2) + "\n", encoding="utf-8"
    )


def host_snapshot_refs(run_dir: Path) -> set[str]:
    refs: set[str] = set()
    sdir = run_dir / "snapshots"
    if not sdir.is_dir():
        return refs
    for path in sdir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for key in ("ref", "document_id"):
            value = data.get(key)
            if value:
                refs.add(str(value))
    return refs


def valid_memory_search(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("ok") is not True:
        return None
    try:
        hit_count = int(value.get("hit_count") or 0)
    except (TypeError, ValueError):
        return None
    if hit_count != 0:
        return None
    query = str(value.get("query") or "").strip()
    if not query:
        return None
    return {
        "query": query,
        "scope": value.get("scope"),
        "ok": True,
        "hit_count": 0,
        "retrieved_at": value.get("retrieved_at"),
    }


def host_recorded_zero_hit(run_dir: Path, memory_search: dict[str, Any]) -> bool:
    rec = valid_memory_search(memory_search)
    if rec is None:
        return False
    for row in load_memory_searches(run_dir):
        if row.get("ok") is not True:
            continue
        try:
            hit_count = int(row.get("hit_count") or 0)
        except (TypeError, ValueError):
            continue
        if hit_count != 0:
            continue
        if str(row.get("query") or "").strip() != rec["query"]:
            continue
        if row.get("scope") != rec["scope"]:
            continue
        return True
    return False


def _host_backed(run_dir: Path, gap: dict[str, Any]) -> bool:
    refs = [str(r) for r in (gap.get("memory_refs") or []) if r]
    if refs:
        known = host_snapshot_refs(run_dir)
        return all(ref in known for ref in refs)
    search = gap.get("memory_search")
    if isinstance(search, dict):
        return host_recorded_zero_hit(run_dir, search)
    return False


def normalize_gap(raw: dict[str, Any]) -> dict[str, Any] | None:
    gap_id = str(raw.get("id") or "").strip()
    question = str(raw.get("question") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    if not gap_id or not question or reason not in REASONS:
        return None
    refs = raw.get("memory_refs") or []
    if not isinstance(refs, list):
        return None
    memory_refs = [str(r) for r in refs if r]
    memory_search = valid_memory_search(raw.get("memory_search"))
    if reason in {"stale", "contradicted"}:
        if not memory_refs:
            return None
        return {
            "id": gap_id,
            "question": question,
            "reason": reason,
            "memory_refs": memory_refs,
        }
    if memory_refs:
        return {
            "id": gap_id,
            "question": question,
            "reason": "uncovered",
            "memory_refs": memory_refs,
        }
    if memory_search is None:
        return None
    return {
        "id": gap_id,
        "question": question,
        "reason": "uncovered",
        "memory_refs": [],
        "memory_search": memory_search,
    }


def upsert_gap(run_dir: Path, raw: dict[str, Any]) -> dict[str, Any] | None:
    gap = normalize_gap(raw)
    if gap is None or not _host_backed(run_dir, gap):
        return None
    gaps = load_gaps(run_dir)
    by_id = {str(g.get("id")): g for g in gaps}
    by_id[gap["id"]] = gap
    save_gaps(run_dir, list(by_id.values()))
    return gap


def accepted_gap(run_dir: Path, gap_id: str) -> dict[str, Any] | None:
    wanted = str(gap_id or "").strip()
    if not wanted:
        return None
    for gap in load_gaps(run_dir):
        if str(gap.get("id")) == wanted:
            return gap
    return None
