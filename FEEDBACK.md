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

## Token Factory inference

- 2026-10-04 · A5 · `response_format={"type": "json_schema", "strict": true}` with the `Plan` schema
  worked on Nemotron 3 Ultra in 4 of 5 calls. The 5th returned truncated JSON because it hit the
  default 8192 completion-token cap: reasoning tokens count against it, with no separate budget.
  → document the default `max_tokens`, and expose a reasoning budget or report reasoning tokens
  separately in `usage`.
- 2026-10-04 · A5 · Ultra plan calls: 8.6–21.9k prompt tokens, 1.9–8.2k completion tokens, 6–22 s,
  $0.010–0.069 per plan.

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
