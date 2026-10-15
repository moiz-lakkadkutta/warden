# Warden — status (2026-10-12)

## Phase A spike: done (A3–A7 live on Nebius)

| Step | Commit | Result |
|---|---|---|
| A3 hello sandbox | `76345ed` | run/fork ≈ 1 s; fork isolation verified; ContreeBackend fixed for IAM auth |
| A4 fabric-base | `2bd9d55` | Fabric suite **1787 passed** in a sandbox, the same as CI (774 s vs 893 s). Kernel has no SysV IPC → `ops/shmshim.c` LD_PRELOAD shim |
| A5 first plan | `8943441` | Ultra + Tavily + sandbox code context; root cause found once source was in context; [spike-A5.md](spike-A5.md) |
| A6 first fix | `f1dbc1a` | #196 fixed in a fork at the pre-fix commit: same status set as the human fix, 3 new tests green, **$0.15**, 18 turns; [spike-A6.md](spike-A6.md) |
| A7 theater check | `f185f32` | repro test **FAILS on the unfixed base** (`10498.95 == 0`), passes on the candidate; guards pass on both; [spike-A7.png](spike-A7.png) |

Pieces of B2, B3 and C1 landed along the way:
- B2 planner: research, grep-first context with top-file source, pre-fix replay (`rev=`),
  citation filter, preserve-scope rule, 16k completion budget.
- B3 implementer: paged `read_file`, `grep -E`, workdir + service wrap, node-id tests, workdir
  stated in the system prompt.
- C1: a theater check that treats errors as inconclusive, never as proof.

Gate on main: ruff ✓ format ✓ mypy --strict ✓ **241 passed**. Token Factory spend so far ≈ **$1**.

## B5 live: fix branches on Fabric (2026-10-10)

- `warden/integrate/branch.py`: the patch leaves the sandbox via `git format-patch`, and the host
  applies it in a cached clone and pushes it (no token in any box). Branches are
  `warden/fix-<issue>-<run_id>`, plus `warden/base-<sha7>` for replays.
- **[Fabric-Inventory#220](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/220)**: a draft
  replay of #196, labelled `PENDING-HUMAN` (the label was created on Fabric). Fabric's CI on it:
  backend pytest ✓, E2E ✓, frontend ✓, OpenAPI drift ✓, Docker build ✓, **backend lint ✗**
  (ruff W292: no trailing newline in both files). Fixed in Warden's `write_file`; the PR itself
  is left as-is, as evidence of the gap Warden's own gate (B4-live) must catch.
- Moiz's call: fix branches and draft PRs go to Fabric itself.

## Infra state (verified 2026-10-10)

- **GHCR:** `ghcr.io/moiz-lakkadkutta/warden/fabric-base:{8cfc493,latest}`, public, digest
  `sha256:fd0970e4…`. It **includes the shim**.
- **Sandboxes:** `warden/fabric-base:latest` → `ccf8d9d5-a460-347a-85e9-d14b34981be3`, a fresh
  import of that digest. Postgres starts with no patch layer. The old patched layer
  (`99482db2…`) is no longer referenced.
- **Layout inside the image:**
  - repo at `/src`, at Fabric `8cfc493`, with full `.git`;
  - `/app` → `/src/backend`, with its `.venv` on `PATH`;
  - db `test` (migrated, empty) and db `fabric_demo` (seeded);
  - `with-services <cmd>` starts Postgres and Redis around a command.
- **Settings in use:** `WARDEN_WORKDIR=/src`, `WARDEN_SHELL_WRAP="with-services sh -c"`,
  `WARDEN_TARGET_REPO=moiz-lakkadkutta/Fabric-Inventory`.

## E3 console on Nebius Serverless (2026-10-15)

- **URL:** https://port8080-cs9x5ndsvxz54x7.tunnel.applications.eu-west2.nebius.cloud/
  (`/healthz` reports the image build sha).
- **Endpoint** `warden-console` = `aiendpoint-e04v1xybmyd76zq065`, project
  `project-e04w3bxjbc0029g2wvxk5h` (the CLI profile's project, shared with regwatch), eu-west2,
  `cpu-d3` `2vcpu-8gb`, 32 GiB disk, `--auth none`, no public IP (managed https tunnel URL).
  About $0.07/h while running (~$1.7/day). Stop billing: `ops/deploy-console.sh --down`.
- **Registry** `warden` = `registry-e04kcgrdbeq7w1kd5m` →
  `cr.eu-west2.nebius.cloud/e04kcgrdbeq7w1kd5m/warden-console:{<sha>,live}`.
- **Redeploy** after `warden console … --out site`: commit, then `ops/deploy-console.sh` (pushes
  `:<sha>` + `:live`, restarts the endpoint so it re-pulls `:live`; the URL stays the same).
- Docker Desktop on this Mac belongs to another macOS user, so the script assembles the image with
  `crane` (no daemon) when `docker info` fails: same layout as `console/Dockerfile`.

## Start here next session

### 1. B6-live: ✓ Phase B exit (2026-10-10). PASS, draft PR opened unattended

Run `20261010-123442-279b`: `warden run --issue 196 --at 43f3851^ --yes`, capped at $1.
- **24:40 wall clock, $0.17** by Warden's meter. Plan $0.02; fix $0.155 over 20 Super calls and
  19 turns.
- The implementer ran `ruff check` before `finish`, and autofix left nothing for the gate to flag.
- Theater: the repro **FAILS on the unfixed base**. Gate: suite 1508 passed, mypy ✓, ruff ✓, one
  alembic head, no OpenAPI drift. FE codegen is listed as not verified.
- Verdict **PASS** → pushed `warden/fix-196-20261010-123442-279b` onto `warden/base-fb19cde` →
  **[Fabric-Inventory#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221)**
  (draft, `PENDING-HUMAN`, +106/−3, 2 files).
- Paid spend so far: $0.24 (run `…-111217-7c89`, FAIL on ruff) + $0.17 = **$0.41** by Warden's
  meter. Still to do: compare with the Token Factory console (it showed $10.00 before the first
  paid run).
- Fixed after the run: the `Total` line showed $0.15 because the plan step never reported its
  cost to the progress line.

**Next (each paid run needs Moiz's OK):** B2 criterion on two more #212 issues, then C5/C6
Hunter, the D3 SWE-rebench harness, and E3/E4 console + README.

Fabric's own CI on #221: **7/7 jobs green** (backend lint + mypy, pytest, E2E, frontend lint
and tests, OpenAPI drift, Docker build). #220 failed lint; #221 is what Warden's gate let through.

### 1b. D3: SWE-rebench harness built (2026-10-12), pilot not yet run

- **Images:** Sandboxes already preloads 6,873 `swerebench/…` task images (and 32k SWE-rebench-V2,
  500 SWE-bench Verified). Tag = the dataset's `image_name` + `:latest`. Tasks come from HF
  `nebius/SWE-rebench`, split `filtered` (6,542 rows), via the public datasets-server.
- **`warden eval --n 10 --seed 7 --budget 5`:**
  1. Samples by seed and caches the rows to `evals/swe_rebench/tasks.jsonl`, *without* the gold
     patch, plus `task_ids.txt`.
  2. Per task: `prepare_base` adds the `testbed-env` wrapper (the task's conda env first on PATH),
     then plan → fix → verify run with profile `swe`. The gate runs the repo's own `test_cmd` on
     the candidate's test files; mypy, ruff, alembic and OpenAPI are reported as "not configured",
     never as green.
  3. Grading follows the official harness: reset the test files to the base commit, apply
     `test_patch`, run F2P + P2P.
  4. One row per task goes to `results.csv` (`task_id, resolved, verdict, false_pass,
     theater_catch, tool_turns, usd, seconds, error`). A re-run skips finished tasks. It stops
     before a task that could push the total over `--budget`.
- **Checked live with no model calls:** wrapper → Python 3.9 + pytest from the testbed env; the
  unfixed spectree-64 grades not resolved, and with the gold patch, resolved (2.4 s each).
- **Planner:** now generic. A `Layout` (root, ls, grep) replaces the hardcoded `/src` and
  `backend`, and the repo description moved to `prompts/repos/{fabric,generic}.md`.
- **Next:** the 10-task pilot, ≈ $2–5 at #196's $0.17/task, capped at $1/task. **It needs Moiz's
  OK.** Then E1 (50 tasks).

### 1c. D3 pilot (2026-10-14): 6 of 10 tasks before the $5 budget stopped it

`warden eval --n 10 --seed 7`, cap $1/task. Results are in
`evals/swe_rebench/attempts/results-2026-10-14-pilot-1.csv`; the three stopped attempts are there too.

| task | verdict | hidden tests | turns | $ |
|---|---|---|---|---|
| MatterMiners__tardis-77 | FAIL | not resolved | 40 (budget) | 0.21 |
| PennyLaneAI__pennylane-5831 | ERROR (cost cap) | — | — | 0.98 |
| SpikeInterface__spikeinterface-2775 | PASS | **not resolved (false PASS)** | 12 | 0.08 |
| astropy__photutils-1956 | FAIL | not resolved | 40 (budget) | 0.53 |
| gmr__pamqp-52 | PASS | **resolved** | 39 | 0.93 |
| iossifovlab__gpf-446 | FAIL | not resolved | 40 (budget) | 0.48 |

- **Resolved 1/6.** Verdicts: 2 PASS (1 false), 3 correct FAILs, 1 cost-cap error. An earlier
  attempt resolved pennylane for $0.08, so variance between runs is large.
- **Cost driver: implementer context growth.** Prompts reach 155–173k tokens by turn 34–40
  because every earlier `read_file` result is re-sent on every call. That is 3.0M prompt tokens
  for pennylane, against 0.6M for tardis, which mostly ran shell commands. Next fix (no spend
  needed): elide old tool outputs from the history.
- **Turn budget:** 3/6 tasks used all 40 turns.
- **Four harness bugs fixed today**, each found by a stopped attempt: class-qualified node ids,
  grading by files (dataset ids are cut at spaces), a `python -m pytest` shim, re-running the
  task install, and sandbox timeouts → exit 124. All 10 unfixed bases were then verified live
  (F2P fails, 606/606 P2P pass).
- **Spend 2026-10-14: $4.12** in total, within the $5 approved.

### 1d. D3 pilot 2 (2026-10-14): history compaction, all 10 tasks for $1.33

Same 10 tasks (`--seed 7`, cap $1/task, 40 turns) after `09f44fd` (old tool output and old
`write_file` content elided from the implementer history). Results: `evals/swe_rebench/results.csv`.

| task | verdict | hidden tests | turns | $ |
|---|---|---|---|---|
| MatterMiners__tardis-77 | PASS | **not resolved (false PASS)** | 32 | 0.09 |
| PennyLaneAI__pennylane-5831 | PASS | resolved | 10 | 0.07 |
| SpikeInterface__spikeinterface-2775 | PASS | **not resolved (false PASS)** | 10 | 0.06 |
| astropy__photutils-1956 | FAIL | not resolved | 40 (budget) | 0.22 |
| gmr__pamqp-52 | FAIL | **resolved (false FAIL)** | 40 (budget) | 0.41 |
| iossifovlab__gpf-446 | FAIL | not resolved | 40 (budget) | 0.18 |
| joshuadavidthomas__django-bird-58 | FAIL | not resolved (theater caught) | 35 | 0.12 |
| planetlabs__planet-client-python-978 | PASS | resolved | 14 | 0.09 |
| python-wheel-build__fromager-352 | PASS | **not resolved (false PASS)** | 8 | 0.03 |
| smok-serwis__coolamqp-28 | FAIL | **resolved (false FAIL)** | 10 | 0.05 |

- **Resolved 4/10** (pilot 1: 1/6). **$0.13/task**, $1.33 total (pilot 1: $0.69/task). Peak
  prompt for tardis 10.5k tokens (was 27k). Compaction fixed the cost driver.
- **Verdicts: 3 false PASS of 5 PASS** (tardis, spikeinterface, fromager). The model fixed its own
  reading of the issue; the hidden tests check something the issue text doesn't spell out, or
  arrive with the test patch. This is what `fix/verifier-false-pass` (regression check + spec
  review) targets.
- **2 false FAILs, both "not shown to work", not a rejected verified fix.** pamqp: 40/40 turns,
  no tests written; the leftover diff happens to pass. coolamqp: the new test sits in
  `tests/test_clustering/test_a.py`, which hangs (likely needs an AMQP broker) until the gate's
  1800 s timeout; 3885 s wall-clock for one task. Follow-up: a shorter suite timeout for the
  `swe` profile, and a hung suite reported as inconclusive rather than red. **Done:** `31bbb32`
  (`Verdict.inconclusive`, still FAIL) and the eval's `false_fail` / `unverified` columns with a
  600 s suite timeout; results.csv backfilled, so coolamqp now counts as unverified.
- **Spend 2026-10-14: $5.45** in total ($4.12 before pilot 2 + $1.33).

### 1e. D3 pilot 3 (2026-10-14): regression check + spec review, 1/10 resolved, 0/1 PASS precision

Same 10 tasks (`--seed 7`, 40 turns, cap $1/task; the last two at $0.40/task so the run stayed within
the $3 approval) on `45e0218`, which adds the regression check and spec review to pilot 2's code.
Results: `evals/swe_rebench/pilot-3/results.csv`. A first attempt ran with `.env`'s
`WARDEN_MAX_TOOL_TURNS=25` and was stopped after 5 tasks as not comparable:
`attempts/results-2026-10-14-pilot-3-25turns.csv` (0/5 resolved, pennylane resolved but FAIL, $0.63).

| task | verdict | hidden tests | turns | $ | what decided the verdict |
|---|---|---|---|---|---|
| MatterMiners__tardis-77 | FAIL | not resolved | 40 (budget) | 0.10 | did not finish; regression (56 tests) green, spec 1 met / 1 unclear |
| PennyLaneAI__pennylane-5831 | FAIL | not resolved | 40 (budget) | 0.15 | `exp.py` overwritten by a 309-byte fragment → IndentationError, suite red |
| SpikeInterface__spikeinterface-2775 | PASS | **not resolved (false PASS)** | 10 | 0.07 | theater ✓, 3 tests green, spec 3/3; **regression not run** |
| astropy__photutils-1956 | FAIL | not resolved | 40 (budget) | 0.35 | did not finish, no tests; spec: 1 of 2 behaviours unmet |
| gmr__pamqp-52 | FAIL | **resolved (false FAIL)** | 40 (budget) | 0.32 | did not finish, no tests; regression flagged 10 tests, all in the task's F2P list |
| iossifovlab__gpf-446 | FAIL | not resolved | 40 (budget) | 0.22 | did not finish; suite 6 failed |
| joshuadavidthomas__django-bird-58 | FAIL | not resolved | 10 | 0.04 | no new tests |
| planetlabs__planet-client-python-978 | FAIL | not resolved | 38 | 0.26 | **regression only**: `test_request_catalog_success` red on candidate |
| python-wheel-build__fromager-352 | FAIL | not resolved | 40 (budget) | 0.14 | did not finish (green at turn 10, never called `finish`); regression (45) + spec 3/3 green |
| smok-serwis__coolamqp-28 | ERROR | — | — | 0.12 | Sandboxes API **500** on `GET /operations/{id}` during the theater run → fixed `8632d19` |

- **PASS precision 0/1** (pilot 2: 2/5). Only one PASS, so precision is not measurable yet. What
  changed is that 4 of pilot 2's 5 PASS tasks did not produce a working fix this time.
- **The new checks caught 1 and missed 1, and caused 1 avoidable FAIL.**
  - *Caught:* planet. The fix broke catalog requests. The regression check was the only red row,
    and the hidden tests fail in the same area (`test_catalog_source_*`). That is a prevented false PASS.
  - *Missed:* spikeinterface. The regression check did not run ("no existing test names a changed
    module"). `related_tests` greps file *contents* for the stem `curationsorting`, which is
    case-sensitive. The test file `test_curationsorting.py` only mentions `CurationSorting`, so it
    was never selected, although its name matches. The spec review agreed with the model's reading
    (3/3 met). The hidden `test_curation` fails.
  - *Would have missed:* tardis and fromager were pilot 2's false PASSes. This time both FAILed
    only because the implementer used all 40 turns. Their regression and spec rows were green,
    so a finished run would most likely have been a false PASS again.
  - *False alarm:* pamqp. All 10 "regressions" are tests whose expectations the upstream fix
    changes on purpose (naive → aware datetimes; they are the task's F2P tests). It FAILed anyway
    for not finishing. A regression the issue asks for is a changed expectation, not a break.
- **Resolved 1/10 (pilot 2: 4/10): variance, not a code regression.** 8 tasks ran in one
  process on `45e0218`. Its `warden/fix`, `warden/llm`, `warden/plan` and implementer prompts are
  byte-identical to pilot 2's `09f44fd` (same `prompt_hash 58fdd824ab34`). The last two (fromager,
  coolamqp) ran in a second process at `66f8f84`, which also has `f114db8` (`read_file` takes
  `end_line`). Main moved during the run (`c82cba5`, `1788e85` …), but neither process loaded those
  commits. Turn-by-turn, pennylane (pilot 2: 10 turns, resolved; pilot 3: 40, not resolved) is the
  same up to turn 8. At turn 9 Super `write_file`s a 309-byte fragment over the 19 kB `exp.py`.
  The remaining turns go to recovery: 6 rejected `read_file{end_line: "500"}` calls, 5 reads past
  EOF, `exp.py` read 22 times, then the fragment written again at turn 39. Across today's runs,
  pennylane resolved 3 of 5 graded attempts and pamqp 3 of 4. 7 of 10 tasks hit the 40-turn
  budget (pilot 2: 5). Per-task outcomes swing with sampling (implementer temperature 0.1, plus a
  fresh Ultra plan each run).
- **Spend: $2.40 for pilot 3** ($0.63 stopped 25-turn attempt + $1.78 for the 10 tasks,
  $0.18/task). Within the $3.00 approved. **Day total $7.85.**

### 1f. E1 prepared (2026-10-14): 50-task sample, bases checked, sharded runs, generated table

- **Sample:** `evals/swe_rebench/e1/` (50 tasks, seed 7, no gold patch). It contains all 10
  pilot tasks: `random.sample` draws the same first 10 offsets for n=10 and n=50.
- **Bases:** `uv run python -m warden.eval.bases evals/swe_rebench/e1 --gold --workers 4` (sandbox
  only) writes `bases.csv` and `excluded.txt`. `warden eval` never runs excluded tasks;
  `warden report` lists them apart and does not count them.
- **Two grading bugs found by the base check, now fixed:** sandbox stdout is cut at 64 KiB
  (dataspec-30 lost 77 of its 672 P2P results), and fixture files in `test_patch` were passed to
  pytest ("not found" on a `.yaml`, so the run failed).
- **Run (one process per shard, k=5; `--budget` applies to each shard, not the total):**
  `WARDEN_MAX_TOOL_TURNS=40 uv run warden eval --out evals/swe_rebench/e1 --shard i/5 --budget B --max-usd 1 --yes`
  Each shard writes `results.shard-i.csv`; re-running the same command resumes.
- **Report:** `uv run warden report evals/swe_rebench/e1/results.shard-*.csv --md --label E1`;
  add `--write-readme` to replace the table between the README's `results` markers.

### 1g. D4 best-of-N smoke (2026-10-14): forks, ranking and summed cost confirmed

`warden eval --candidates 3`, 40 turns, on the two pilot-2 misjudgements (scratch dir
`evals/swe_rebench/smoke-d4/`, not a headline result). Cap was $0.65/task to stay inside the
smoke's $2 (an earlier 25-turn attempt was stopped at $0.57).

| task | #1 | #2 | #3 | chosen | resolved | $ |
|---|---|---|---|---|---|---|
| gmr__pamqp-52 | FAIL, 10 regressions, hidden ✓ | FAIL, 29 regressions, hidden ✗ | skipped (budget) | #1 | ✓ | 0.60 |
| python-wheel-build__fromager-352 | clean PASS, hidden ✗ | skipped (early stop) | skipped | #1 | ✗ (false PASS) | 0.07 |

- All three candidates forked the same base checkpoint (the earlier attempt reached #3: three children of
  one uuid). Cost per candidate is in events (`candidate` tag), and the cap covered the sum.
- The ranking chose the candidate that resolves on pamqp: both were FAIL (out of turns, no
  test), and fewer regressions decided it.
- Early stop cannot fix a false PASS: on fromager #1 was a clean PASS that the hidden tests
  reject, so #2 and #3 never ran. Best-of-N helps the FAIL side (false FAIL, out-of-turns) and
  not the false-PASS side until the verifier itself improves.
- The budget skip uses the costliest candidate so far as the estimate: at $0.65 cap a $0.33
  candidate leaves room for 1-2. For N=3 use `--max-usd 1.5`.

### 1h. B3 implementer fixes from pilot 3 (2026-10-14): clobber guard, replace_in_file, finish nudge

Offline only (fake sandbox, scripted LLM); not yet measured live.
- **Clobber guard.** Super wrote a 309-byte fragment over 19 kB pennylane `exp.py` and spent the
  remaining ~30 turns recovering. `write_file` now refuses to shrink a file of >= 1 kB below half
  its size; the error names both sizes and points at `replace_in_file`. `allow_shrink=true`
  overrides. A refusal counts as a turn, the `tool` event carries
  `refused={reason, old_bytes, new_bytes}`, and the implementer emits a `refused_shrink` note.
  Each `write_file` now costs one extra `cat` (sandbox exec, no fork).
- **`replace_in_file(path, old, new)`.** A sixth tool. `old` must match exactly once, and a miss
  or an ambiguous match is a turn with an error. The test-deletion guard and `tests_added`
  tracking cover it. The prompt now steers edits to it, which departs from the plan's
  "whole-file rewrites only" (B3). The prompt hash changed.
- **Finish nudge.** 7 of 10 pilot-3 runs used all 40 turns. fromager's new tests were green at
  turn 10, and the implementer never called `finish`. After a test command exits 0 with new tests
  written and no edit since, two turns without `finish` get one user note ("Your new tests
  pass..."), at most once per run, plus a `finish_nudge` event.
- To measure on the next eval, count from events: `refused_shrink` and `finish_nudge` notes,
  `replace_in_file` calls vs `write_file`, and turns-to-finish.

### 1a. Earlier notes: blocked by Token Factory billing (resolved)

`warden run --issue 196 --at 43f3851^ --yes` (run `20261010-111217-7c89`) fetched the issue, then
got **HTTP 402 "budget exhausted"** on the first Lightning call. A 402 now ends as a clean exit 4
with a fix line (`ProviderBudgetError`). **After adding funds:**
`WARDEN_MAX_TOOL_TURNS=40 uv run warden resume 20261010-111217-7c89 --yes`, or a fresh
`warden run --issue 196 --at 43f3851^ --yes`. Expected: about $0.20 and 15–20 min, ending in a
verdict and, unless it's FAIL, a draft PR on Fabric.

#### Original notes for this step


`warden/pipeline.py:live_steps()` has `hunt` and `plan` live; `fix`, `verify` and `pr` are still
`_not_ready`. The spikes already contain the code to lift:

- **fix:** `spikes/a6_first_fix.py`. Do this:
  1. `open_base`, then a kept `git checkout` run when replaying.
  2. `ToolDispatcher(log, cwd=s.workdir, shell_wrap=s.shell_wrap)`.
  3. `run_implementer`.
  4. Keep the candidate box alive for verify: `Box.from_ref(backend, cand.image_uuid, ...)`.
- **verify:** theater from `spikes/a7_theater.py`, plus the gate (step 2). Fill in `Verdict`:
  set `theater_failed_on_base`, and turn `inconclusive` into a concern, never a PASS.
- **pr:** `export_patch` → `push_fix_branch` → `open_draft_pr`, as in `spikes/b5_push_fix.py`.
  The PR body must only claim checks that ran; `evidence_table` can't express "gate not run" yet.
- Add `warden run --issue N --at <rev>` for replays. Planner context uses `rev=`; fix/verify check
  out the rev.

Acceptance: `warden run --issue 196 --at 43f3851^` from a clean shell produces a Verdict with the
theater row, under $3, unattended.

### 2. B4-live: ✓ done (2026-10-10, `07622b9`, [spike-B4.md](spike-B4.md))

`run_gate(commands=PROFILES[s.gate_profile], cwd=s.workdir, wrap=s.shell_wrap)` on the A6
candidate gives suite 1509 passed (= Fabric CI on #220), mypy ✓, **ruff ✗** (= CI), alembic 1 head
✓, no OpenAPI drift, and FE codegen not verified. Next: call it from the live `verify` step, and
fill `Verdict` from `verdict_fields()` + `not_verified`. `evidence_table` must render "not run" /
"not verified" rows honestly.

### 3. B2 criterion: plans name the right files on 3 closed issues

#196 ✓. Pick two more from PR #212 (`43f3851`, which fixed #196 #197 #200 #205 #206 #207). Always
replay at `43f3851^`, with issue comments excluded (they hold the human plans).

### 4. Smaller follow-ups

- **Acceptance-to-test check.** Every `acceptance` item should map to a test. Both #196 runs
  missed the human plan's "PAID still counts" test.
- **Make "run the failing test first" stick.** The implementer skipped protocol step 2 (run the
  test before implementing). The theater check covers the risk, but a nudge would help.
- **Older-schema replays.** The image DB is migrated at `8cfc493`, and replays at older commits
  run on that newer schema. That's fine for #196, but a migration-touching issue needs
  `alembic downgrade` to the replay commit's head.
- **Prompt growth in the implementer.** Prompts reached 34k tokens by turn 19; earlier pages of a
  rewritten file could be dropped from the history.

## Gotchas (cost real time this session)

- **Scripts outside the repo don't find `.env`.** `load_dotenv()` walks up from the script's own
  directory. In a scratchpad script the SDK then sends the literal env-var NAMES and gets
  `403 You do not have permission`. Pass the `.env` path explicitly.
- **`docker` hangs on the macOS Keychain** (the `desktop` credential helper). Use a throwaway
  `DOCKER_CONFIG` dir: symlink `cli-plugins` and `contexts` into it, and put `ghcr.io` auth inline
  (from `gh auth token`). Delete it after use.
- **`images.oci(ref)` caches by tag.** After re-pushing a registry tag it returns the OLD import.
  Use `images.import_from(ref, tag=…)` to refresh. Digest refs are rejected.
- **The sandbox shell is dash.** No bash-isms in `run()` commands (`$RANDOM` is empty, no `[[`).
- **Pushing to GHCR** was blocked by the auto-mode permission check until Moiz asked for it
  explicitly.
- **Never plan against HEAD for a closed issue.** The fix is already in the code, and the model
  will happily cite the tests it added.
