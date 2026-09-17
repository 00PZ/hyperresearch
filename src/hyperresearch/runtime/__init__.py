"""Runtime adapters: model text only."""

from hyperresearch.runtime.errors import (
    AgentRuntimeError,
    BrowserUnsupported,
    IllegalHostAction,
    MalformedStructuredOutput,
    RuntimeFailure,
    RuntimeTimeout,
    ToolsRequired,
    UncertainSubmission,
    UnexpectedToolResponse,
)
from hyperresearch.runtime.fake import FakeRuntime
from hyperresearch.runtime.model import ModelRuntime, capabilities_require_tools
from hyperresearch.runtime.types import (
    AgentResult,
    AgentRuntime,
    AgentTask,
    HostAction,
    ResearchContext,
)

__all__ = [
    "AgentResult",
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentTask",
    "BrowserUnsupported",
    "FakeRuntime",
    "HostAction",
    "IllegalHostAction",
    "MalformedStructuredOutput",
    "ModelRuntime",
    "ResearchContext",
    "RuntimeFailure",
    "RuntimeTimeout",
    "ToolsRequired",
    "UncertainSubmission",
    "UnexpectedToolResponse",
    "capabilities_require_tools",
]
