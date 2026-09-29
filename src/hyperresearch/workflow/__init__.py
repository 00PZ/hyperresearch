"""Company workflow worker. Not imported by hpr CLI or pipeline core."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hyperresearch.core.frontmatter import FRONTMATTER_RE
from hyperresearch.pipeline.knowledge import SHOSHIN_WIKI_PREFIX
from hyperresearch.pipeline.package import PackageError, package_path, validate_package

REPORT_PREFIX = "companies/shoshin/research/reports/"
QUEUE_PREFIX = "companies/shoshin/research/queue/"
INDEX_SLUG = "companies/shoshin/research/index"
SCHEMA_TYPES = {
    "research-report": REPORT_PREFIX,
    "research-queue": QUEUE_PREFIX,
    "research-index": INDEX_SLUG,
}


class LockHeldError(Exception):
    pass


class WorkflowError(Exception):
    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class IndexParseError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _body_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _markdown(meta: dict[str, Any], body: str = "") -> str:
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    return f"---\n{dumped}---\n{body}"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}



def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise IndexParseError("unparseable index")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise IndexParseError("unparseable index") from exc
    if data is None:
        return {}, text[match.end() :]
    if not isinstance(data, dict):
        raise IndexParseError("unparseable index")
    return data, text[match.end() :]


def _report_title(text: str, run_id: str) -> str:
    body = text
    fm_title = ""
    match = FRONTMATTER_RE.match(text)
    if match:
        body = text[match.end() :]
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            raw = data.get("title")
            if isinstance(raw, str) and raw.strip():
                fm_title = raw.strip()
    for line in body.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            if heading:
                return heading
    return fm_title or run_id


def _frontmatter_and_rest(page: dict[str, Any]) -> tuple[dict[str, Any], str]:
    content = page.get("content")
    if not isinstance(content, str):
        raise IndexParseError("unparseable index")
    return _parse_frontmatter(content)


@contextmanager
def company_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockHeldError("company lock held") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def workflow_path(run_dir: Path) -> Path:
    return run_dir / "workflow.json"


def load_workflow(run_dir: Path) -> dict[str, Any]:
    path = workflow_path(run_dir)
    if not path.exists():
        return {
            "publication": {"status": "idle"},
            "ingest": {"status": "idle"},
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"publication": {"status": "idle"}, "ingest": {"status": "idle"}}


def save_workflow(run_dir: Path, data: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = workflow_path(run_dir)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, dest)


def freeze_envelope(
    *,
    run_id: str,
    report_bytes: bytes,
    title: str,
    package_digest: str,
    company: str = "shoshin",
    raw_bodies: dict[str, bytes] | None = None,
    raw_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    published = report_bytes
    raw: list[dict[str, Any]]
    if raw_items is not None:
        raw = list(raw_items)
    else:
        raw = [{"sha256": _sha(v), "body": v.decode("utf-8")} for v in (raw_bodies or {}).values()]
    return {
        "slug": f"{REPORT_PREFIX}{run_id}",
        "title": title,
        "published_at": _now(),
        "published_report_sha256": _sha(published),
        "published_report": published.decode("utf-8") if published[:1] != b"\x00" else "",
        "raw": raw,
        "index_row": {
            "slug": f"{REPORT_PREFIX}{run_id}",
            "run_id": run_id,
            "company": company,
            "title": title,
            "package_digest": package_digest,
            "published_at": None,
        },
    }


def _evidence_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    if isinstance(payload, dict) and "body" in payload:
        return _body_bytes(payload.get("body") or "")
    return raw


def _raw_items_from_package(dest: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for snap in manifest.get("snapshots") or []:
        rel = str(snap.get("path") or "")
        spath = dest / rel
        if not rel or not spath.is_file():
            continue
        body = _evidence_bytes(spath)
        items.append(
            {
                "sha256": _sha(body),
                "path": rel,
                "document_id": snap.get("document_id"),
                "namespace": snap.get("namespace"),
                "type": snap.get("type"),
                "provenance": snap.get("provenance") or [],
                "body": body.decode("utf-8"),
            }
        )
    return items


def _raw_iter(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _fail_pub(state: dict[str, Any], pub: dict[str, Any], run_dir: Path, **fields: Any) -> dict[str, Any]:
    pub.update(fields)
    save_workflow(run_dir, state)
    return state


def publish_package(
    gbrain: Any,
    run_dir: Path,
    *,
    company: str,
    run_id: str,
    bearer: str | None,
) -> dict[str, Any]:
    state = load_workflow(run_dir)
    pub = state.setdefault("publication", {"status": "idle"})
    if not bearer:
        return _fail_pub(state, pub, run_dir, status="failed", reason="unconfigured")
    dest = package_path(run_dir)
    try:
        manifest = validate_package(dest)
    except PackageError as exc:
        reason = "stale_bindings" if exc.reason == "stale_bindings" else "artifact_error"
        return _fail_pub(state, pub, run_dir, status="failed", reason=reason)
    envelope = pub.get("envelope")
    if not isinstance(envelope, dict):
        report = (dest / str((manifest.get("report") or {}).get("path") or "report.md")).read_bytes()
        text = report.decode("utf-8", errors="replace")
        title = _report_title(text, run_id)
        envelope = freeze_envelope(
            run_id=run_id,
            report_bytes=report,
            title=title,
            package_digest=str(manifest.get("package_digest") or ""),
            company=company,
            raw_items=_raw_items_from_package(dest, manifest),
        )
        pub["envelope"] = envelope
        save_workflow(run_dir, state)
    slug = envelope["slug"]
    published_hash = envelope["published_report_sha256"]
    try:
        remote = gbrain.get_page(slug)
    except Exception as exc:
        return _fail_pub(state, pub, run_dir, status="failed", reason="http", detail=str(exc))
    if remote is None:
        skip_report = False
    elif isinstance(remote, dict):
        fm = _mapping(remote.get("frontmatter"))
        remote_sha = fm.get("published_sha256")
        if remote_sha == published_hash:
            skip_report = True
        else:
            return _fail_pub(state, pub, run_dir, status="failed", reason="conflict")
    else:
        return _fail_pub(state, pub, run_dir, status="failed", reason="conflict")
    verification = manifest.get("verification") or {}
    try:
        if not skip_report:
            gbrain.put_page(
                slug,
                _markdown(
                    {
                        "type": "research-report",
                        "title": envelope.get("title"),
                        "run_id": run_id,
                        "company": company,
                        "verified_hash": verification.get("verified_hash"),
                        "package_digest": envelope["index_row"].get("package_digest"),
                        "published_sha256": published_hash,
                    },
                    envelope.get("published_report") or "",
                ),
            )
        pub["status"] = "partial"
        save_workflow(run_dir, state)
        for item in _raw_iter(envelope.get("raw")):
            body_hash = str(item.get("sha256") or "")
            if not body_hash:
                continue
            source = f"research-{body_hash}"
            try:
                existing = gbrain.get_raw_data(slug, source)
            except Exception as exc:
                return _fail_pub(state, pub, run_dir, status="failed", reason="http", detail=str(exc))
            if not isinstance(existing, list):
                return _fail_pub(state, pub, run_dir, status="failed", reason="http", detail="raw:non_list")
            if existing:
                row = existing[0] if isinstance(existing[0], dict) else {}
                data = _mapping(row.get("data"))
                got_sha = data.get("sha256")
                if got_sha == body_hash:
                    continue
                return _fail_pub(state, pub, run_dir, status="failed", reason="raw_conflict")
            if item.get("body") is not None:
                body_text = item["body"] if isinstance(item["body"], str) else _body_bytes(item["body"]).decode("utf-8")
            elif item.get("path"):
                body_text = _evidence_bytes(dest / str(item["path"])).decode("utf-8")
            else:
                continue
            gbrain.put_raw_data(
                slug,
                source,
                {
                    "sha256": body_hash,
                    "document_id": item.get("document_id"),
                    "namespace": item.get("namespace"),
                    "type": item.get("type"),
                    "provenance": item.get("provenance") or [],
                    "body": body_text,
                },
            )
        try:
            index = gbrain.get_page(INDEX_SLUG, include_content=True)
        except Exception as exc:
            return _fail_pub(state, pub, run_dir, status="failed", reason="http", detail=str(exc))
        meta: dict[str, Any]
        md_body: str
        reports: list[dict[str, Any]]
        if index is None:
            meta, md_body = {}, ""
            reports = []
        elif not isinstance(index, dict):
            return _fail_pub(state, pub, run_dir, status="failed", reason="conflict")
        else:
            try:
                meta, md_body = _frontmatter_and_rest(index)
            except IndexParseError:
                return _fail_pub(state, pub, run_dir, status="failed", reason="conflict")
            reports_raw = meta.get("reports")
            if reports_raw is None:
                reports = []
            elif not isinstance(reports_raw, list):
                return _fail_pub(state, pub, run_dir, status="failed", reason="conflict")
            else:
                reports = [r for r in reports_raw if isinstance(r, dict)]
        row = dict(envelope["index_row"])
        row["published_at"] = envelope["published_at"]
        row["verified_hash"] = verification.get("verified_hash")
        reports = [r for r in reports if r.get("run_id") != run_id]
        reports.append(row)
        payload = dict(meta)
        payload["type"] = payload.get("type") or "research-index"
        payload["reports"] = reports
        gbrain.put_page(INDEX_SLUG, _markdown(payload, md_body))
    except Exception as exc:
        return _fail_pub(state, pub, run_dir, status="failed", reason="http", detail=str(exc))
    pub["status"] = "ok"
    save_workflow(run_dir, state)
    return state


def _paperclip_headers() -> dict[str, str]:
    return {"content-type": "application/json"}


def paperclip_post(client: Any, payload: dict[str, Any]) -> Any:
    url = os.environ["PAPERCLIP_API_URL"].rstrip("/") + "/issues"
    return client.post(url, json=payload, headers=_paperclip_headers())


def _issue_id(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in ("id", "issue_id", "issueId"):
            if data.get(key):
                return str(data[key])
        nested = data.get("issue") or data.get("data") or {}
        if isinstance(nested, dict) and nested.get("id"):
            return str(nested["id"])
    return None


def reconcile_ingest(client: Any, research_slug: str) -> dict[str, Any]:
    url = os.environ["PAPERCLIP_API_URL"].rstrip("/") + "/issues"
    page = 1
    while True:
        response = client.get(url, params={"page": page})
        if response.status_code >= 400:
            return {"status": "uncertain"}
        data = response.json()
        items = data if isinstance(data, list) else data.get("issues") or data.get("items") or []
        if not items:
            return {"status": "uncertain"}
        for item in items:
            desc = str(item.get("description") or item.get("body") or "")
            if research_slug in desc:
                return {"status": "posted", "id": item.get("id")}
        if isinstance(data, dict) and not data.get("next") and page >= int(data.get("pages") or page):
            return {"status": "uncertain"}
        page += 1
        if page > 50:
            return {"status": "uncertain"}


def dispatch_ingest(
    client: Any,
    run_dir: Path,
    *,
    research_slug: str,
    retry: bool = False,
) -> dict[str, Any]:
    state = load_workflow(run_dir)
    pub = state.get("publication") or {}
    ingest = state.setdefault("ingest", {"status": "idle"})
    if pub.get("status") != "ok":
        if retry:
            raise WorkflowError("ingest-retry requires publication ok")
        return state
    status = ingest.get("status") or "idle"
    if status == "posted":
        return state
    if status == "failed" and not retry:
        return state
    if status in {"in_flight", "uncertain"} and not retry:
        ingest.update(reconcile_ingest(client, research_slug))
        save_workflow(run_dir, state)
        return state
    ingest["intent"] = {"research_slug": research_slug, "at": _now()}
    ingest["status"] = "in_flight"
    save_workflow(run_dir, state)
    payload = {
        "company_id": os.environ.get("PAPERCLIP_COMPANY_ID"),
        "agent_id": os.environ.get("PAPERCLIP_LIBRARIAN_AGENT_ID"),
        "description": (
            f"research_slug={research_slug}\n"
            "wiki-draft: compile-from-report seed from that research report."
        ),
    }
    try:
        response = paperclip_post(client, payload)
    except Exception:
        ingest["status"] = "uncertain"
        save_workflow(run_dir, state)
        return state
    code = response.status_code
    if code in {401, 403}:
        ingest["status"] = "failed"
        save_workflow(run_dir, state)
        return state
    if code == 409:
        ingest.update(reconcile_ingest(client, research_slug))
        save_workflow(run_dir, state)
        return state
    if code >= 500 or code >= 400:
        ingest["status"] = "uncertain"
        save_workflow(run_dir, state)
        return state
    ident = _issue_id(response.json() if response.content else None)
    if ident:
        ingest["status"] = "posted"
        ingest["id"] = ident
    else:
        ingest["status"] = "uncertain"
    save_workflow(run_dir, state)
    return state


def merge_queue(gbrain: Any, slug: str, **fields: Any) -> dict[str, Any]:
    page = gbrain.get_page(slug, include_content=True)
    fm: dict[str, Any]
    rest: str
    if isinstance(page, dict):
        try:
            fm, rest = _frontmatter_and_rest(page)
        except IndexParseError:
            fm, rest = _mapping(page.get("frontmatter")), ""
    else:
        fm, rest = {}, ""
    merged = {**fm, **fields, "type": "research-queue"}
    if "query" not in merged or "run_id" not in merged:
        raise WorkflowError("queue put_page dropped query/run_id")
    gbrain.put_page(slug, _markdown(merged, rest))
    return merged


def wiki_draft(gbrain: Any, research_slug: str) -> str:
    page = gbrain.get_page(research_slug)
    if isinstance(page, dict):
        return str(page.get("compiled_truth") or "")
    return str(page or "")


def load_queue_pages(gbrain: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in gbrain.list_pages_by_prefix(QUEUE_PREFIX, page_type="research-queue"):
        slug = str(row.get("slug") or "")
        if not slug:
            continue
        page = gbrain.get_page(slug, include_content=True)
        if not isinstance(page, dict):
            continue
        try:
            fm, _rest = _frontmatter_and_rest(page)
        except IndexParseError:
            fm = _mapping(page.get("frontmatter"))
        items.append({"slug": slug, **fm})
    return items



_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_DASH_ONLY = re.compile(r"^[\-\u2013\u2014\u2212*·.\s]+$")
_SKU = re.compile(r"^sku\b", re.I)


def _skip_gap(text: str, wiki_slug: str) -> bool:
    if not text or _DASH_ONLY.fullmatch(text):
        return True
    if _SKU.match(text):
        return True
    return "primer" in wiki_slug.lower() and " " not in text


def _parse_wiki_gaps(body: str, wiki_slug: str = "") -> list[str]:
    items: list[str] = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            name = stripped.lstrip("#").strip().rstrip(":").lower()
            in_section = name in {"open-gaps", "gaps"}
            continue
        if not in_section:
            continue
        match = _LIST_ITEM.match(line)
        if not match:
            continue
        text = match.group(1).strip()
        if _skip_gap(text, wiki_slug):
            continue
        items.append(text)
    return items


def harvest_gaps(*, company: str, gbrain: Any, lock_path: Path) -> dict[str, Any]:
    if company != "shoshin":
        raise WorkflowError("unknown company", code=1)
    try:
        with company_lock(lock_path):
            return _harvest_locked(gbrain)
    except LockHeldError:
        raise WorkflowError("lock held", code=1) from None


def _harvest_locked(gbrain: Any) -> dict[str, Any]:
    existing: set[str] = set()
    for row in gbrain.list_pages_by_prefix(QUEUE_PREFIX, page_type="research-queue"):
        slug = str(row.get("slug") or "")
        page = gbrain.get_page(slug, include_content=True) if slug else None
        if not isinstance(page, dict):
            continue
        try:
            fm, _rest = _frontmatter_and_rest(page)
        except IndexParseError:
            fm = _mapping(page.get("frontmatter"))
        digest = str(fm.get("dedup_hash") or "")
        if digest:
            existing.add(digest)
    added: list[dict[str, Any]] = []
    for row in gbrain.list_pages_by_prefix(SHOSHIN_WIKI_PREFIX):
        slug = str(row.get("slug") or "")
        if not slug.startswith(SHOSHIN_WIKI_PREFIX):
            continue
        page = gbrain.get_page(slug)
        if not isinstance(page, dict):
            continue
        text = str(page.get("compiled_truth") or "")
        for gap_text in _parse_wiki_gaps(text, slug):
            digest = _sha(f"{slug}{gap_text}".encode())
            if digest in existing:
                continue
            existing.add(digest)
            payload = {
                "type": "research-queue",
                "origin": "wiki-gap",
                "query": gap_text,
                "status": "pending",
                "wiki_slug": slug,
                "gap_text": gap_text,
                "dedup_hash": digest,
            }
            gbrain.put_page(f"{QUEUE_PREFIX}{digest}", _markdown(payload))
            added.append(payload)
    return {"ok": True, "added": len(added), "items": added}


def drain(
    *,
    company: str,
    tier: str,
    vault: Any,
    gbrain: Any,
    paperclip: Any | None,
    run_hpr: Callable[..., Any],
    lock_path: Path,
    queue_pages: list[dict[str, Any]] | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    if company != "shoshin":
        raise WorkflowError("unknown company", code=1)
    try:
        with company_lock(lock_path):
            pages = load_queue_pages(gbrain) if queue_pages is None else queue_pages
            return _drain_locked(
                company=company,
                tier=tier,
                vault=vault,
                gbrain=gbrain,
                paperclip=paperclip,
                run_hpr=run_hpr,
                queue_pages=pages,
                runtime=runtime,
            )
    except LockHeldError:
        raise WorkflowError("lock held", code=1) from None


def _drain_locked(
    *,
    company: str,
    tier: str,
    vault: Any,
    gbrain: Any,
    paperclip: Any | None,
    run_hpr: Callable[..., Any],
    queue_pages: list[dict[str, Any]],
    runtime: Any | None,
) -> dict[str, Any]:
    results = []
    for page in queue_pages:
        item: dict[str, Any] = page
        slug = str(page.get("slug") or item.get("slug") or "")
        status = str(item.get("status") or "pending")
        run_id = item.get("run_id")
        if status == "pending":
            run_id = run_id or uuid.uuid4().hex[:12]
            merge_queue(gbrain, slug, status="running", run_id=run_id, query=item.get("query"))
            status = "running"
        if not run_id:
            continue
        run_dir = vault.run_dir(str(run_id))
        manifest = None
        try:
            from hyperresearch.core.runs import load_manifest

            manifest = load_manifest(vault, str(run_id))
        except Exception:
            manifest = None
        pkg = (manifest or {}).get("package") if isinstance(manifest, dict) else None
        if isinstance(pkg, dict) and pkg.get("status") == "invalid":
            results.append({"run_id": run_id, "action": "skip-invalid"})
            continue
        research_status = (manifest or {}).get("status")
        complete = False
        if research_status == "verified":
            try:
                validate_package(package_path(run_dir))
                complete = True
            except PackageError:
                complete = False
        if research_status == "completed" and tier != "full":
            results.append({"run_id": run_id, "action": "light-skip"})
            continue
        if not complete:
            resume = research_status == "verified" or (
                status == "running" and manifest is not None
            )
            run_hpr(str(item.get("query") or ""), str(run_id), resume=resume)
            try:
                from hyperresearch.core.runs import load_manifest

                manifest = load_manifest(vault, str(run_id))
            except Exception:
                manifest = None
            research_status = (manifest or {}).get("status")
            if research_status == "completed" and tier != "full":
                results.append({"run_id": run_id, "action": "light-skip"})
                continue
            try:
                validate_package(package_path(run_dir))
                complete = research_status == "verified"
            except PackageError:
                complete = False
        if not complete:
            results.append({"run_id": run_id, "action": "research"})
            continue
        bearer = os.environ.get("GBRAIN_SHOSHIN_BEARER") or os.environ.get("GBRAIN_SHOSHIN_CONTENT_BEARER")
        state = publish_package(gbrain, run_dir, company=company, run_id=str(run_id), bearer=bearer)
        if (state.get("publication") or {}).get("status") == "ok" and paperclip is not None:
            dispatch_ingest(
                paperclip,
                run_dir,
                research_slug=f"{REPORT_PREFIX}{run_id}",
            )
            wf = load_workflow(run_dir)
            if (wf.get("ingest") or {}).get("status") == "posted":
                merge_queue(
                    gbrain,
                    slug,
                    status="published",
                    run_id=run_id,
                    query=item.get("query"),
                    ingest_id=(wf.get("ingest") or {}).get("id"),
                )
            else:
                merge_queue(
                    gbrain,
                    slug,
                    status="published_pending_ingest",
                    run_id=run_id,
                    query=item.get("query"),
                )
        results.append({"run_id": run_id, "action": "publish", "workflow": load_workflow(run_dir)})
    return {"ok": True, "results": results}


def ingest_retry(
    paperclip: Any,
    run_dir: Path,
    research_slug: str,
    lock_path: Path,
) -> dict[str, Any]:
    try:
        with company_lock(lock_path):
            state = load_workflow(run_dir)
            if (state.get("publication") or {}).get("status") != "ok":
                raise WorkflowError("ingest-retry requires publication ok")
            ingest = state.get("ingest") or {}
            if ingest.get("status") != "failed":
                raise WorkflowError("ingest-retry only from failed")
            return dispatch_ingest(paperclip, run_dir, research_slug=research_slug, retry=True)
    except LockHeldError:
        raise WorkflowError("lock held", code=1) from None
