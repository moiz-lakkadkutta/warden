# Warden

**Hunt → Plan → Fix → Verify → PR.** An autonomous coding agent that fixes an issue test-first in
a forked Nebius Sandbox, then has a second, adversarial pass prove the new tests are real, the old
tests still pass, and the diff does what the issue says, before a draft PR is opened.

![warden doctor, warden run --demo and warden console in a terminal with no credentials](docs/demo/hero.gif)

```bash
uv tool install git+https://github.com/moiz-lakkadkutta/warden   # not on PyPI: `warden` there is another project
warden doctor          # checks tools + keys, no network
warden run --demo      # the whole loop on a fake sandbox, no credentials, a few seconds
warden console         # the run's timeline, branch tree and evidence table as one HTML page
```

**Live console** on a Nebius Serverless AI endpoint:
<https://port8080-h79fp774t88b2wc.tunnel.applications.eu-west2.nebius.cloud/> · the run behind
draft PR #221, with its branch tree:
<https://port8080-h79fp774t88b2wc.tunnel.applications.eu-west2.nebius.cloud/20261010-123442-279b/>

**Headline numbers** (real runs; details and sources below):

| Measure | Result |
|---|---|
| SWE-rebench, 49 graded tasks, hidden tests Warden never sees | **18 / 49 resolved** (37%) at **$0.49 / task** (E2; E1 was 11 / 49) |
| Verifier PASS precision on those 49, E1 → E2 | **29% → 48%** (4 of 14 → 13 of 27) |
| A closed money bug in a real ERP, re-solved from the issue text | draft PR [#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221): $0.17 of model spend, target CI 7/7 green |
| Fabric campaign, 9 issues | **1 of 9 PASS** ($7.70): not yet an autonomous fixer on that codebase |

**Runs on Nebius, with NVIDIA models:** Token Factory serves every model call · Sandboxes fork
every issue and run the theater check on the unfixed base · a Serverless AI endpoint hosts the
console · **Nemotron 3 Ultra** plans and judges, **Nemotron 3 Super** implements, **Nemotron 3.5
Lightning** parses. [Details ↓](#runs-on-nebius)

## Try it

**In one minute, no keys or accounts:**

1. Open the **live console** (Nebius Serverless AI endpoint): <https://port8080-h79fp774t88b2wc.tunnel.applications.eu-west2.nebius.cloud/>. The run behind draft PR
   #221 shows its phase timeline, sandbox fork tree, per-call cost and evidence table:
   <https://port8080-h79fp774t88b2wc.tunnel.applications.eu-west2.nebius.cloud/20261010-123442-279b/>
   If the endpoint is ever down, the same pages are mirrored on GitHub Pages:
   <https://moiz-lakkadkutta.github.io/warden/>
2. Run the whole loop locally on a fake sandbox (needs [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv tool install git+https://github.com/moiz-lakkadkutta/warden
   warden doctor          # what's installed, what's missing and how to fix it (no network)
   warden run --demo      # replays a recorded run: every phase, running cost, evidence table
   warden console         # the same run as an HTML page
   ```

3. Read the real output: draft PR
   [Fabric-Inventory#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221) (evidence
   table, `PENDING-HUMAN` label, the target repo's CI 7/7 green) and the issues the Hunter filed,
   [#225](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/225) and
   [#227](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/227).

**A live run on Nebius** needs a Token Factory key, a Nebius project id and **Sandboxes beta
access** (`CONTREE_BASE_URL`); a Tavily key is optional. One SWE-rebench task, on Nebius's
preloaded image, graded by hidden tests, spend capped at $1:

```bash
git clone https://github.com/moiz-lakkadkutta/warden && cd warden && uv sync --all-extras --dev
cp .env.example .env          # fill NEBIUS_API_KEY, NEBIUS_PROJECT_ID (and TAVILY_API_KEY)
uv run warden doctor --live
uv run warden eval --n 1 --budget 1 --out runs/try -y
uv run warden console         # open the page it writes
```

## What it has done (real runs, not demos)

- **[Fabric-Inventory#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221)**: a
  closed ERP issue (#196, a money bug in invoice totals) was re-solved unattended from the issue
  text alone, with comments hidden, on the code as it was before the human fix, for **$0.17** of model spend.
  Draft PR labelled `PENDING-HUMAN`. The target repo's own CI: **7/7 jobs green.**
- The theater check on that run: the new repro test **fails on the unfixed base**
  (`10498.95 == 0`) and passes on the candidate ([screenshot](docs/spike-A7.png)).
- **Hunter (IDOR / cross-tenant axis), live on the seeded demo tenant:** `warden hunt --axis idor`
  boots Fabric in a sandbox fork, signs up two tenants, enumerates id-taking endpoints from
  `/openapi.json` and probes cross-org reads and cross-firm writes — reproducing twice before it
  files. On current Fabric it **finds no cross-tenant hole and files nothing**: org-level reads are
  isolated (422) and the firm-in-org guard holds across 8 `firm_id` endpoints, independently
  confirming the Wave-B isolation fix. Deterministic — **$0 of model spend** per hunt.
- **Hunter replay at the pre-fix commit (`--at d9c37cb^`):** it confirmed the cross-firm write on 6
  endpoints (2× each) and filed
  [Fabric-Inventory#225](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/225), labelled
  as a regression of the register entry and as already fixed on main. The fix step did **not**
  pass: across 3 attempts ($1.49) the Super candidates guarded the router layer, not the service
  layer where the human fix lives. Their repro tests stayed red, so the verifier rejected them and
  **no PR was opened**. A second try put the decoded attacker-token claims in the issue and filed
  `/parties` alone ([#227](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/227)). That
  candidate guarded the service layer, and its repro failed on the unfixed base as required. It
  still came back **FAIL ($0.52)**: it ran out of turns before calling `finish`, and one test that
  is red on the base in replays also failed. No PR.
- SWE-rebench on Nebius's preloaded task images: see [Results](#results).

## Runs on Nebius

| Piece | How Warden uses it |
|---|---|
| Token Factory (OpenAI-compatible) | every model call; JSON-schema structured output; function calling |
| Token Factory **Sandboxes** | every issue is a fork of a base checkpoint (~1 s). The **theater check** forks the *unfixed* base and runs only the new tests (they must fail). The **regression check** runs the base's own tests on both forks. The gate runs on disposable forks, so the candidate is never modified |
| Preloaded SWE-rebench images | `warden eval` plans, fixes and verifies on the task image, then grades with the hidden tests the agent never saw |
| **Serverless AI endpoint** + Container Registry | the run console ([live](https://port8080-h79fp774t88b2wc.tunnel.applications.eu-west2.nebius.cloud/)): `console/` is a read-only FastAPI app over the rendered runs (`/api/runs`, `/api/runs/<id>/events`), shipped as a non-root image to Nebius Container Registry and run on a `cpu-d3` endpoint by [`ops/deploy-console.sh`](ops/deploy-console.sh) |

## NVIDIA models

| Role | Model | Why |
|---|---|---|
| Plan, verdict (spec review) | `nvidia/Nemotron-3-Ultra-550b-a55b` | reasoning and adversarial judgement |
| Implement (tool-calling loop) | `nvidia/nemotron-3-super-120b-a12b` | 19 of 19 tool calls well-formed on the first Fabric fix; a third of Ultra's cost |
| Research queries, parsing | `nvidia/Nemotron-3_5-Lightning` | cheapest; structured output for small tasks |

Models are bound by env (`WARDEN_*_MODEL`) and swap without code changes.

## How the verifier decides

The implementer's word is never evidence. A candidate is **FAIL** unless every row holds:

| Check | Where | Fails when |
|---|---|---|
| Theater | fork of the unfixed base + only the new test files | every new test passes without the fix |
| Gate | disposable forks of the candidate | suite red, or a configured lint / type / migration / OpenAPI check red |
| Existing tests | base and candidate, with the **base's** test files named after or mentioning a changed module | a test green on base is red on the candidate (no related test file: not verified, and a concern) |
| Spec review | one Ultra call over the issue + diff | a behaviour the issue states is not implemented, or the new tests use a look-alike of the issue's own artefact (another exception class than its traceback's, another YAML `kind`, another command), *and* the model's quote of it is found verbatim in the issue |

Inconclusive results (collection errors, timeouts, unquotable claims) are concerns, never proof.
Checks a repo cannot run are listed as **not verified**, never shown as green. Money / tax /
schema / security changes are labelled `PENDING-HUMAN`. Warden never merges.

**Theater retry** (`WARDEN_THEATER_RETRIES`, default 1; 0 = off). When the *only* FAIL is the
theater check (every new test passes on the unfixed base, or fails there for the wrong reason /
still fails with the fix) and everything else is green, the implementer continues once more from
its candidate checkpoint with the per-test reasons and the base pytest output, keeps the fix and
writes a test that bites (it can run its tests on the unfixed source with `run_tests_unfixed`),
within min(20, max turns / 3) extra turns and the same per-issue cost cap. Verify then reruns;
the verdict is the last attempt's and says "repro found on retry 1", and the first attempt's
verdict, candidate and evidence stay as `*.attempt1.*`. A red suite, a regression or an unmet
spec behaviour never takes the theater retry; that is the verify retry's case.

**Verify retry** (`WARDEN_VERIFY_RETRIES`, default 1; 0 = off). When the theater check *proves*
the repro (a new test fails on the unfixed base and passes with the fix) but the suite, mypy /
ruff, a migration / OpenAPI check, the regression check or the spec review is red, the
implementer continues once from its candidate checkpoint with a compact note: the failing test
node ids with pytest's one-line error, mypy / ruff errors as `file:line`, the regressed tests,
the unmet behaviours, and "keep the fix and the repro; do not edit, skip or silence tests or
checks". It gets min(8, max turns / 3) extra turns within the same per-issue cap, and is skipped
(logged as `verify_retry: skipped`) when less than 20% of the cap is left, so a near-cap
candidate keeps its FAIL rather than becoming an ERROR. Verify then reruns every check
unchanged. Fabric #196 and #206 (`camp-replay-212-d`) were this case: valid repro, 2 suite
failures plus mypy / ruff errors. The verdict carries `verify_retry: 1`, the events
`verify_retry: sent / start / outcome`, and the eval CSV a `verify_retry` column
(`1:PASS;3:FAIL` per candidate).

**Guard on the main behaviour.** The spec review maps each stated behaviour to the new tests
that exercise it, main behaviour first. When every test mapped to the main behaviour passes on
the unfixed base (a guard, not a repro) the candidate is at most PASS WITH CONCERNS, even when
another new test reproduces (E1 checkov-831: a `kind: Pod` test reproduced, the workload test
was a guard). That verdict takes the same one theater retry, asking for a test of the issue's own
scenario: if the fix does not handle it, the new test stays red with the fix and the verdict is
FAIL. PASS WITH CONCERNS never stops best-of-N early, so later candidates still run.

**Best-of-N** (`WARDEN_CANDIDATES=N`, `warden eval --candidates N`; default 1). After one plan,
the implementer runs up to N times, each candidate on its own fork of the same base checkpoint,
and every candidate goes through all four checks above. Candidates run one after another and
differ by temperature (0.1, 0.5, 0.8) and by a note telling candidate k how the verifier judged
the earlier ones. The verifier then picks one by a fixed rule, first difference wins:

1. verdict: PASS > PASS WITH CONCERNS > FAIL only because a check could not finish > FAIL
2. a candidate that changed the code beats an empty diff (an empty diff cannot fix anything;
   on E1's pamqp two empty FAILs outranked the 2-line fix the hidden tests accept)
3. a new test that fails on the unfixed base (theater check) beats one that did not
4. fewer regressions + unmet spec behaviours, then fewer concerns
5. smaller diff (lines added + removed), then lower cost, then the earlier candidate

A clean PASS (PASS with a theater-proven test) stops the run early: later candidates are skipped,
since only a smaller diff could outrank it. (The catch: a false PASS stops the run too, so
best-of-N rescues runs that end in FAIL, not the verifier's own false PASSes.) A candidate is also skipped when the spend so far plus
the costliest candidate so far would pass the per-issue cap; the cap covers all candidates
together and is never caught. `verdict.json` lists every candidate with its verdict and why the
winner won; the PR evidence table says "chosen from N candidates"; every event carries its
`candidate` index, so the console draws one branch per candidate.

## Results

E1: 50 SWE-rebench tasks (`warden eval --n 50`, sample in
[`evals/swe_rebench/e1/task_ids.txt`](evals/swe_rebench/e1/task_ids.txt); one base cannot be graded and is
excluded), run as 5 shards with 3 candidates per task, a $1.50 per-task cap and 60 tool turns.
Every row is in [`evals/swe_rebench/e1/`](evals/swe_rebench/e1/). "Resolved" means the dataset's
hidden tests pass, and Warden never sees those tests. Regenerate the table with
`warden report evals/swe_rebench/e1/results.shard-*.csv --md --label E1 --write-readme`.

<!-- results:start -->
| metric | E1 |
|---|---|
| Resolved | **11 / 49** (22%, 95% CI 13–36%) |
| Verifier PASS precision (PASS the hidden tests accept) | 4 of 14 PASS (29%) |
| Clean PASS precision (PASS without concerns) | 4 of 11 PASS (36%) |
| Verifier PASS that hidden tests reject (false PASS) | 10 of 14 PASS |
| Verifier FAIL on a fix hidden tests accept (false FAIL) | 6 of 35 FAIL |
| Unverified (FAIL only because the suite timed out) | 1 |
| Theater catches (new test already passes on the unfixed base) | 14 |
| Errors (run did not reach a verdict) | 0 |
| Cost | **$0.54 / task**, $26.41 total |
| Median wall-clock per task | 14 min 21 s |
| Mean tool turns (runs that reached a verdict) | 47.7 |
| Excluded (base cannot be graded; see excluded.txt) | 1 |
<!-- results:end -->

What E1 shows:

- **Best-of-N earns its cost.** Candidate 1 alone passes the hidden tests on 7 tasks; picking
  among three resolves 11. On one task (pamqp) a candidate that passes the hidden tests was
  ranked below an empty-diff FAIL, because a cut test file read as 237 regressions (fixed
  below), so a better ranker would reach 12.
- **False PASSes are mostly the benchmark, not the verifier.** Of the 10, 8 need an API, message
  or naming choice that the issue never states (yarl's hidden test even rejects what the issue
  asks for). 2 are verifier misses: the repro used a different exception class (pre-commit) or
  resource kind (checkov) than the issue's own example, so it "reproduced" something else. Six of
  the 10 were candidate 1 passing and stopping early, at $0.08–$0.54.
- **Theater catches:** 14 new tests already passed on the unfixed base and were not counted as
  evidence.
- **No harness errors** after the transport retry; the first pass lost 7 of 49 tasks to
  Sandboxes poll timeouts, which were re-run (`docs/status.md` §1j).

**E1 + verifier fixes** (2026-10-16): the 14 tasks E1 marked PASS plus pamqp, re-run with the same
settings after four fixes: the spec review checks the issue's own artefact; a main behaviour
tested only by a guard is PASS WITH CONCERNS and takes the theater retry; the regression check
restores test files from git (a 64 KiB stdout cut caused pamqp's wrong pick), and an empty diff
ranks last. Rows: [`evals/swe_rebench/e1-precision/`](evals/swe_rebench/e1-precision/);
`e1-before.csv` holds E1's rows for the same 15 tasks. Both tables come from `warden report`:

| metric | E1 (same 15) | E1 + verifier fixes |
|---|---|---|
| Resolved | **4 / 15** (27%, 95% CI 11–52%) | **5 / 15** (33%, 95% CI 15–58%) |
| Verifier PASS precision (PASS the hidden tests accept) | 4 of 14 PASS (29%) | **5 of 8 PASS (62%)** |
| Clean PASS precision (PASS without concerns) | 4 of 11 PASS (36%) | 4 of 7 PASS (57%) |
| Verifier PASS that hidden tests reject (false PASS) | 10 of 14 PASS | 3 of 8 PASS |
| Verifier FAIL on a fix hidden tests accept (false FAIL) | 0 of 1 FAIL | 0 of 7 FAIL |
| Cost | $0.30 / task, $4.55 total | $0.37 / task, $5.54 total |

- Both verifier misses are now right: pre-commit's first two candidates FAILed on the
  artefact rule, and the third, told why, caught `pre_commit.util.CalledProcessError` and
  resolves. Checkov's candidate tested `kind: Deployment` and resolves.
- 3 of the 4 correct PASSes held (sqlparse, pennylane, pyjwt). setup-cfg-fmt was lost to the
  implementer, not the verifier: all 3 candidates used the 60 turns (99 unchanged re-reads) and
  none passes the hidden tests, so FAIL is the right verdict.
- 5 former false PASSes are now FAIL, each with no candidate the hidden tests accept. The 3 left
  (crate, yarl, fromager) are the under-specified kind.
- A re-run samples new candidates, so this mixes verifier and implementer changes. A cleaner
  verifier-only check reran the new spec review on E1's own chosen candidates: it flags both
  misses, keeps all 4 correct PASSes, and leaves the other 8 under-specified false PASSes alone
  (`docs/status.md` §1k).
- pamqp: all three candidates produced an empty diff this time, so the new ranking rule had
  nothing to choose between; a unit test replays E1's recorded case.

**E2** (2026-10-19): the same 49 tasks and settings as E1 (5 shards, 3 candidates, $1.50 cap, 60
turns), after everything merged since: the verifier fixes above, the implementer's large-file
tools and edit mode (`docs/status.md` §1l) and the verify retry (§1n). Rows:
[`evals/swe_rebench/e2/`](evals/swe_rebench/e2/). Table:
`warden report evals/swe_rebench/e2 --md --label E2 --baseline evals/swe_rebench/e1 --baseline-label E1`.

| metric | E1 | E2 |
|---|---|---|
| Resolved | **11 / 49** (22%, 95% CI 13–36%) | **18 / 49** (37%, 95% CI 25–51%) |
| Verifier PASS precision (PASS the hidden tests accept) | 4 of 14 PASS (29%) | 13 of 27 PASS (48%) |
| Clean PASS precision (PASS without concerns) | 4 of 11 PASS (36%) | 7 of 19 PASS (37%) |
| Verifier PASS that hidden tests reject (false PASS) | 10 of 14 PASS | 14 of 27 PASS |
| Verifier FAIL on a fix hidden tests accept (false FAIL) | 6 of 35 FAIL | 5 of 21 FAIL |
| Unverified (FAIL only because the suite timed out) | 1 | 0 |
| Theater catches (new test already passes on the unfixed base) | 14 | 10 |
| Errors (run did not reach a verdict) | 0 | 1 |
| Cost | **$0.54 / task**, $26.41 total | **$0.49 / task**, $24.16 total |
| Median wall-clock per task | 14 min 21 s | 14 min 11 s |
| Mean tool turns (runs that reached a verdict) | 47.7 | 41.7 |
| Excluded (base cannot be graded; see excluded.txt) | 1 | 1 |
| Verify retries (valid repro, red gate / regression / spec: sent back once) | — | 13 candidates, 3 then PASS |

- **Resolved 11 → 18:** 8 tasks gained, 1 lost (pyjwt). The confidence intervals overlap, and a
  re-run samples new candidates, so this is a likely gain, not a proven one.
- "Resolved" counts the chosen candidate passing the hidden tests, whatever the verdict. Warden
  would have opened a correct draft PR on **13** tasks (a PASS the hidden tests accept); the
  other 5 were false FAILs, where the right fix opened nothing.
- **PASS precision 29% → 48%** across all 49. That is lower than the 62% on the 15-task re-run
  above, which was the set of E1 PASSes. 14 false PASSes remain, most of them the
  under-specified kind.
- **Verify retry:** 13 candidates were sent back once and 3 then passed: 2 are correct
  (lightstep, pre-commit) and 1 is a false PASS (pynetdicom).
- 1 error: a Sandboxes `POST /instances` failed with an API status error on virtualenv-2821.

Earlier 10-task pilots (4/10 and 1/10 resolved at $0.13–$0.18 per task, one candidate) are in
[`evals/swe_rebench/results.csv`](evals/swe_rebench/results.csv) and
[`evals/swe_rebench/pilot-3/`](evals/swe_rebench/pilot-3/results.csv), with notes in `docs/status.md` §1d–1e.

### Fabric campaign (a real ERP codebase)

`warden campaign` was run on [Fabric-Inventory](https://github.com/moiz-lakkadkutta/Fabric-Inventory)
(FastAPI + Postgres RLS, double-entry ledger, GST). It plans every issue, groups the issues into
waves by the files they touch, runs fix → verify → draft PR for each wave in parallel, merges the
wave's passing fixes with `git merge --no-ff` in the sandbox, and runs the full gate on the merged
result. Fabric had one open code issue, so 8 of the 9 issues are closed ones, replayed at the
commit before the human fix from the issue text alone. Per-issue rows, notes and run ids are in
[`evals/fabric/campaign.md`](evals/fabric/campaign.md).

| found | confirmed | PASS | PASS_WITH_CONCERNS | FAIL | PENDING-HUMAN | PRs | USD |
|---|---|---|---|---|---|---|---|
| 9 | 3 | **1** | 0 | 8 | 8 of 9 | 1 ([#226](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/226), Fabric CI 7/7 green) | **$7.70** (29 attempts) |

Not yet useful as an autonomous fixer on this codebase: 1 of 9. In the first 15 attempts the
implementer (Nemotron 3 Super) never called `finish` in 13. It looped on Fabric's 900-line test
files, re-reading them or re-sending them whole past the 8192-token completion limit. A 60-turn,
best-of-2 retry fixed none of the five it retried.

Warden then changed the implementer's tools (`docs/status.md` §1l):
- 200-line read pages;
- no whole-file rewrite of a file over 300 lines;
- a 16k completion budget;
- "edit mode", which withholds the read tools when the model loops;
- auto-finish of a checked green candidate.

The six #212 issues were re-run twice (`camp-replay-212-c`, `-d`). With edit mode, 5 of 6
candidates edited source code (1 before), cached re-reads fell from 71 to 32, and 2 repros were
valid (1 before). PASS stayed at 0 of 6 in both re-runs: the remaining failures are wrong repros,
suite regressions and lint, not the loop. `camp-replay-212-e` (2026-10-19) retried #196 and
#206 with the verify retry: #196's valid repro went back for 8 turns and stayed red on 2 suite
tests and mypy; #206's repro was invalid, so it did not qualify. Both FAIL, $0.41, no PR. The safety side held:
- No FAIL produced a branch or a PR.
- The theater check rejected eight new tests that failed for the wrong reason.
- The spec review caught a candidate that was green on suite, mypy and ruff but implemented 1 of
  the issue's 4 behaviours.
- Every money or tax issue was labelled `PENDING-HUMAN`.

## How it works

```
issue ─▶ PLANNER (Ultra + Tavily citations, sandbox code context) ─▶ Plan{files, tests, risk_class}
      ─▶ base.fork() ─▶ IMPLEMENTER (Super, tool calls, write the failing test first)
      ─▶ VERIFIER: theater · gate · existing tests · spec review ─▶ Verdict
      ─▶ PR: patch leaves the sandbox via format-patch, pushed from the host; evidence table body
```

Every model call and sandbox operation is appended to `runs/<id>/events.jsonl` with tokens, cost
and wall-clock; `warden console` renders it to one self-contained HTML page plus
`runs/<id>/timeline.gif` (Pillow, `uv sync --extra gif`), and `warden console <id>... --out site`
writes a static site (index + one page per run) that `.github/workflows/console-pages.yml`
publishes to GitHub Pages; the same site runs on a Nebius Serverless AI endpoint
(`console/`, deployed by `ops/deploy-console.sh`). A per-issue cost cap stops the run cleanly, and
`warden resume <run_id>` continues from the last completed phase.

## Quickstart (live)

```bash
git clone https://github.com/moiz-lakkadkutta/warden && cd warden && uv sync --all-extras --dev
cp .env.example .env         # Nebius key + project id, Sandboxes URL, Tavily key
uv run warden doctor --live
uv run warden run --issue 196 --at '43f3851^' --dry-run   # plan only
uv run warden eval --n 10 --seed 7 --budget 5             # SWE-rebench sample, capped spend
```

[examples/demo-issue-201](examples/demo-issue-201) is a recorded `--demo` run; CI replays it offline.

## Use it in your repo

Add the `warden` label to an issue and Warden opens a **draft** PR with the evidence table, then
comments on the issue with the PR link (or why there is none, and what the run cost). Copy this to
`.github/workflows/warden.yml`:

```yaml
name: warden
on:
  issues:
    types: [labeled]
permissions:
  contents: write        # push the warden/fix-* branch
  pull-requests: write   # open the draft PR
  issues: write          # comment on the issue, create the PENDING-HUMAN label
jobs:
  fix:
    if: github.event.label.name == 'warden' && !github.event.issue.pull_request
    runs-on: ubuntu-latest
    timeout-minutes: 90
    steps:
      - uses: moiz-lakkadkutta/warden@main   # pin a commit sha in production
        with:
          nebius-api-key: ${{ secrets.NEBIUS_API_KEY }}
          nebius-project-id: ${{ secrets.NEBIUS_PROJECT_ID }}
          tavily-api-key: ${{ secrets.TAVILY_API_KEY }}
          base-image: ${{ vars.WARDEN_BASE_IMAGE }}   # your repo at /src, test deps installed
          test-command: pytest -q
          max-usd: "2.0"
          # planner-model / implementer-model / tools-model override the Nemotron defaults
```

Set the secrets `NEBIUS_API_KEY`, `NEBIUS_PROJECT_ID`, `TAVILY_API_KEY` and the variable
`WARDEN_BASE_IMAGE`, and create a `warden` label. All inputs are in [action.yml](action.yml).

What it will not do: run for a labeler without write access, run in a fork, check out PR code,
put a token in a sandbox (the patch leaves the sandbox and is pushed from the runner), push a
change to `.github/`, or merge. Issue text reaches the models fenced as untrusted data, and issue
comments are never read. Money / tax / schema / security fixes get `PENDING-HUMAN`. PRs opened
with the default `GITHUB_TOKEN` do not trigger your CI; pass a GitHub App token as `github-token`
if you want CI on them.

## Limitations

- The Hunter (finding bugs before fixing them) covers one axis, IDOR / cross-tenant access, on
  Fabric's API. It finds nothing on current Fabric (the known holes are fixed) and the issues it filed
  (#225, #227) come from a replay at the pre-fix commit; it does not cover the frontend. Other issues come
  from GitHub or the benchmark. **Not built:** the race, money, malformed-input and flow hunt
  axes (`warden/hunt/axes/{race,money,malformed,flow}.py` are signatures only), the
  concurrency check in verify (`warden/verify/race.py`), and a migration
  upgrade → downgrade → upgrade round-trip in the gate (the gate checks a single alembic head).
- **A PASS is evidence, not proof.** SWE-rebench issues are often under-specified: a hidden test
  can require an interface or behaviour the issue never names. In the live check of the latest
  fixes, two of three PASSes had every row green: a repro that fails on base, the related base
  tests green, and every stated behaviour implemented. The hidden tests still rejected them. A
  verifier that reads only the issue cannot catch this. The false-PASS rate is reported, not
  hidden, and every PR is a draft for a human to review.
- **Run-to-run variance is large.** The same task resolves in some runs and not in others:
  pennylane resolved in 4 of 6 graded runs on 2026-10-14; one failure came from the model
  overwriting a 19 kB file with a fragment. Write guards and `replace_in_file` target that.
  Results at n = 10–50 come with confidence intervals, and single-task anecdotes are not results.
- **Best-of-N trades cost for rescues.** Three candidates raise the cost per task (pilot 2:
  $0.13; E1 with N = 3: $0.54). Early stop means a false PASS on candidate 1 is never
  challenged by candidates 2 and 3, so best-of-N rescues FAILs, not false PASSes.
- **Sandboxes beta transport errors.** 7 of 49 E1 tasks first ended in an error on a timed-out or
  dropped `GET /operations/{id}` poll. Runs are now retried on those, and the affected tasks were
  re-run. The numbers are in [FEEDBACK.md](FEEDBACK.md).
- End-to-end runs so far target one codebase, [Fabric-Inventory](https://github.com/moiz-lakkadkutta/Fabric-Inventory)
  (FastAPI + Postgres ERP); the SWE-rebench sample is the cross-repo evidence.
- Nemotron vs frontier models: we report what these models do on our harness and do not compare
  to other leaderboards.

## Feedback on the tooling

[FEEDBACK.md](FEEDBACK.md): dated, measured notes on Sandboxes, Token Factory and Nemotron.

## License

MIT
