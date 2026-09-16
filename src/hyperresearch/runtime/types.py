"""AgentRuntime seam: model text only. No tools, no workspace, no Jarvis session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

HostActionKind = Literal["search", "fetch", "evidence_read", "complete"]


@dataclass(frozen=True)
class ResearchContext:
    """Frozen per run. Steps do not infer scope from random paths."""

    run_id: str
    canonical_query: str
    tier: str
    profile: str
    runtime_name: str
    workspace_root: Path


@dataclass(frozen=True)
class HostAction:
    kind: HostActionKind
    args: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class AgentTask:
    task_id: str
    role: str
    payload: str
    model: str
    output_schema: type | None = None
    # Empty means complete-only (critics, polish).
    allowed_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentResult:
    text: str
    structured: Any = None
    usage: dict[str, Any] = field(default_factory=dict)
    requested_model: str = ""
    reported_model: str | None = None
    actual_model: None = None  # Spec 1: leave unknown
    runtime_metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentRuntime(Protocol):
    name: str

    async def run(self, task: AgentTask, context: ResearchContext) -> AgentResult: ...

    async def run_many(
        self, tasks: list[AgentTask], context: ResearchContext, concurrency: int
    ) -> list[AgentResult]: ...
