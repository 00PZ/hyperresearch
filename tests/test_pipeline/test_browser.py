"""Claude-in-Chrome must not silent-skip."""

from __future__ import annotations

import pytest

from hyperresearch.pipeline.orchestrator import _assert_no_chrome
from hyperresearch.runtime.errors import BrowserUnsupported


def test_queued_escalation_is_unsupported(tmp_vault):
    from hyperresearch.core.escalation import enqueue

    enqueue(tmp_vault.db, "https://example.com", "login_wall", vault_tag="run-1")
    with pytest.raises(BrowserUnsupported, match="unsupported"):
        _assert_no_chrome(tmp_vault, "run-1")
