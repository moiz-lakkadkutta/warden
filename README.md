# Warden

**Hunt → Plan → Fix → Verify → PR.** An autonomous coding agent that fixes an issue test-first in
a forked Nebius Sandbox, then has a second, adversarial pass prove the new tests are real, the old
tests still pass, and the diff does what the issue says, before a draft PR is opened. It runs on
**Nebius Token Factory Sandboxes** with **NVIDIA Nemotron 3 Ultra / Super / 3.5 Lightning**.

```bash
uvx warden doctor            # checks tools + keys, no network
uvx warden run --demo        # the whole loop on a fake sandbox, no credentials, ~2 s
uvx warden console           # the run's timeline + evidence table
```

## What it has done (real runs, not demos)

- **[Fabric-Inventory#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221)**: a
  closed ERP issue (#196, a money bug in invoice totals) was re-solved unattended from the issue
  text alone, with comments hidden, on the code as it was before the human fix. **24 min, $0.17.**
  Draft PR labelled `PENDING-HUMAN`. The target repo's own CI: **7/7 jobs green.**
- The theater check on that run: the new repro test **fails on the unfixed base**
  (`10498.95 == 0`) and passes on the candidate ([screenshot](docs/spike-A7.png)).
- SWE-rebench on Nebius's preloaded task images: see [Results](#results).

## Runs on Nebius

| Piece | How Warden uses it |
|---|---|
| Token Factory (OpenAI-compatible) | every model call; JSON-schema structured output; function calling |
| Token Factory **Sandboxes** | every issue is a fork of a base checkpoint (~1 s). The **theater check** forks the *unfixed* base and runs only the new tests (they must fail). The **regression check** runs the base's own tests on both forks. The gate runs on disposable forks, so the candidate is never modified |
| Preloaded SWE-rebench images | `warden eval` plans, fixes and verifies on the task image, then grades with the hidden tests the agent never saw |

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
| Spec review | one Ultra call over the issue + diff | a behaviour the issue states is not implemented, *and* the model's quote of it is found verbatim in the issue |

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
spec behaviour never retries.

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
  ranked below a FAIL, so a better ranker would reach 12.
- **False PASSes are mostly the benchmark, not the verifier.** Of the 10, 8 need an API, message
  or naming choice that the issue never states (yarl's hidden test even rejects what the issue
  asks for). 2 are verifier misses: the repro used a different exception class (pre-commit) or
  resource kind (checkov) than the issue's own example, so it "reproduced" something else. Six of
  the 10 were candidate 1 passing and stopping early, at $0.08–$0.54.
- **Theater catches:** 14 new tests already passed on the unfixed base and were not counted as
  evidence.
- **No harness errors** after the transport retry; the first pass lost 7 of 49 tasks to
  Sandboxes poll timeouts, which were re-run (`docs/status.md` §1j).

Earlier 10-task pilots (4/10 and 1/10 resolved at $0.13–$0.18 per task, one candidate) are in
[`evals/swe_rebench/results.csv`](evals/swe_rebench/results.csv) and
[`evals/swe_rebench/pilot-3/`](evals/swe_rebench/pilot-3/results.csv), with notes in `docs/status.md` §1d–1e.

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
publishes to GitHub Pages. A per-issue cost cap stops the run cleanly, and
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

- The Hunter (finding bugs before fixing them) is designed but not yet shipped; issues come from
  GitHub or the benchmark.
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
