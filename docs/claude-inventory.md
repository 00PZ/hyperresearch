# Claude inventory → pipeline / host-action

Maps every Claude Code skill, slash command, hook, subagent, and tool
allowlist at baseline `75b1ecfb2891184fad2cc1a2ddf9abe476f5b54c` onto Spec 1
responsibilities. Skill markdown stays as prompt/template source. It is not
the executor.

Default CLI path (`hpr run`) must not invoke any row whose Spec 1 owner is
"Claude session extra".

## Slash commands

| Claude surface | Location | Spec 1 owner |
|---|---|---|
| `/hyperresearch` | skill `hyperresearch` frontmatter (`name: hyperresearch`) | **Claude session extra.** Host equivalent: `hpr run "<query>"`. |
| `/research`, `/research-ensemble`, `/research-layercake` | retired skill dirs pruned on install | Dropped. Do not restore. |

No files under `.claude/commands/`. Slash triggers are skill frontmatter.

## Entry + step skills

Prompt text: `src/hyperresearch/skills/*.md`. Installed to
`.claude/skills/<slug>/SKILL.md` by `hyperresearch install`.

| Skill | Step | Spec 1 owner |
|---|---|---|
| `hyperresearch` | router | Orchestrator. Pins canonical query, chooses light/full, walks the step graph. |
| `hyperresearch-1-decompose` | 1 | Pipeline step. Persist `query.md` + `prompt-decomposition.json` (tier, levers, headings). |
| `hyperresearch-1-5-chapter-partition` | 1.5 | Out of Spec 1 (dissertation). Leave skill text; do not execute. |
| `hyperresearch-2-width-sweep` | 2 | Pipeline step. Host actions: `search`, `fetch`. Scholar sweep via core, not WebSearch. |
| `hyperresearch-3-contradiction-graph` | 3 | Pipeline step (full). Model text only; vault already on disk. |
| `hyperresearch-4-loci-analysis` | 4 | Pipeline step (full). `run_many` for parallel loci-analysts. |
| `hyperresearch-5-depth-investigation` | 5 | Pipeline step (full). Host-action loop (`search`/`fetch`/`evidence_read`/`complete`). |
| `hyperresearch-6-cross-locus-reconcile` | 6 | Pipeline step (full). Model text; writes `comparisons.md`. |
| `hyperresearch-7-source-tensions` | 7 | Pipeline step (full). Model text; writes `source-tensions.json`. |
| `hyperresearch-8-corpus-critic` | 8 | Pipeline step (full). May propose host `fetch` for named gaps. |
| `hyperresearch-9-evidence-digest` | 9 | Pipeline step (full). Model text; writes `evidence-digest.md`. |
| `hyperresearch-10-triple-draft` | 10 | Pipeline step. Light: one draft. Full: `run_many` draft-orchestrators. `allowed_actions=()`. |
| `hyperresearch-11-synthesize` | 11 | Pipeline step (full). Writes `final_report_<tag>.md`. `allowed_actions=()`. |
| `hyperresearch-12-critics` | 12 | Pipeline step (full). Four critics via `run_many`. `allowed_actions=()`. |
| `hyperresearch-13-gap-fetch` | 13 | Pipeline step (full). Host `fetch` only. |
| `hyperresearch-14-patcher` | 14 | **Host patch apply.** Model proposes patch set; host applies. `allowed_actions=()`. |
| `hyperresearch-14-5-cite-check` | 14.5 | Core `citecheck` + optional model spot-check. Gate on **final** report hash. |
| `hyperresearch-15-polish` | 15 | Host patch apply (hygiene). Mutation invalidates ship-check. |
| `hyperresearch-16-readability-audit` | 16 | Model recommends; host applies selected patches. Mutation invalidates gates. |

`hpr run resume` executes the next **host** step. It must not return
`Skill(skill: "hyperresearch-N-…")`.

## Hooks

| Hook | Location | Spec 1 owner |
|---|---|---|
| PreToolUse `WebSearch\|WebFetch` | `.claude/settings.json` + `.hyperresearch/hook.js` | **Claude session extra.** Host `search`/`fetch` replace WebSearch/WebFetch. Default CLI does not install or fire this hook. |

No Stop, PostToolUse, SessionStart, or Notification hooks in this package.

## Subagents (Claude Code Task tool)

Installed to `.claude/agents/`. Prompt bodies live in `core/hooks.py`.
`tools:` is the Claude allowlist — Spec 1 drops it; the host loop is the
only way to touch network or disk.

| Agent | Claude tools | Spec 1 owner |
|---|---|---|
| `hyperresearch-fetcher` | Bash, Read, Write, WebSearch | Host `fetch` (+ optional `search`). Role still exists as a model call that **proposes** URLs, never fetches. |
| `hyperresearch-source-analyst` | Bash, Read, Write | Model text on one note via `evidence_read`. Leaf. `allowed_actions=()`. |
| `hyperresearch-loci-analyst` | Bash, Read, Write | Model text. `run_many`. `allowed_actions=()`. |
| `hyperresearch-depth-investigator` | Bash, Read, Write, Task | Host-action loop. No nested Task/runtime. |
| `hyperresearch-corpus-critic` | Bash, Read, Write | Model text; may propose host `fetch`. |
| `hyperresearch-draft-orchestrator` | Bash, Read, Write | Model text. `allowed_actions=()`. |
| `hyperresearch-synthesizer` | Read, Write | Model text. Host writes the report file. |
| `hyperresearch-dialectic-critic` | Bash, Read, Write | Model text. `allowed_actions=()`. |
| `hyperresearch-depth-critic` | Bash, Read, Write | Model text. `allowed_actions=()`. |
| `hyperresearch-width-critic` | Bash, Read, Write | Model text. `allowed_actions=()`. |
| `hyperresearch-instruction-critic` | Bash, Read, Write | Model text. `allowed_actions=()`. |
| `hyperresearch-patcher` | **Read, Edit** | Host patch apply. Model cannot Edit. |
| `hyperresearch-cite-checker` | Bash, Read, Write | Core citecheck + model sample. Host writes findings. |
| `hyperresearch-polish-auditor` | **Read, Edit** | Host patch apply. |
| `hyperresearch-readability-recommender` | Read, Write | Model JSON suggestions; host patch apply. |
| `hyperresearch-browser-fetcher` | Bash, Read, Write, ToolSearch | **Unsupported in Spec 1.** Claude-in-Chrome. Do not silent-skip: mark the step/escalation `unsupported`. |

Retired (pruned on install, not ported): `hyperresearch-analyst`,
`hyperresearch-auditor`, `hyperresearch-rewriter`, `hyperresearch-subrun`,
`hyperresearch-merger`, `hyperresearch-readability-reformatter`.

## Tool allowlists vs host capabilities

| Claude tool | Used by | Spec 1 replacement |
|---|---|---|
| WebSearch | fetcher, PreToolUse hook | Host action `search` (vault FTS + configured web provider). |
| WebFetch | PreToolUse matcher only | Host action `fetch` → `core.fetcher.fetch_and_save`. |
| Bash | most agents (`hpr fetch`, `hpr search`, `hpr note`) | Host process calls core APIs directly. No shell-out to `hpr` from the model. |
| Read | all | Host `evidence_read` (vault notes, run artifacts). Path-traversal rejected. Workspace from config, never from model output. |
| Write | drafts, synthesizer, critics, logs | Host writes artifacts. Models return text/structured payloads. |
| Edit | patcher, polish-auditor | Host patch apply (`base_report_hash`, unique `old_text` or `occurrence`, atomic set, cumulative caps). |
| Task | depth-investigator, orchestrator skill | `AgentRuntime.run` / `run_many`. No nested agent tools. |
| ToolSearch | browser-fetcher | Unused. Browser lane unsupported. |

ModelRuntime never sends `tools`, `tool_choice`, or `parallel_tool_calls`.
Unexpected `tool_calls` in a response → `unexpected_tool_response`, not
execution.

## Install / docs (Claude session extra)

| Surface | Spec 1 owner |
|---|---|
| `hyperresearch install` (default) | Vault init + crawl4ai detect stay. Claude hooks/skills/agents and `CLAUDE.md` injection move behind the Claude extra / `--claude` path. Core and default CLI must not import `core.hooks`. |
| `hyperresearch install --global` | Claude-only. `~/.claude/` entry skill + agents. Not on default path. |
| `hyperresearch install --steps-only` | Claude-only lazy step-skill bootstrap. |
| `inject_agent_docs` → `CLAUDE.md` | Claude session extra. |
| `core/hooks.py` PreToolUse JS | Claude session extra. |
| `hpr run resume` → `skill_to_invoke` | Remove from default resume. Resume runs the next host step. |

## Not load-bearing for Spec 1

- Dissertation step 1.5 and chapter loop.
- Claude-in-Chrome / browser-fetcher (explicit **unsupported**).
- GBrain, wiki, Jarvis `/v1/runs`, Telegram, Kubernetes, Sandcastle.
