"""Typed AgentRuntime failures. Orchestrator retries or stops; it does not parse prose."""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base for runtime-seam failures."""


class RuntimeFailure(AgentRuntimeError):  # noqa: N818
    """Provider/runtime failed the call. Safe to retry the same task_id."""


class UncertainSubmission(AgentRuntimeError):  # noqa: N818
    """Request may have been accepted remotely. Do not auto-retry."""


class RuntimeTimeout(UncertainSubmission):
    """Call exceeded the host timeout."""


class MalformedStructuredOutput(AgentRuntimeError):  # noqa: N818
    """output_schema was set and the payload could not be parsed/validated."""


class UnexpectedToolResponse(AgentRuntimeError):  # noqa: N818
    """Response contained tool_calls (or equivalent). Do not execute them."""


class ToolsRequired(AgentRuntimeError):  # noqa: N818
    """Config or advertised capabilities require tools. Refuse to start."""


class IllegalHostAction(AgentRuntimeError):  # noqa: N818
    """Model proposed a host action the task/policy forbids."""


class BrowserUnsupported(AgentRuntimeError):  # noqa: N818
    """Claude-in-Chrome / browser-fetcher is unsupported in Spec 1."""
