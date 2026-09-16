"""Host patch apply: base hash, unique old_text/occurrence, atomic set, cumulative caps."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyperresearch.pipeline.checkpoints import TaskLog

STATE_NAME = "patch-state.json"
IMMUTABLE_NAMES = frozenset({
    "query.md",
    "prompt-decomposition.json",
    "evidence-digest.md",
    "cite-check-pairs.json",
    "cite-check-findings.json",
})


class PatchError(Exception):
    """Patch rejected. Report unchanged."""


class StructuralEscalationError(PatchError):
    """Critic asked for more than a surgical patch. Block; do not regenerate."""


@dataclass(frozen=True)
class PatchOp:
    old_text: str
    new_text: str
    occurrence: int | None = None
    path: str = ""


@dataclass(frozen=True)
class PatchSet:
    base_report_hash: str
    ops: tuple[PatchOp, ...]


@dataclass(frozen=True)
class PatchPolicy:
    max_hunk_bytes: int = 2048
    max_set_bytes: int = 8192
    max_ops: int = 24
    max_cumulative_bytes: int = 16384
    max_cumulative_hunks: int = 64


@dataclass
class PatchState:
    cumulative_bytes: int = 0
    cumulative_hunks: int = 0
    last_report_hash: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "cumulative_bytes": self.cumulative_bytes,
            "cumulative_hunks": self.cumulative_hunks,
            "last_report_hash": self.last_report_hash,
        }

    @classmethod
    def load(cls, path: Path) -> PatchState:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            cumulative_bytes=int(data.get("cumulative_bytes") or 0),
            cumulative_hunks=int(data.get("cumulative_hunks") or 0),
            last_report_hash=str(data.get("last_report_hash") or ""),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_ops(text: str, ops: tuple[PatchOp, ...]) -> str:
    out = text
    for op in ops:
        out = _apply_one(out, op)
    return out


def _apply_one(text: str, op: PatchOp) -> str:
    if not op.old_text:
        raise PatchError("old_text is required")
    count = text.count(op.old_text)
    if count == 0:
        raise PatchError("old_text not found")
    if op.occurrence is None:
        if count != 1:
            raise PatchError(f"old_text is not unique ({count} matches); pass occurrence")
        return text.replace(op.old_text, op.new_text, 1)
    if op.occurrence < 1:
        raise PatchError("occurrence is 1-based")
    idx = 0
    start = 0
    while idx < op.occurrence:
        pos = text.find(op.old_text, start)
        if pos < 0:
            raise PatchError(f"occurrence {op.occurrence} out of range ({count} matches)")
        idx += 1
        if idx == op.occurrence:
            return text[:pos] + op.new_text + text[pos + len(op.old_text):]
        start = pos + len(op.old_text)
    raise PatchError("occurrence not found")


def _hunk_bytes(op: PatchOp) -> int:
    return len(op.old_text.encode("utf-8")) + len(op.new_text.encode("utf-8"))


def apply_patch_set(
    report_path: Path,
    patch_set: PatchSet,
    *,
    workspace_root: Path,
    state_path: Path,
    policy: PatchPolicy | None = None,
    task_id: str = "patch",
    log: TaskLog | None = None,
) -> dict[str, Any]:
    policy = policy or PatchPolicy()
    if log is not None and log.is_success(task_id):
        payload = log.result_payload(task_id)
        return payload or {"ok": True, "reconciled": True, "skipped": True}

    if not patch_set.ops:
        raise PatchError("empty patch set")
    if len(patch_set.ops) > policy.max_ops:
        raise StructuralEscalationError(f"too many ops: {len(patch_set.ops)} > {policy.max_ops}")

    for op in patch_set.ops:
        name = Path(op.path).name if op.path else report_path.name
        if name in IMMUTABLE_NAMES:
            raise PatchError(f"late-stage immutable path: {name}")
        if op.path:
            target = (workspace_root / op.path).resolve()
            if not target.is_relative_to(workspace_root.resolve()):
                raise PatchError(f"path escapes workspace: {op.path}")
            if target != report_path.resolve():
                raise PatchError(f"wrong path {op.path}")
        hunk = _hunk_bytes(op)
        if hunk > policy.max_hunk_bytes:
            raise StructuralEscalationError(f"hunk too large: {hunk} > {policy.max_hunk_bytes}")
        if not op.old_text.strip() and op.new_text:
            raise StructuralEscalationError("full replace is not a surgical patch")

    set_bytes = sum(_hunk_bytes(op) for op in patch_set.ops)
    if set_bytes > policy.max_set_bytes:
        raise StructuralEscalationError(f"set too large: {set_bytes} > {policy.max_set_bytes}")

    current = report_path.read_text(encoding="utf-8-sig") if report_path.exists() else ""
    current_hash = content_hash(current)
    intended = apply_ops(current, patch_set.ops) if current_hash == patch_set.base_report_hash else None
    intended_hash = content_hash(intended) if intended is not None else None
    if log is not None:
        rec = log.get(task_id)
        if rec and rec.intended_hash and current_hash == rec.intended_hash:
            args = rec.args or {}
            delta_b = int(args.get("bytes") or 0)
            delta_h = int(args.get("hunks") or 0)
            prev_b = int(args.get("prev_bytes") or 0)
            prev_h = int(args.get("prev_hunks") or 0)
            target_b = prev_b + delta_b
            target_h = prev_h + delta_h
            state = PatchState.load(state_path)
            if state.cumulative_bytes < target_b or state.cumulative_hunks < target_h:
                state.cumulative_bytes = target_b
                state.cumulative_hunks = target_h
                state.last_report_hash = current_hash
                state.save(state_path)
            result = {
                "ok": True,
                "reconciled": True,
                "hash": current_hash,
                "bytes": delta_b,
                "hunks": delta_h,
                "cumulative_bytes": state.cumulative_bytes,
                "cumulative_hunks": state.cumulative_hunks,
            }
            log.succeed(task_id, result)
            return result

    if current_hash != patch_set.base_report_hash:
        raise PatchError(
            f"stale base_report_hash: have {current_hash[:12]} want {patch_set.base_report_hash[:12]}"
        )

    state = PatchState.load(state_path)
    if state.cumulative_bytes + set_bytes > policy.max_cumulative_bytes:
        raise StructuralEscalationError(
            f"cumulative byte cap: {state.cumulative_bytes + set_bytes} > {policy.max_cumulative_bytes}"
        )
    if state.cumulative_hunks + len(patch_set.ops) > policy.max_cumulative_hunks:
        raise StructuralEscalationError("cumulative hunk cap exceeded")

    assert intended is not None and intended_hash is not None
    if log is not None:
        log.begin(
            task_id,
            "patch",
            {
                "base": patch_set.base_report_hash,
                "bytes": set_bytes,
                "hunks": len(patch_set.ops),
                "prev_bytes": state.cumulative_bytes,
                "prev_hunks": state.cumulative_hunks,
            },
            intended_hash=intended_hash,
        )

    tmp = report_path.with_suffix(report_path.suffix + ".tmp")
    tmp.write_text(intended, encoding="utf-8")
    tmp.replace(report_path)

    state.cumulative_bytes += set_bytes
    state.cumulative_hunks += len(patch_set.ops)
    state.last_report_hash = intended_hash
    state.save(state_path)

    result = {
        "ok": True,
        "hash": intended_hash,
        "bytes": set_bytes,
        "hunks": len(patch_set.ops),
        "cumulative_bytes": state.cumulative_bytes,
        "cumulative_hunks": state.cumulative_hunks,
    }
    if log is not None:
        log.succeed(task_id, result)
    return result
