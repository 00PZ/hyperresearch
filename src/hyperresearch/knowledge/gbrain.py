"""GBrain KnowledgeReader via HTTP MCP. Lazy extra; pipeline must not import at load."""

from __future__ import annotations

import os
from typing import Any

import httpx

from hyperresearch.pipeline.knowledge import (
    SHOSHIN_REPORT_PREFIX,
    SHOSHIN_SKIP_PREFIXES,
    SHOSHIN_WIKI_PREFIX,
    error_result,
    is_stark_ref,
)

DEFAULT_MCP_URL = "https://brain-jarvis-company.tail8ab21.ts.net/mcp"


class GBrainError(Exception):
    pass


class GBrainClient:
    def __init__(
        self,
        url: str,
        bearer: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.bearer = bearer
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "authorization": f"Bearer {bearer}",
            },
        )
        self._rpc_id = 0

    def close(self) -> None:
        self._client.close()

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = self._client.post(self.url, json=payload)
        if response.status_code >= 400:
            raise GBrainError(f"http:{response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise GBrainError("http:non_object")
        if data.get("error"):
            raise GBrainError(str(data["error"]))
        result = data.get("result")
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if isinstance(content, list) and content and isinstance(content[0], dict):
                text = content[0].get("text")
                if isinstance(text, str):
                    return text
            return content
        return result

    def get_page(self, slug: str) -> Any:
        return self.call("get_page", {"slug": slug})

    def get_raw_data(self, key: str) -> Any:
        return self.call("get_raw_data", {"key": key})

    def put_page(self, slug: str, **kwargs: Any) -> Any:
        """Unconditional replace. No if-match."""
        args = {"slug": slug, **kwargs}
        args.pop("if_match", None)
        args.pop("ifMatch", None)
        return self.call("put_page", args)

    def put_raw_data(self, key: str, body: bytes | str, **kwargs: Any) -> Any:
        return self.call("put_raw_data", {"key": key, "body": body, **kwargs})

    def search(self, query: str, **kwargs: Any) -> Any:
        return self.call("search", {"query": query, **kwargs})

    def list_pages(self, **kwargs: Any) -> Any:
        return self.call("list_pages", dict(kwargs))


def _pages_from(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [p for p in result if isinstance(p, dict)]
    if isinstance(result, dict):
        for key in ("pages", "hits", "results", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
    return []


def _slug_of(page: dict[str, Any]) -> str:
    return str(page.get("slug") or page.get("id") or page.get("ref") or "")


def _type_of(slug: str, page: dict[str, Any]) -> str:
    declared = str(page.get("type") or "")
    if declared:
        return declared
    if slug.startswith(SHOSHIN_REPORT_PREFIX):
        return "research-report"
    if slug.startswith(SHOSHIN_WIKI_PREFIX):
        return "wiki"
    return ""


def _allowed_slug(slug: str) -> bool:
    if not slug or is_stark_ref(slug):
        return False
    if slug.startswith(SHOSHIN_SKIP_PREFIXES):
        return False
    return slug.startswith(SHOSHIN_WIKI_PREFIX) or slug.startswith(SHOSHIN_REPORT_PREFIX)


class GBrainReader:
    def __init__(self, client: GBrainClient, *, namespace: str = "shoshin") -> None:
        self.client = client
        self.namespace = namespace

    @classmethod
    def from_env(cls, transport: httpx.BaseTransport | None = None) -> GBrainReader:
        url = os.environ.get("GBRAIN_MCP_URL") or DEFAULT_MCP_URL
        bearer = (
            os.environ.get("GBRAIN_SHOSHIN_BEARER")
            or os.environ.get("GBRAIN_SHOSHIN_CONTENT_BEARER")
            or ""
        )
        if not bearer:
            raise GBrainError("unconfigured")
        return cls(GBrainClient(url, bearer, transport=transport))

    def search(self, query: str, scope: str | None = None) -> dict[str, Any]:
        try:
            result = self.client.search(query, scope=scope) if scope else self.client.search(query)
        except (GBrainError, httpx.HTTPError, ValueError, TypeError) as exc:
            return error_result(str(exc))
        hits: list[dict[str, Any]] = []
        for page in _pages_from(result):
            slug = _slug_of(page)
            if not _allowed_slug(slug):
                continue
            provenance = page.get("provenance") or []
            if not isinstance(provenance, list):
                provenance = []
            hits.append(
                {
                    "ref": slug,
                    "namespace": self.namespace,
                    "document_id": slug,
                    "type": _type_of(slug, page),
                    "content_hash": str(page.get("content_hash") or page.get("hash") or ""),
                    "provenance": [str(p) for p in provenance if p],
                    "title": page.get("title"),
                }
            )
        return {"ok": True, "hits": hits, "hit_count": len(hits)}

    def get(self, ref: str) -> dict[str, Any]:
        if is_stark_ref(ref) or not _allowed_slug(ref):
            return {"ok": False, "error": "prohibited", "ref": ref}
        try:
            page = self.client.get_page(ref)
        except (GBrainError, httpx.HTTPError, ValueError, TypeError) as exc:
            return {**error_result(str(exc)), "ref": ref}
        if not isinstance(page, dict):
            body = str(page or "")
            return {
                "ok": True,
                "body": body,
                "namespace": self.namespace,
                "document_id": ref,
                "type": _type_of(ref, {}),
                "content_hash": "",
                "provenance": [],
            }
        body = str(page.get("body") or page.get("content") or "")
        provenance = page.get("provenance") or []
        if not isinstance(provenance, list):
            provenance = []
        return {
            "ok": True,
            "body": body,
            "namespace": self.namespace,
            "document_id": str(page.get("slug") or ref),
            "type": _type_of(ref, page),
            "content_hash": str(page.get("content_hash") or page.get("hash") or ""),
            "provenance": [str(p) for p in provenance if p],
        }
