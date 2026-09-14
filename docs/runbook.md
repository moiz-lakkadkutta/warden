# Warden Runbook

Nebius × NVIDIA Global AI Hackathon · decided 14 Sep 2026 · submit 28 Oct, deadline 30 Oct 10:00 PT (19:00 Berlin)

## 1. Reward model — what we are optimizing

The rules make three things true at once. A project can win **either** one overall cash prize **or** one track prize plus one bonus. Multiple submissions are allowed if substantially different, at most one per track. Four criteria are weighted equally and ties go to whoever scores highest on Technological Implementation, then Design, then Impact, then Idea.

| Prize | Cash | Who can win it | Our play |
|---|---|---|---|
| Grand / 2nd / 3rd | $20k / $10k / $6k | any track | Warden, on the strength of a number nobody else has |
| Track winner ×4 | Jetson Orin Nano | one per track | Warden (Coding) and RegWatch (Best Apps) both eligible |
| Best Use of Tavily | $3,000 | one bonus per project | RegWatch is built around Tavily so this isn't blocked if Warden places overall |
| City winner ×20 | $500 | attendees of a Builders & Brews city | Berlin, Sep 29 — disclose on the form |
| Most Valuable Feedback ×10 | $100 + swag | the feedback field | FEEDBACK.md kept from day 1; Sandboxes beta will generate real findings |

Expected value ranking of effort: (1) Warden's SWE-rebench number and evidence artefact, because it is the only lever on the $36k; (2) RegWatch's Tavily integration, because $3k for ~10 days of work is the best cash-per-hour on the board; (3) Berlin, one evening for $500 plus ~$200 credits; (4) feedback, one hour.

How each criterion is scored, and the concrete thing that earns the points:

| Criterion | What the judge is looking for (rules text) | Warden evidence | RegWatch evidence |
|---|---|---|---|
| Technological Implementation | "how effectively does it use Token Factory / AI Cloud and Nemotron" | three Nemotron sizes with stated jobs; Sandboxes fork/rollback used for theater check and best-of-N; Serverless Endpoint for console | Ultra + Lightning; Serverless Jobs for the crawl; Tavily search+extract with domain filters and date windows |
| Design | "a complete, coherent product experience" | one loop, one artefact: PR with an evidence table a judge can read without running anything | one page: dated change feed → "what to do this month" with citations |
| Potential Impact | "credible, specific case for solving a real problem" | solo founder shipping GL/GST-correct code; a wrong number reaches the books; the 19-issue laptop campaign already happened | fines and 12-month cancellation windows for merchants who miss a rule; no dedicated tool exists for SMEs |
| Quality of the Idea | "creative, non-obvious use" | the agent *finds* the bug (race probes, IDOR sweeps) before fixing it; verifier proves tests aren't theater | regulation diff against a store profile, not a generic news feed |

## 2. Tech stack (pinned)

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.12, `uv` | matches Fabric backend; Contree SDK is Python |
| LLM access | `openai` SDK → `https://api.tokenfactory.nebius.com/v1` | OpenAI-compatible; function calling + `response_format: json_schema` supported |
| Models | `nvidia/Nemotron-3-Ultra-550b-a55b` (plan, verdict), `nvidia/nemotron-3-super-120b-a12b` (implement), `nvidia/Nemotron-3_5-Lightning` (tool calls, parsing, hunt execution) | reasoning up, execution down; NVIDIA's own routing story |
| Sandboxes | `contree-sdk` + `contree-client` (ContreeSync for the orchestrator, async for parallel forks) | images are immutable checkpoints; `.run(disposable=False)` forks |
| Research | `tavily-python` (search `advanced`, `include_domains`, `include_raw_content`, `time_range`) | cited plans; RegWatch's core |
| Orchestration | plain Python state machine + `asyncio`; no LangChain | fewer moving parts; every step is a typed dataclass we can log |
| Schemas | `pydantic` v2 → JSON schema for every model output (plan, verdict, finding) | structured output = parseable, diffable, testable |
| Storage | SQLite (`runs.db`) for run state + `runs/<id>/` folder of JSONL events, cost, artefacts | zero infra; committed example run for judges |
| Console | static HTML generated per run + a tiny FastAPI viewer, deployed as a Nebius Serverless Endpoint | demo URL that survives the Sandboxes beta |
| RegWatch | FastAPI + Jinja page; crawl as a Nebius Serverless Job on a schedule; Postgres on Nebius | same stack, no new learning |
| CI | GitHub Actions: lint, unit tests, one recorded end-to-end run replayed from fixtures (no live API calls) | green badge for judges |
| License | MIT, top-level `LICENSE` | rules require MIT / Apache-2.0 / MPL-2.0 |

## 3. Warden architecture

### 3.1 Components

```
                 ┌──────────────┐
  repo + app ───▶│   HUNTER     │  eight probe axes → findings (confirmed only)
                 └──────┬───────┘
                        ▼  GitHub issue per root cause (repro, expected/actual, severity)
                 ┌──────────────┐
                 │   PLANNER    │  Ultra + Tavily → Plan{files, tests, landing_order, risk_class, citations}
                 └──────┬───────┘
                        ▼
      base ──fork──▶ candidate sandbox
                 ┌──────────────┐
                 │ IMPLEMENTER  │  Super writes; Lightning drives read/write/run tools; TDD loop
                 └──────┬───────┘
                        ▼  checkpoint "candidate"
                 ┌──────────────┐
                 │  VERIFIER    │  fresh context. theater check · gate · race probe → Verdict
                 └──────┬───────┘
                        ▼
                 ┌──────────────┐
                 │ INTEGRATOR   │  waves by file-conflict graph · merge --no-ff · PR + evidence table
                 └──────────────┘
```

Every component reads and writes one `Run` record; every model call, tool call, and sandbox op is appended to `runs/<id>/events.jsonl` with tokens and wall-clock. The console renders that file. Nothing in the demo depends on live state.

### 3.2 Sandbox model → pipeline mapping

Contree concepts: an **image** is an immutable filesystem state. `image.run(shell=..., disposable=False)` executes and returns a new image (a checkpoint, with `.uuid`, `.stdout`, `.stderr`, `.exit_code`). Calling `.run()` on any checkpoint forks it; two children of one parent are independent.

| Laptop pipeline (today) | Warden on Sandboxes |
|---|---|
| `git worktree add ~/fabric-worktrees/issue-N` | `candidate = base.run("git checkout -b fix/N", disposable=False)` |
| `CREATE DATABASE test_N TEMPLATE test_template` | Postgres + migrated schema + seed baked into `base`; every fork inherits it for free |
| Theater check: revert source to fork point, prove new tests fail, restore | `theater = base.run(copy_in(new_test_files) + "pytest <new tests>")` → `exit_code != 0` required |
| Fresh verifier with its own DB | `verify = candidate.run(...)` — the verifier never touches the implementer's image |
| Race proof with threads/Barrier | run inside `verify` against the in-image API; count rows in the in-image Postgres |
| Integrate on green; next wave from updated head | `integration = integration.run("git merge --no-ff fix/N", disposable=False)`; next wave forks `integration` |
| ≤2 heavy agents (Mac CPU starvation) | ≤4 concurrent forks (beta cap 50 ops; credit budget) |
| Best-of-N (option C, W4) | `cands = [candidate_base.run(impl_i) for i in range(N)]`; verifier ranks by evidence |

Base image build (`ops/build-base.sh`): from `backend/Dockerfile.dev`, add Postgres 16 + Redis, `entrypoint.sh` that starts `pg_ctl` and `redis-server` then execs the command, run `alembic upgrade head` and `make seed-demo`, commit, push to a registry Token Factory can pull from, `sdk.images.use("<registry>/warden/fabric-base:<sha>")`. Success test: in-sandbox `pytest -q` count equals the Mac count. If Postgres-in-image fails under the sandbox process model, fall back to one Nebius managed Postgres per wave with `DATABASE_URL` injected; do not fall back to SQLite (RLS tests need Postgres).

### 3.3 Model routing and cost controls

| Step | Model | Output schema | Budget per issue |
|---|---|---|---|
| Hunter: choose probes for a flow | Ultra | `ProbePlan` | 1 call |
| Hunter: execute probes, parse results | Lightning | `ProbeResult` | ~20–60 cheap calls |
| Planner | Ultra | `Plan` (files, tests, order, risk_class, citations[]) | 1–2 calls |
| Implementer | Super | free-form diff + tool calls | ≤ 25 tool turns, else abort |
| Tool selection inside implementer | Lightning | function calling | bundled |
| Verifier verdict | Ultra | `Verdict` (PASS / PASS-WITH-CONCERNS / FAIL, evidence[]) | 1 call |

Rules: log `usage` from every response into `cost.jsonl`; hard cap $3 per issue in W2 falling to $2 by W4; if Ultra planning exceeds 40% of spend, move planning to Super and keep Ultra only for verdicts; Lightning handles anything that is "read this output and tell me what happened". Always send the JSON schema in both `response_format` and the prompt text (Token Factory docs recommend this).

### 3.4 Hunter — the discovery phase

Ported directly from `flow-bug-finder` and `RECIPES.md`. The Hunter boots the app inside a fork of `base`, signs up its own "QA-<run>" org (respecting the 3/hr/IP signup limit by reusing one org per axis), and runs, in order of historical yield: race (N parallel calls with distinct idempotency keys on every state-transition endpoint, then count vouchers/stock rows/allocations in psql), IDOR (org A's token against every id-taking endpoint enumerated from `/openapi.json` with org B's UUIDs), money invariants (trial balance, books == GSTR-1, control == sub-ledger, CGST == SGST, inventory never negative, conservation), malformed input, cross-document flow holes. Frontend axis is out of scope for the hackathon.

Findings must be reproduced twice before filing; anything else is logged as "plausible" and not filed. Findings are grouped by shared root cause and filed as GitHub issues with the exact format from the skill (repro curl, expected/actual + request_id, impact, severity). The known-issues baseline (from `docs/reviews/**` and `12-bug-register.md`) is loaded first so re-finds of fixed bugs are labelled regressions.

For the SWE-rebench eval the Hunter is skipped — the task already carries the failing test. For the demo, the Hunter is the opening scene.

### 3.5 Verifier — the differentiator

Non-negotiables, each producing a row in the evidence table:

- **Theater check.** Fork `base`, copy in only the new test files, run them. They must fail. If they pass, verdict is FAIL with reason "tests do not exercise the change".
- **Gate.** In `candidate`: full backend suite, `mypy .` whole tree, `ruff check` + `ruff format --check`, `alembic upgrade head` from empty DB + single head, `make openapi-snapshot` drift.
- **Race probe.** If `risk_class` includes concurrency: real threads with a Barrier against the in-image API; count effects in psql; must be exactly one.
- **Migration round-trip** if a migration exists: upgrade → downgrade → upgrade.
- **Ask-vs-Decide.** `risk_class ∈ {money, tax, schema, security}` → PR labelled `PENDING-HUMAN`, never auto-merged. The PR body says why.

The verifier runs with a fresh context and an adversarial brief; it never sees the implementer's reasoning, only the diff and the sandbox.

### 3.6 Data model

```
Run(id, repo, base_image, started_at, status, cost_usd)
Finding(run_id, axis, severity, repro, expected, actual, confirmed:bool, issue_url)
Plan(issue, files[], tests[], landing_order, risk_class[], citations[{url, claim}])
Candidate(issue, image_uuid, tool_turns, tests_added[], cost_usd)
Verdict(candidate, theater:{passed_on_base:bool}, gate:{suite, mypy, ruff, alembic, openapi}, race:{n, effects}, decision, concerns[])
Wave(n, issues[], integration_image_uuid, merged[])
```

All stored as JSON in `runs/<id>/` and mirrored to `runs.db`. A checked-in `examples/fabric-issue-XXX/` folder contains one complete run for judges to read.

### 3.7 PR body (the artefact judges read)

```
## Warden evidence — #201 double-posted voucher on concurrent finalize

| Check | Result |
|---|---|
| New tests on unfixed base | FAIL 3/3 (as required) |
| Full suite on candidate | 214 passed |
| Race probe (8 threads, Barrier) | 1 voucher (was 3) |
| mypy / ruff / alembic head / openapi | clean / clean / single head / no drift |
| Risk class | money → PENDING-HUMAN |
| Cost | $1.42 · Ultra 2 calls · Super 11 turns · Lightning 37 calls |

Plan citations: SQLAlchemy `with_for_update` docs <url>; Postgres row locks <url>
```

## 4. RegWatch architecture (build Oct 17–27)

Input is a store profile: country of establishment, ship-to countries, product categories, platform (Shopify). A Serverless Job runs daily: Tavily `search` with `include_domains` over a fixed source list (EUR-Lex, gesetze-im-internet / BMJ, Commission guidance pages, Shopify's own EU compliance pages), `time_range: week`, `search_depth: advanced`, then `extract` on new hits. Lightning normalises each document into `RegChange{source, published, jurisdiction, instrument, summary}`. Ultra classifies applicability against the profile (`applies / does_not / unclear`) and drafts a merchant-language to-do with citations. Output is a FastAPI page on a Serverless Endpoint: dated change feed and a "what to do this month" panel. Seed with two regulations you already know cold: the withdrawal function (Directive 2023/2673, §356a BGB, in force 19 June 2026) and EUDR SME enforcement (30 June 2026). Demo profile: a German furniture or leather-goods store shipping EU-wide.

Why Tavily is load-bearing rather than decorative: domain-filtered, date-windowed search plus extract is the whole ingestion layer, and every to-do carries the URL it came from. That is the sentence to put in the "Best Use of Tavily" pitch.

## 5. Evaluation plan — the number

**SWE-rebench (public, reproducible).** Nebius preloads SWE-bench Verified, SWE-rebench and SWE-rebench-V2 environments. `mini-swe-agent[contree]` supports `--environment-class contree`, but its `ContreeEnvironment` cannot yet take a custom model client, so plan for a thin harness of our own: for each task, `images.use(task_image)` → Planner → Implementer → Verifier → run the task's hidden tests. Report resolved rate, verifier false-PASS rate (verifier said PASS, hidden tests failed), theater-check catch rate, and cost per task. Sample 30 tasks in W4, 50 in W5, fixed seed, list the task IDs in the README.

**Fabric campaign (private repo, public log).** ~10 issues from the Hunter plus the 2026-09 backlog. Report issues found, confirmed, fixed, PASS / PASS-WITH-CONCERNS / FAIL, PENDING-HUMAN count, total cost. The checked-in example run is one of these.

**Honesty rule.** A lower implementer pass-rate with zero verifier false-PASS is a better story than a high pass-rate you can't prove. Report both. Past NVIDIA hackathon judges explicitly preferred measured numbers to polished concept demos.

## 6. Week-by-week runbook

Each week has an exit criterion. Miss it by more than two days and apply the drop order: best-of-N first, then shrink RegWatch to one regulation, never the eval run.

**W1 · Sep 15–21 · Spike.** Exit: theater-check screenshot exists.

- Mon: redeem `NEBIUS-DEVPOST-GLOBAL26`; Builders Program for +$25 TF and $25 Tavily; Token Factory API key; request Sandboxes beta access (base URL + project id); Discord `#sandboxes`: ask pricing, and whether images persist through the Dec 1–15 judging window. Register for Berlin B&B Sep 29. Create `warden` repo, MIT, README stub, FEEDBACK.md.
- Tue: hello sandbox (`images.use("ubuntu:latest").run("echo hi")`), fork test (checkpoint → two divergent branches), time each op, note in FEEDBACK.md.
- Wed–Thu: Fabric base image with Postgres + Redis + migrations + seed; in-sandbox pytest count == Mac count. Tag by git sha.
- Fri: one Ultra plan with `Plan` schema on a closed issue from PR #211; one Tavily lookup with the URL in the plan; diff against the human plan comment.
- Sat: Super implementation in a fork with `read_file` / `write_file` / `run_shell`; Lightning for tool selection. New tests pass on candidate.
- Sun: theater check on that candidate: fork base, new tests only, must fail. Screenshot the two results side by side. Log cost.

**W2 · Sep 22–28 · Loop.** Exit: `warden fix --issue N` produces a PR with an evidence table, unattended, under $3.

- Planner / Implementer / Verifier as typed steps over `Run`; `events.jsonl` + `cost.jsonl`; verifier gate (suite, mypy, ruff, alembic, openapi); PR body generator; `examples/` recorded run; CI replaying it from fixtures.

**W3 · Sep 29–Oct 5 · Verifier + Hunter v1.** Exit: first PASS-with-evidence on a real open Fabric issue, and the Hunter files one confirmed P0 with a repro on its own.

- Mon Sep 29: Berlin B&B — collect the credits, talk to the Nebius DevRel about Sandboxes, note names for the feedback doc.
- Race probe with threads/Barrier; migration round-trip; PENDING-HUMAN labelling; Hunter axes: race, IDOR, money invariants against the seeded demo tenant inside a fork; known-issues baseline loader.

**W4 · Oct 6–12 · Waves + eval harness.** Exit: 10 SWE-rebench tasks run end-to-end with a results.csv.

- Wave planner from the file-conflict graph; integrator with `merge --no-ff` on the integration image; SWE-rebench harness; first 10 tasks; fix the harness, not the model. Decision point Oct 10: best-of-N in or out.

**W5 · Oct 13–19 · The number + console.** Exit: 50-task results table in the README; console live on a Serverless Endpoint.

- Full eval run (budget it first — see §8); Fabric batch of ~10 issues; console viewer deployed; README rewritten for judges with the routing table and Sandboxes section above the fold. **Oct 17: start RegWatch** — profile schema, Tavily ingestion for the two seed regulations, Lightning normaliser.

**W6 · Oct 20–29 · Ship both.** Exit: both submitted Oct 28.

- Oct 20–23: RegWatch Ultra classifier + page + Serverless Job schedule; demo profile seeded.
- Oct 24: RegWatch demoable. Freeze Warden features; bug-fix only.
- Oct 25–26: both videos (scripts in §7); FEEDBACK.md → submission field.
- Oct 27: dry-run both demo URLs and both READMEs from a clean machine; check licenses visible at top level.
- Oct 28: submit both on Devpost; disclose Berlin attendance; confirm the "new project" statement (the pipeline existed as a Claude Code skill, not shippable code).

## 7. Submission runbook

**README skeleton (Warden).** Title + one-line thesis; 60-second GIF of the branching tree; "Runs on Nebius" section (Token Factory models with IDs, Sandboxes, Serverless Endpoint) and "NVIDIA models" routing table — both above the fold because the rules require the README to "highlight NVIDIA model usage and Nebius integration"; Results table (SWE-rebench: resolved %, false-PASS %, theater catches, $/task; Fabric campaign); How it works (the §3.1 diagram); Quickstart (`uv sync`, env vars, `warden fix --issue`); Example run link; Limitations (beta caps, Nemotron vs frontier gap, private test repo); License.

**Video script — Warden (2:45).** 0:00 the problem in one sentence over a screenshot of a double-posted voucher · 0:15 Hunter fires eight parallel finalize calls, psql shows three vouchers, an issue appears · 0:45 Planner output with a Tavily citation · 1:05 Implementer in the candidate fork, tests going green · 1:30 the branching tree: base with new tests failing, candidate passing, race probe returning one voucher · 2:05 PR evidence table, PENDING-HUMAN label · 2:20 results table and cost · 2:35 model routing slide and "runs on Nebius Token Factory Sandboxes".

**Video script — RegWatch (2:00).** 0:00 a merchant's fine notice in one sentence · 0:15 store profile · 0:30 Tavily ingestion with domain filter visible · 0:55 Ultra's applicability verdict and to-do with citations · 1:25 the monthly page · 1:45 stack slide.

**Feedback field.** Concrete, reproducible, with timings: sandbox op latency, what the beta docs lacked (pricing, process model for services in an image), the mini-swe-agent client limitation, Nemotron structured-output behaviour vs the docs' "JSON mode" tag, anything Lightning did well or badly as a tool router. Ten bullets with numbers beats a paragraph of praise.

**Devpost form.** Track; demo URL (console for Warden, page for RegWatch); repo URL; video URL; feedback; Builders & Brews city (Berlin); statement that the project is new work.

## 8. Cost budget

| Item | Estimate | Note |
|---|---|---|
| Credits available | $25 + $25 TF, $25 Tavily, ~$200 from Berlin | plus $1 trial |
| W1–W3 development calls | $20–30 | mostly Lightning and Super |
| Fabric campaign (10 issues × $2) | $20 | Ultra capped to plan + verdict |
| SWE-rebench 50 tasks × ~$1.50 | $75 | run once, W5; 10-task pilot in W4 ≈ $15 |
| RegWatch | $10 | Lightning for extraction; Ultra once per change |
| Sandboxes | unknown (beta) | ask in W1; assume compute is metered |
| Buffer | $40 | if the beta bills sandboxes, cut the eval to 30 tasks |

Approximate list prices used: Ultra $1 / $3 per M tokens in/out, Super ~$0.30 / $0.80–0.90, Lightning $0.06 / $0.24.

## 9. Risk register

| Risk | Signal | Mitigation |
|---|---|---|
| Sandboxes beta breaks during judging | forum: no guarantees given | console serves recorded runs; video shows live runs; checked-in example run |
| Postgres won't run inside a sandbox image | W1 Wed–Thu | managed Postgres per wave via `DATABASE_URL` |
| Nemotron structured output unreliable for `Plan` | W1 Fri | schema in prompt and `response_format`; retry with Super; fall back to `json_object` + pydantic validation |
| Implementer burns turns | tool_turns > 25 | abort, mark FAIL, count it — it's data |
| Ultra cost blowout | Ultra > 40% of spend | planning → Super |
| mini-swe-agent can't use Token Factory client | documented | own thin harness (already planned) |
| Solo capacity (~55 build-days in 46) | any weekly exit missed by >2 days | drop order: best-of-N → RegWatch scope → never the eval |
| Fabric is a private business codebase | — | keep private; public example run + public SWE-rebench number |
| Crowded coding track (~5k registrants, several "evidence-bound" agents already public) | — | differentiate on the Hunter and on the measured false-PASS rate, not on UI |

## 10. Self-scoring checklist before submitting

Score each 1–5 as a hostile judge would; fix anything under 4.

- Implementation: can a judge see three Nemotron models, Sandboxes forks, and a Nebius deployment in the first screen of the README?
- Design: can a judge understand one complete run from the PR body alone?
- Impact: is there a specific person, a specific wrong number, and a specific cost of getting it wrong?
- Idea: is there one thing in the demo the judge has not seen in twenty other coding-agent entries (the Hunter's race probe; the theater check)?
- Number: is there a table with task IDs, a resolved rate, a false-PASS rate, and a dollar figure?
- Rules: MIT at top level, video under 3:00 on YouTube, demo URL works from a clean machine, feedback written, track chosen, Berlin disclosed.
