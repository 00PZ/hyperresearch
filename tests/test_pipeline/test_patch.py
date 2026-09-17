"""Patch apply: allow/reject, cumulative cap, crash-before-checkpoint."""

from __future__ import annotations

import pytest

from hyperresearch.pipeline.checkpoints import TaskLog
from hyperresearch.pipeline.patch import (
    PatchError,
    PatchOp,
    PatchPolicy,
    PatchSet,
    PatchState,
    StructuralEscalationError,
    apply_patch_set,
    content_hash,
)


def _report(tmp_vault, text: str):
    path = tmp_vault.root / "research" / "notes" / "final_report_p.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_apply_unique_old_text(tmp_vault):
    path = _report(tmp_vault, "alpha beta gamma")
    state = tmp_vault.root / "research" / "runs" / "p" / "patch-state.json"
    result = apply_patch_set(
        path,
        PatchSet(base_report_hash=content_hash("alpha beta gamma"), ops=(PatchOp("beta", "BETA"),)),
        workspace_root=tmp_vault.root,
        state_path=state,
    )
    assert path.read_text(encoding="utf-8") == "alpha BETA gamma"
    assert result["ok"]
    assert result["cumulative_bytes"] > 0


def test_reject_non_unique_without_occurrence(tmp_vault):
    path = _report(tmp_vault, "x x x")
    with pytest.raises(PatchError, match="not unique"):
        apply_patch_set(
            path,
            PatchSet(base_report_hash=content_hash("x x x"), ops=(PatchOp("x", "y"),)),
            workspace_root=tmp_vault.root,
            state_path=tmp_vault.root / "ps.json",
        )
    assert path.read_text(encoding="utf-8") == "x x x"


def test_occurrence_replaces_nth(tmp_vault):
    path = _report(tmp_vault, "x x x")
    apply_patch_set(
        path,
        PatchSet(
            base_report_hash=content_hash("x x x"),
            ops=(PatchOp("x", "Y", occurrence=2),),
        ),
        workspace_root=tmp_vault.root,
        state_path=tmp_vault.root / "ps.json",
    )
    assert path.read_text(encoding="utf-8") == "x Y x"


def test_stale_hash_rejected(tmp_vault):
    path = _report(tmp_vault, "now")
    with pytest.raises(PatchError, match="stale"):
        apply_patch_set(
            path,
            PatchSet(base_report_hash="deadbeef", ops=(PatchOp("now", "then"),)),
            workspace_root=tmp_vault.root,
            state_path=tmp_vault.root / "ps.json",
        )


def test_wrong_path_and_query_immutable(tmp_vault):
    path = _report(tmp_vault, "body")
    with pytest.raises(PatchError, match="wrong path"):
        apply_patch_set(
            path,
            PatchSet(
                base_report_hash=content_hash("body"),
                ops=(PatchOp("body", "x", path="research/notes/other.md"),),
            ),
            workspace_root=tmp_vault.root,
            state_path=tmp_vault.root / "ps.json",
        )
    with pytest.raises(PatchError, match="immutable"):
        apply_patch_set(
            path,
            PatchSet(
                base_report_hash=content_hash("body"),
                ops=(PatchOp("body", "x", path="research/runs/t/query.md"),),
            ),
            workspace_root=tmp_vault.root,
            state_path=tmp_vault.root / "ps.json",
        )


def test_atomic_rollback_on_missing_old_text(tmp_vault):
    path = _report(tmp_vault, "keep me")
    with pytest.raises(PatchError, match="not found"):
        apply_patch_set(
            path,
            PatchSet(
                base_report_hash=content_hash("keep me"),
                ops=(PatchOp("keep me", "ok"), PatchOp("missing", "nope")),
            ),
            workspace_root=tmp_vault.root,
            state_path=tmp_vault.root / "ps.json",
        )
    assert path.read_text(encoding="utf-8") == "keep me"


def test_cumulative_cap_bypass_blocked(tmp_vault):
    path = _report(tmp_vault, "aaaa bbbb")
    policy = PatchPolicy(max_hunk_bytes=20, max_set_bytes=20, max_cumulative_bytes=12)
    state = tmp_vault.root / "ps.json"
    h = content_hash(path.read_text(encoding="utf-8"))
    apply_patch_set(
        path,
        PatchSet(base_report_hash=h, ops=(PatchOp("aaaa", "AAAA"),)),
        workspace_root=tmp_vault.root,
        state_path=state,
        policy=policy,
    )
    h2 = content_hash(path.read_text(encoding="utf-8"))
    with pytest.raises(StructuralEscalationError, match="cumulative"):
        apply_patch_set(
            path,
            PatchSet(base_report_hash=h2, ops=(PatchOp("bbbb", "BBBB"),)),
            workspace_root=tmp_vault.root,
            state_path=state,
            policy=policy,
        )
    assert "BBBB" not in path.read_text(encoding="utf-8")


def test_crash_before_checkpoint_reconciles(tmp_vault):
    path = _report(tmp_vault, "old text here")
    state = tmp_vault.root / "research" / "runs" / "p" / "patch-state.json"
    log_path = tmp_vault.root / "research" / "runs" / "p" / "task_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8")
    h = content_hash(original)
    pset = PatchSet(base_report_hash=h, ops=(PatchOp("old text", "new text"),))

    class BoomLog(TaskLog):
        def succeed(self, task_id, result=None):
            raise RuntimeError("crash before checkpoint")

    boom = BoomLog(log_path)
    with pytest.raises(RuntimeError, match="crash"):
        apply_patch_set(
            path,
            pset,
            workspace_root=tmp_vault.root,
            state_path=state,
            task_id="p1",
            log=boom,
        )
    assert path.read_text(encoding="utf-8") == "new text here"
    # Resume: same task_id + args, file already at intended hash → no-op apply
    log2 = TaskLog(log_path)
    # begin was written with intended_hash; current file matches it
    result = apply_patch_set(
        path,
        pset,
        workspace_root=tmp_vault.root,
        state_path=state,
        task_id="p1",
        log=log2,
    )
    assert result.get("reconciled") or result.get("ok")
    assert path.read_text(encoding="utf-8") == "new text here"
    # cumulative should not double-count on reconcile
    again = apply_patch_set(
        path,
        pset,
        workspace_root=tmp_vault.root,
        state_path=state,
        task_id="p1",
        log=log2,
    )
    assert again.get("skipped") or again.get("reconciled")


def test_crash_after_report_replacement_before_counter_persist(tmp_vault, monkeypatch):
    path = _report(tmp_vault, "old text here")
    state = tmp_vault.root / "research" / "runs" / "p" / "patch-state.json"
    log_path = tmp_vault.root / "research" / "runs" / "p" / "task_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    h = content_hash(path.read_text(encoding="utf-8"))
    pset = PatchSet(base_report_hash=h, ops=(PatchOp("old text", "new text"),))
    n = {"i": 0}
    real_save = PatchState.save

    def boom(self, path_):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("save fail")
        return real_save(self, path_)

    monkeypatch.setattr(PatchState, "save", boom)
    log = TaskLog(log_path)
    with pytest.raises(RuntimeError, match="save fail"):
        apply_patch_set(
            path,
            pset,
            workspace_root=tmp_vault.root,
            state_path=state,
            task_id="p-journal",
            log=log,
        )
    assert path.read_text(encoding="utf-8") == "new text here"
    log2 = TaskLog(log_path)
    restored = apply_patch_set(
        path,
        pset,
        workspace_root=tmp_vault.root,
        state_path=state,
        task_id="p-journal",
        log=log2,
    )
    assert restored.get("reconciled") or restored.get("ok")
    assert restored["bytes"] > 0
    assert restored["hunks"] == 1
    loaded = PatchState.load(state)
    assert loaded.cumulative_bytes == restored["cumulative_bytes"]
    assert loaded.cumulative_bytes > 0

    again = apply_patch_set(
        path,
        pset,
        workspace_root=tmp_vault.root,
        state_path=state,
        task_id="p-journal",
        log=log2,
    )
    assert again.get("skipped") or again.get("reconciled")
    assert PatchState.load(state).cumulative_bytes == loaded.cumulative_bytes

    path.write_text("new text here extra", encoding="utf-8")
    h2 = content_hash(path.read_text(encoding="utf-8"))
    policy = PatchPolicy(max_cumulative_bytes=loaded.cumulative_bytes + 1)
    with pytest.raises(StructuralEscalationError, match="cumulative"):
        apply_patch_set(
            path,
            PatchSet(base_report_hash=h2, ops=(PatchOp("extra", "EXTRA"),)),
            workspace_root=tmp_vault.root,
            state_path=state,
            policy=policy,
            task_id="p-next",
            log=log2,
        )
