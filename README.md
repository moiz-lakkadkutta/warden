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
| Existing tests | base and candidate, with the **base's** test files | a test green on base is red on the candidate |
| Spec review | one Ultra call over the issue + diff | a behaviour the issue states is not implemented, *and* the model's quote of it is found verbatim in the issue |

Inconclusive results (collection errors, timeouts, unquotable claims) are concerns, never proof.
Checks a repo cannot run are listed as **not verified**, never shown as green. Money / tax /
schema / security changes are labelled `PENDING-HUMAN`. Warden never merges.

## Results

SWE-rebench pilots, the same 10 tasks (`warden eval --n 10 --seed 7`, $1/task cap, 40 tool
turns; pilot 3's last two tasks ran at $0.40 to stay inside its budget). Every row is in [`evals/swe_rebench/results.csv`](evals/swe_rebench/results.csv) (pilot 2)
and [`evals/swe_rebench/pilot-3/results.csv`](evals/swe_rebench/pilot-3/results.csv) (pilot 3); the
task ids are in [`task_ids.txt`](evals/swe_rebench/task_ids.txt). "Resolved" means the dataset's
hidden tests pass, and Warden never sees those tests.

| metric | pilot 2 | pilot 3 (+ regression check, spec review) |
|---|---|---|
| Resolved | **4 / 10** | **1 / 10** |
| Verifier PASS that hidden tests reject (false PASS) | 3 of 5 PASS | 1 of 1 PASS |
| Verifier FAIL on a fix hidden tests accept (false FAIL) | 1 of 5 FAIL (ran out of turns before writing a test) | 1 of 8 FAIL (ran out of turns; no test) |
| Unverified (FAIL only because the suite timed out) | 1 | 0 |
| Harness error | 0 | 1 (Sandboxes API 500; retry added) |
| Theater catches (new test already passes on the unfixed base) | 1 | 0 |
| Cost | **$0.13 / task**, $1.33 total | **$0.18 / task**, $1.78 total |

What pilot 3 shows about the new checks: the regression check stopped one false PASS (planet: the
fix broke catalog requests, and the hidden tests fail there too). It missed one: on spikeinterface
it did not select the module's own test file, so it never ran. On pamqp it flagged tests the
upstream fix changes on purpose. The resolved count fell from 4 to 1, and that is not the
verifier, because hidden tests grade the fix. The implementer code was unchanged, and Super's tool
use varied run to run (one clobbered a 19 kB file with a 309-byte fragment; 7 of 10 runs used all
40 turns). The same task resolves in some runs and not in others. At 10 tasks and one run each,
these numbers are a pilot, not a benchmark; details in `docs/status.md` §1e.

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
- SWE-rebench issues are often under-specified: a hidden test can require an interface the issue
  never names. A verifier that only reads the issue cannot catch those, and the false-PASS rate
  is reported with that in mind, not hidden.
- End-to-end runs so far target one codebase, [Fabric-Inventory](https://github.com/moiz-lakkadkutta/Fabric-Inventory)
  (FastAPI + Postgres ERP); the SWE-rebench sample is the cross-repo evidence.
- Nemotron vs frontier models: we report what these models do on our harness and do not compare
  to other leaderboards.

## Feedback on the tooling

[FEEDBACK.md](FEEDBACK.md): dated, measured notes on Sandboxes, Token Factory and Nemotron.

## License

MIT
