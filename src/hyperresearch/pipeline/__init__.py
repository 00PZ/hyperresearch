"""Host-owned research pipeline."""

from hyperresearch.pipeline.host_actions import (
    HostBudget,
    HostExecutor,
    budget_from_profile,
    run_host_action_loop,
)
from hyperresearch.pipeline.orchestrator import execute_run, resume_run
from hyperresearch.pipeline.patch import (
    PatchError,
    PatchOp,
    PatchPolicy,
    PatchSet,
    StructuralEscalationError,
    apply_patch_set,
)

__all__ = [
    "HostBudget",
    "HostExecutor",
    "PatchError",
    "PatchOp",
    "PatchPolicy",
    "PatchSet",
    "StructuralEscalationError",
    "apply_patch_set",
    "budget_from_profile",
    "execute_run",
    "resume_run",
    "run_host_action_loop",
]
