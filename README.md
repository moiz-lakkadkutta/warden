# Warden

**Hunt → Plan → Fix → Verify → PR.** An autonomous coding agent that finds the bug, fixes it
test-first in a forked Nebius Sandbox, and has a second agent prove the tests are real before a PR
is opened. Runs on **Nebius Token Factory Sandboxes** with **NVIDIA Nemotron 3 Ultra / Super / 3.5 Lightning**.

> Status: Phase A (spike) — see [docs/warden-plan.md](docs/warden-plan.md). `warden run --demo` works today.

```bash
uvx warden doctor            # checks tools + keys, no network
uvx warden run --demo        # whole loop on a fake sandbox, no credentials, ~2 s
uvx warden console           # render the run's timeline + evidence table
```

## Runs on Nebius

| Piece | How Warden uses it |
|---|---|
| Token Factory (OpenAI-compatible) | all model calls; JSON-schema structured output; function calling |
| Token Factory **Sandboxes** | every issue is a fork of a base checkpoint; the **theater check** forks the unfixed base and runs only the new tests (they must fail); best-of-N forks N candidates |
| Serverless Endpoint | hosts the read-only run console |

## NVIDIA models

| Role | Model | Why |
|---|---|---|
| Plan, verdict, probe planning | `nvidia/Nemotron-3-Ultra-550b-a55b` | reasoning and adversarial judgement |
| Implement | `nvidia/nemotron-3-super-120b-a12b` | code at a third of Ultra's cost |
| Tool calls, parsing, probe execution | `nvidia/Nemotron-3_5-Lightning` | harness-trained for tool use; cheapest |

## Results

_Filled in Phase E: SWE-rebench resolved %, verifier false-PASS %, theater catches, $/task, with task ids._

## How it works

```
issue ─▶ HUNTER (race / IDOR / money invariants / malformed / flow probes) ─▶ files issue with repro
      ─▶ PLANNER (Ultra + Tavily citations) ─▶ Plan{files, tests, risk_class}
      ─▶ base.fork() ─▶ IMPLEMENTER (Super; tools via Lightning; TDD)
      ─▶ VERIFIER (fresh Ultra): theater check · full gate · race probe ─▶ Verdict
      ─▶ INTEGRATOR: waves · merge --no-ff · PR with evidence table (PENDING-HUMAN for money/tax/schema/security)
```

## Quickstart (live)

```bash
git clone … && cd warden && uv sync --all-extras --dev
cp .env.example .env         # Nebius key, Sandboxes base URL, Tavily key
uv run warden doctor --live
uv run warden init /path/to/repo
uv run warden run --issue 201 --dry-run
```

## Feedback on the tooling

See [FEEDBACK.md](FEEDBACK.md).

## License

MIT
