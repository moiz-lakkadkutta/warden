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
- 2026-10-14 · D3 pilot 3 · `GET /sandboxes/v1/operations/{id}` returned **HTTP 500**
  (`Internal Error`, 49-byte body) 8 min 23 s into a run (13:32:12 → 13:40:35 UTC). The run was a
  pytest file that hangs without an AMQP broker (smok-serwis__coolamqp-28, theater check, 1800 s
  timeout). The SDK raised `ApiStatusCodeError(status=500)` from `.wait()`, so the task ended as an
  ERROR row after $0.12 of model spend and 1823 s. Warden now retries a run twice on a 5xx
  (`8632d19`). → document whether a 5xx on the operations endpoint means the operation failed or
  only the poll did, and whether polling the same operation id again is safe. Retrying the poll
  would be cheaper than re-running the command.

- 2026-10-14 · runtime · `run()` stdout is silently cut at **65,535 bytes**, with no flag or marker
  on the result. A `pytest -rA` log for coverahealth__dataspec-30 (672 P2P ids) lost its last 77
  results, so a correct gold patch graded "not resolved" (P2P 614/672). Workaround: write the log
  into a kept checkpoint and read it back in 60 kB chunks (672/672). → document the cap, and set a
  `truncated` field (or return the full stream as an artifact).
- 2026-10-14 · concurrency · Probe for a sharded eval (5 shards × 3 candidates ≈ 15 forks), each
  fork `sleep 20` kept on one SWE-rebench base, with 4 base-validation workers running alongside:
  N=15 → 15/15 ok, latency 21.2–23.2 s; N=25 → 25/25, 21.3–23.1 s; N=40 → 40/40, 21.7–23.7 s;
  N=60 → 60/60 ok but max 44.9 s (wall 45.6 s): past ~50 in flight, runs **queue silently**
  instead of erroring. Good behaviour, but invisible: → expose queue position / a 429 with
  Retry-After so a client can tell queueing from a slow command.
- 2026-10-14 · eval images · 50-task E1 sample (seed 7) base check, no model calls: 50/50 images
  present, 13–90 s per task (prepare + base grade + gold grade), parallelism 4. With the two
  grading fixes in, 49/50 grade as the dataset says (F2P fails on the base, gold resolves). The
  outlier is pypa__pip-11987, whose F2P test already passes on the unfixed base.
- 2026-10-15 · C5 hunter · booting a long-lived app server inside a sandbox for probing works, but
  the model fights the fork-per-run contract. The IDOR hunter boots Fabric's uvicorn over
  `with-services sh -c` and drives it over localhost in a single `run`, so the server and all HTTP
  probes share one process. Because two `run`s on a Box fork it (by design), every reproduction
  **reboots Postgres+Redis+uvicorn from the seeded checkpoint**: a full 34-endpoint sweep measured
  **8.6 s then 7.7 s** on two runs of one fork (`20261015-162240-1f6d`). That's cheap here, but a
  heavier app (or a 5× reproduction) pays the cold boot each time. A supported way to **keep a
  warm service across N runs on one fork** — or a "services already up" base variant — would cut
  reproduction latency. Upside worth noting: the whole hunt is **$0 of Token Factory spend** (the
  probe is deterministic; Lightning is optional and was not needed), so Sandboxes carried the
  entire discovery phase alone.

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
- 2026-10-14 · tool calls · Nemotron 3 Super invents tool arguments that are not in the declared
  function schema, and mistypes them: on collective/icalendar#1928 (run 20261014-122844-b9ee) it
  sent `read_file{start_line: 940, end_line: "970"}` (an undeclared key, integer as a string) 9 times
  in 25 turns, although the parameters schema says `additionalProperties: false` and lists every
  key as required (Warden does not set the function-level `strict` flag). Every one was rejected,
  and the run ended with no change ($0.084 implementer, $0.11 total). Warden now accepts `end_line`
  (f114db8). → document whether Token Factory honours `strict: true` on tool definitions for
  Nemotron, so schema-conformant arguments can be guaranteed instead of validated after the fact.
- 2026-10-14 · tool calls · Nemotron 3 Super re-reads instead of editing: on icalendar#1928 (run
  20261014-123340-85cd, 40-turn cap) 26 of 40 turns were read_file calls on 4 files it had already
  been shown, often the same range twice (recur.py 237-260 at turns 31 and 40), and it never
  wrote the fix. With a 60-turn cap (20261014-123938-6f4c) it finished, in 52 turns. Warden now
  tolerates invented/mistyped arguments (drop, coerce, clamp; recorded on the event), answers an
  unchanged re-read with a one-line note, and nudges after 8 turns without write_file/run_shell.
  → a recommended sampling setup (or a "tool-use" system-prompt preset) for Super that curbs
  repeated identical tool calls would help every agent built on it.
- 2026-10-14 · D3 pilot 3 · Nemotron 3 Super sometimes sends a *fragment* as `write_file`'s
  whole-file `content`. On pennylane-5831 (run 20261014-123250-d047) it replaced the 19 089-byte
  `exp.py` with 309 bytes (one method) at turn 9, and again at turn 39. The same task on the same
  prompt (hash 58fdd824ab34) wrote the whole file and resolved in 10 turns in pilot 2. In pilot 3,
  7 of 10 tasks used all 40 turns (pilot 2: 5). → guidance on which edit-tool shape Super is
  trained on (whole file vs str_replace vs unified diff) would let agents pick the one it uses
  reliably.
- (fill in after Phase B3) Lightning as a tool router: turn count, wrong-tool rate.

## Nebius Serverless

- (fill in at Phase E3)
