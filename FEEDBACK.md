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

## Token Factory inference

- (fill in after Phase A5) structured-output behaviour of Nemotron 3 Ultra vs the "JSON mode" tag.

## Nemotron models

- (fill in after Phase B3) Lightning as a tool router: turn count, wrong-tool rate.

## Nebius Serverless

- (fill in at Phase E3)
