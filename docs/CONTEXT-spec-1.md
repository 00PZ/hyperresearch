# Glossary — HyperResearch agent-agnostic fork

Single context for Spec 1 (Epic 1). Not GBrain. Not Shoshin wiki. Not the software-factory repo.

## Language

**Pipeline**
Host-owned step graph that runs the upstream 16-step research method. The executable authority for a run.
_Avoid_: skill, Claude command, subagent roster as the orchestrator

**Step**
One numbered phase in that graph. Prompt text may come from templates. Control flow does not.

**Host capability**
Search, fetch, file I/O, citation, and patch-apply executed by the workstation process.

**Host action**
A structured request from the model (search, fetch, evidence-read, complete). The host validates, executes or rejects, and returns provenance. This is the research loop. Not a remote tool call.

**AgentRuntime**
The seam for **model text only**. No tools, no workspace, no Jarvis session.
_Avoid_: Jarvis `/v1/runs`; Claude Skill; omp-as-researcher

**FakeRuntime**
Deterministic AgentRuntime for CI. No network.

**ModelRuntime**
Reference production AgentRuntime. OpenAI-compatible chat/completions (or equivalent provider SDK) with `tools` omitted. Preventive isolation: the client never offers tools.

**Hermes/Jarvis (Spec 1 role)**
Commissions a run, monitors progress, consumes the report. Does not execute research steps and is not the model backend.

**AgentTask**
One model invocation: role, messages/payload, required model id, output schema, host `task_id`.

**AgentResult**
Host-parsed outcome: text, structured object, usage, requested model. `actual_model` stays unknown unless the transport documents a trustworthy execution id. Provider echo of the request is not that.

**Orchestrator**
The CLI process on `ssh dev` that owns manifests, checkpoints, retries, the host-action loop, and concurrency.

**Patch set**
Structured operations applied atomically by the host against a known report hash.

**Canonical query**
The persisted user research question for a run. Later steps read it. They do not rewrite it.

**Ship check**
Upstream general verification: report structure, citation density, quote integrity, retracted citations, required artifacts. Both light and full must pass it.

**completed**
Light-tier terminal state after required light steps **and** ship check. Not a full citation audit.

**verified**
Full-tier terminal state: ship check plus independence plus full cite-check on the **final** report bytes (content hash). Any later mutation clears this until cite-check runs again.

**Independence gate**
Epic 1 is done only when light and full paths complete with Claude Code absent, FakeRuntime conformance green, and one live light plus one live full run through **ModelRuntime**.

**omp**
Coding executor on `ssh dev`. Writes the fork. Not an AgentRuntime.

**codegraph**
Workstation indexer for the feat worktree.

**analysis**
Official `gbrain-base-v2` page type. Spec 2 only.
_Avoid_: research-report (not in Spec 1)
