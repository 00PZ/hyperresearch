"""GBrain KnowledgeReader via HTTP MCP. Lazy extra; pipeline must not import at load."""

from __future__ import annotations

import json
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
_JSON_TOOLS = frozenset({"search", "list_pages", "get_page", "get_raw_data"})
_SEARCH_ARGS = frozenset({"limit", "offset", "types", "source_id"})
_LIST_ARGS = frozenset({"type", "tag", "limit", "offset", "sort", "updated_after", "source_id"})


class GBrainError(Exception):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message or code)
        self.code = code


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
        rpc_id = self._rpc_id
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = self._client.post(self.url, json=payload)
        if response.status_code >= 400:
            raise GBrainError(f"http:{response.status_code}")
        data = _rpc_message(response, rpc_id)
        if data.get("error"):
            raise GBrainError(str(data["error"]))
        return _decode_result(name, data.get("result"))

    def get_page(self, slug: str, include_content: bool | None = None) -> Any:
        args: dict[str, Any] = {"slug": slug}
        if include_content is not None:
            args["include_content"] = include_content
        try:
            return self.call("get_page", args)
        except GBrainError as exc:
            if exc.code == "page_not_found":
                return None
            raise

    def get_raw_data(self, slug: str, source: str | None = None) -> Any:
        args: dict[str, Any] = {"slug": slug}
        if source is not None:
            args["source"] = source
        return self.call("get_raw_data", args)

    def put_page(self, slug: str, content: str) -> Any:
        return self.call("put_page", {"slug": slug, "content": content})

    def put_raw_data(self, slug: str, source: str, data: dict[str, Any]) -> Any:
        return self.call("put_raw_data", {"slug": slug, "source": source, "data": data})

    def search(self, query: str, **kwargs: Any) -> Any:
        args: dict[str, Any] = {"query": query}
        for key in _SEARCH_ARGS:
            if key in kwargs and kwargs[key] is not None:
                args[key] = kwargs[key]
        return self.call("search", args)

    def list_pages(self, **kwargs: Any) -> Any:
        args = {k: v for k, v in kwargs.items() if k in _LIST_ARGS and v is not None}
        return self.call("list_pages", args)

    def list_pages_by_prefix(
        self,
        prefix: str,
        *,
        page_type: str | None = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        page_size = min(max(int(limit), 1), 100)
        offset = 0
        matched: list[dict[str, Any]] = []
        while True:
            args: dict[str, Any] = {"limit": page_size, "offset": offset, **kwargs, "sort": "slug"}
            if page_type is not None:
                args["type"] = page_type
            rows = self.list_pages(**args)
            if not isinstance(rows, list):
                raise GBrainError("unrecognized list_pages payload")
            for row in rows:
                if isinstance(row, dict) and str(row.get("slug") or "").startswith(prefix):
                    matched.append(row)
            if len(rows) < page_size:
                return matched
            offset += len(rows)


def _rpc_message(response: httpx.Response, rpc_id: int) -> dict[str, Any]:
    ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "text/event-stream":
        msg = _sse_message(response.text, rpc_id)
    elif ctype == "application/json":
        try:
            parsed = response.json()
        except json.JSONDecodeError as exc:
            raise GBrainError("transport:invalid_json") from exc
        if not isinstance(parsed, dict) or parsed.get("id") != rpc_id:
            raise GBrainError("transport:id")
        msg = parsed
    else:
        raise GBrainError("transport:content_type")
    return msg


def _sse_message(text: str, rpc_id: int) -> dict[str, Any]:
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == rpc_id:
            return msg
    raise GBrainError("transport:no_matching_message")


def _content_text(result: dict[str, Any]) -> str | None:
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            return text
    return None


def _decode_result(name: str, result: Any) -> Any:
    if not isinstance(result, dict):
        if name in _JSON_TOOLS:
            raise GBrainError("transport:bad_result")
        return result
    if result.get("isError"):
        code, message = _iserror(result)
        raise GBrainError(message, code)
    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)):
        return structured
    if isinstance(structured, str) and name in _JSON_TOOLS:
        try:
            return json.loads(structured)
        except json.JSONDecodeError as exc:
            raise GBrainError("non_json") from exc
    text = _content_text(result)
    if name in _JSON_TOOLS:
        if text is None:
            raise GBrainError("non_json")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GBrainError("non_json") from exc
    if text is None:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _iserror(result: dict[str, Any]) -> tuple[str, str]:
    text = _content_text(result) or ""
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict) and payload.get("error"):
        code = str(payload["error"])
        return code, str(payload.get("message") or code)
    return "mcp:isError", text or "mcp:isError"


def _allowed_slug(slug: str) -> bool:
    if not slug or is_stark_ref(slug):
        return False
    if slug.startswith(SHOSHIN_SKIP_PREFIXES):
        return False
    return slug.startswith(SHOSHIN_WIKI_PREFIX) or slug.startswith(SHOSHIN_REPORT_PREFIX)


def _provenance(frontmatter: Any) -> list[str]:
    if not isinstance(frontmatter, dict):
        return []
    raw = frontmatter.get("provenance")
    if not isinstance(raw, list):
        raw = frontmatter.get("sources")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


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
        """Search company knowledge.

        If ``scope`` is a non-empty string, pass ``types=[scope]`` to
        ``client.search``. Otherwise omit ``types``.
        """
        kwargs: dict[str, Any] = {}
        if isinstance(scope, str) and scope:
            kwargs["types"] = [scope]
        try:
            result = self.client.search(query, **kwargs)
        except (GBrainError, httpx.HTTPError, ValueError, TypeError) as exc:
            return error_result(str(exc))
        if not isinstance(result, list):
            return error_result("unrecognized search payload")
        best: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for chunk in result:
            if not isinstance(chunk, dict):
                continue
            slug = str(chunk.get("slug") or "")
            if not _allowed_slug(slug):
                continue
            prev = best.get(slug)
            score = float(chunk.get("score") or 0)
            if prev is None:
                best[slug] = chunk
                order.append(slug)
            elif score > float(prev.get("score") or 0):
                best[slug] = chunk
        hits: list[dict[str, Any]] = [
            {
                "ref": slug,
                "namespace": self.namespace,
                "document_id": slug,
                "type": str(best[slug].get("type") or ""),
                "content_hash": str(best[slug].get("content_hash") or ""),
                "provenance": [],
                "title": best[slug].get("title"),
            }
            for slug in order
        ]
        return {"ok": True, "hits": hits, "hit_count": len(hits)}

    def get(self, ref: str) -> dict[str, Any]:
        if is_stark_ref(ref) or not _allowed_slug(ref):
            return {"ok": False, "error": "prohibited", "ref": ref}
        try:
            page = self.client.get_page(ref)
        except (GBrainError, httpx.HTTPError, ValueError, TypeError) as exc:
            return {**error_result(str(exc)), "ref": ref}
        if page is None:
            return {"ok": False, "error": "note_not_found", "ref": ref}
        if not isinstance(page, dict):
            return {**error_result("unrecognized get_page payload"), "ref": ref}
        return {
            "ok": True,
            "body": str(page.get("compiled_truth") or ""),
            "namespace": self.namespace,
            "document_id": str(page.get("slug") or ref),
            "type": str(page.get("type") or ""),
            "content_hash": str(page.get("content_hash") or ""),
            "provenance": _provenance(page.get("frontmatter")),
        }
