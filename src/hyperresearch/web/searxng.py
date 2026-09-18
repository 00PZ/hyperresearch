"""SearXNG JSON search adapter. Not a fetch backend."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from hyperresearch.core.config import default_searxng_trip_log
from hyperresearch.runtime.errors import SearchBlockError
from hyperresearch.web.base import WebResult

RUN_TRIP_LOG_NAME = "searxng-trip.jsonl"
_ALLOWED_SEARCH = frozenset({"none", "searxng"})
_TIMEOUT_S = 30.0


@dataclass
class SearchCallResult:
    hits: list[WebResult]
    unresponsive_engines: list[list[str]] = field(default_factory=list)


def resolve_searxng_url(cfg: Any, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    if "SEARXNG_URL" in env:
        return env["SEARXNG_URL"]
    return str(getattr(cfg, "searxng_url", "") or "")


def invalid_searxng_url_reason(url: str) -> str | None:
    stripped = url.strip() if url else ""
    if not stripped:
        return "searxng_unconfigured"
    parsed = urlparse(stripped)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or not parsed.hostname
    ):
        return "searxng_config"
    return None


def validate_search_config(cfg: Any, environ: Mapping[str, str] | None = None) -> str | None:
    sp = getattr(cfg, "search_provider", "none") or "none"
    fp = getattr(cfg, "web_provider", "")
    if fp == "searxng":
        return "searxng_config"
    if sp not in _ALLOWED_SEARCH:
        return "searxng_config"
    if sp != "searxng":
        return None
    return invalid_searxng_url_reason(resolve_searxng_url(cfg, environ))


def trip_log_path(cfg: Any) -> Path:
    raw = getattr(cfg, "searxng_trip_log", "") or ""
    return Path(raw or default_searxng_trip_log())


def searxng_search(
    base_url: str,
    query: str,
    max_results: int = 10,
    *,
    timeout: float = _TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
) -> SearchCallResult:
    limit = max(1, min(int(max_results), 50))
    if client is not None:
        return _once(client, base_url, query, limit)
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as owned:
        return _once(owned, base_url, query, limit)


def _once(client: httpx.Client, base_url: str, query: str, limit: int) -> SearchCallResult:
    endpoint = base_url.rstrip("/") + "/search"
    try:
        resp = client.get(endpoint, params={"q": query, "format": "json"})
    except httpx.HTTPError as exc:
        raise SearchBlockError("searxng_http", str(exc)) from exc
    if resp.status_code != 200:
        raise SearchBlockError("searxng_http", f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SearchBlockError("searxng_http", "non-json body") from exc
    if not isinstance(data, dict):
        raise SearchBlockError("searxng_http", "json body is not an object")
    if "results" not in data or not isinstance(data["results"], list):
        raise SearchBlockError("searxng_http", "results missing or not a list")
    engines = _engines(data.get("unresponsive_engines"))
    return SearchCallResult(hits=_hits(data["results"], limit), unresponsive_engines=engines)


def _engines(raw: Any) -> list[list[str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SearchBlockError("searxng_http", "unresponsive_engines is not a list")
    out: list[list[str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        engine = item[0]
        if not isinstance(engine, str) or not engine.strip():
            continue
        reason = item[1]
        out.append([engine, reason if isinstance(reason, str) else str(reason)])
    return out


def _hits(raw: list[Any], limit: int) -> list[WebResult]:
    seen: set[str] = set()
    hits: list[WebResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        if url in seen:
            continue
        seen.add(url)
        title = item.get("title")
        content = item.get("content")
        title_s = title if isinstance(title, str) else ""
        content_s = content if isinstance(content, str) else ""
        hits.append(WebResult(url=url, title=title_s, content=content_s[:500]))
        if len(hits) >= limit:
            break
    return hits


def trip_row_for(
    run_id: str,
    task_id: str,
    result: SearchCallResult,
    ts: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": f"{run_id}:{task_id}",
        "ts": ts or datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "hits": len(result.hits),
        "unresponsive_engines": result.unresponsive_engines,
    }


def search_call_path(run_dir: Path, task_id: str) -> Path:
    return run_dir / "search-calls" / f"{task_id}.json"


def save_search_call(
    run_dir: Path,
    task_id: str,
    result: SearchCallResult,
    trip_row: dict[str, Any],
) -> None:
    path = search_call_path(run_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hits": [{"url": h.url, "title": h.title, "content": h.content} for h in result.hits],
        "unresponsive_engines": result.unresponsive_engines,
        "trip_row": trip_row,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_search_call(run_dir: Path, task_id: str) -> dict[str, Any] | None:
    path = search_call_path(run_dir, task_id)
    if not path.exists():
        return None
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _repair_jsonl_tail(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if not text:
        return
    decoder = json.JSONDecoder()
    idx = 0
    last_end = 0
    n = len(text)
    truncated = False
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            _, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            truncated = True
            break
        last_end = end
        idx = end
    if truncated:
        kept = text[:last_end]
        if kept and not kept.endswith("\n"):
            kept += "\n"
        path.write_text(kept, encoding="utf-8")
    elif last_end and not text.endswith("\n"):
        path.write_text(text[:last_end] + "\n", encoding="utf-8")


def append_trip_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _repair_jsonl_tail(path)
        event_id = row.get("event_id")
        if event_id and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("event_id") == event_id:
                    return
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def dual_write_trip(run_path: Path, workstation_path: Path, row: dict[str, Any]) -> None:
    try:
        append_trip_row(run_path, row)
        append_trip_row(workstation_path, row)
    except SearchBlockError:
        raise
    except OSError as exc:
        raise SearchBlockError("searxng_trip_log", str(exc)) from exc
