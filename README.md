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

SWE-rebench pilot, 10 tasks (`warden eval --n 10 --seed 7`, $1/task cap, 40 tool turns). Every
row is in [`evals/swe_rebench/results.csv`](evals/swe_rebench/results.csv); the task ids are in
[`task_ids.txt`](evals/swe_rebench/task_ids.txt). "Resolved" means the dataset's hidden tests
pass, and Warden never sees those tests.

| metric | pilot 2 (2026-10-14) |
|---|---|
| Resolved | **4 / 10** |
| Verifier PASS that hidden tests reject (false PASS) | 3 of 5 PASS |
| Verifier FAIL on a fix hidden tests accept (false FAIL) | 1 of 5 FAIL (ran out of turns before writing a test) |
| Unverified (FAIL only because the suite timed out) | 1 |
| Theater catches (new test already passes on the unfixed base) | 1 |
| Cost | **$0.13 / task**, $1.33 total |

Pilot 2 ran *before* the regression check and spec review existed. All three false PASSes came
from the implementer fixing its own reading of the issue, which is the failure those two checks
target. The next eval measures them. At 10 tasks these numbers are a pilot, not a benchmark.

## How it works

```
issue ─▶ PLANNER (Ultra + Tavily citations, sandbox code context) ─▶ Plan{files, tests, risk_class}
      ─▶ base.fork() ─▶ IMPLEMENTER (Super, tool calls, write the failing test first)
      ─▶ VERIFIER: theater · gate · existing tests · spec review ─▶ Verdict
      ─▶ PR: patch leaves the sandbox via format-patch, pushed from the host; evidence table body
```

Every model call and sandbox operation is appended to `runs/<id>/events.jsonl` with tokens, cost
and wall-clock; `warden console` renders it. A per-issue cost cap stops the run cleanly, and
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
