"""OpenAI-compatible chat/completions. No tools on the wire."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from hyperresearch.runtime.errors import (
    RuntimeFailure,
    RuntimeTimeout,
    ToolsRequired,
    UnexpectedToolResponse,
)
from hyperresearch.runtime.parse import parse_structured
from hyperresearch.runtime.types import AgentResult, AgentTask, ResearchContext

_TOOL_KEYS = frozenset({"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"})
_TOOL_FEATURES = frozenset({"tools", "tool_calls", "function_calling", "functions"})
# Upstream profile fields are Claude Code aliases, not provider ids.
_ROLE_ALIASES = frozenset({"haiku", "sonnet", "opus", "default"})


def resolve_provider_model(requested: str | None, default_model: str) -> str:
    name = (requested or "").strip()
    if not name or name in _ROLE_ALIASES:
        return default_model
    return name


def capabilities_require_tools(capabilities: dict[str, Any] | None) -> bool:
    if not capabilities:
        return False
    tools = capabilities.get("tools")
    if tools is True or tools in {"required", "enabled"}:
        return True
    if isinstance(tools, (list, dict)) and tools:
        return True
    if capabilities.get("tool_choice") in {"required", "auto", "any"}:
        return True
    for key in ("supported_features", "features"):
        feats = capabilities.get(key) or []
        if isinstance(feats, list) and any(str(f).lower() in _TOOL_FEATURES for f in feats):
            return True
    return False


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


class ModelRuntime:
    name = "model"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        default_model: str,
        timeout_s: float = 120.0,
        reasoning_effort: str | None = None,
        capabilities: dict[str, Any] | None = None,
        tools_required: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if tools_required or capabilities_require_tools(capabilities):
            raise ToolsRequired("ModelRuntime refuses to start when tools are required or enabled")
        if not base_url:
            raise ToolsRequired("ModelRuntime needs a base_url")
        self.base_url = base_url
        self.api_key = api_key
        self.default_model = default_model
        self.timeout_s = timeout_s
        self.reasoning_effort = reasoning_effort
        self._client = client
        self._owns_client = client is None
        self.last_request_body: dict[str, Any] | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> ModelRuntime:
        base = kwargs.pop("base_url", None) or os.environ.get("HYPERRESEARCH_MODEL_BASE_URL") or os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
        key = kwargs.pop("api_key", None) or os.environ.get("HYPERRESEARCH_MODEL_API_KEY") or os.environ.get(
            "OPENAI_API_KEY", ""
        )
        model = kwargs.pop("default_model", None) or os.environ.get("HYPERRESEARCH_MODEL") or os.environ.get(
            "OPENAI_MODEL", "gpt-4o-mini"
        )
        effort = kwargs.pop("reasoning_effort", None) or os.environ.get("HYPERRESEARCH_REASONING_EFFORT") or None
        if effort:
            kwargs["reasoning_effort"] = effort
        if kwargs.get("timeout_s") is None:
            raw = os.environ.get("HYPERRESEARCH_MODEL_TIMEOUT_S")
            if raw:
                kwargs["timeout_s"] = float(raw)
            elif effort in {"high", "xhigh"}:
                kwargs["timeout_s"] = 3600.0
        return cls(base_url=base, api_key=key, default_model=model, **kwargs)

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _body(self, task: AgentTask) -> dict[str, Any]:
        model = resolve_provider_model(task.model, self.default_model)
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": task.payload}],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if task.output_schema is not None:
            body["response_format"] = {"type": "json_object"}
        return body

    async def run(self, task: AgentTask, context: ResearchContext) -> AgentResult:
        body = self._body(task)
        if _TOOL_KEYS & body.keys():
            raise ToolsRequired("internal error: tools leaked into request")
        self.last_request_body = body
        url = chat_completions_url(self.base_url)
        client = self._client_or_create()
        try:
            response = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException as e:
            raise RuntimeTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise RuntimeFailure(str(e)) from e

        if response.status_code >= 400:
            raise RuntimeFailure(f"HTTP {response.status_code}: {response.text[:500]}")

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise RuntimeFailure(f"non-JSON response: {e}") from e

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("tool_calls") or message.get("function_call") or data.get("tool_calls"):
            raise UnexpectedToolResponse("provider returned tool_calls; not executing")

        text = message.get("content") or ""
        if not isinstance(text, str):
            text = json.dumps(text)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        structured = parse_structured(text, None, task.output_schema)
        reported = data.get("model")
        reported_model = reported if isinstance(reported, str) else None
        return AgentResult(
            text=text,
            structured=structured,
            usage=dict(usage),
            requested_model=resolve_provider_model(task.model, self.default_model),
            reported_model=reported_model,
            actual_model=None,
            runtime_metadata={"id": data.get("id"), "host": urlparse(url).netloc},
        )

    async def run_many(
        self, tasks: list[AgentTask], context: ResearchContext, concurrency: int
    ) -> list[AgentResult]:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        sem = asyncio.Semaphore(concurrency)
        out: list[AgentResult | None] = [None] * len(tasks)

        async def one(i: int, t: AgentTask) -> None:
            async with sem:
                out[i] = await self.run(t, context)

        await asyncio.gather(*[one(i, t) for i, t in enumerate(tasks)])
        return [r for r in out if r is not None]
