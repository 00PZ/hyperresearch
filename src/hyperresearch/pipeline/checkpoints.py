"""Write-ahead task_id log for model calls and host actions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hyperresearch.runtime.types import AgentResult

LOG_NAME = "task_log.jsonl"
TERMINAL_SUCCESS = "success"
UNCERTAIN_REMOTE = "uncertain_remote"
PENDING = "pending"
FAILED = "failed"


@dataclass
class TaskRecord:
    task_id: str
    kind: str
    status: str
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    intended_hash: str | None = None
    error: str | None = None


class TaskLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_id: dict[str, TaskRecord] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec = TaskRecord(
                    task_id=data["task_id"],
                    kind=data.get("kind", ""),
                    status=data.get("status", PENDING),
                    args=data.get("args") or {},
                    result=data.get("result"),
                    intended_hash=data.get("intended_hash"),
                    error=data.get("error"),
                )
                self._by_id[rec.task_id] = rec

    def get(self, task_id: str) -> TaskRecord | None:
        return self._by_id.get(task_id)

    def is_success(self, task_id: str) -> bool:
        rec = self._by_id.get(task_id)
        return rec is not None and rec.status == TERMINAL_SUCCESS

    def result_payload(self, task_id: str) -> dict[str, Any] | None:
        rec = self.get(task_id)
        return None if rec is None else rec.result

    def result_text(self, task_id: str) -> str:
        payload = self.result_payload(task_id)
        if not payload:
            return ""
        text = payload.get("text", "")
        return text if isinstance(text, str) else ""

    def result_agent(self, task_id: str, fallback_model: str = "") -> AgentResult:
        return load_agent_result(self.result_payload(task_id), fallback_model)



    def _append(self, rec: TaskRecord) -> None:
        self._by_id[rec.task_id] = rec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(rec), sort_keys=True) + "\n")

    def begin(
        self,
        task_id: str,
        kind: str,
        args: dict[str, Any] | None = None,
        intended_hash: str | None = None,
    ) -> TaskRecord:
        existing = self._by_id.get(task_id)
        if existing and existing.status == TERMINAL_SUCCESS:
            return existing
        rec = TaskRecord(
            task_id=task_id,
            kind=kind,
            status=PENDING,
            args=dict(args or {}),
            intended_hash=intended_hash,
        )
        self._append(rec)
        return rec

    def succeed(self, task_id: str, result: dict[str, Any] | None = None) -> TaskRecord:
        rec = self._by_id.get(task_id)
        if rec is None:
            rec = TaskRecord(task_id=task_id, kind="", status=TERMINAL_SUCCESS, result=result)
        else:
            rec = TaskRecord(
                task_id=rec.task_id,
                kind=rec.kind,
                status=TERMINAL_SUCCESS,
                args=rec.args,
                result=result,
                intended_hash=rec.intended_hash,
            )
        self._append(rec)
        return rec

    def fail(self, task_id: str, error: str) -> TaskRecord:
        rec = self._by_id.get(task_id)
        kind = rec.kind if rec else ""
        args = rec.args if rec else {}
        new = TaskRecord(task_id=task_id, kind=kind, status=FAILED, args=args, error=error)
        self._append(new)
        return new

    def mark_uncertain_remote(self, task_id: str) -> TaskRecord:
        rec = self._by_id.get(task_id)
        kind = rec.kind if rec else "model"
        args = rec.args if rec else {}
        new = TaskRecord(task_id=task_id, kind=kind, status=UNCERTAIN_REMOTE, args=args)
        self._append(new)
        return new

    def resume_model(self, task_id: str) -> str:
        """What resume should do for a model call. Never auto-POST uncertain_remote."""
        rec = self._by_id.get(task_id)
        if rec is None:
            return "launch"
        if rec.status == TERMINAL_SUCCESS:
            return "skip"
        if rec.status == UNCERTAIN_REMOTE:
            return UNCERTAIN_REMOTE
        if rec.status == PENDING and rec.kind == "model":
            self.mark_uncertain_remote(task_id)
            return UNCERTAIN_REMOTE
        if rec.status == FAILED:
            return "retry"
        return "launch"


def dump_agent_result(result: AgentResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "structured": result.structured,
        "usage": dict(result.usage or {}),
        "requested_model": result.requested_model,
        "reported_model": result.reported_model,
        "runtime_metadata": dict(result.runtime_metadata or {}),
    }


def load_agent_result(payload: dict[str, Any] | None, fallback_model: str = "") -> AgentResult:
    if not payload:
        return AgentResult(text="", requested_model=fallback_model)
    return AgentResult(
        text=str(payload.get("text") or ""),
        structured=payload.get("structured"),
        usage=dict(payload.get("usage") or {}),
        requested_model=str(payload.get("requested_model") or fallback_model),
        reported_model=payload.get("reported_model"),
        runtime_metadata=dict(payload.get("runtime_metadata") or {}),
    )

