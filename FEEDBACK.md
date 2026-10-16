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
- 2026-10-14 · E1 (5 shards × 3 candidates, ~15 runs in flight) · **7 of 49 tasks ended ERROR**
  polling `GET /sandboxes/v1/operations/{id}`: 5 × `ApiTimeoutError` (read timeout) and 2 ×
  `ContreeTransportError` (`ReadError`, connection dropped). They hit 299–1856 s into a task, after
  $0.11–0.60 of model spend each ($2.25 in all, lost). These were not 5xx, so the `8632d19` retry
  did not apply. Warden now also retries these (`5077c58`), and re-runs the whole command on a new
  fork. → the same ask as the 500 above: say whether re-polling the same operation id is safe
  after a read timeout, and give the SDK a built-in poll retry. A dropped poll costs the client a
  whole command re-run.
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
- 2026-10-16 · runtime · The silent **65,535-byte** stdout cap (above) hit again, this time in
  reading *files*: `cat tests/test_frame_unmarshaling.py` on gmr__pamqp-52 returned 65,535 of
  65,709 bytes. Warden copied that cut file onto a candidate for its regression check, got a
  SyntaxError, counted 237 "regressions", and ranked the only candidate the hidden tests accept
  last (E1's one wrong best-of-N pick). The same cut hit `replace_in_file` on any file over
  64 KiB (it wrote the cut read back). Workaround: read files as base64 chunks of 40 kB
  (sha256 matches) and restore files with `git checkout` inside the sandbox. → a `read_file` /
  `download` call in the SDK, or the `truncated` flag asked for above, would have made both
  bugs visible on day one.

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
- 2026-10-15 · C5 replay (#225) · Nemotron 3 Ultra exhausted its completion budget on reasoning
  before emitting the `Plan` JSON, three calls in a row (run 20261015-165152-7771): each call hit
  `max_tokens=16384` exactly, 43–51 s each, and `content` began with prose ("The user is asking me
  to…") instead of JSON — strict `json_schema` did not stop it, and the `json_object` fallback
  failed the same way. **$0.27 for no plan.** The same prompt planned fine on the runs before and
  after. The input was an issue with six curl repros (~6 kB). → a server-side cap on reasoning
  tokens (or a guarantee that structured-output mode reserves room for the answer) would make
  `response_format` dependable for long inputs.
- 2026-10-15 · C5 replay (#225) · the Super read-loop again, on a 6-endpoint security fix at
  Fabric `d9c37cb^`: with 60 turns (run 20261015-165529-50c3) Super made 25 `read_file` + 13 `grep`
  calls and only 2 edits — both to the test file — and never wrote the guard. Its repro test was
  right (POST /items → 201 on the unfixed base) but stayed red. With 25 turns (20261015-163738-774e)
  it also stopped mid-fix. → same ask as above: a tool-use preset that curbs re-reading.
- 2026-10-15 · structured output · Nemotron 3.5 Lightning now reasons by default, and the reasoning
  counts against `max_tokens`. In the Fabric campaign (run `camp-head-1-210`) the planner's
  ResearchQueries call (strict `json_schema`) used **8192 of 8192 completion tokens twice** (35 s
  each) and returned truncated JSON; the `json_object` fallback then answered with the *schema
  envelope* (`{"title": …, "type": "object", "properties": {<the answer>}}`), so the plan step
  failed for $0.007. A probe on the same request: baseline 1119 completion tokens of which 1024
  reasoning (4.4 s); `reasoning_effort="low"` 1133/1035 (no effect); `/no_think` in the system
  prompt 822/738; **`chat_template_kwargs: {"enable_thinking": false}` 72/0, 0.5 s**, same
  answer. Warden now sends that for every Lightning call (8fb5576) and unwraps the schema echo.
  → document the thinking switch per model on the model card (it is the only one that works),
  and say whether `strict` json_schema is meant to stop a reasoning model before `max_tokens`.
- 2026-10-15 · tool calls · Nemotron 3 Super re-sends a large file whole through `write_file`
  even with `replace_in_file` offered. On Fabric#210 (run `camp-head-1-210`, a test-only issue)
  it re-sent the 926-line `tests/test_migrations_router.py` 9 times; 8 replies hit the server's
  **8192-token completion limit** (26–34 s each) and the tool call arrived **without its `content`
  argument** (no truncation signal in the tool call itself). 40 turns, 0 tests, $0.26. Warden now
  tells the model the reply was cut and to use `replace_in_file` (54747c7). → expose
  `finish_reason=length` on truncated tool calls (or reject them) instead of delivering a
  partial argument object.
- 2026-10-15 · Fabric campaign (E2) · On a real ERP codebase, Nemotron 3 Super's tool loop is
  the bottleneck. In 13 of 15 attempts across 9 issues, it never called `finish` (40 or 60 turns).
  In single runs, Warden's "unchanged since turn N, you already have it" reply to a repeat
  `read_file` fired 30 (#200, 40 turns), 71 (#197, 60×2) and 86 (#200, 60×2) times; the files
  were 900-line test modules. A 60-turn, best-of-2 retry cost $2.21 for 5 issues and fixed none
  (the 40-turn single run: $1.11 for 6, 1 fixed). → a Super sampling or prompt recipe for
  long-file editing (page through, then `str_replace`) would directly raise the fix rate.
- 2026-10-15 · inference · Token Factory's completion budget for Nemotron 3 Super when a request
  sends no `max_tokens` is **8192** (finish_reason=length). With `max_tokens=32768`, the same
  echo prompt (25k prompt tokens) returned **24980 tokens, finish=stop, 83 s**. So the 8192 that
  cut Super's whole-file `write_file` replies is a default, not a model limit. Warden now sends
  16384 (`WARDEN_IMPLEMENTER_MAX_TOKENS`). → document the per-model default and maximum
  `max_tokens` on the model card.
- 2026-10-15 · Nemotron 3 Super · tool loop · Super keeps calling `read_file` on text it already
  has, whatever the tool result says. In `camp-replay-212-c` (6 Fabric issues, 40 turns) Warden
  answered a repeat read with a cached note and, from the 3rd repeat, an explicit "STOP: reading
  again shows nothing new; your next call must be replace_in_file / write_file / run_shell /
  finish". It still re-read **87 times** across 6 runs (16 on #196's 50-line test file, 13 of them
  after the STOP text), and **0 of 6 runs edited a source file**. Median completion was ~100
  tokens a turn. Thinking is on by default but short: 62 reasoning chars on a probe tool turn.
  Withholding the read tools from the `tools` list for 3 turns ("edit mode") worked where the
  text did not. In `camp-replay-212-d`: 5 of 6 runs edited source, 26 `replace_in_file` calls
  (0 before), 32 cached re-reads. → a recommended sampling setup for agentic tool loops
  (temperature, repetition controls) on the Super model card. A repeat-call penalty or a "you
  already called this" signal would help any agent framework.
- (fill in after Phase B3) Lightning as a tool router: turn count, wrong-tool rate.

## Nebius Serverless

- 2026-10-15 · AI endpoints · E3 run console (FastAPI, ~25 MB image layer on `python:3.12-slim`)
  on `cpu-d3 2vcpu-8gb`, eu-west2, CLI 0.12.287. First deploy end-to-end **307 s**: registry
  create 4 s, assemble + push 72 s, `endpoint create` → serving `/healthz` **235 s**. Redeploy
  (push 13 s, `endpoint restart` to re-pull a moved `:live` tag) **296 s**, of which the URL was
  down **~260 s** (one 503, then the tunnel answers `404` until the container is up). Warm
  requests from Berlin: `/healthz` ~0.25 s, run page (27 KB) 0.23 s, index 0.44 s.
- 2026-10-15 · AI endpoints · `nebius ai endpoint` has no `update`: changing the image means
  `restart` (re-pulls the same tag; URL kept) or delete + create (new tunnel URL, so every link in
  a README breaks). → an `update --image` that rolls in place, or a stable per-name URL.
- 2026-10-15 · AI endpoints · `--platform` defaults to `gpu-h100-sxm` even for a container that
  exposes one HTTP port; forgetting `--platform cpu-d3 --preset 2vcpu-8gb` would bill an H100 for
  a static site. → default to the cheapest CPU preset, or make `--platform` required.
- 2026-10-15 · AI endpoints · good: `--dry-run` validates the whole request offline in <2 s, the
  managed `https://port<N>-….tunnel.applications.<region>.nebius.cloud` URL needs no public IP,
  and a same-project Container Registry image pulls with no credentials. The tunnel returning
  `404` (not `503`) while the container restarts reads like a routing bug to a monitor. → `503`
  with `Retry-After` during start-up.
- 2026-10-15 · Container Registry · pushing works daemonless (`crane` + the `nebius` docker
  credential helper from `nebius registry configure-helper`), which mattered: the Mac's Docker
  Desktop was owned by another macOS user. → say in the quickstart that any OCI client works.
