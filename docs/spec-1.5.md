# SPEC 1.5 — SearXNG search adapter (Phase A+B)

Implementer contract. Do not re-grill the JSON/HTML lock, the host-search seam, or the trip-log split. Do not implement until the operator says `go`.

**Revision 1.5.3:** on successful SearXNG JSON (including empty SERP), host `hint` is `None` — do not reuse the crawl4ai “cannot web-search” string. 1.5.2 durable `SearchCallResult` / title-content rule still holds.

Tracker: `/opt/data/.scratch/hyperresearch-fork/`
Glossary: `CONTEXT.md`
Card: `issues/02-searxng-search-adapter.md` (`ready-for-agent`)
Follow-up (not this PR): `issues/03-jarvis-searxng-trip-card.md`

**Baseline:** `00PZ/hyperresearch` `origin/main` after Spec 1 merge (`feat: agent-agnostic host pipeline (Spec 1)` #1). New branch from that tip. Do not reopen Spec 1. Do not mix gost, residential, or a Hermes overlay into this sitting.

Coding: omp on `ssh dev`, feat worktree under `/home/vdm/git/`. codegraph on that tree before symbol edits.

**Seam (operator-confirmed):** host `search` action. SearXNG is the search provider only. Fetch stays crawl4ai. The 16-step graph does not change. Search metadata (`unresponsive_engines`) travels in a per-call `SearchCallResult`, not via `WebProvider.search() -> list[WebResult]` and not via mutable `last_response`.

---

## Problem Statement

Spec 1 made the host own search as a structured action. On `main`, that action still calls the **combined** web provider’s `search()`. Crawl4ai (the production fetcher) raises `NotImplementedError`. Vault FTS still runs. The model is told to invent `https` URLs and `fetch`. That is not production web discovery.

Homelab SearXNG already serves JSON. We need the host to query it as `search_provider`, keep crawl4ai as `fetch_provider`, fail closed when SearXNG is selected and broken, and persist engine CAPTCHA rows so a later Jarvis card can trip — without putting Telegram, gost, or SaaS keys in this PR.

## Solution

Split `[web]` into `search_provider` and `fetch_provider`. Production vaults **explicitly** set `search_provider = "searxng"`. Application default (and pytest) is `none` (vault FTS only) so existing vaults keep starting.

Host search still returns vault hits. Web hits and engine diagnostics come from one `SearchCallResult` per host-action `task_id`. If SearXNG is selected and the URL is missing, HTTP fails, or the body is not JSON, the **run** fails (`blocked_on=search` plus a `blocked_reason`) even when vault FTS returned notes. An empty JSON `results` list is success and the run continues — including when every engine is unresponsive.

CAPTCHA on some engines with remaining hits is success. Every successful JSON search writes a trip-log row (healthy, empty, or mixed). Rows carry a stable `event_id`. HyperResearch does not ping Telegram. Jarvis reads the JSONL later (follow-up card).

No auth. No redirects. No HTML fallback. No Tavily/Exa/Serply keys. No gost.

## User Stories

1. As the operator, I want a new branch from `origin/main` after Spec 1, so that SearXNG work does not reopen the agent-agnostic PR.
2. As the operator, I want production web discovery to use private SearXNG JSON, so that research is not billed per Tavily/Exa query and does not depend on Grok’s web-search tool.
3. As the operator, I want fetch to stay crawl4ai, so that a search backend cannot silently become the page extractor.
4. As an implementer, I want `search_provider` values limited to `none` and `searxng`, so that Tavily/Exa/Serply cannot be selected as the production search slot.
5. As an implementer, I want `fetch_provider` to keep the existing fetch backends, so that crawl4ai/builtin/exa/tavily/parallel/serply fetch behaviour is unchanged.
6. As an implementer, I want `searxng` rejected as `fetch_provider`, so that a JSON search adapter is never asked to download pages.
7. As a CLI user, I want `[web] search_provider`, `fetch_provider`, and `searxng_url` in config, so that one combined `provider` is not doing two jobs.
8. As a CLI user, I want `SEARXNG_URL` to override `searxng_url`, so that a workstation secret/env can point at Tailscale or LAN without committing a host.
9. As a CLI user, I want `web.provider` treated as a deprecated alias of `fetch_provider`, so that existing vaults keep fetching.
10. As an implementer, I want `[search]` (FTS ranking) and `[fetch]` (FetchSettings) left as they are, so that engine config is not stuffed into unrelated sections.
11. As an implementer, I want no `vault_search` rename, so that local FTS stays the vault search it already is.
12. As CI, I want default pytest to set `search_provider=none`, so that unit tests never require a live SearXNG or a URL.
13. As a CLI user, I want a run with `search_provider=searxng` and an empty URL (after env override) to refuse to start the step graph (`blocked_on=search`, `blocked_reason=searxng_unconfigured`), so that a misconfigured vault cannot “succeed” on vault FTS alone.
14. As the host, I want SearXNG search to be `GET {searxng_url}/search?q=...&format=json`, so that the HTML UI is never the API.
15. As the host, I want timeout 30s and **no automatic HTTP retry** on that attempt, so that a hung instance fails the run once. Operator `hpr run resume` is a new attempt, not that retry.
16. As the host, I want no Authorization header and no API key for SearXNG in v1, so that this sitting does not invent auth.
17. As the host, I want `limit` clamped 1–50 (default 10), matching the existing host-action clamp, so that the model cannot request an unbounded SERP.
18. As the host, I want each hit’s snippet capped at 500 characters, preserving existing `web_hit` behaviour, so that search payloads stay bounded.
19. As the host, I want URL dedup first-wins, so that duplicate engines do not pad the list.
20. As the host, I want a JSON object body required: parse failure or a non-object → `searxng_http`, fail the run.
21. As the host, I want HTTP 4xx/5xx/3xx, timeout, and DNS failure → `searxng_http`, fail the run, so that a 403 from disabled JSON is not an empty SERP.
22. As the host, I want HTTP 200 with an HTML/nginx/Anubis/limiter page → `searxng_http`, fail the run, so that `raise_for_status()` cannot hide a 200+HTML wall.
23. As the host, I want no HTML scrape and no UI parse, so that `unresponsive_engines` is not faked from markup.
24. As the host, I want no `SEARXNG_HTML_FALLBACK` behaviour, so that a denied JSON format cannot silently become the browser page.
25. As a researcher, I want HTTP 200 with `results: []` to continue the run, so that a real empty SERP is not a blocked research topic.
26. As a researcher, I want vault FTS hits still returned on host `search`, so that already-fetched notes remain visible beside web hits.
27. As a researcher, I want `searxng_http` to fail the run **even if** vault FTS returned notes, so that a down search instance cannot be papered over by the local vault.
28. As a researcher, I want some engines CAPTCHA’d with remaining hits to count as search success, so that one banned Google does not kill discovery.
29. As the host, I want `unresponsive_engines` on the per-call result even when `hits` is empty, so that zero-hit CAPTCHA is not dropped because `list[WebResult]` has nowhere to put it.
30. As the host, I want the same trip-log row in the run directory and a stable workstation JSONL, so that a trip is cross-run and not trapped in one run directory.
31. As the host, I want both trip-log writes to succeed for a new `event_id`, so that a cross-run Jarvis card cannot be silently empty. A new event that cannot be fully recorded → `blocked_on=search`, `blocked_reason=searxng_trip_log`.
32. As Jarvis (later card), I want rows to include UTC timestamp, run id, `event_id`, hit count, and `unresponsive_engines`, so that “Brave/Google twice” and “>50% of a UTC day” can be computed without parsing HTML. The day’s denominator is every successful JSON search row (healthy, empty, mixed).
33. As Jarvis (later card), I want HyperResearch **not** to send Telegram, so that `hpr` stays a workstation CLI.
34. As the host, I want search HTTP allowed only to the configured SearXNG origin (private/CGNAT/Tailscale/Cluster DNS ok), so that SSRF for **model fetch URLs** stays closed.
35. As the host, I want fetch SSRF (`allow_private_hosts` empty by default) unchanged for model-proposed URLs, so that a research query cannot fetch `169.254.169.254`.
36. As the host, I want the SearXNG origin taken only from config/env, never from a host-action argument, so that the model cannot redirect search at an arbitrary URL.
37. As the pipeline, I want the 16-step graph, host-action kinds, and fetch path unchanged, so that Spec 1 proofs stay valid.
38. As the pipeline, I want host `search` to call the search provider only, never `fetch_provider.search()`, so that crawl4ai’s `NotImplementedError` is not a web_error hint anymore when SearXNG is configured.
39. As FakeRuntime/pytest, I want injected `search_fn` still to short-circuit host search, so that existing host-action tests do not need SearXNG.
40. As CI, I want mocked HTTP covering: JSON hits, empty results, `unresponsive_engines` present, 500, 403, 3xx, 200+HTML, truncated/non-JSON, timeout — so that fail-closed vs continue is mechanical.
41. As the operator, I want the live probe off unless an explicit live mark **and** `HYPERRESEARCH_LIVE_SEARXNG=1` **and** `SEARXNG_URL` are all set, so that a workstation that already exports `SEARXNG_URL` cannot accidentally hit the instance from default pytest.
42. As an implementer, I want ruff on `src/` + `tests/` and mypy on `runtime/` + `pipeline/` still required, so that a sitting that only greened new tests is not mergeable.
43. As an upstream-sync reviewer, I want Tavily/Exa/Serply modules left in the tree unused, so that this PR is not a SaaS deletion and not a SaaS enablement.
44. As the operator, I want no new Tavily, Exa, or Serply keys, so that v1 has no silent SaaS fallback.
45. As the operator, I want gost, residential hops, SearXNG limiter, and Jarvis egress to SearXNG **out of this PR**, so that Phase C/D/E stay later sittings.
46. As the operator, I want the homelab instance left with `search.formats: [html, json]` and `limiter: false`, so that this fork does not “JSON-only” the server and kill the browser UI.
47. As a future Phase E author, I want in-cluster clients documented as Cluster DNS `http://searxng.searxng.svc.cluster.local:8080`, so that Tailscale is not baked into the adapter.
48. As omp, I want this spec named in the coding prompt, so that Telegram bun/git does not become the coding path.
49. As an implementer, I want search to return a per-call result `{hits, unresponsive_engines}`, so that diagnostics survive zero hits and concurrent searches cannot mix state.
50. As a CLI user, I want absent `search_provider` to default to `none`, so that existing vaults keep starting as vault-only instead of dying on an unset SearXNG URL.
51. As a CLI user, I want `fetch_provider` to win when both `provider` and `fetch_provider` are present, so that the deprecated alias cannot override an explicit fetch backend.
52. As a CLI user, I want a **set** `SEARXNG_URL` (including empty) to override `searxng_url`, and an **unset** variable to leave config, so that `export SEARXNG_URL=` can deliberately unconfigure.
53. As a CLI user, I want load→save to emit `search_provider`, `fetch_provider`, `searxng_url`, and `searxng_trip_log` (not the deprecated `provider` key), so that round-trip does not resurrect the combined slot.
54. As the orchestrator, I want search failures to persist `blocked_on=search` and `blocked_reason` **before** the generic `except Exception` path, so that Spec 1’s `_block_if_still_running(..., blocked_on="host-error")` cannot relabel them.
55. As the operator, I want `hpr run resume` after I fix the URL or instance to **re-query** only a search `task_id` that never got a durable `SearchCallResult` (HTTP/config failure). Completed host actions stay checkpointed. “No retry” is not a ban on that resume.
56. As the trip log, I want `event_id = run_id + ":" + host_action_task_id`, so that a partial dual-write plus resume cannot count Google twice.
57. As the trip log, I want every successful JSON search (healthy hits, empty SERP, all engines unresponsive) to write one row, so that the daily percentage has a real denominator.
58. As the search client, I want redirects disabled and every 3xx treated as `searxng_http`, so that “only the configured origin” is mechanical and a 302 to another private host never gets a second request.
59. As the host, I want only non-object entries and missing/non-string/blank URLs skipped; otherwise-valid hits with null or non-string title/content stay, normalized to `""`, then dedup and `limit`, so that a null title cannot occupy the quota as a skip and cannot blow up snippet slicing.
60. As a researcher, I want `results: []` with every engine unresponsive to continue and still write a trip row, so that implementers cannot reinterpret that case as `searxng_http`.
61. As the host, I want the complete `SearchCallResult` and the exact trip-log row persisted **before** either JSONL append, so that resume can finish a successful search from disk with no second HTTP request.

## Implementation Decisions

- Highest seam: host `search` action → search provider. Do not add a new orchestrator step. Do not change the 16-step graph. Fetch execution stays the existing fetch provider.
- Search metadata boundary: do **not** use `WebProvider.search() -> list[WebResult]` as the SearXNG return, and do **not** store `unresponsive_engines` on mutable provider state (`last_response` or similar). Concurrent host searches must not share diagnostics.

```python
class SearchCallResult:
    hits: list  # WebResult-shaped; may be empty
    unresponsive_engines: list  # [] if absent; else two-element [engine, reason] rows
```

  The search provider’s `search(query, max_results)` returns `SearchCallResult`. Combined fetch providers are not called for host search. Injected `search_fn` in tests may still return the host payload directly.

- Search-provider allowlist: `none` | `searxng`. Anything else is a start-of-run config error (`blocked_on=search`, `blocked_reason=searxng_config`).
- Fetch-provider: existing factory names. `searxng` is not a fetch backend (`blocked_on=search` is wrong here — reject at config load / start with `blocked_reason=searxng_config` if a run would start; a vault that only fetches never needs SearXNG).
- Application default: `search_provider = "none"` when the key is absent. Existing vaults stay vault-only and **keep starting**. Production setup must set `search_provider = "searxng"` explicitly. Pytest uses the same default.
- `fetch_provider` wins over deprecated `web.provider` when both are present. If only `provider` is present, it is `fetch_provider`. `provider` never selects search.
- Config keys under `[web]`: `search_provider`, `fetch_provider`, `searxng_url`, `searxng_trip_log`. Do not add a `[searxng]` table. Do not reuse `[search]` or `[fetch]`.
- Env: if `SEARXNG_URL` is **set** (including empty string), it is the resolved URL. If **unset**, use `searxng_url`. If `search_provider` is not `searxng`, both are ignored.
- Resolved SearXNG URL (when provider is `searxng`) must be `http` or `https`, have a host, and have no userinfo (embedded credentials). Fail start: `blocked_on=search`, `blocked_reason=searxng_unconfigured` for missing/blank; `blocked_reason=searxng_config` for bad scheme/userinfo.
- Serialization: `load` then `save` writes `search_provider`, `fetch_provider`, `searxng_url`, `searxng_trip_log`. It does not write `provider`. Unknown extra `[web]` keys remain ignored on load (existing forward-compat).
- Start-of-run gate: `search_provider=searxng` and resolved URL missing/blank/invalid → do not start the step graph.
- Request: `GET {resolved_url}/search?q=...&format=json`. Timeout 30s. **Follow redirects: off.** No automatic HTTP retry. No auth header. Do not expose extra SearXNG params from the model in v1.
- 3xx, 4xx, 5xx, timeout, DNS, connect failure → `searxng_http`. A 302 `Location` to another host (including private) must not be requested. Tests assert request count == 1.
- Response contract: body must parse as a JSON **object**. Missing `results` or a non-list `results` → `searxng_http`. HTML, empty body, or JSON array/string/number → `searxng_http`.
- `unresponsive_engines`: absent → `[]`. Present but not a list → `searxng_http`. List entries that are not a two-element sequence with a non-empty string engine are dropped from diagnostics; they do not fail the search.
- Result entries (one rule): skip non-objects; skip if `url` is missing, null, non-string, or empty after strip. **Retain** otherwise-valid hits. `title` and `content`: `str` kept; `null` → `""`; any other type → `""`. Never slice a non-string. Dedup by URL first-wins. **Then** apply `limit`. Do not issue a second HTTP request to backfill. Returning fewer than `limit` after filtering is success.
- `searxng_http` fails the **run** (`blocked_on=search`, `blocked_reason=searxng_http`) even when vault FTS returned notes. Do not convert that into `web_error` plus `ok: true`.
- Empty SERP: HTTP 200, JSON object, `results` is `[]` → search action `ok: true`, `hits: []`, run continues. **Also continue when `unresponsive_engines` names every engine and `results` is `[]`.** That is success, not `searxng_http`. Host payload `hint` is `None` on any successful SearXNG JSON (hits or empty). The string `Provider cannot web-search. Propose fetch actions with https URLs.` is only for `search_provider=none`.
- Host hit shape stays `url` / `title` / `snippet` (snippet from `content`, cap 500).
- Failure propagation: raise a typed search-block exception that carries `blocked_on="search"` and `blocked_reason`. Persist those manifest fields **before** the exception leaves the host action. `execute_run`’s generic `except Exception: _block_if_still_running(...)` (default `host-error`) must not overwrite a persisted search block. If the typed exception reaches that handler with the run still `running`, the handler must persist `blocked_on=search` (not `host-error`). Do not overwrite an already-persisted `blocked_on` of `budget`, `verify`, `cite-check`, or `independence`.
- No automatic HTTP retry ≠ no operator resume. `hpr run resume` re-reads config/env and skips host actions whose `task_id` is terminal success. Re-query SearXNG only when that `task_id` has **no** durable `SearchCallResult` (true `searxng_http` / unconfigured). If still unconfigured, stay `blocked_on=search` without replaying completed steps. If a durable result exists, resume must **not** HTTP.
- Trip-log row (identical object to both files):

```json
{"event_id":"<run_id>:<task_id>","ts":"<UTC ISO-8601>","run_id":"<run tag>","hits":0,"unresponsive_engines":[["google","CAPTCHA"]]}
```

  `event_id` is `run_id + ":" + host_action_task_id`. Write to the run-directory JSONL and append to the stable workstation JSONL (`[web] searxng_trip_log`, default user-level HyperResearch data file). UTC day is the aggregator’s grouping key, not a filename. HyperResearch does not compute the trip and does not notify Telegram.

- Durable call record (before either JSONL append): after a successful JSON parse and mapping, persist the complete `SearchCallResult` (every surviving hit: url, title, snippet/content) **and** the exact trip-log row object under the host-action `task_id`. Then append that same row to the run-directory JSONL and the stable workstation JSONL.
- Dual-write / resume:
  - A **new** `event_id` must land in **both** JSONL files or the search action is not terminal success (`blocked_reason=searxng_trip_log`) until resume repairs it.
  - Resume with a durable `SearchCallResult`: complete any missing JSONL append from the saved trip-log row (same `event_id`, no second observation), then complete the host action from the saved hits. **Zero additional HTTP.** Do not append a duplicate to a file that already contains that `event_id`.
  - Crash after both appends and before the host-action success checkpoint: resume reads the durable `SearchCallResult`, does not HTTP, does not append again, returns the original hits to the model, then checkpoints success.
  - Concurrent writers: exclusive lock (or equivalent) around each append. Aggregators and tests treat `event_id` as the unique key; duplicate lines with the same `event_id` count as one.
  - Every successful JSON search writes a row, including healthy hits, empty SERP, and empty SERP with all engines unresponsive. Failed searches (`searxng_http`) do **not** write a success row and do **not** persist a `SearchCallResult`.
- SSRF split: search client contacts **only** the resolved SearXNG origin from config/env (private addresses allowed for that origin). Because redirects are off, that origin cannot change mid-call. Fetch of model URLs keeps the existing closed SSRF gate. Do not add the SearXNG host to global `allow_private_hosts` as a side effect of search.
- Host search with `search_provider=none` is vault FTS only (plus injected `search_fn` in tests). Do not call crawl4ai `search()`.
- No silent SaaS fallback if SearXNG fails.
- Sitting quality: `ruff check` on `src/` + `tests/`; mypy on `runtime/` + `pipeline/` (not whole-package mypy).
- Keep leftover Claude Code install extras. Do not port skills/hooks to Hermes.

## Testing Decisions

Good tests assert external behaviour: which provider is called on `search` vs `fetch`, start-gate vs continue vs fail-the-run, JSON mapping, trip-log `event_id`, and `blocked_on` after the generic orchestrator handler. Do not assert crawl4ai internals. Do not parse HTML in tests except as a **fixture body** that must be rejected.

**Seams (highest first):**

1. Host-action `search` — vault FTS + `SearchCallResult`; fail-closed vs empty-SERP continue.
2. SearXNG search HTTP — JSON contract, no redirects, filter-then-limit.
3. Start-of-run config gate and load/save round-trip.
4. Orchestrator exception path — `blocked_on=search` survives generic `host-error` handling; HTTP re-query only when no durable `SearchCallResult`.
5. Fetch path — unchanged; SSRF still closed for model URLs.
6. Trip-log writer — durable result first, dual file, `event_id` dedup, partial write, concurrent append.

**Prior art:** stubbed Tavily provider tests; `safe_http` SSRF tests; host-action tests that inject `search_fn`. No live network in default pytest.

**Required suites:**

- Config: absent `search_provider` → `none`, run starts. `search_provider=searxng` without URL → no step graph, `searxng_unconfigured`. Unset `SEARXNG_URL` leaves config; set-empty env overrides to blank. Both `provider` and `fetch_provider` → fetch_provider wins. Save omits `provider`. Unknown `search_provider` and `fetch_provider=searxng` → `searxng_config`. Bad scheme / userinfo URL → `searxng_config`.
- Host search `none`: vault FTS only; crawl4ai `search()` not called; missing web search is not `searxng_http`.
- Host search `searxng`: does not call fetch-provider `search()`. Vault hits still present on success. Zero hits still carry `unresponsive_engines` on the call result.
- Mock 200 JSON with hits → mapped; snippet ≤ 500; URL dedup first-wins after filtering.
- Malformed entries: skip non-objects and missing/non-string/blank URLs; **keep** hits whose title/content is null or a non-string, normalized to `""`; then `limit` on survivors.
- Mock 200 JSON `results: []` → `ok: true`, run not blocked, trip row with `hits: 0`, `hint` is `None` (not the crawl4ai cannot-web-search string).
- Mock 200 JSON `results: []` and every engine in `unresponsive_engines` → `ok: true`, run not blocked, trip row includes those engines. Not `searxng_http`.
- Mock 200 JSON with hits plus `unresponsive_engines` → `ok: true`, row persisted.
- Mock 500, 403, 302, timeout, DNS, 200 `text/html`, 200 `{not json}`, 200 JSON array → `searxng_http`, run blocked, even with vault FTS stubs returning notes. 302 fixture: `Location` to another private host; assert exactly one HTTP request.
- Orchestrator: force the typed search exception through `execute_run`’s generic handler; manifest `blocked_on` is `search` (not `host-error`) and `blocked_reason` is preserved.
- Resume after `searxng_http` / unconfigured (no durable result): completed fetch `task_id` not replayed; search `task_id` HTTP once more; after URL repair, unconfigured block clears and search runs; HTTP client still does not retry 500s inside one attempt.
- Recovery after successful JSON: persist `SearchCallResult` + trip row, then fail the workstation append (or crash after both appends before the success checkpoint). Resume: **one HTTP request total** across original attempt and recovery; identical `event_id` rows; original hits returned to the model; no extra JSONL line. Concurrent appends: unique `event_id`s; duplicates collapse. Failed `searxng_http` writes no success row and no durable result.
- SSRF: search does not fetch a model URL; fetch still refuses private literals unless allowlisted on **fetch** settings.
- Injected `search_fn` still short-circuits.
- Live: skip unless mark `searxng_live` **and** `HYPERRESEARCH_LIVE_SEARXNG=1` **and** `SEARXNG_URL`. Default pytest collection does not select it. Assert JSON object and `results` is a list.
- ruff + mypy sitting gates as in Spec 1.

## Out of Scope

- Gost ClusterIP, residential hops, gluetun, human SSH captcha tunnel.
- SearXNG limiter, engine trim, `public_instance`, gitops overlay changes.
- Jarvis / in-cluster Hermes egress to `searxng.searxng.svc.cluster.local:8080` (Phase E).
- Jarvis Cron Inbox `7703` notifier (follow-up card, not this PR).
- Tavily / Exa / Serply keys or `search_provider` values.
- HTML fallback, UI scraping, SearXNG auth, POST `/search`, following redirects.
- Changing light/full ship gates, ModelRuntime, patch caps, or the 16-step graph.
- Spec 2 / GBrain / wiki write / `knowledge/wiki`.
- Scrapling, Firecrawl, Grok web-search tool.
- Implementing from this Jarvis Telegram session, Kubernetes Jobs, Sandcastle.

## Further Notes

- Homelab SearXNG already has `search.formats: [html, json]` and `limiter: false`. This spec does not change that instance. Dropping `html` would kill the browser UI; dropping `json` would 403 this adapter.
- Official API: `GET /search?q=&format=json`. A format not in `search.formats` is 403, not HTML fallback. 200+HTML still happens in front of SearXNG (proxy, bot wall). That is why the body must parse as JSON.
- Production workstation vault must set `search_provider = "searxng"` and a URL (or `SEARXNG_URL`). The application default is `none` on purpose so old vaults do not fail closed.
- Deliberate continue: `results: []` with every engine unresponsive is an empty SERP, not an HTTP failure. The trip row still exists so Jarvis can count that call in the day’s denominator.
- Trip thresholds for the **Jarvis** follow-up (not `hpr`): after `event_id` dedup, Brave or Google unresponsive twice, or unresponsive engines on >50% of that UTC day’s **successful search rows**. Card: `issues/03-jarvis-searxng-trip-card.md`. Do not import Telegram into HyperResearch.
- Human spec gate is this document. Operator says `go`, then omp implements. Operator says `merge` later to land GitHub.
- In-cluster URL for a later sitting: `http://searxng.searxng.svc.cluster.local:8080`. Workstation Spec 1.5 uses whatever resolved URL the operator sets (Tailscale or LAN).
