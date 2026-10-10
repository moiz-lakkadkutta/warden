# Warden — status (2026-10-10, end of session 2)

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

## Start here next session

### 1. B6-live: `warden run --issue N` end to end (the Phase B exit)

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
