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

## Token Factory inference

- (fill in after Phase A5) structured-output behaviour of Nemotron 3 Ultra vs the "JSON mode" tag.

## Nemotron models

- (fill in after Phase B3) Lightning as a tool router: turn count, wrong-tool rate.

## Nebius Serverless

- (fill in at Phase E3)
