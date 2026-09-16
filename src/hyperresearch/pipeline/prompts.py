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
- Prefer specific documentation chapter/section URLs over site homepages.
- After successful fetches, you may evidence_read those note ids, then complete.
"""

DRAFT_CONTRACT = """You have no tools. Write the research report as markdown, not JSON.

Requirements:
- Use every required_section_headings value as an H2, in order.
- Cite evidence with wiki links [[note_id]] using only note ids listed under Evidence.
- Do not invent note ids such as src-note.
- If Evidence is empty, write the report without wiki-link citations.
- Do not wrap the report in JSON.
"""

SYNTH_CONTRACT = """You have no tools. Synthesize the provided drafts into ONE final markdown report.

Requirements:
- Use every required_section_headings value as an H2, in order.
- Cite evidence with wiki links [[note_id]] using only note ids listed under Evidence.
- Do not invent note ids such as src-note.
- Do not paste drafts unchanged — integrate them.
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

CITE_CONTRACT = """You have no tools. Judge each sampled citation-sentence pair against the source excerpts.

Reply with JSON only:
{"findings": [{"verdict": "unsupported|partially-supported|wrong-source", "severity": "critical|major", "sentence": "<verbatim>", "cited_note_id": "<id>", "evidence": "<what the source says or lacks>", "suggested_fix": "<swap|soften|delete>"}]}

Write {"findings": []} only when every sampled pair is supported.
Do not edit the report. Do not wrap the JSON in prose.
"""

CRITIC_CONTRACT = """You have no tools. Critique the report from your assigned angle.

Reply with JSON only:
{"findings": [{"severity": "critical|major|minor", "issue": "...", "suggestion": "..."}]}

Never edit the report. Empty findings means no issues from this angle.
"""

_INVESTIGATE_ROLES = frozenset({"width", "investigator", "corpus_critic", "gap_fetch"})
_DRAFT_ROLES = frozenset({"draft"})
_PATCH_ROLES = frozenset({"patcher", "polish", "readability"})
_CRITIC_ROLES = {
    "critic_dialectic": "Angle: dialectic. Counter-evidence the draft missed or straw-manned.",
    "critic_depth": "Angle: depth. Shallow spots where evidence could fill substance.",
    "critic_width": "Angle: width. Corpus clusters the draft ignores despite evidence.",
    "critic_instruction": "Angle: instruction. Decomposition items the draft missed, under-covered, reordered, or reformatted.",
}

_ROLE_HINTS = {
    "contradiction": "Build an explicit graph of opposing claims across the evidence. Rank contested fights. Identify consensus.",
    "loci": "From the contradiction graph, name the load-bearing loci (forks in the evidence) as JSON.",
    "reconcile": "Reconcile cross-locus tensions using the loci and evidence. Write comparisons.",
    "tensions": "Extract expert disagreements as JSON from comparisons and evidence.",
    "digest": "Write an evidence digest: load-bearing claims with verbatim quotes and source note ids.",
}


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
    hint = _ROLE_HINTS.get(role)
    if hint:
        parts.append(f"Role: {role}. {hint} You have no tools.\n")
    critic = _CRITIC_ROLES.get(role)
    if critic:
        parts.append(f"Role: {role}. {critic}\n")
        parts.append(CRITIC_CONTRACT)
    elif role == "decompose":
        parts.append(DECOMPOSE_CONTRACT)
    elif role in _INVESTIGATE_ROLES:
        parts.append(HOST_ACTION_CONTRACT)
    elif role in _DRAFT_ROLES:
        parts.append(DRAFT_CONTRACT)
    elif role == "synthesizer":
        parts.append(SYNTH_CONTRACT)
    elif role in _PATCH_ROLES:
        parts.append(PATCH_CONTRACT)
    elif role == "cite_checker":
        parts.append(CITE_CONTRACT)
    else:
        parts.append("Reply with the artifact text for this step. You have no tools.\n")
    return "\n".join(parts)


def evidence_extra(
    notes_dir: Path,
    decomp_path: Path | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> str:
    lines = ["Evidence notes (cite as [[note_id]]):"]
    found = False
    if sources is not None:
        for src in sources:
            note_id = str(src.get("note_id") or "")
            if not note_id:
                continue
            path = notes_dir / f"{note_id}.md"
            if not path.exists():
                continue
            found = True
            text = path.read_text(encoding="utf-8-sig")[:1500]
            lines.append(f"### [[{note_id}]]\n{text}\n")
    if not found:
        lines.append("(none yet)")
    if decomp_path is not None and decomp_path.exists():
        lines.append("Decomposition:\n" + decomp_path.read_text(encoding="utf-8-sig")[:4000])
    return "\n".join(lines)


def inline_artifacts(items: list[tuple[str, Path]], *, limit: int = 8000) -> str:
    chunks: list[str] = []
    for label, path in items:
        if path.exists():
            chunks.append(f"## {label}\n{path.read_text(encoding='utf-8-sig')[:limit]}\n")
        else:
            chunks.append(f"## {label}\n(missing)\n")
    return "\n".join(chunks)


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
