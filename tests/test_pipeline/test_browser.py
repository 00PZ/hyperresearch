"""Claude-in-Chrome is skipped; research continues."""

from __future__ import annotations

from hyperresearch.core.escalation import enqueue, queue_stats
from hyperresearch.pipeline.orchestrator import _assert_no_chrome


def test_queued_escalation_is_abandoned_not_crash(tmp_vault):
    enqueue(tmp_vault.db, "https://example.com", "login_wall", vault_tag="run-1")
    _assert_no_chrome(tmp_vault, "run-1")
    stats = queue_stats(tmp_vault.db, vault_tag="run-1")
    assert stats["queued"] == 0
    assert stats["needs_human"] == 0
    assert stats["abandoned"] >= 1
