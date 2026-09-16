# SPEC 1 — Agent-agnostic HyperResearch fork

Implementer contract. Do not re-grill O-01–O-06. Do not implement until the operator says `go`.

Tracker: `/opt/data/.scratch/hyperresearch-fork/`
Glossary: `CONTEXT.md`
Card: `issues/01-agent-agnostic-fork.md` (`ready-for-agent`)

**Baseline:** `jordan-gibbs/hyperresearch` commit `75b1ecfb2891184fad2cc1a2ddf9abe476f5b54c` (package metadata still says 0.11.1; `main` has unreleased work including the run-tag path-traversal fix). Pin that commit, not the version string alone.
Fork target: `00PZ/hyperresearch` with `upstream` remote kept.
Coding: omp on `ssh dev`, feat worktree under `/home/vdm/git/`. codegraph on that tree before symbol edits.

**Q7/Q8 supersede (operator review, this revision):** Jarvis `/v1/runs` is not the research backend. Observation-and-stop is not isolation. Reference production runtime is a **direct model/provider adapter**. Live light and live full proofs run through that adapter, not through Jarvis.

---

## Problem Statement

HyperResearch already has a 16-step research method and a reusable Python core (vault, fetch, cite-check, independence, run manifests, ship checks). The **executable** path is still Claude Code: markdown skills, subagents, and `run resume` returning a Skill invocation instead of the next step.

We need that method as ordinary application-owned pipeline code so a workstation CLI can run light and full research with Claude Code absent.

## Solution

Fork at the pinned commit. Keep the Python core and its tests. Move orchestration out of Claude skills into a host step graph.

Responsibilities:

- **HyperResearch CLI (ssh dev)** owns steps, evidence collection, host actions, checkpoints, patch apply, ship checks, cite-check, independence.
- **ModelRuntime** supplies reasoning/generation through a provider or model gateway. No tools on the wire.
- **Hermes/Jarvis** commissions the run, monitors, consumes results. It does not execute steps and is not the model backend.

Ship when FakeRuntime conformance, a no-Claude static gate, and one live light plus one live full ModelRuntime run all pass.

GBrain, company `research/` prefixes, wiki promotion, Kubernetes Jobs, Sandcastle, Grok, Claude adapter, and a restricted Hermes worker/profile are later.

## User Stories

1. As the operator, I want a GitHub fork from commit `75b1ecf`, so that the baseline includes the run-tag path-traversal fix and is not an ambiguous “0.11.1”.
2. As the operator, I want the fork usable as a Python package (`hyperresearch` / `hpr`) on `ssh dev`, so that I am not running research inside Telegram.
3. As an implementer, I want an inventory that maps every Claude skill, command, hook, subagent role, and tool allowlist to a pipeline or host-action responsibility, so that nothing load-bearing stays implicit.
4. As an implementer, I want versioned conformance fixtures for canonical query, step graph, manifests, evidence digest, critic outputs, ship check, citation verification, and final report, so that later refactors have a contract.
5. As a CLI user, I want `hpr run "<query>"` to start a pipeline the host owns, so that a missing Claude Code binary does not block a run.
6. As a CLI user, I want `hpr run resume <run-id>` to continue from the last checkpoint without duplicating in-flight model calls, so that a CLI crash is not a double bill.
7. As a CLI user, I want light queries to take the upstream light path (decompose → width → draft → polish → readability), pass **ship check**, and finish as `completed`, so that bounded questions stay cheap and are not labeled `verified`.
8. As a CLI user, I want full queries to take the upstream full path, pass **ship check plus** independence and full cite-check, and finish as `verified` only on the final report hash.
9. As the pipeline, I want a frozen `ResearchContext` per run (run id, canonical query, tier, profile, runtime name, workspace root), so that steps do not infer scope from random paths.
10. As the pipeline, I want the canonical query persisted once and re-read by later steps, so that polish cannot silently change the question.
11. As the pipeline, I want retries, concurrency limits, checkpoints, and manifests in host code, so that those rules do not depend on a harness.
12. As the pipeline, I want every model invocation to go through `AgentRuntime.run` / `run_many`, so that FakeRuntime and ModelRuntime are interchangeable.
13. As CI, I want a FakeRuntime that returns fixture payloads, so that the step graph can be traversed without models.
14. As CI, I want FakeRuntime to cover one task, parallel tasks, structured output, runtime failure, timeout, malformed structured output, missing usage metadata, and illegal host-action proposals, so that adapters cannot drift.
15. As CI, I want ModelRuntime to pass the same runtime contract tests as FakeRuntime (with HTTP mocked), so that the adapter is not a special case.
16. As a runtime, I want role-to-model mapping from profile config sent as provider request fields, so that selection is not prose. `actual_model` stays unknown unless the transport documents trustworthy execution metadata (a status echo of the request is not that).
17. As ModelRuntime, I want OpenAI-compatible `chat/completions` (or the provider’s equivalent) with **no `tools` parameter**, so that isolation is preventive.
18. As ModelRuntime, I want base URL, model id, and key from config/env, never committed, so that the fork stays publishable.
19. As the operator, I want one live **light** run through ModelRuntime, so that the adapter is proven on a real provider.
20. As the operator, I want one live **full** run through ModelRuntime, so that Epic 1 matches the locked proof bar.
21. As CI, I want a static check that core pipeline modules do not import, subprocess, or read Claude Code paths/config, so that independence is mechanical.
22. As CI, I want the test suite and at least one FakeRuntime end-to-end fixture to pass in an image/environment with no Claude Code installed, so that “works on my Claude” cannot sneak back.
23. As the patch stage, I want models to propose a patch set, not rewrite files, so that “patch, never regenerate” is host-enforced.
24. As host patch apply, I want rejection of wrong paths, non-unique old_text, oversized hunks, too many operations, canonical-query edits, and evidence-file edits during polish, so that the model cannot bypass safety.
25. As host patch apply, I want a structural-escalation / blocked state when a critic asks for more than a surgical patch, so that the report is not regenerated to satisfy the critic.
26. As cite-check, I want `verified` to mean full cite-check passed on the exact final report hash, so that an earlier draft cannot stamp the shipped file.
27. As independence audit, I want derivative-source clustering on the full path only, so that reprints do not count as consensus and light runs are not blocked on it.
28. As a vault user, I want existing markdown+SQLite vault behaviour preserved for operational runs on the workstation, so that source reuse still works without GBrain.
29. As an upstream-sync reviewer, I want GBrain-free core and runtime adapters isolated from vault/fetch/cite-check, so that future upstream pulls stay possible.
30. As an implementer, I want upstream unit/integration tests that still apply to unchanged core to stay green, so that the fork does not silently break vault/fetch/OA recovery.
31. As an implementer, I want Claude skill markdown treated as prompt/template source, not as the executor, so that method text is not thrown away and not executed by Claude Code.
32. As a CLI user, I want `hyperresearch install` Claude-session behaviour either removed from the default path or isolated behind an extra that core does not import, so that install does not require Claude Code for Spec 1.
33. As omp, I want a feat worktree (not `master`) and a prompt that names this spec, so that Telegram bun/git does not become the coding path.
34. As omp, I want `codegraph init` on that worktree and `codegraph` query/callees before editing existing symbols, so that edits land on the real call graph.
35. As the operator, I want run workspaces on the workstation, so that Jarvis PVC `/opt/data/hyperresearch` is not the Spec 1 store.
36. As the operator, I want dissertation tier left untouched if it would expand Spec 1, so that Epic 1 stays light+full.
37. As a future Spec 2 author, I want this fork to have no GBrain client in core, so that independence is not faked by company adapters.
38. As a future Spec 2 author, I want no wiki-write function anywhere in this fork’s research runtime, so that promotion stays Librarian/Reviewer later.
39. As CI, I want malformed AgentRuntime results to fail the step with a typed error, so that the orchestrator can retry or stop instead of parsing prose.
40. As the operator, I want usage metadata captured when present and tolerated when absent, so that billing debug is possible without failing a good run.
41. As the host, I want search/fetch/file/citation/patch-apply to run only in the workstation process, so that no organizational agent’s tools are on the research path.
42. As an investigator role, I want to propose structured host actions (search, fetch, evidence-read, complete) inside iteration/time/cost limits, so that a single inline excerpt is not the whole investigation.
43. As the host, I want to validate those actions against ToolPolicy and workspace rules, execute permitted ones with provenance, and return results to the model, so that remote tools stay unused.
44. As the orchestrator, I want a write-ahead `task_id` for each model call and each host action, so that resume does not replay completed work.
45. As `run_many`, I want a failed member retried by its `task_id` only, so that successful siblings are not launched again.
46. As full-tier, I want polish/readability after cite-check to re-run cite-check if the report hash changed, so that verification always names the bytes we keep.
47. As host patch apply, I want a base report hash, unique match or `occurrence`, atomic set apply, and cumulative change caps across the whole late-stage cycle, so that many small patches cannot regenerate the report.
48. As a CLI user, I want upstream profile budgets and research levers preserved, so that scale is not reduced to model-name maps.
49. As a CLI user, I want source provenance and untrusted-source handling preserved, so that the vault does not treat every fetch as trusted.
50. As a CLI user, I want scholarly discovery and open-access recovery preserved, so that paywalled abstracts are not cited as if read.
51. As a CLI user, I want Claude-in-Chrome browser escalation marked **unsupported** in Spec 1 (or replaced by a host browser capability if already present without Claude), so that a missing Chrome skill cannot silently skip a step.
52. As a CLI user, I want upstream blocked-run behaviour preserved when ship check or cite-check fails, so that a failed gate is not a `completed` run.
53. As Hermes/Jarvis, I want to start, watch, and read a research run without being the model endpoint, so that this Telegram STS is not a tool-enabled research worker.
54. As the orchestrator, I want a host action that changed the vault or report to be idempotent or reconcilable if the success checkpoint never landed, so that resume does not double-fetch or double-patch.
55. As ModelRuntime, I want a documented model-only endpoint contract plus rejection of unexpected tool-call responses, so that “no tools in our request” is not treated as proof of the gateway’s internals.
56. As a CLI user, I want terminal success to mean: light runs pass ship-check on the **final** report; full runs additionally pass independence and cite-check on the **final** applicable artifacts, so that polish cannot leave a stale gate on an older snapshot.

## Implementation Decisions

- Freeze upstream at `75b1ecfb2891184fad2cc1a2ddf9abe476f5b54c`. Record that SHA in the fork README. Keep an `upstream` remote. Do not pin “0.11.1” alone.
- Treat existing Python core (vault, fetch, OA recovery, cite-check, independence, profiles, levers, run manifests, ship/verification, untrusted sources) as the reusable layer. Extend it; do not rewrite it for Spec 1.
- Replace Claude skill markdown as the **executor** with an application-owned step graph. Keep skill text as prompt/templates, with golden tests where upstream already has them. `hpr run resume` must execute the next host step, not return a Claude Skill invocation.
- **Runtimes**
  - FakeRuntime: CI reference.
  - ModelRuntime: production reference. Provider HTTP (OpenAI-compatible chat/completions or documented SDK equivalent). Request contains messages + model id. **No tools array, no tool-choice, no parallel_tool_calls.**
  - **Tool-free qualification:** a supported endpoint must have documented model-only execution semantics, and that mode must be selected in adapter config. Validate advertised capabilities where the API exposes them. Capture tests prove HyperResearch **offers** no tools; they do not prove a gateway’s internal behaviour. If a response contains tool calls, `tool_calls`, or equivalent, fail `unexpected_tool_response` and do not execute them. If config or capability data shows tools required or enabled, refuse to start.
  - Jarvis `/v1/runs` and `/v1/chat/completions` (tool-enabled `AIAgent`) are **not** AgentRuntime backends in Spec 1.
  - A restricted Hermes worker (`skip_memory`, `skip_context_files`, `skip_background_review`, empty toolsets, constructor-level isolation, conformance tests) is **out of Spec 1**. It would be a later adapter, not “watch Jarvis SSE and cancel”.
- Runtime seam:

```python
class HostAction:
    kind: Literal["search", "fetch", "evidence_read", "complete"]
    args: dict[str, Any]
    reason: str

class AgentTask:
    task_id: str
    role: str
    payload: str                 # inline; may include prior host-action results
    model: str
    output_schema: type | None
    allowed_actions: tuple[str, ...]   # empty means complete-only (critics, polish)

class AgentResult:
    text: str
    structured: Any | None       # may contain HostAction proposals or a completion
    usage: dict[str, Any]
    requested_model: str
    reported_model: str | None   # provider field if present; not proof of execution
    actual_model: None           # Spec 1: leave unknown
    runtime_metadata: dict[str, Any]

class AgentRuntime(Protocol):
    name: str
    async def run(self, task: AgentTask, context: ResearchContext) -> AgentResult: ...
    async def run_many(
        self, tasks: list[AgentTask], context: ResearchContext, concurrency: int
    ) -> list[AgentResult]: ...
```

- **Host-action loop** (investigator/fetcher-style roles): repeat until `complete` or budget exhausted: model returns structured `HostAction`s → host validates (policy, workspace, caps) → host executes search/fetch/evidence_read using core → host appends provenance-bearing results to the next payload → next `AgentTask` with same role and new `task_id`. Critics/patch/polish roles have `allowed_actions=()`. Iteration, time, and cost limits come from the active profile/levers that **mean** those quantities (spend ceiling, investigator caps, explicit time budget). Do not map `vault_check_interval_s` (or any unrelated interval) to wall-clock.
- **Isolation is preventive:** ModelRuntime never offers tools. Host actions are the only way to touch the network or disk. Observation-and-stop of a tool-enabled agent is not isolation.
- omp is not an AgentRuntime. Peer-gateway and Paperclip are not research backends.
- Model id and provider from profile config, sent as request fields. Do not treat `status.model` / echoed request fields as `actual_model`. Upstream profile values `haiku`, `sonnet`, `opus`, and `default` are Claude Code role aliases, not provider ids. ModelRuntime maps those aliases to `default_model` (`HYPERRESEARCH_MODEL` / `OPENAI_MODEL`) before the HTTP request. An explicit provider model id on the task is sent unchanged.
- **Resume:** write-ahead `task_id` for model calls and host actions. Resume skips records in a terminal success state. In-flight model HTTP: if no durable provider idempotency is documented for that adapter, do not automatically retry an uncertain submit (`uncertain_remote`). Provider-specific idempotency, if used, must be verified against that provider’s docs, not copied from Hermes.
- **Host-action crash window:** a fetch/patch/vault write may succeed on disk before the success checkpoint is written. Each host action must be **idempotent** (same `task_id` + args yields the same persisted effect) **or** resume must **reconcile** observed state (content hash, vault note id, evidence snapshot) before replay. Cumulative patch-byte/hunk counters are part of the checkpoint and must survive resume. Do not replay a patch set whose `base_report_hash` no longer matches.
- `run_many`: retry only failed members.
- **Light vs full (final artifacts)**
  - Any mutation of the report or of the evidence snapshot **invalidates** the corresponding gate. Invalidation is a **persisted** manifest/checkpoint write, or the gate is re-run on the files that will be kept. Mutating a loaded dict in memory is not invalidation.
  - Empty or whitespace-only model output for a draft/synthesis step is a **blocked-run**. The host does not invent a Findings body, citations, or `[[src-note]]` stand-in so ship-check can pass.
  - Light: upstream light steps, then **ship check on the final report**. State `completed`. Not `verified`.
  - Full: upstream full graph, then **ship check on the final report**, **independence on the final evidence snapshot**, **cite-check on `(final report hash, final evidence snapshot)`**. State `verified` only when all three pass on those finals. Presence of `cite-check-findings.json` is not that gate. Independence fail-closed: `compute_independence` must run at ship time on the current evidence snapshot and persist `{evidence_hash, scored, clusters}` (or equivalent). Missing artifact, stale `evidence_hash`, or exception → blocked. Cluster membership is not itself a fail unless upstream already treats a given summary as failure.
  - Order: synthesis → critics → (optional gap-fetch) → patch → cite-check → polish → readability. If polish or readability changes the report hash, re-run ship-check and cite-check on the new hash (and independence if the evidence snapshot also changed). Failed ship check, independence, or cite-check → upstream **blocked-run** behaviour, not `completed`/`verified`.
- **Patch apply:** `base_report_hash`; unique `old_text` or `occurrence`; atomic set; per-hunk, per-set, and **cumulative** caps across patch + cite-check second pass + polish + readability. Exceeding cumulative cap → `blocked` / structural escalation. Canonical query and evidence files immutable in late stage.
- Workspace on the workstation, from config, never from model output. Path traversal rejected.
- Preserve: profile budgets, levers, provenance, untrusted-source handling, scholar/OA recovery. Claude-in-Chrome browser escalation: **unsupported** in Spec 1 unless a host-side replacement already exists without Claude; do not no-op the step silently.
- Company / GBrain / `publish-gbrain` are not Spec 1 CLI surface.
- Runtime adapters in a dedicated package.
- Live ModelRuntime light and full runs are operator-gated proofs, not default pytest. Mocked HTTP stays in pytest.

## Testing Decisions

Good tests assert external behaviour: step transitions, manifest fields, `completed` vs `verified`, ship check on both tiers, citation gate on a named hash, patch apply/reject, host-action allow/deny, runtime contract (no tools on the wire), resume without duplicate work, and “Claude Code absent”.

**Seams (highest first):**

1. `AgentRuntime` — model text only. FakeRuntime + mocked ModelRuntime.
2. Host-action executor — search/fetch/evidence_read/complete.
3. Host patch apply.
4. Orchestrator + run manifest (including `task_id` write-ahead).
5. Ship check / cite-check / independence — existing core tests.
6. Static no-Claude gate.

**Prior art:** `tests/test_core/` at the pinned commit. Keep green where behaviour is unchanged. Include the run-tag path-traversal coverage from that commit.

**Required new suites:**

- Runtime contract: no `tools` in captured ModelRuntime requests; refuse to start if config/capabilities require tools; fail `unexpected_tool_response` if the stub returns tool calls (do not execute them).
- Host-action loop: propose → validate → execute → provenance round-trip; illegal action rejected; budget stop.
- Crash after persistent effect, before checkpoint: replay is a no-op or reconciles; cumulative patch counters unchanged except for the reconciled action; `base_report_hash` mismatch does not apply a second patch.
- Patch safety: allow; reject path/size/full replace/missing old_text/non-unique old_text; stale hash; cumulative-cap bypass; atomic rollback.
- Light fixture: ship check on **final** report pass → `completed`, never `verified`. Light fixture: ship check fail → blocked. Light fixture: polish after a passing ship-check that changes the report → ship-check runs again. Light fixture: empty draft → blocked, and the report file is not a host-authored cited body.
- Full fixture: ship + independence + cite-check on **final** report and **final** evidence snapshot; polish that changes hash → ship-check and cite-check again (independence if evidence changed) → `verified`. Existence of `cite-check-findings.json` alone must not yield `verified`.
- Resume: completed `task_id`s not replayed; `uncertain_remote` does not auto-POST; `run_many` retries only the failed member.
- No-Claude static gate.
- `ruff check` and `mypy src/hyperresearch/` (strict, as in upstream CONTRIBUTING) on `runtime/` and `pipeline/`. A sitting that only greened its own pytest files is not mergeable.
- Live ModelRuntime light + full: documented commands, not default pytest. Both required before Spec 1 is done.

## Out of Scope

- Spec 2 / Epic 2: GBrain bridge, `analysis` prefixes, `research/index.md`, `put_raw_data`, company isolation, Librarian promotion, wiki-draft, Reviewer, `wiki-pass-check`.
- New GBrain page type `research-report`.
- Kubernetes Job runner, Sandcastle, OpenSandbox.
- Grok Build / direct xAI **as a named extra adapter** (a generic OpenAI-compatible ModelRuntime may still point at an xAI-compatible base URL via config).
- Claude Code adapter.
- Jarvis `/v1/runs` or `/v1/chat/completions` as AgentRuntime.
- Restricted Hermes worker/profile (constructor skip flags, empty toolsets). Later adapter only, with its own conformance tests and a pinned Hermes commit.
- Dissertation tier as a Spec 1 proof.
- Open Thread auto-scheduling.
- Implementing from this Jarvis Telegram session.
- Paperclip, Shoshin content pipeline, Holmberg packets.
- Writing `knowledge/wiki` or `knowledge/drafts`.
- Mass-migrating `opencode/` or Jarvis sessions into research.

## Further Notes

- Application-owned orchestrator is new: upstream `run resume` still returns a Claude Skill invocation (inspected at `75b1ecf`).
- Official Hermes API constructs `AIAgent` with the `api_server` platform toolset, session DB, and memory providers. Instructions do not strip tools. Stop is cancellation after the fact. That path is rejected as the Spec 1 backend.
- Hermes idempotency (SQLite store, ~24h, in-memory fallback) is **not** the ModelRuntime resume contract. Do not copy it.
- Human spec gate is this document. Operator says `go`, then omp implements. Operator says `merge` later to land GitHub.
- Exact run-workspace TTL is Spec 2.
