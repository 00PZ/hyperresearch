"""Parse structured model output and HostAction proposals."""

from __future__ import annotations

import json
from dataclasses import is_dataclass
from typing import Any

from hyperresearch.runtime.errors import IllegalHostAction, MalformedStructuredOutput
from hyperresearch.runtime.types import HostAction, HostActionKind

_KINDS: frozenset[str] = frozenset({"search", "fetch", "evidence_read", "complete"})


def parse_structured(text: str, structured: Any, output_schema: type | None) -> Any:
    if output_schema is None:
        return structured
    raw = structured
    if raw is None:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise MalformedStructuredOutput(str(e)) from e
    return coerce_schema(raw, output_schema)


def coerce_schema(raw: Any, schema: type) -> Any:
    if schema is dict:
        if not isinstance(raw, dict):
            raise MalformedStructuredOutput(f"expected dict, got {type(raw).__name__}")
        return raw
    if schema is list:
        if not isinstance(raw, list):
            raise MalformedStructuredOutput(f"expected list, got {type(raw).__name__}")
        return raw
    try:
        from pydantic import BaseModel

        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_validate(raw)
    except MalformedStructuredOutput:
        raise
    except Exception as e:
        raise MalformedStructuredOutput(str(e)) from e
    if is_dataclass(schema) and isinstance(schema, type):
        if isinstance(raw, schema):
            return raw
        if not isinstance(raw, dict):
            raise MalformedStructuredOutput(f"expected object for {schema.__name__}")
        try:
            return schema(**raw)
        except TypeError as e:
            raise MalformedStructuredOutput(str(e)) from e
    if isinstance(raw, schema):
        return raw
    raise MalformedStructuredOutput(f"cannot coerce {type(raw).__name__} to {schema}")


def host_action_from_mapping(data: dict[str, Any]) -> HostAction:
    kind = data.get("kind")
    if kind not in _KINDS:
        raise IllegalHostAction(f"unknown host-action kind {kind!r}")
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise IllegalHostAction("host-action args must be an object")
    return HostAction(kind=kind, args=dict(args), reason=str(data.get("reason") or ""))


def iter_host_actions(structured: Any) -> list[HostAction]:
    """Pull HostAction proposals out of a structured result. Empty if none."""
    if structured is None:
        return []
    if isinstance(structured, HostAction):
        return [structured]
    if isinstance(structured, list):
        out: list[HostAction] = []
        for item in structured:
            out.extend(iter_host_actions(item))
        return out
    if isinstance(structured, dict):
        if "kind" in structured:
            return [host_action_from_mapping(structured)]
        if "actions" in structured:
            return iter_host_actions(structured["actions"])
        if "action" in structured:
            return iter_host_actions(structured["action"])
    return []


def allowed_kinds(allowed_actions: tuple[str, ...]) -> frozenset[str]:
    """Empty allowlist → complete-only."""
    if not allowed_actions:
        return frozenset({"complete"})
    return frozenset(allowed_actions)


def assert_action_allowed(action: HostAction, allowed_actions: tuple[str, ...]) -> None:
    kinds = allowed_kinds(allowed_actions)
    if action.kind not in kinds:
        raise IllegalHostAction(
            f"host-action {action.kind!r} not in allowed {sorted(kinds)}"
        )


# Re-export for callers that type against the alias.
HostActionKind = HostActionKind
