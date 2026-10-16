# Warden — Build Plan

Repo `warden` · Coding & Agentic Engineering track · MIT · build Sep 15 – Oct 24, freeze Oct 24, submit Oct 28

This is the execution plan. Every step has a file to create, a command to run, and a "done when" you can check without judgement. It is written so a Claude Code session can execute a step from this document alone.

## 0. One-paragraph product

`warden` is a CLI that points at a repository and a running instance of its app and produces evidence-backed pull requests without a human in the loop. It hunts bugs (races, cross-tenant leaks, broken money invariants, malformed input), files them with a copy-paste repro, plans the fix with cited documentation, implements test-first in a forked Nebius Sandbox, and has a second, fresh agent prove the tests are real before a PR is opened. Money, tax, schema and security changes are labelled `PENDING-HUMAN` and never auto-merged. Everything it does is recorded in a run log that a browser console renders.

## 1. Fixed decisions (do not re-derive)

| Decision | Value |
|---|---|
| Language / tooling | Python 3.12, `uv`, `ruff`, `mypy --strict`, `pytest` |
| LLM client | `openai` SDK, base `https://api.tokenfactory.nebius.com/v1` |
| Models | `PLANNER=nvidia/Nemotron-3-Ultra-550b-a55b` · `IMPLEMENTER=nvidia/nemotron-3-super-120b-a12b` · `TOOLS=nvidia/Nemotron-3_5-Lightning` · `VERDICT=PLANNER`. All four read from env so they can be swapped without code changes. |
| Sandboxes | `contree-sdk` + `contree-client`; orchestrator uses `ContreeSync`; parallel forks use the async client |
| Structured output | pydantic v2 models → `model_json_schema()` passed as `response_format={"type":"json_schema","json_schema":{...}}` AND pasted into the prompt |
| Research | `tavily-python`; `search(search_depth="advanced", include_domains=[...], max_results=5)` then `extract` |
| Orchestration | hand-written state machine; no agent framework |
| State | `runs/<run_id>/events.jsonl`, `cost.jsonl`, `artifacts/`; `runs.db` (SQLite) index |
| Console | static HTML per run + FastAPI viewer; deployed as a Nebius Serverless Endpoint |
| Test repo | Fabric ERP (private). Public eval: SWE-rebench via Nebius preloaded images |
| Concurrency | max 4 sandbox forks at once (`WARDEN_MAX_FORKS=4`) |
| Cost caps | `WARDEN_MAX_USD_PER_ISSUE=3.0` (W2) → `2.0` (W4); `WARDEN_MAX_TOOL_TURNS=25` |
| Never | auto-merge to `main`; touch a previously applied migration; call `$queryRawUnsafe`-style raw SQL in generated code; flush Redis to bypass rate limits |

## 2. Repository layout

```
warden/
├── README.md                 # judges read this first — see §9
├── LICENSE                   # MIT
├── FEEDBACK.md               # running notes for the feedback prize
├── pyproject.toml            # uv; deps: openai, contree-sdk, contree-client, tavily-python, pydantic, typer, rich, httpx, jinja2, fastapi, uvicorn
├── .env.example
├── .github/workflows/ci.yml  # ruff, mypy, pytest (offline), replay of examples/ run
├── warden/
│   ├── cli.py                # typer: warden hunt | plan | fix | verify | run | eval | console
│   ├── config.py             # pydantic-settings; all env vars in one place
│   ├── llm/
│   │   ├── client.py         # TokenFactory wrapper: chat(), structured(), tools(); cost logging
│   │   ├── routing.py        # which model for which role
│   │   └── cost.py           # usage → USD; per-run and per-issue caps
│   ├── sandbox/
│   │   ├── box.py            # Box: fork(), run(), copy_in(), read_file(), tag(); thin over contree
│   │   ├── images.py         # build/pull/tag base images
│   │   └── services.py       # start postgres/redis/app inside a box; health wait
│   ├── schemas.py            # Finding, ProbePlan, Plan, Candidate, Verdict, Wave, RunEvent
│   ├── hunt/
│   │   ├── hunter.py         # orchestrates axes; baseline loader
│   │   ├── axes/race.py idor.py money.py malformed.py flow.py
│   │   └── filer.py          # groups by root cause; opens GitHub issues via gh
│   ├── plan/
│   │   ├── planner.py        # Ultra + Tavily → Plan
│   │   └── research.py       # tavily search+extract, citation objects
│   ├── fix/
│   │   ├── implementer.py    # TDD loop; Super writes, Lightning routes tools
│   │   └── tools.py          # read_file, write_file, run_shell, list_files, grep — JSON schemas
│   ├── verify/
│   │   ├── verifier.py       # orchestrates the checks → Verdict
│   │   ├── theater.py        # fork base, copy new tests only, must fail
│   │   ├── gate.py           # suite, mypy, ruff, alembic, openapi drift
│   │   └── race.py           # threads + Barrier probe inside the box
│   ├── integrate/
│   │   ├── waves.py          # file-conflict graph → waves
│   │   ├── merger.py         # merge --no-ff on the integration image
│   │   └── pr.py             # evidence-table PR body; gh pr create
│   ├── runlog/
│   │   ├── events.py         # append-only JSONL writer + SQLite index
│   │   └── render.py         # run → static HTML (console)
│   └── eval/
│       ├── swe_rebench.py    # task loop over Nebius preloaded images
│       └── report.py         # results.csv → README table
├── console/                  # FastAPI viewer + Dockerfile for Serverless Endpoint
├── ops/
│   ├── build-base.sh         # Fabric base image
│   ├── entrypoint.sh         # start pg + redis, exec "$@"
│   └── Dockerfile.base
├── evals/
│   ├── swe_rebench/results.csv, task_ids.txt
│   └── fabric/campaign.md
├── examples/fabric-issue-XXX/   # one complete recorded run
└── tests/                    # unit tests with recorded LLM/sandbox fixtures
```

## 3. Environment and secrets

`.env.example` (never commit `.env`):

```
NEBIUS_API_KEY=
TOKENFACTORY_BASE_URL=https://api.tokenfactory.nebius.com/v1
CONTREE_BASE_URL=            # from Sandboxes beta access
NEBIUS_PROJECT_ID=
TAVILY_API_KEY=
GITHUB_TOKEN=                # repo scope; used by gh
WARDEN_PLANNER_MODEL=nvidia/Nemotron-3-Ultra-550b-a55b
WARDEN_IMPLEMENTER_MODEL=nvidia/nemotron-3-super-120b-a12b
WARDEN_TOOLS_MODEL=nvidia/Nemotron-3_5-Lightning
WARDEN_MAX_FORKS=4
WARDEN_MAX_USD_PER_ISSUE=3.0
WARDEN_MAX_TOOL_TURNS=25
WARDEN_BASE_IMAGE=warden/fabric-base:latest
```

`config.py` validates all of these at startup and prints a one-line table of which model is bound to which role. If any key is missing the CLI exits with the variable name — no partial runs.

## 4. Core abstractions (write these first; everything else depends on them)

### 4.1 `sandbox/box.py`

Wrap Contree so the rest of the code never sees SDK details.

```python
@dataclass
class Box:
    image: Any            # contree image handle (checkpoint)
    uuid: str

    @classmethod
    def from_tag(cls, tag: str) -> "Box": ...            # sdk.images.use(tag)
    def run(self, shell: str, *, cwd="/app", env=None, timeout=900, keep=True) -> RunResult
        # image.run(shell=..., cwd=..., env=..., disposable=not keep).wait()
        # returns RunResult(stdout, stderr, exit_code, box=Box(new image) if keep else None)
    def fork(self) -> "Box"                             # no-op alias: any run() on self forks
    def copy_in(self, files: dict[str, bytes | Path], keep=True) -> "Box"
        # image.run(shell="true", files={...}, disposable=False)
    def read_file(self, path: str) -> str               # run("cat path") with stdout capture
    def tag(self, name: str) -> None                    # image.tag_as(name)
```

Rules: every `run` appends a `RunEvent(kind="sandbox", shell, exit_code, ms, from_uuid, to_uuid)`. A `RunResult` with `exit_code != 0` is never swallowed silently — callers decide. Default timeout 15 minutes; the full Fabric suite must fit.

### 4.2 `llm/client.py`

```python
class LLM:
    def structured(self, role: Role, system: str, user: str, schema: type[BaseModel], *, max_retries=2) -> BaseModel
    def chat(self, role: Role, messages: list[dict], *, tools: list[dict] | None = None) -> ChatResult
```

`structured()` sends `response_format={"type":"json_schema","json_schema":{"name":schema.__name__,"schema":schema.model_json_schema(),"strict":True}}` and also appends the schema as text to the system prompt (Token Factory docs recommend both). On validation failure it retries with the pydantic error appended; on the second failure it falls back to `{"type":"json_object"}` and validates again; on the third it raises. Every call logs `usage.prompt_tokens`, `usage.completion_tokens`, computed USD, role, latency into `cost.jsonl`, and the cost cap is checked before each call.

### 4.3 `schemas.py`

```python
class Citation(BaseModel):
    url: str
    claim: str


class Finding(BaseModel):
    axis: Literal["race", "idor", "money", "malformed", "flow"]
    severity: Literal["P0", "P1", "P2", "P3"]
    title: str
    repro: str
    expected: str
    actual: str
    request_id: str | None
    confirmed: bool
    reproduced_times: int
    root_cause_key: str


class Plan(BaseModel):
    issue: int
    summary: str
    files_to_touch: list[str]
    tests_to_add: list[str]
    landing_order: list[str]
    risk_class: list[Literal["money", "tax", "schema", "security", "concurrency", "none"]]
    citations: list[Citation]  # must be non-empty if any external library is touched
    acceptance: list[str]  # human-readable checks the verifier will run


class Verdict(BaseModel):
    decision: Literal["PASS", "PASS_WITH_CONCERNS", "FAIL"]
    theater_failed_on_base: bool
    suite: dict[str, int]
    mypy_ok: bool
    ruff_ok: bool
    alembic_single_head: bool
    openapi_drift: bool
    race: dict | None
    concerns: list[str]
    reasons: list[str]
```

## 5. Phase plan with done-criteria

### Phase A — Spike (Sep 15–21)

| # | Step | Command / file | Done when |
|---|---|---|---|
| A1 | Accounts | redeem `NEBIUS-DEVPOST-GLOBAL26`; Builders Program; Token Factory key; Sandboxes beta access; Tavily key; Discord `#sandboxes` question on pricing + judging-window persistence; register Berlin B&B Sep 29 | all keys in `.env`; Discord question posted; FEEDBACK.md has its first entry |
| A2 | Repo | `uv init warden`, layout from §2, `LICENSE` MIT, CI skeleton | `uv run ruff check . && uv run mypy .` green on empty modules |
| A3 | Hello sandbox | `Box.from_tag("ubuntu:latest").run("echo hi")`; then `c = box.run("echo $RANDOM > /tmp/r")`, `a = c.run("echo A >> /tmp/r")`, `b = c.run("echo B >> /tmp/r")` | `a.read_file("/tmp/r") != b.read_file("/tmp/r")`; latencies for run and fork recorded in FEEDBACK.md |
| A4 | Fabric base image | `ops/Dockerfile.base` FROM `backend/Dockerfile.dev` + `postgresql-16` + `redis-server`; `ops/entrypoint.sh` starts both, waits for `pg_isready`, execs `"$@"`; bake `alembic upgrade head` + `make seed-demo`; push to a registry; `sdk.images.oci("docker://<registry>/warden/fabric-base:<sha>")`; `tag_as("warden/fabric-base:latest")` | in-sandbox `pytest -q` passed count == Mac count (record both numbers) |
| A5 | First plan | `warden plan --issue <closed issue from PR #211> --dry-run` | a `Plan` JSON with ≥1 citation whose URL resolves; diff vs the human plan comment written to `docs/spike-A5.md` |
| A6 | First fix | `warden fix --issue <same> --no-verify` | candidate box exists; `pytest <new tests>` passes in it; cost < $3 |
| A7 | First theater check | `warden verify --candidate <uuid> --only theater` | new tests FAIL on base fork; screenshot of both results saved to `docs/spike-A7.png` |

If A4 fails because Postgres can't start under the sandbox process model: create a Nebius managed Postgres, bake only the app into the image, inject `DATABASE_URL` via `env=` on every `run`, and create a fresh database per issue with `CREATE DATABASE test_N TEMPLATE test_template` from inside the box. Decide by Thursday evening; do not spend Friday on it.

### Phase B — The loop (Sep 22–28)

| # | Step | Done when |
|---|---|---|
| B1 | `runlog/events.py` + `cost.py`; every LLM and sandbox call emits an event | `runs/<id>/events.jsonl` replays into a readable timeline via `warden console --run <id> --static` |
| B2 | `plan/planner.py` with Tavily research; prompt in `prompts/planner.md` | on 3 closed issues, plans name the right files (compare to the merged diff) |
| B3 | `fix/implementer.py`: TDD loop — write failing test → run → implement → run; tools via Lightning function calling; Super produces edits as full-file rewrites (not diffs — Nemotron is more reliable at whole files) | 3 closed issues re-fixed with new tests green; average tool turns and cost logged |
| B4 | `verify/gate.py`: suite, `mypy .`, `ruff check` + `format --check`, `alembic upgrade head` on an empty DB + `alembic heads` == 1, `make openapi-snapshot` drift | gate runs on the candidate box and produces the Verdict fields |
| B5 | `integrate/pr.py`: evidence table body; `gh pr create --draft` | a draft PR exists on a scratch branch of Fabric with the table filled from real numbers |
| B6 | `examples/fabric-issue-XXX/` recorded; CI replays it with LLM + sandbox calls stubbed from fixtures | CI green with the replay |

Exit: `warden run --issue N` from a clean shell produces a draft PR with evidence, unattended, under $3.

### Phase C — Verifier + Hunter (Sep 29 – Oct 5)

| # | Step | Done when |
|---|---|---|
| C0 | Berlin B&B (Sep 29 evening) | credits redeemed; two named contacts in FEEDBACK.md |
| C1 | `verify/theater.py` as a first-class step with its own event and screenshotable output | Verdict has `theater_failed_on_base` |
| C2 | `verify/race.py`: N threads + `threading.Barrier`, distinct `Idempotency-Key` per call, count effect rows in psql inside the box; applies when `risk_class` contains `concurrency` | reproduces a known race on the unfixed base (count > 1) and shows 1 on the candidate |
| C3 | Migration round-trip: `upgrade head → downgrade -1 → upgrade head` when a migration exists | in gate |
| C4 | `PENDING-HUMAN` labelling and PR body reason | risk_class money/tax/schema/security → label present, merge blocked |
| C5 | `hunt/axes/race.py`, `idor.py`, `money.py`: ported from RECIPES.md; run inside a fork against the seeded demo tenant; baseline loader reads `docs/reviews/12-bug-register.md` and marks re-finds as regressions | Hunter files one confirmed P0 with a repro that a human can paste and reproduce |
| C6 | `hunt/filer.py`: group by `root_cause_key`, `gh issue create` with the exact finding format | issues open on a scratch label `warden-hunt` |

Exit: first `PASS` verdict with all evidence rows on a real, open Fabric issue.

### Phase D — Waves + eval harness (Oct 6–12)

| # | Step | Done when |
|---|---|---|
| D1 | `integrate/waves.py`: parse each Plan's `files_to_touch` + functions (via `grep -n "def "` in the box) → conflict graph → waves; ≤1 migration per wave | wave plan printed for 6 issues matches what you'd do by hand |
| D2 | `integrate/merger.py`: `integration = integration.run("git merge --no-ff fix/N")`; on conflict → FAIL that issue, continue | 3-issue wave merges cleanly; next wave forks the updated image |
| D3 | `eval/swe_rebench.py`: for each task id → `sdk.images.use(<preloaded image>)` → plan → fix → verify → run task's hidden tests | 10 tasks complete; `results.csv` has columns `task_id, resolved, verdict, false_pass, theater_catch, tool_turns, usd, seconds` |
| D4 | best-of-N: `WARDEN_CANDIDATES=N` (eval: `--candidates N`), each candidate on its own fork of the base, verified, ranked by the rule in README "How the verifier decides" | `results.csv` has `candidates, chosen, resolved_candidates`; verdict.json lists every candidate and why the winner won |

Exit: `results.csv` with 10 rows; wave integration works.

### Phase E — The number + console (Oct 13–19)

| # | Step | Done when |
|---|---|---|
| E1 | Full eval: 50 SWE-rebench tasks, fixed seed, `task_ids.txt` committed; budget ≤ $75; run overnight with `WARDEN_MAX_FORKS=4` | `results.csv` 50 rows; `report.py` renders the README table |
| E2 | Fabric batch: ~10 issues (Hunter's + backlog) in waves; `evals/fabric/campaign.md` | table: found / confirmed / PASS / PWC / FAIL / PENDING-HUMAN / USD |
| E3 | Console: `runlog/render.py` static HTML per run; `console/` FastAPI serves `runs/` read-only; Dockerfile; deploy as Serverless Endpoint | public URL shows the example run's branching tree |
| E4 | README per §9 | a stranger can run `warden run` in 10 minutes following it |

Exit: README shows the number; demo URL works from a phone.

### Phase F — Ship (Oct 20–28)

Oct 20–23 RegWatch (separate plan). Oct 24 Warden freeze; only bug fixes. Oct 25–26 video (script in runbook §7), FEEDBACK.md into the form. Oct 27 clean-machine dry run of README + demo URL; license visible; video under 3:00. Oct 28 submit.

## 5b. Developer-experience requirements (from the DX research; full rules in the UX brief)

These are build items, not polish. Each lands in the phase noted.

| Item | Phase | Spec |
|---|---|---|
| `warden doctor` | A2 | one line per check (Python, git, gh, Nebius key, Sandboxes reachable, each model answers, Tavily key) with ✓/✗ and the fix; no heavy imports; < 100 ms to first line |
| `warden init` | B1 | detects repo + test runner, writes `warden.toml`; ≤ 5 questions |
| Streaming phase log | B1 | one line per phase with elapsed + running `$`; sub-steps indented; `--plain`; auto-off animation when not a TTY; respects `NO_COLOR` / `FORCE_COLOR` |
| stdout = data, stderr = progress | B1 | `warden run --json` emits JSONL events and a final `result` line (`run_id`, `verdict`, `pr_url`, `cost_usd`) so `| jq` works |
| Cost cap message | B1 | "cost cap $3.00 reached at Fix; stopping. Raise with --max-usd" |
| `--dry-run` | B2 | stops after Plan, prints the plan |
| `warden resume <run_id>` | B6 | run_id printed at start; state in `runs/<id>/`; resumes from the last completed phase |
| PR prompt + `--yes` | B5 | the only confirmation in the product |
| `--demo` | E4 | bundled tiny repo + recorded run so a judge needs no GitHub token |
| Error style | B1 | human sentence, then the fix, then `run_id`; traceback only with `--debug` |
| Startup latency | A2 | `typer-slim`; lazy-import openai/contree/tavily inside commands; `[project.scripts] warden = "warden.cli:app"`; `requires-python = ">=3.12"` so `warden` works |
| README hero | E4 | VHS `.tape` → GIF in CI; three commands: `warden doctor`, `warden run --demo`, console URL |

## 6. Prompts (keep in `prompts/*.md`, versioned, referenced by hash in events)

**planner.md** — system: "You are planning a fix in a FastAPI + SQLAlchemy 2 + Postgres codebase with row-level security and a double-entry ledger. Cite documentation URLs for any library behaviour you rely on. Classify risk. Output only the JSON schema below." user: issue title + body + comments, `git ls-files` of relevant dirs, the failing test if present, Tavily results (title, url, 400-char snippet). Include the schema text.

**implementer.md** — system: repo conventions lifted from Fabric's CLAUDE.md (Zod-like validators, error envelope, idempotency, no raw SQL, no edits to applied migrations), "write the failing test first, run it, then implement, run again; never delete a failing test to make the suite pass; whole-file rewrites only". Tools: `list_files`, `read_file`, `grep`, `write_file`, `run_shell` (cwd `/app`, timeout 600s).

**verifier.md** — system: "You are an adversarial reviewer. You did not write this code. Assume the tests may be theater. Your job is to find a reason to FAIL." user: diff, test names, gate outputs, theater result, race result. Output `Verdict`.

**hunter_probeplan.md** — system: the eight-axis method from `flow-bug-finder` condensed; user: `/openapi.json` endpoint list + the flow map section for the target flow; output `ProbePlan`.

## 7. Tests (`tests/`)

Unit: schemas round-trip; cost math; wave planner on synthetic conflict graphs; PR body renderer; theater logic with a fake `Box`. Integration (offline): replay of `examples/` via recorded fixtures. Live (manual, `-m live`): A3-style sandbox smoke; one structured call per model. CI runs unit + replay only.

## 8. Operating rules while building

Commit each green step with the step id in the message (`feat(B3): implementer TDD loop`). Run anything longer than five minutes as a background task. If a sandbox op hangs > 15 minutes, cancel via the operations API and log it in FEEDBACK.md with the operation id. Never test against Fabric `main`; use branch `warden-scratch`. Keep `WARDEN_MAX_FORKS` at 4 until the Discord answer on billing arrives.

## 9. README structure (judges)

1. Name + one sentence + 30-second GIF of the branching tree.
2. **Runs on Nebius** — Token Factory model IDs, Sandboxes (fork/rollback used for the theater check and best-of-N), Serverless Endpoint for the console.
3. **NVIDIA models** — routing table (role → model → why).
4. **Results** — SWE-rebench table (n, resolved %, verifier false-PASS %, theater catches, $/task, task_ids link) and Fabric campaign table.
5. How it works — the pipeline diagram; one paragraph per stage.
6. Quickstart — `uv sync`, `.env`, `warden run --repo … --issue …`.
7. Example run — link into `examples/` and the console.
8. Limitations — beta caps, Nemotron vs frontier, private test repo, what the Hunter doesn't cover (frontend).
9. Feedback → link to FEEDBACK.md. License.

## 10. Acceptance checklist before freeze (Oct 24)

- `warden run` unattended on a fresh issue → draft PR with all evidence rows.
- Hunter finds and files at least one confirmed P0 on the seeded tenant on its own.
- `results.csv` has 50 rows; README table generated from it, not typed.
- Verifier false-PASS rate is measured and printed even if it is not zero.
- Console URL loads the example run on a phone.
- CI green; `LICENSE` MIT at top level; `.env` not in git; no secrets in `examples/`.
