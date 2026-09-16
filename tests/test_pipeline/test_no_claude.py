"""Core pipeline / runtime / default CLI must not talk to Claude Code."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "hyperresearch"

SCAN = [
    ROOT / "pipeline",
    ROOT / "runtime",
    ROOT / "cli" / "run_cmd.py",
]

FORBIDDEN_MODS = {"anthropic", "claude_code", "claude"}
FORBIDDEN_SNIPPETS = (
    ".claude/",
    "Skill(skill",
    "claude code",
    "anthropic.com",
)


def _iter_py(path: Path):
    if path.is_file():
        yield path
        return
    yield from path.rglob("*.py")


def test_no_claude_imports_or_paths():
    hits = []
    for root in SCAN:
        for file in _iter_py(root):
            text = file.read_text(encoding="utf-8")
            rel = file.relative_to(ROOT)
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                hits.append(f"{rel}: parse error {e}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0].lower()
                        if top in FORBIDDEN_MODS:
                            hits.append(f"{rel}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0].lower()
                    if top in FORBIDDEN_MODS:
                        hits.append(f"{rel}:{node.lineno} from {node.module}")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    low = node.value.lower()
                    if any(s in low for s in (".claude/", "skill(skill")):
                        hits.append(f"{rel}:{node.lineno} string {node.value!r}")
            low = text.lower()
            if "subprocess" in low and "claude" in low:
                hits.append(f"{rel}: subprocess+claude")
    assert hits == []


def test_run_help_does_not_import_hooks():
    import hyperresearch.cli.run_cmd as run_cmd

    src = Path(run_cmd.__file__).read_text(encoding="utf-8")
    assert "hyperresearch.core.hooks" not in src
    assert "Skill(skill" not in src
