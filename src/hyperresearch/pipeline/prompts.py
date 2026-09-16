"""Runtime-neutral role payloads. Claude skill files are not executed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

DECOMPOSE_CONTRACT = """You have no tools. The host will write your JSON to prompt-decomposition.json.

Reply with JSON only. Required keys:
- pipeline_tier: "light" or "full" (use the declared tier if provided)
- sub_questions: array of strings
- entities: array of objects with name and type
- required_section_headings: array of H2 strings, NEVER empty
- response_format: short string such as "markdown-report"
- citation_style: "wiki-link"

Do not include a "report" field. Do not write the research report.
"""

HOST_ACTION_CONTRACT = """You have no tools. The host executes actions you propose.

Reply with JSON only:
{"actions": [{"kind": "<kind>", "args": {}, "reason": "<short>"}]}

Allowed kinds:
- search: args.query (string), optional args.limit (int)
- fetch: args.url (https URL)
- evidence_read: args.note_id
- complete: empty args; use when enough evidence has been fetched

Rules:
- Do not write the research report in this step.
- If search errors because the provider cannot search, fetch official https URLs instead.
- Prefer primary/official sources.
- After successful fetches, you may evidence_read those note ids, then complete.
"""

DRAFT_CONTRACT = """You have no tools. Write the final research report as markdown, not JSON.

Requirements:
- Use every required_section_headings value as an H2, in order.
- Cite evidence with wiki links [[note_id]] using only note ids listed under Evidence.
- Do not invent note ids such as src-note.
- If Evidence is empty, write the report without wiki-link citations.
- Do not wrap the report in JSON.
"""

PATCH_CONTRACT = """You have no tools. Propose surgical patches, or apply nothing.

Reply with JSON only:
{"base_report_hash": "<hash given below>", "ops": [{"old_text": "...", "new_text": "...", "reason": "..."}]}

Rules:
- old_text must match the current report uniquely.
- Do not regenerate the whole report.
- If no change is needed, ops must be [].
- Do not include a "report" field.
"""

_INVESTIGATE_ROLES = frozenset({"width", "investigator", "corpus_critic", "gap_fetch"})
_DRAFT_ROLES = frozenset({"draft", "synthesizer"})
_PATCH_ROLES = frozenset({"patcher", "polish", "readability"})


def role_payload(
    role: str,
    query: str,
    *,
    extra: str = "",
    declared_tier: str = "",
) -> str:
    parts = [f"Canonical research query:\n{query.strip()}\n"]
    if declared_tier:
        parts.append(f"Declared run tier: {declared_tier}\n")
    if extra.strip():
        parts.append(extra.rstrip() + "\n")
    if role == "decompose":
        parts.append(DECOMPOSE_CONTRACT)
    elif role in _INVESTIGATE_ROLES:
        parts.append(HOST_ACTION_CONTRACT)
    elif role in _DRAFT_ROLES:
        parts.append(DRAFT_CONTRACT)
    elif role in _PATCH_ROLES:
        parts.append(PATCH_CONTRACT)
    else:
        parts.append("Reply with the artifact text for this step. You have no tools.\n")
    return "\n".join(parts)


def evidence_extra(notes_dir: Path, decomp_path: Path | None = None) -> str:
    lines = ["Evidence notes (cite as [[note_id]]):"]
    found = False
    if notes_dir.is_dir():
        for path in sorted(notes_dir.glob("*.md")):
            if path.name.startswith("final_report_"):
                continue
            found = True
            text = path.read_text(encoding="utf-8-sig")[:1500]
            lines.append(f"### [[{path.stem}]]\n{text}\n")
    if not found:
        lines.append("(none yet)")
    if decomp_path is not None and decomp_path.exists():
        lines.append("Decomposition:\n" + decomp_path.read_text(encoding="utf-8-sig")[:4000])
    return "\n".join(lines)


def report_extra(report_text: str, report_hash: str) -> str:
    return f"base_report_hash: {report_hash}\n\nCurrent report:\n{report_text}\n"


def web_hit(result: Any) -> dict[str, str]:
    url = getattr(result, "url", "") or ""
    title = getattr(result, "title", "") or ""
    content = getattr(result, "content", "") or ""
    if isinstance(result, dict):
        url = str(result.get("url") or url)
        title = str(result.get("title") or title)
        content = str(result.get("content") or result.get("snippet") or content)
    return {"url": url, "title": title, "snippet": content[:500]}
