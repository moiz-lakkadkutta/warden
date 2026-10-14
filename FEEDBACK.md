# Feedback on Nebius & NVIDIA tooling

Running notes for the hackathon's mandatory feedback field (and the Most Valuable Feedback prize).
Every entry: date · tool · what happened · numbers · what would have helped.

## Token Factory Sandboxes (Contree)

- 2026-09-15 · docs · Pricing for Sandboxes is not published; the beta cap (50 concurrent ops, 180-day
  checkpoint retention) is. Budgeting a 50-task eval without a price is guesswork. → publish a per-minute
  / per-checkpoint price, even as "beta, subject to change".
- 2026-09-15 · docs · `running-commands.md` documents `disposable`, `files`, `env`, `cwd`, but not how a
  long-running service (Postgres) behaves inside an image between `run()` calls. → one page on the
  process model: is each `run` a fresh process tree? can a service persist across a checkpoint?
- 2026-09-15 · integrations · `mini-swe-agent` `ContreeEnvironment` cannot take a user-provided client,
  so Token Factory models can't be plugged in without a fork. → accept a client / base_url.
- 2026-10-01 · auth · `GET /v1/whoami` with a valid Token Factory key + project id returns 200, but
  every permission is `false` (import, spawn, spawn_disposable, list, cancel, set_image_tag) while
  limits show 50 concurrent instances. Nothing says which role/beta flag is missing. → whoami (or the
  console) should name the missing grant and where to request it.
- 2026-10-01 · docs · the project header is `Project`; sending a guessed `X-Project-Id` gives
  `400 Missing "Project" header`. The CLI auth page names `--project`/`NEBIUS_AI_PROJECT` but not the
  HTTP header. → document the raw header for non-SDK clients.
- 2026-10-04 · A3 numbers (Berlin → api.tokenfactory.nebius.com, ubuntu:latest, sync SDK 0.3.6):
  whoami 0.38 s · `images.use("ubuntu:latest", strict=True)` 0.25 s · disposable `echo hi` 0.74 s ·
  kept run 1.01 s · fork A 1.19 s · fork B 1.06 s · read-back runs 0.81–0.95 s. Fork isolation correct
  (A and B each saw only their own write on top of the shared parent). ~1 s per checkpoint is fast
  enough for theater + best-of-N; a 50-task eval at ~40 runs/task is ~35 min of pure run latency.
- 2026-10-04 · runtime · `shell=` runs under /bin/sh → dash on ubuntu, so `$RANDOM` is empty (the
  plan's own A3 example silently wrote a blank line). → say which shell `shell=` uses in the run docs.
- 2026-10-04 · SDK · `whoami.token_expiration` is always ~minutes ahead and the SDK prints
  "Token expires in 0 hours" on every client start, yet the same key keeps working across days. The
  warning is noise or the field means something else. → document the field; don't warn on it.
- 2026-10-04 · SDK · `ContreeSync(...)` takes a `ContreeConfig`, but the getting-started page shows
  passing a `ContreeAsyncClient`; `WhoAmI.permissions` is a dict, not an object. Both cost a failed run.
- 2026-10-04 · runtime · **No SysV IPC in the sandbox kernel** (Linux 7.0.6, `shmget` → ENOSYS).
  Postgres 16 always creates one 56-byte SysV segment, so `pg_ctl start` dies with
  "could not create shared memory segment: Function not implemented". Shared mmap, memfd, /dev/shm
  and Redis all work. Workaround: a 100-line LD_PRELOAD shim (ops/shmshim.c) emulating
  shmget/shmat/shmdt/shmctl over anonymous shared mmap. → enable CONFIG_SYSVIPC, or document its
  absence next to "full root access"; every Postgres-backed app hits this on day one.
- 2026-10-04 · A4 numbers: OCI import of a 633 MB public GHCR image 56.5 s (re-import of the same
  digest 0.4 s); adding a 2-file layer via `files=` 1.2 s; Fabric suite **1787 passed in 774 s** on
  the sandbox's 4 cores vs 893 s on a GitHub Actions runner, identical count.
- 2026-10-04 · SDK · with credentials unset, `IAMAuth()` sends the literal env-var NAMES as the token
  and project, and the API answers `403 You do not have permission to perform this action`. That
  read like a permissions/beta problem for 20 minutes. → fail client-side with "NEBIUS_API_KEY not
  set", or answer 401 "invalid token".
- 2026-10-10 · SDK · `images.oci("docker://ghcr.io/x/y:TAG")` resolves an existing Sandboxes image
  by that tag before importing, so after re-pushing the same registry tag it silently returns the
  OLD import (0.5 s, old uuid) instead of the new content. Digest refs (`@sha256:…`) are rejected as
  a "Bad tag format". `images.import_from(ref, tag=new)` forces a fresh import (54.9 s for 633 MB).
  → resolve by registry digest, or document that `oci()` is cache-by-tag and how to refresh.

- 2026-10-12 · eval images · The preloaded benchmark environments are there but undocumented:
  `images(tagged=True)` lists 39,520 tags (32,064 `swerebenchv2/…`, 6,873 `swerebench/…`, 500
  `swebench/…`, 73 base images), with no page that names the tag scheme. It is SWE-bench's:
  `swerebench/sweb.eval.x86_64.<owner>_1776_<repo>-<n>:latest` = instance `<owner>__<repo>-<n>`,
  i.e. the dataset's `image_name` plus `:latest`. `images.use(tag)` resolves in 0.3–0.5 s, the
  first run on a task image takes 27 s (cold), and later runs take 2–3 s. Inside: repo at
  `/testbed` on the base commit, `/issue.md`, and the task's conda env at
  `/opt/miniconda3/envs/testbed` — **not on PATH** (`python` is the 3.12 in /opt/conda; the
  task wants 3.9), so the hidden tests fail to import until you prepend that env. One sentence
  in the docs would save the probing.
- 2026-10-12 · eval images · Grading works as the official harness does: test_patch + FAIL_TO_PASS
  fail on the base (spectree-64: F2P 0/1, P2P 6/6) and pass with the gold patch (1/1, 6/6), 2.4 s
  per grade.

- 2026-10-14 · eval images · Four image quirks the harness has to absorb (none documented):
  (1) the dataset's FAIL_TO_PASS/PASS_TO_PASS ids are cut at the first space (pennylane
  `test_label[op0-None-Exp((2+3j)`), so you must run test FILES and match by first token, as the
  official log parser does; (2) on some images the `pytest` script's shebang names an env without
  the package installed, while `python -m pytest` works; (3) editable installs can point outside
  /testbed (spikeinterface: `__editable__` .pth → `/spikeinterface/src`), so re-run
  `install_config.install` first; (4) a run past its `timeout` raises `OperationTimedOutError`
  from `.wait()` rather than returning an exit code. With (1)–(3) handled, all 10 sampled bases
  grade as expected (F2P fails, 606/606 P2P pass, 4–18 s to prepare).

## Token Factory inference

- 2026-10-04 · A5 · `response_format={"type": "json_schema", "strict": true}` with the `Plan` schema
  worked on Nemotron 3 Ultra in 4 of 5 calls. The 5th returned truncated JSON because it hit the
  default 8192 completion-token cap: reasoning tokens count against it, with no separate budget.
  → document the default `max_tokens`, and expose a reasoning budget or report reasoning tokens
  separately in `usage`.
- 2026-10-04 · A5 · Ultra plan calls: 8.6–21.9k prompt tokens, 1.9–8.2k completion tokens, 6–22 s,
  $0.010–0.069 per plan.
- 2026-10-10 · billing · mid-run `402 Payment Required: You have exhausted your budget` on a
  Lightning call. Warden's own meter (list-price estimates in cost.py) totals ≈ $1 for everything
  so far, so either the promo credits were not applied to this project or real prices differ a lot.
  There is no API to read the remaining balance before a run. → expose a balance/usage endpoint
  (or a response header) so agents can stop before a 402 instead of at it.
- 2026-10-10 · billing · root cause of the 402 above: the console showed **$10.00 account balance**
  while the API said "You have exhausted your budget". The real state was "Trial ended. Usage
  stopped based on your after-trial preference", fixed by "Switch to paid usage". The error text
  points at money, the cause was a trial-state switch. → return a distinct error
  (`trial_ended: switch to paid usage`) with a console link.

## Nemotron models

- 2026-10-04 · A5 · Nemotron 3.5 Lightning, small structured task (≤3 queries + ≤8 identifiers,
  ~100 output tokens of JSON): 2.6–4.6k completion tokens and 11–19 s per call, so it is slower
  than the Ultra plan call that follows it. Cost is fine ($0.001). → a documented way to turn
  reasoning down for Lightning would make it a real low-latency router.
- 2026-10-06 · A6 · Nemotron 3 Super as a tool-calling implementer (5 tools + finish,
  `tool_choice=auto`): 19 calls, every one a well-formed tool call, so the Lightning prose router was
  never needed. 368k prompt / 16.5k completion tokens, 87 s of model time, $0.125 for a
  correct 2-file fix with 3 tests. Typical per-call latency 1–3 s at 10–34k prompt tokens.
- 2026-10-06 · A6 · Super's habits worth knowing: it reaches for `cd /app` (a Docker convention)
  even when told nothing about /app, and it re-reads a file it cannot see whole rather than
  writing blind (good). Once the workdir is stated in the system prompt it never `cd`s.
- 2026-10-10 · B5 · Super's whole-file rewrites (`write_file` content) end without a trailing
  newline. On Fabric#220 that was the ONLY CI failure (ruff W292 in both files; pytest, E2E,
  OpenAPI drift, frontend all green). Fixed deterministically in Warden's write_file.
- (fill in after Phase B3) Lightning as a tool router: turn count, wrong-tool rate.

## Nebius Serverless

- (fill in at Phase E3)
