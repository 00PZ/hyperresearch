"""KnowledgeReader protocol and in-core adapters (none, files). GBrain is lazy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

ALLOWED_COMPANIES = frozenset({"shoshin"})
KNOWLEDGE_BACKENDS = frozenset({"none", "files", "gbrain"})
STARK_PREFIXES = ("companies/stark/", "stark/")
SHOSHIN_WIKI_PREFIX = "companies/shoshin/knowledge/wiki/"
SHOSHIN_REPORT_PREFIX = "companies/shoshin/research/reports/"
SHOSHIN_SKIP_PREFIXES = (
    "companies/shoshin/research/index",
    "companies/shoshin/research/queue/",
)


class KnowledgeReader(Protocol):
    def search(self, query: str, scope: str | None = None) -> dict[str, Any]: ...
    def get(self, ref: str) -> dict[str, Any]: ...


def is_stark_ref(ref: str) -> bool:
    lowered = ref.strip().lower()
    return lowered.startswith(STARK_PREFIXES) or "/stark/" in lowered


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def empty_success() -> dict[str, Any]:
    return {"ok": True, "hits": [], "hit_count": 0}


def error_result(error: str) -> dict[str, Any]:
    return {"ok": False, "hits": [], "hit_count": 0, "error": error}


@dataclass
class NoneReader:
    """Deliberate empty-success. Does not consult a store."""

    def search(self, query: str, scope: str | None = None) -> dict[str, Any]:
        return empty_success()

    def get(self, ref: str) -> dict[str, Any]:
        return {"ok": False, "error": "note_not_found", "ref": ref}


@dataclass
class FilesReader:
    root: Path
    namespace: str = "shoshin"

    def search(self, query: str, scope: str | None = None) -> dict[str, Any]:
        try:
            root = self.root.resolve()
        except OSError as exc:
            return error_result(f"files:{exc}")
        if not root.is_dir():
            return error_result("files:missing_tree")
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        try:
            for path in sorted(root.rglob("*.md")):
                if is_stark_ref(str(path)):
                    continue
                text = path.read_text(encoding="utf-8-sig")
                if needle and needle not in text.lower() and needle not in path.name.lower():
                    continue
                rel = path.relative_to(root).as_posix()
                hits.append(
                    {
                        "ref": rel,
                        "namespace": self.namespace,
                        "document_id": rel,
                        "type": "wiki",
                        "content_hash": _hash(text),
                        "provenance": [],
                        "title": path.stem,
                    }
                )
        except OSError as exc:
            return error_result(f"files:{exc}")
        return {"ok": True, "hits": hits, "hit_count": len(hits)}

    def get(self, ref: str) -> dict[str, Any]:
        if is_stark_ref(ref):
            return {"ok": False, "error": "prohibited", "ref": ref}
        try:
            root = self.root.resolve()
            path = (root / ref).resolve()
        except OSError as exc:
            return error_result(f"files:{exc}")
        if not path.is_relative_to(root) or not path.is_file():
            return {"ok": False, "error": "note_not_found", "ref": ref}
        try:
            body = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            return error_result(f"files:{exc}")
        return {
            "ok": True,
            "body": body,
            "namespace": self.namespace,
            "document_id": Path(ref).as_posix(),
            "type": "wiki",
            "content_hash": _hash(body),
            "provenance": [],
        }


@dataclass
class FakeKnowledgeReader:
    """CI double. Never opens a network."""

    hits: list[dict[str, Any]] = field(default_factory=list)
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    search_error: str | None = None
    get_error: str | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def search(self, query: str, scope: str | None = None) -> dict[str, Any]:
        self.calls.append(("search", query))
        if self.search_error:
            return error_result(self.search_error)
        return {"ok": True, "hits": list(self.hits), "hit_count": len(self.hits)}

    def get(self, ref: str) -> dict[str, Any]:
        self.calls.append(("get", ref))
        if is_stark_ref(ref):
            return {"ok": False, "error": "prohibited", "ref": ref}
        if self.get_error:
            return {**error_result(self.get_error), "ref": ref}
        page = self.pages.get(ref)
        if page is None:
            return {"ok": False, "error": "note_not_found", "ref": ref}
        return {"ok": True, **page}


def load_reader(
    backend: str,
    *,
    files_root: Path | None = None,
    gbrain_factory: Any | None = None,
) -> KnowledgeReader:
    if backend == "none":
        return NoneReader()
    if backend == "files":
        if files_root is None:
            raise ValueError("files backend needs a tree")
        return FilesReader(root=files_root)
    if backend == "gbrain":
        if gbrain_factory is not None:
            reader: KnowledgeReader = gbrain_factory()
            return reader
        from hyperresearch.knowledge.gbrain import GBrainReader

        return GBrainReader.from_env()
    raise ValueError(f"unknown knowledge backend {backend!r}")


def snapshot_from_get(ref: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = str(payload.get("body") or "")
    provenance = payload.get("provenance") or []
    if not isinstance(provenance, list):
        provenance = []
    return {
        "ref": ref,
        "namespace": str(payload.get("namespace") or ""),
        "document_id": str(payload.get("document_id") or ref),
        "type": str(payload.get("type") or ""),
        "content_hash": str(payload.get("content_hash") or _hash(body)),
        "retrieved_at": _now(),
        "body": body,
        "provenance": [str(p) for p in provenance if p],
    }
