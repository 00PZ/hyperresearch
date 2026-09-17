"""Deterministic AgentRuntime for CI. No network."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from hyperresearch.runtime.errors import RuntimeFailure
from hyperresearch.runtime.parse import parse_structured
from hyperresearch.runtime.types import AgentResult, AgentTask, ResearchContext

Response = AgentResult | str | dict[str, Any] | BaseException | Callable[..., Any]


class FakeRuntime:
    name = "fake"

    def __init__(
        self,
        responses: dict[str, Response] | None = None,
        default: Response | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[AgentTask] = []
        self.executed_host_actions: list[Any] = []

    def _lookup(self, task: AgentTask) -> Response:
        if task.task_id in self.responses:
            return self.responses[task.task_id]
        if task.role in self.responses:
            return self.responses[task.role]
        if self.default is not None:
            return self.default
        raise RuntimeFailure(f"no fake response for task_id={task.task_id!r} role={task.role!r}")

    def _materialize(self, spec: Response, task: AgentTask, context: ResearchContext) -> AgentResult:
        if callable(spec) and not isinstance(spec, type):
            spec = spec(task, context)
        if isinstance(spec, BaseException):
            raise spec
        if isinstance(spec, type) and issubclass(spec, BaseException):
            raise spec(task.task_id)
        if isinstance(spec, AgentResult):
            result = spec
        elif isinstance(spec, str):
            result = AgentResult(text=spec, requested_model=task.model)
        elif isinstance(spec, dict):
            text = spec.get("text", json_dumps(spec))
            result = AgentResult(
                text=text if isinstance(text, str) else json_dumps(spec),
                structured=spec.get("structured", spec),
                usage=dict(spec.get("usage") or {}),
                requested_model=task.model,
                reported_model=spec.get("reported_model"),
                runtime_metadata=dict(spec.get("runtime_metadata") or {}),
            )
        else:
            raise RuntimeFailure(f"unusable fake response type {type(spec).__name__}")

        structured = parse_structured(result.text, result.structured, task.output_schema)
        return AgentResult(
            text=result.text,
            structured=structured,
            usage=dict(result.usage),
            requested_model=result.requested_model or task.model,
            reported_model=result.reported_model,
            actual_model=None,
            runtime_metadata=dict(result.runtime_metadata),
        )

    async def run(self, task: AgentTask, context: ResearchContext) -> AgentResult:
        self.calls.append(task)
        spec = self._lookup(task)
        return self._materialize(spec, task, context)

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


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)
