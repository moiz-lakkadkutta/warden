# CLAUDE.md — Warden

You are building Warden: an autonomous hunt → plan → fix → verify → PR agent that runs on Nebius
Token Factory Sandboxes with NVIDIA Nemotron models. The plan of record is `docs/warden-plan.md`
(phases A–F with done-criteria); the wider context is `docs/runbook.md`; UI/DX rules are in
`docs/ux-brief.md`. Read the plan section for the phase you are working in before writing code.

## Fixed decisions (do not re-derive)
- Python 3.12, `uv`, `ruff`, `mypy --strict`, `pytest`. `typer-slim`; heavy imports lazy inside commands.
- Models come from env (`WARDEN_*_MODEL`); roles: PLANNER/VERDICT = Nemotron 3 Ultra, IMPLEMENTER =
  Nemotron 3 Super, TOOLS = Nemotron 3.5 Lightning. Never hard-code a model id outside `config.py`/`cost.py`.
- All model output is a pydantic schema in `warden/schemas.py`, requested via `LLM.structured()`.
- All sandbox access goes through `warden/sandbox/box.py`. A `run(keep=True)` returns a child
  checkpoint; two runs on one Box are a fork. Tests use `sandbox/fake.py`; never call Contree in tests.
- Every LLM/sandbox call emits a `RunEvent` (runs/<id>/events.jsonl). stdout is data, stderr is progress.
- Cost cap per issue is enforced in `llm/cost.py`; do not catch `CostCapExceededError` to keep going.
- Money/tax/schema/security risk → PR labelled `PENDING-HUMAN`; Warden never merges to main.

## Working rules
- Commit each green step with its plan id: `feat(B3): implementer TDD loop`.
- `uv run ruff check . && uv run ruff format --check . && uv run mypy warden && uv run pytest -q` before "done".
- Anything longer than 5 minutes runs as a background task.
- New env var → `.env.example` + `config.py` + `doctor` check.
- When you hit friction with Nebius/NVIDIA tooling, append a dated entry to `FEEDBACK.md` with numbers.
