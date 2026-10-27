# Devpost form: Warden (copy-paste)

One block per form field, in form order. Copy what's inside each grey block.
Deadline: **Oct 30, 10:00 PT** (19:00 Berlin).

Before you paste, two checks:
- **Video URL**: upload `warden-demo.mp4` + `warden-demo.srt` (captions) to YouTube first.
- **Berlin**: only tick/write it if you attended Sep 29.

---

## Project name

```
Warden
```

## Elevator pitch / tagline

```
An autonomous coding agent that has to prove its fix before it asks for your review.
```

## Track / category

```
Coding
```

## Video demo link

```
<paste your YouTube link here>
```

## Demo URL (try it out)

```
https://port8080-h79fp774t88b2wc.tunnel.applications.eu-west2.nebius.cloud/
```

## Repository URL

```
https://github.com/moiz-lakkadkutta/warden
```

## Built with (tags; add one at a time if the field wants that)

```
nebius-token-factory, nebius-sandboxes, nebius-serverless-ai-endpoints, nebius-container-registry, nvidia-nemotron-3-ultra, nvidia-nemotron-3-super, nvidia-nemotron-3.5-lightning, tavily, python, fastapi, pydantic, uv, github-actions
```

## Builders & Brews city (only if you attended)

```
Berlin (Sep 29, 2026)
```

## New project statement (if asked)

```
Warden was written for this hackathon, starting 14 Sep 2026 (first commit that day). Before that the hunt → plan → fix → verify idea existed only as a personal Claude Code skill (prompts, no shippable code); every line in this repo is new.
```

---

## About the project (the big Markdown story field)

```markdown
## Inspiration

I run a small ERP (FastAPI + Postgres, double-entry ledger, Indian GST) on my own. A wrong number in a dashboard query or a missing tenant check reaches someone's books. Coding agents now write plausible fixes in minutes, so the bottleneck has moved from writing the fix to trusting it: a green test the agent wrote proves nothing if that test would have passed without the fix. I wanted an agent whose second half exists to doubt the first, and that shows its evidence instead of narrating confidence.

Who it's for: small teams and solo maintainers of code where a wrong fix costs money (ERP, billing, invoicing, multi-tenant SaaS), who use coding agents but cannot afford to review every agent PR line by line. Warden does the doubting first and hands them a draft PR with its evidence, or nothing.

## What makes Warden different

Many agents now fix code and run tests in a sandbox. Warden's verifier asks a harder question, and it is tested on real code:

- **It proves the test is real.** New tests must **fail on a fork of the unfixed base** (the theater check). A test that passes without the fix doesn't count; it rejected 14, then 10, such tests across our two SWE-rebench runs, and eight wrong-reason failures on Fabric.
- **It finds bugs too.** The Hunter boots the app in a sandbox fork and confirms cross-tenant holes twice before filing an issue, for $0 of model spend.
- **It works on a production codebase, not only a benchmark.** A real draft PR on a live ERP ([#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221)), with that repo's own CI 7/7 green.
- **It publishes its own error rate.** The verifier's PASS precision (48%) and its 1-of-9 result on the ERP campaign are in the README, next to the 18/49.
- **You can install it today.** It's a GitHub Action: label an issue `warden`, get a draft PR or a comment explaining why not. No token ever enters a sandbox, and Warden never merges.
- **Every run can be inspected.** A live console on a Nebius Serverless endpoint shows each run's fork tree, cost and evidence.

## What it does

Warden runs **Hunt → Plan → Fix → Verify → PR** on one issue:

- **Hunt** (one axis so far, IDOR / cross-tenant): boots the app in a sandbox fork, signs up two tenants, enumerates id-taking endpoints from `/openapi.json`, probes cross-org reads and cross-firm writes, and files an issue only for findings that reproduce twice. It uses no model calls.
- **Plan**: Nemotron 3 Ultra reads the issue and the code in the sandbox, cites docs found with Tavily, names the files and tests, and classifies risk (money / tax / schema / security).
- **Fix**: Nemotron 3 Super, in a tool-calling loop on its own **fork** of a base sandbox checkpoint, writes a failing test first, then the fix.
- **Verify**: the implementer's word is never evidence. A candidate is FAIL unless:
  - the new tests **fail on a fork of the unfixed base** (the theater check);
  - the suite and configured lint / type / migration / OpenAPI checks pass;
  - the base's related tests still pass on the candidate;
  - an Ultra spec review finds every behaviour the issue states implemented, quoting the issue verbatim.

  Best-of-N runs up to N candidates on separate forks and picks one by a fixed rule.
- **PR**: a PASS opens a **draft** PR whose body is the evidence table: each claim, the check behind it, and what was *not* verified. Risky changes are labelled `PENDING-HUMAN`. A FAIL opens nothing. Warden never merges.

Every model call and sandbox step goes to `events.jsonl` with tokens, cost and time. `warden console` renders it as a page with the phase timeline, the fork tree, and the evidence table, and that console is live on Nebius. It also runs as a GitHub Action: label an issue `warden` and it opens a draft PR or comments why it didn't.

Try it with no keys: `uv tool install git+https://github.com/moiz-lakkadkutta/warden && warden run --demo`

## How we built it

- **Nebius Token Factory** (OpenAI-compatible API) serves every model call: JSON-schema structured output for every plan, verdict and finding (pydantic schemas), and native function calling for the implementer loop.
- **NVIDIA Nemotron, routed by role** (set by env, swappable without code changes):
  - **Nemotron 3 Ultra** (`nvidia/Nemotron-3-Ultra-550b-a55b`) plans and writes the spec-review verdict, where reasoning and adversarial judgement matter;
  - **Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`) is the implementer: 19 of 19 tool calls well-formed on the first Fabric fix, at about a third of Ultra's cost;
  - **Nemotron 3.5 Lightning** (`nvidia/Nemotron-3_5-Lightning`) handles small structured tasks such as research queries and parsing.
- **Nebius Token Factory Sandboxes**: every run is an immutable checkpoint, and two runs from one checkpoint are a fork (about 1 s each in our measurements). The fork tree *is* the verifier: the candidate, the theater check on the unfixed base, the regression check, and N best-of-N candidates are all siblings of the same base, and the gate runs on disposable forks, so the candidate is never modified. The patch leaves the sandbox via `git format-patch` and is pushed from the host, so no token ever enters a sandbox.
- **Preloaded SWE-rebench images** on Sandboxes: `warden eval` plans, fixes and verifies on the task image, then grades with the dataset's hidden tests, which the agent never sees.
- **Nebius Serverless AI endpoint + Container Registry**: the run console is a read-only FastAPI app shipped as a non-root image to Nebius Container Registry and run on a `cpu-d3` endpoint (`ops/deploy-console.sh`).
- Python 3.12, `uv`, typer-slim, pydantic, a plain state machine (no agent framework), `mypy --strict`, offline tests against a fake sandbox, and CI that replays a recorded run.

### How Warden uses Tavily

The planner never cites from memory. Nemotron 3.5 Lightning turns the issue into search queries; Warden calls **Tavily search at runtime** (`search_depth="advanced"`, `include_domains` limited to official documentation sites, 3 results per query); Nemotron 3 Ultra then writes the plan, and any citation whose URL did not come back from Tavily is dropped and logged, so **every cited URL in a plan resolves**. On the run behind PR #221, two `tavily.search` calls about SQLAlchemy `in_()` filtering on an enum column are in its `events.jsonl` and on the live console. A Tavily error is logged and planning continues without web sources. Code: [`warden/plan/research.py`](https://github.com/moiz-lakkadkutta/warden/blob/main/warden/plan/research.py), [`warden/plan/planner.py`](https://github.com/moiz-lakkadkutta/warden/blob/main/warden/plan/planner.py).

## Challenges we ran into

- **A PASS on under-specified issues.** On SWE-rebench our verifier's first PASS precision was 4 of 14 (29%). Of the 10 false PASSes, 8 needed an API, message or name the issue never states; a verifier that reads only the issue cannot catch those. The 2 real misses (a repro that used a different exception class or resource kind than the issue's own example) we did fix.
- **Nemotron 3 Super on large files.** On Fabric's 900-line test files Super re-read files or re-sent them whole past the completion limit, and in the first 15 campaign attempts it called `finish` in only 2. We changed the tools (200-line read pages, no whole-file rewrite over 300 lines, an edit mode that withholds read tools when it loops, auto-finish of a checked green candidate). Loops went down, but PASS on those issues did not go up.
- **Sandboxes beta transport.** 7 of 49 E1 tasks first ended in a timed-out or dropped operation poll, and `run()` stdout is cut silently at 65,535 bytes, which once made a correct patch grade as unresolved. We added retries and chunked reads, re-ran the affected tasks, and logged both in `FEEDBACK.md` with numbers.
- **Honest hunting.** On current Fabric the Hunter finds no cross-tenant hole, because the known one was fixed. Rather than inflate a non-finding, we replayed the commit before the fix (`--at d9c37cb^`): there it confirmed the cross-firm write on 6 endpoints and filed [#225](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/225) and [#227](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/227). The fix step then **failed** on both. The verifier rejected every candidate and no PR was opened, which is the safety property working, but it is not a fix.

## Accomplishments that we're proud of

- **A real draft PR from issue text alone:** [Fabric-Inventory#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221). A closed money bug was re-solved unattended, with comments hidden, on the code before the human fix, for $0.17, and the target repo's own CI went 7/7 green. Its repro test fails on the unfixed base (`10498.95 == 0`) and passes on the candidate.
- **A measured number, with its error bars:** 18 / 49 SWE-rebench tasks resolved (37%, 95% CI 25–51%) at $0.49 per task, $24.16 in total, up from 11 / 49 (22%) on the same tasks a week earlier, with the verifier's false-PASS rate printed next to it rather than hidden.
- **Verifier PASS precision 29% → 48%** on those 49 tasks (4 of 14 → 13 of 27 PASS accepted by the hidden tests) after the spec review learned to check the issue's own artefact, a guard-only test stopped counting as a repro, and a valid repro with a red check goes back once (62% on the 15 tasks first marked PASS).
- **The theater check works:** it rejected 14 and then 10 new tests that already passed without the fix, and on Fabric it rejected eight tests that failed for the wrong reason.
- **Nothing unsafe shipped:** no FAIL produced a branch or a PR, and every money or tax issue was labelled `PENDING-HUMAN`.

## What we learned

- **Fork-per-step is the right primitive for verification.** "Run the new tests on the unfixed code" is one line when sandboxes fork in about a second. Without forks it is a rebuild.
- **Precision is the metric that matters for trust,** and it has a ceiling set by the issue text. Once a hidden test demands something the issue never says, no issue-reading verifier can catch it. The honest output is "PASS is evidence, not proof" and a draft PR.
- **Model sizing by role pays off.** Ultra's judgement is worth its price on one plan and one verdict per issue. Super's well-formed tool calls are worth more in a 60-turn loop than raw strength. Lightning is cheap, but it reasons by default, which makes it slower than expected for a router.
- **A benchmark win is not a codebase win.** On a real ERP the campaign passed **1 of 9** issues ($7.70, 29 attempts). That is the number that tells us what to build next.

## What's next for Warden

- Make the implementer reliable on large real-world files (Fabric campaign: 1 of 9 today).
- More Hunter axes: race (concurrent finalize → duplicate vouchers), money, malformed input and flow are designed but not built (only the IDOR axis runs today); frontend is not covered.
- A concurrency check in verify and a migration downgrade round-trip in the gate.
- Spend the verifier's doubt where it matters: when the spec review sees an under-specified behaviour, say so in the PR instead of passing it.
- Run on more repos than Fabric and the SWE-rebench sample, and publish each run's console page.
```

---

## Feedback on Nebius / NVIDIA tooling (feedback field)

```
Full dated log with every number and run id: https://github.com/moiz-lakkadkutta/warden/blob/main/FEEDBACK.md — the 12 that cost us most:

SANDBOXES
1. run() stdout is silently cut at 65,535 bytes, no flag. It made a correct gold patch grade "not resolved" (P2P 614/672) and once cut a 65,709-byte file read, so our regression check saw 237 fake regressions and picked the wrong best-of-N candidate. Ask: a `truncated` field or a file download call.
2. No SysV IPC in the sandbox kernel (shmget → ENOSYS), so Postgres 16 cannot start. We wrote a 100-line LD_PRELOAD shim. Every Postgres-backed app hits this on day one. Ask: enable CONFIG_SYSVIPC or document it next to "full root access".
3. Operation polling under load: 7 of 49 eval tasks ended in ApiTimeoutError / dropped connections on GET /operations/{id} (299–1856 s in, $2.25 of model spend lost), plus one HTTP 500. Ask: say whether re-polling the same operation id is safe, and retry polls in the SDK.
4. Past ~50 runs in flight, runs queue silently (60 concurrent: latency 21 s → 45 s, no error). Ask: queue position or 429 + Retry-After.
5. Speed is excellent: fork ≈ 1 s, a 633 MB image import 56 s, and Fabric's 1787-test suite ran in 774 s vs 893 s on GitHub Actions. Fork-per-run is what made the theater check and best-of-N cheap.
6. The 39,520 preloaded SWE-rebench/SWE-bench image tags are undocumented (tag scheme, /testbed layout, task conda env not on PATH). One docs page would save hours of probing.

TOKEN FACTORY + NEMOTRON
7. Reasoning tokens count against max_tokens, and the default is 8192: Ultra spent 16,384/16,384 on reasoning three calls in a row before emitting the strict json_schema Plan ($0.27, no plan); Lightning used 8192/8192 on a ~100-token JSON answer. Only chat_template_kwargs {"enable_thinking": false} turned Lightning's thinking off (1119 → 72 tokens, 4.4 s → 0.5 s); reasoning_effort="low" did nothing. Ask: document the thinking switch and default/max max_tokens on each model card.
8. Truncated tool calls arrive without finish_reason=length on the call: Super's write_file came back with no `content` argument 8 times in one run. Ask: flag or reject truncated tool calls.
9. Nemotron 3 Super is a strong tool caller (19/19 well-formed calls, $0.125 for a correct 2-file fix), but on 900-line files it re-reads in a loop (87 repeat reads in 6 runs, 0 edits) even after explicit STOP text; only removing read tools from the tool list for 3 turns broke the loop. Ask: a recommended sampling/prompt preset for agentic tool loops.
10. Billing: "402 You have exhausted your budget" while the console showed $10.00; the real cause was "Trial ended, switch to paid usage". Ask: a distinct error and a balance endpoint so agents stop before a 402.

SERVERLESS AI ENDPOINTS
11. On 2026-10-27 our console endpoint went to ERROR "VMStopped" with no notification, and `nebius ai endpoint restart` was refused: "This Endpoint/Devlab is outdated. Please create a new one." Recreating took 383 s and produced a NEW public URL, breaking every link already handed out. Ask: a stable per-name URL or in-place upgrade, and an alert when an endpoint leaves RUNNING.
12. --platform defaults to gpu-h100-sxm even for a one-port static site; forgetting it bills an H100. Ask: default to the cheapest CPU preset. (Good: --dry-run validates offline in <2 s, and same-project registry pulls need no credentials.)
```
