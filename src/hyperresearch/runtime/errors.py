"""Typed AgentRuntime failures. Orchestrator retries or stops; it does not parse prose."""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base for runtime-seam failures."""


class RuntimeFailure(AgentRuntimeError):
    """Provider/runtime failed the call."""


class RuntimeTimeout(AgentRuntimeError):
    """Call exceeded the host timeout."""


class MalformedStructuredOutput(AgentRuntimeError):
    """output_schema was set and the payload could not be parsed/validated."""


class UnexpectedToolResponse(AgentRuntimeError):
    """Response contained tool_calls (or equivalent). Do not execute them."""


class ToolsRequired(AgentRuntimeError):
    """Config or advertised capabilities require tools. Refuse to start."""


class IllegalHostAction(AgentRuntimeError):
    """Model proposed a host action the task/policy forbids."""


class BrowserUnsupported(AgentRuntimeError):
    """Claude-in-Chrome / browser-fetcher is unsupported in Spec 1."""
