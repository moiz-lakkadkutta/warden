"""warden — hunt → plan → fix → verify → PR, on Nebius Sandboxes with Nemotron.

DX rules (clig.dev / Heroku): first output < 100 ms; `--help` and `doctor` never touch the
network; stdout is data, stderr is progress; `--json` streams JSONL events; every prompt has a flag.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn, cast

import typer

from warden import ui
from warden.ui import fail

if TYPE_CHECKING:
    from warden.config import Settings
    from warden.hunt.issue import EventIssue
    from warden.runlog.events import EventLog
    from warden.schemas import Phase

app = typer.Typer(
    name="warden",
    help="Autonomous hunt → plan → fix → verify → PR agent on Nebius Token Factory Sandboxes.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,  # unexpected errors: plain traceback, rich never imported
)

PHASES = ("hunt", "plan", "fix", "verify", "pr")
MIN_TOOL_TURNS = 40  # doctor warns below this: live fixes needed 52 (icalendar#1928)

# NOTE: `typer.Exit` subclasses RuntimeError — never `except RuntimeError` / `except Exception`
# around command bodies, or exits (and the cost cap) get swallowed.


@app.callback()
def main(
    plain: Annotated[bool, typer.Option("--plain", help="No colour or animation.")] = False,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show tracebacks after the error block.")
    ] = False,
) -> None:
    """Global options; they must precede the subcommand: `warden --plain run --demo`."""
    ui.configure(ui.resolve(plain=plain, debug=debug))


def _err(msg: str) -> None:
    ui.err(msg)


def _mark(ok: bool) -> str:
    return ui.paint("✓", "32") if ok else ui.paint("✗", ui.RED)


@contextlib.contextmanager
def _guarded(log: EventLog) -> Iterator[None]:
    """The only sanctioned catches around the machine: each becomes the error block + exit code.

    Cost cap / provider budget (402) → 4, PR open → 1, not-ready → 3. Nothing else is caught
    (see the NOTE above): `typer.Exit` and real bugs propagate. The log is closed on every path.
    """
    from warden.integrate.pr import PrOpenError
    from warden.llm.client import ProviderBudgetError
    from warden.llm.cost import CostCapExceededError
    from warden.pipeline import NotReadyError
    from warden.sandbox.external import BaseBuildError

    try:
        yield
    except CostCapExceededError as e:  # cost.py already emitted the cost_cap event
        fail(str(e), run_id=log.run_id, code=4, exc=e)
    except BaseBuildError as e:
        fail(str(e), fix=e.fix, run_id=log.run_id, code=3, exc=e)
    except ProviderBudgetError as e:  # client.py already emitted the error event
        _record_failure(log, str(e), e.fix, 4)
        fail(str(e), fix=f"{e.fix} {log.run_id}", run_id=log.run_id, code=4, exc=e)
    except PrOpenError as e:
        _record_failure(log, str(e), e.fix, 1)
        fail(str(e), fix=e.fix, run_id=log.run_id, exc=e)
    except NotReadyError as e:
        _record_failure(log, str(e), e.fix, 3)
        fail(str(e), fix=e.fix, run_id=log.run_id, code=3, exc=e)
    finally:
        log.close()


def _record_failure(log: EventLog, message: str, fix: str | None, code: int) -> None:
    """An `error` event in the phase that was running, so `warden comment` can say why."""
    from warden.pipeline import PHASES, completed_phases

    phase = PHASES[min(len(completed_phases(log.dir)), len(PHASES) - 1)]
    log.emit(phase, "error", {"text": message, "fix": fix, "exit_code": code})


def _fail_run(log: EventLog, message: str, *, fix: str, code: int) -> NoReturn:
    """`fail` for a run that has a log: the reason is recorded before the error block."""
    _record_failure(log, message, fix, code)
    fail(message, fix=fix, run_id=log.run_id, code=code)


def _plain_override(plain: bool) -> None:
    if plain:
        cfg = ui.current()
        ui.configure(ui.UIConfig(color=False, animate=False, debug=cfg.debug))


# --------------------------------------------------------------------------- doctor
@app.command()
def doctor(
    live: Annotated[
        bool, typer.Option(help="Also call each model once (costs a few cents).")
    ] = False,
) -> None:
    """Check local tools, credentials and (optionally) model reachability. No network by default."""
    from warden.config import MAX_CANDIDATES, MAX_THEATER_RETRIES, load_settings

    print("warden doctor", file=sys.stderr)
    s = load_settings()
    checks: list[tuple[str, bool, str]] = []

    checks.append(
        ("Python ≥ 3.12", sys.version_info >= (3, 12), "install with: uv python install 3.12")
    )
    checks.append(("git", shutil.which("git") is not None, "install git"))
    checks.append(
        ("gh (GitHub CLI)", shutil.which("gh") is not None, "brew install gh && gh auth login")
    )
    checks.append(
        (
            "NEBIUS_API_KEY",
            bool(s.nebius_api_key),
            "get a key at tokenfactory.nebius.com → put it in .env",
        )
    )
    checks.append(
        (
            "CONTREE_BASE_URL (Sandboxes)",
            bool(s.contree_base_url),
            "request Sandboxes beta access; set CONTREE_BASE_URL",
        )
    )
    checks.append(("NEBIUS_PROJECT_ID", bool(s.nebius_project_id), "copy from the Nebius console"))
    checks.append(
        ("TAVILY_API_KEY", bool(s.tavily_api_key), "free key at tavily.com; needed for cited plans")
    )
    checks.append(
        (
            f"WARDEN_TARGET_REPO ({s.target_repo or 'unset'})",
            s.target_repo.count("/") == 1,
            "set WARDEN_TARGET_REPO=owner/name in .env",
        )
    )
    checks.append(
        (
            f"WARDEN_WORKDIR ({s.workdir})",
            s.workdir.startswith("/"),
            "set WARDEN_WORKDIR to the absolute repo root inside the base image",
        )
    )
    from warden.verify.gate import PROFILES

    checks.append(
        (
            f"WARDEN_GATE_PROFILE ({s.gate_profile})",
            s.gate_profile in PROFILES,
            f"set WARDEN_GATE_PROFILE to one of: {', '.join(PROFILES)}",
        )
    )
    suite = s.test_cmd or (PROFILES[s.gate_profile].suite if s.gate_profile in PROFILES else "")
    checks.append(
        (
            f"WARDEN_TEST_CMD ({s.test_cmd or 'unset'})",
            bool(suite),
            "set WARDEN_TEST_CMD to the repo's test command (e.g. `pytest -q`); "
            f"profile {s.gate_profile} runs no suite without it",
        )
    )
    checks.append(
        (
            f"WARDEN_SPEC_CHECK ({'on' if s.spec_check else 'off'})",
            True,
            "set WARDEN_SPEC_CHECK=0 to skip the verifier's spec review (one Ultra call per issue)",
        )
    )
    checks.append(
        (
            f"WARDEN_CANDIDATES ({s.candidates})",
            1 <= s.candidates <= MAX_CANDIDATES,
            f"set WARDEN_CANDIDATES to 1-{MAX_CANDIDATES} (1 = one implementer run per issue)",
        )
    )
    checks.append(
        (
            f"WARDEN_THEATER_RETRIES ({s.theater_retries or 'off'})",
            0 <= s.theater_retries <= MAX_THEATER_RETRIES,
            f"set WARDEN_THEATER_RETRIES to 0-{MAX_THEATER_RETRIES} (0 = a theater FAIL is final)",
        )
    )
    checks.append(
        (
            f"WARDEN_IDLE_NUDGE_AFTER ({s.idle_nudge_after or 'off'}), "
            f"WARDEN_READ_CACHE ({'on' if s.read_cache else 'off'})",
            True,
            "implementer read-loop guards",
        )
    )
    checks.append(
        (
            f"WARDEN_BASE_BRANCH ({s.base_branch or 'unset'})",
            bool(s.base_branch.strip()),
            "set WARDEN_BASE_BRANCH to the branch fix PRs target, e.g. main",
        )
    )
    checks.append(
        (
            f"WARDEN_PR_TEMPLATE ({s.pr_template or 'built-in'})",
            not s.pr_template or Path(s.pr_template).expanduser().is_file(),
            "point WARDEN_PR_TEMPLATE at a markdown file, or unset it for the built-in body",
        )
    )
    checks.append((".env not tracked by git", not _env_tracked(), "git rm --cached .env"))
    warnings: list[str] = []
    if s.max_tool_turns < MIN_TOOL_TURNS:
        warnings.append(
            f"WARDEN_MAX_TOOL_TURNS={s.max_tool_turns} is below {MIN_TOOL_TURNS}: real fixes run "
            "out of turns (icalendar#1928 needed 52) → set WARDEN_MAX_TOOL_TURNS=60 in .env"
        )

    if live:
        checks.extend(_live_model_checks(s))

    width = max(len(c[0]) for c in checks) + 2
    all_ok = True
    for name, ok, fix in checks:
        all_ok &= ok
        line = f"  {_mark(ok)} {name.ljust(width)}"
        if not ok:
            line += f"→ {fix}"
        _err(line)
    for warning in warnings:  # advisory: never changes the exit code
        _err(f"  {ui.paint('!', '33')} {warning}")
    _err("")
    _err("Model routing:")
    for role, model in s.routing_table():
        _err(f"  {role.ljust(12)} {model}")
    raise typer.Exit(code=0 if all_ok else 1)


def _env_tracked() -> bool:
    import subprocess

    if shutil.which("git") is None or not Path(".git").exists():
        return False
    res = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], capture_output=True, text=True
    )
    return res.returncode == 0


def _live_model_checks(s: object) -> list[tuple[str, bool, str]]:
    from warden.config import Role, Settings
    from warden.llm.client import LLM
    from warden.llm.cost import CostCapExceededError, CostMeter
    from warden.runlog.events import EventLog

    assert isinstance(s, Settings)
    out: list[tuple[str, bool, str]] = []
    log = EventLog(s.runs_dir, "doctor")
    llm = LLM(s, CostMeter(cap_usd=0.50, log=log), log)
    try:
        for role in (Role.PLANNER, Role.IMPLEMENTER, Role.TOOLS):
            try:
                r = llm.chat(
                    role, "plan", [{"role": "user", "content": "Reply with the single word: ok"}]
                )
                out.append(
                    (f"model {s.model_for(role)}", bool(r.content), "check model id / quota")
                )
            except CostCapExceededError:
                raise  # never caught to continue
            except Exception as exc:  # noqa: BLE001
                out.append((f"model {s.model_for(role)}", False, str(exc)[:80]))
    finally:
        log.close()
    return out


# ------------------------------------------------------------------------------ init
@app.command()
def init(
    repo: Annotated[Path, typer.Argument(help="Path to the target repository.")] = Path("."),
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept detected defaults.")] = False,
) -> None:
    """Detect the repo's test runner and write warden.toml. Never prompts (0 questions)."""
    repo = repo.resolve()
    app_dir = "backend" if (repo / "backend").is_dir() else "."
    runner, test_cmd = _detect_test_cmd(repo, app_dir)
    cfg = repo / "warden.toml"
    if cfg.exists() and not yes:
        fail(f"{cfg} exists", fix="re-run with --yes to overwrite")
    if not (repo / ".git").exists():
        _err(f"warning: {repo} has no .git — Warden opens PRs from a git checkout")
    if runner == "unknown":
        _err("could not detect a test runner; set test_cmd in warden.toml")
    cfg.write_text(
        "\n".join(
            [
                "# warden.toml — project settings",
                "# precedence: flags > env > this file > ~/.config/warden/config.toml",
                f'test_cmd = "{test_cmd}"',
                f'app_dir = "{app_dir}"',
                'base_image = "warden/fabric-base:latest"',
                "max_usd_per_issue = 3.0",
                "max_forks = 4",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _err(f"wrote {cfg} (test runner: {runner}, app dir: {app_dir})")


def _detect_test_cmd(repo: Path, app_dir: str) -> tuple[str, str]:
    """(runner, command). Looks only at the root and app_dir — no recursive globs."""
    roots = [repo] if app_dir == "." else [repo, repo / app_dir]
    for root in roots:
        if any(
            (root / f).exists() for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
        ):
            return "pytest", "pytest -q"
    for root in roots:
        if (root / "package.json").exists():
            return "npm", "npm test"
        if (root / "go.mod").exists():
            return "go", "go test ./..."
        if (root / "Cargo.toml").exists():
            return "cargo", "cargo test"
    return "unknown", ""


# ------------------------------------------------------------------------------- run
@app.command()
def run(
    issue: Annotated[int | None, typer.Option(help="GitHub issue number to fix.")] = None,
    demo: Annotated[
        bool,
        typer.Option(help="Run against the bundled demo with the fake sandbox (no keys needed)."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Stop after Plan and print it.")
    ] = False,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Stream JSONL events to stdout; final line is the result."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Open the PR without asking.")] = False,
    max_usd: Annotated[float | None, typer.Option(help="Override the per-issue cost cap.")] = None,
    plain: Annotated[bool, typer.Option(help="No colour or animation.")] = False,
    run_id: Annotated[str | None, typer.Option("--run-id", hidden=True)] = None,
    at: Annotated[
        str | None,
        typer.Option(
            "--at",
            help="Replay a closed issue at this commit (e.g. the fix's parent): plan, fix and "
            "verify run on that code, issue comments are hidden, the PR targets warden/base-<sha>.",
        ),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="A public GitHub repo (owner/name) to fix the issue in: Warden clones its default "
            "branch head into a sandbox, installs it with test deps and caches that checkpoint.",
        ),
    ] = None,
    test_cmd: Annotated[
        str | None,
        typer.Option(
            "--test-cmd",
            help="The repo's own test command, no file args (default: WARDEN_TEST_CMD; with "
            "--repo, detected: python -m pytest).",
        ),
    ] = None,
    install_cmd: Annotated[
        str | None,
        typer.Option(
            "--install-cmd",
            help="With --repo: the install command run in the clone (default: detected from "
            "pyproject.toml / uv.lock / requirements files).",
        ),
    ] = None,
    no_pr: Annotated[
        bool,
        typer.Option(
            "--no-pr",
            help="Stop after Verify: no push, no fork, no PR (same as --stop-after verify).",
        ),
    ] = False,
    stop_after: Annotated[
        str | None,
        typer.Option("--stop-after", help="Stop after this phase: hunt, plan, fix or verify."),
    ] = None,
    from_event: Annotated[
        bool,
        typer.Option(
            "--from-event",
            help="Take the issue and repo from the GitHub Actions `issues` event at "
            "$GITHUB_EVENT_PATH (comments are ignored). The PR is a draft, as always.",
        ),
    ] = False,
) -> None:
    """Hunt → plan → fix → verify → PR for one issue. Prints run_id first (see `warden resume`)."""
    from warden.config import load_settings
    from warden.runlog.events import EventLog, stdout_stream

    _plain_override(plain)
    s = load_settings()
    if max_usd is not None:
        s.max_usd_per_issue = max_usd
    if test_cmd is not None:
        s.test_cmd = test_cmd
    stop = _stop_phase(dry_run=dry_run, no_pr=no_pr, stop_after=stop_after)
    if repo is not None:
        from warden.sandbox.external import valid_repo

        if not valid_repo(repo):
            fail(
                f"--repo {repo!r} is not owner/name", fix="e.g. --repo collective/icalendar", code=2
            )
    elif install_cmd is not None:
        fail("--install-cmd needs --repo", fix="add --repo owner/name", code=2)
    if run_id is not None and (s.runs_dir / run_id / "run.json").exists():
        fail(
            f"run {run_id} already exists",
            fix=f"warden resume {run_id}, or pick another --run-id",
            run_id=run_id,
            code=2,
        )
    log = EventLog(s.runs_dir, run_id, json_stream=stdout_stream() if json_out else None)
    _err(f"run_id {log.run_id}   (resume with: warden resume {log.run_id})")
    progress = ui.PhaseLog()  # stderr; --json only changes stdout

    with _guarded(log):
        if demo:
            import warden.demo

            result = warden.demo.run_demo(
                s, log, stop_after=stop, progress=progress, yes=yes, json_out=json_out
            )
            if not json_out:
                _err(result["evidence"])
            raise typer.Exit(0)

        event_issue = None
        if from_event:
            event_issue = _event_issue(log, issue)
            issue = event_issue.issue.number
            s.target_repo = event_issue.repo
        if issue is None:
            _fail_run(
                log,
                "nothing to run",
                fix="pass --issue N, or --demo to try Warden without credentials",
                code=2,
            )
        missing = s.missing_for("NEBIUS_API_KEY", "CONTREE_BASE_URL", "NEBIUS_PROJECT_ID")
        if missing:
            _fail_run(log, f"missing {', '.join(missing)}", fix="run `warden doctor`", code=2)
        from warden.pipeline import live_steps, run_pipeline

        if repo is not None:
            s = _external_settings(s, log, repo, test_cmd=test_cmd, install_cmd=install_cmd)
        result = run_pipeline(
            s,
            log,
            live_steps(),
            start_after=None,
            json_out=json_out,
            yes=yes,
            progress=progress,
            stop_after=stop,
            requested_issue=issue,
            at_rev=at,
            issue=event_issue.issue if event_issue is not None else None,
        )
        if not json_out:
            _err(result["evidence"])


STOPS = ("hunt", "plan", "fix", "verify")
EXTERNAL_BASE = "base.json"  # runs/<id>/base.json: a --repo run's checkpoint, re-applied on resume


def _stop_phase(*, dry_run: bool, no_pr: bool, stop_after: str | None) -> Phase | None:
    """--dry-run = plan, --no-pr = verify, --stop-after <phase>; two different stops is an error."""
    if stop_after is not None and stop_after not in STOPS:
        fail(
            f"--stop-after {stop_after!r} is not a phase", fix=f"one of: {', '.join(STOPS)}", code=2
        )
    asked = {p for p in ("plan" if dry_run else None, "verify" if no_pr else None, stop_after) if p}
    if len(asked) > 1:
        fail(
            f"--dry-run, --no-pr and --stop-after disagree ({', '.join(sorted(asked))})",
            fix="pass one of them",
            code=2,
        )
    return cast("Phase | None", next(iter(asked), None))


def _external_settings(
    s: Settings, log: EventLog, repo: str, *, test_cmd: str | None, install_cmd: str | None
) -> Settings:
    """Build (or reuse) the sandbox base for a public repo; the run's settings point at it."""
    from warden.sandbox.box import ContreeBackend
    from warden.sandbox.external import prepare_base, remote_head

    head = remote_head(repo)
    _err(f"base: {repo}@{head.sha[:7]} ({head.branch}) — building or reusing the sandbox base")
    backend = ContreeBackend(s.nebius_api_key, s.contree_base_url, s.nebius_project_id)
    base = prepare_base(
        backend,
        log,
        repo,
        head,
        cache_path=s.runs_dir / ".bases.json",
        install_cmd=install_cmd,
        test_cmd=test_cmd or s.test_cmd or None,
    )
    _err(f"base: {base.image} ({'cached' if base.cached else 'built'}) · tests: {base.test_cmd}")
    (log.dir / EXTERNAL_BASE).write_text(base.to_json(), encoding="utf-8")
    return s.model_copy(update=base.overrides())


def _event_issue(log: EventLog, issue: int | None) -> EventIssue:
    """The issue named by $GITHUB_EVENT_PATH; a usage error (exit 2) when there is none."""
    import os

    from warden.hunt.issue import EventError, issue_from_event

    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path:
        _fail_run(
            log,
            "--from-event needs GITHUB_EVENT_PATH",
            fix="run inside a GitHub Actions `issues` workflow, or pass --issue N instead",
            code=2,
        )
    try:
        ev = issue_from_event(Path(path))
    except EventError as e:
        _fail_run(log, str(e), fix="trigger on `issues: [labeled]` for an open issue", code=2)
    if issue is not None and issue != ev.issue.number:
        _fail_run(
            log,
            f"--issue {issue} disagrees with the event's issue #{ev.issue.number}",
            fix="drop --issue when using --from-event",
            code=2,
        )
    return ev


@app.command()
def comment(
    run_id: Annotated[str, typer.Option("--run-id", "--run", help="The run to summarise.")],
    exit_code: Annotated[
        int, typer.Option("--exit-code", help="`warden run`'s exit code, for a crashed run.")
    ] = 0,
    log_url: Annotated[
        str | None, typer.Option("--log-url", help="Link to the CI log for this run.")
    ] = None,
) -> None:
    """Print a Markdown issue comment for a run: the draft PR link, or why not, and the cost."""
    from warden.config import load_settings
    from warden.integrate.comment import issue_comment

    s = load_settings()
    text = issue_comment(s.runs_dir / run_id, run_id=run_id, exit_code=exit_code, log_url=log_url)
    sys.stdout.write(text)


@app.command("eval")
def eval_swe(
    n: Annotated[int, typer.Option(help="How many SWE-rebench tasks to sample.")] = 10,
    seed: Annotated[int, typer.Option(help="Sampling seed (fixed for a reportable run).")] = 7,
    budget: Annotated[
        float, typer.Option(help="Stop before a task that could take total spend over this (USD).")
    ] = 5.0,
    max_usd: Annotated[float | None, typer.Option(help="Override the per-task cost cap.")] = None,
    candidates: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=8,
            help="Best-of-N: implementer candidates per task (overrides WARDEN_CANDIDATES); "
            "the per-task cap covers all of them.",
        ),
    ] = None,
    out: Annotated[
        Path, typer.Option(help="Directory for tasks.jsonl, task_ids.txt, results.csv.")
    ] = Path("evals/swe_rebench"),
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Start without asking.")] = False,
    shard: Annotated[
        str | None,
        typer.Option(
            help="Run only shard i of k (tasks at index ≡ i mod k) into results.shard-i.csv; "
            "start k processes on one committed tasks.jsonl. --budget is per shard."
        ),
    ] = None,
) -> None:
    """SWE-rebench on Nebius preloaded images: plan → fix → verify → hidden tests → results.csv.

    Tasks already in results.csv are skipped, so re-running the same command resumes. Tasks in
    the out dir's excluded.txt (bases that cannot be graded) are not run."""
    from warden.config import load_settings
    from warden.eval import shard as sh
    from warden.eval import swe_rebench as swe
    from warden.eval.bases import read_excluded
    from warden.runlog.events import EventLog

    try:
        part = sh.parse_shard(shard) if shard is not None else None
    except ValueError as e:
        fail(str(e), fix="e.g. --shard 0/5 … --shard 4/5", code=2)
    s = load_settings()
    if max_usd is not None:
        s.max_usd_per_issue = max_usd
    if candidates is not None:
        s.candidates = candidates
    missing = s.missing_for("NEBIUS_API_KEY", "CONTREE_BASE_URL", "NEBIUS_PROJECT_ID")
    if missing:
        fail(f"missing {', '.join(missing)}", fix="run `warden doctor`", code=2)
    tasks_path = out / "tasks.jsonl"
    if part is not None and not tasks_path.exists():  # k shards must share one sampled list
        fail(
            f"no {tasks_path}", fix="sample first: warden eval --out DIR --n N (no --shard)", code=2
        )
    if tasks_path.exists():
        tasks = swe.load_tasks(tasks_path)
    else:
        _err(f"sampling {n} tasks from {swe.DATASET} ({swe.SPLIT}, seed {seed})")
        tasks = swe.fetch_sample(tasks_path, n, seed=seed)
        (out / "task_ids.txt").write_text("\n".join(t.instance_id for t in tasks) + "\n")
    if part is not None:
        tasks = sh.take(tasks, *part)
    skip = read_excluded(out / "excluded.txt")
    tasks = [t for t in tasks if t.instance_id not in skip]
    csv_path = out / sh.csv_name(part)
    todo = [t for t in tasks if t.instance_id not in swe.done_ids(csv_path)]
    _err(
        f"{len(todo)} of {len(tasks)} tasks to run · budget ${budget:.2f} · "
        f"cap ${s.max_usd_per_issue:.2f}/task · {s.candidates} candidate(s)/task · "
        f"results → {csv_path}"
    )
    if not todo:
        raise typer.Exit(0)
    if not yes and not typer.confirm("Start (this spends Token Factory credit)?", err=True):
        raise typer.Exit(1)

    def show(row: swe.Row) -> None:
        mark = "✓" if row.resolved else "✗"
        extra = f"  {row.error}" if row.error else ""
        if row.candidates > 1:
            picked = row.resolved_candidates or "none"
            extra += f"  #{row.chosen} of {row.candidates} (hidden tests accept: {picked})"
        _err(
            f"{mark} {row.task_id}  {row.verdict}  ${row.usd:.2f}  {row.seconds}s"
            f"{'  FALSE PASS' if row.false_pass else ''}{extra}"
        )

    log = EventLog(s.runs_dir, "eval" if part is None else f"eval-shard-{part[0]}")
    with _guarded(log):
        rows = swe.run_eval(todo, s, csv_path=csv_path, budget_usd=budget, on_row=show)
    resolved = sum(r.resolved for r in rows)
    _err(f"ran {len(rows)} · resolved {resolved} · spent ${sum(r.usd for r in rows):.2f}")


@app.command()
def campaign(
    issues: Annotated[str, typer.Option(help="Comma-separated issue numbers, e.g. 196,197,200.")],
    at: Annotated[
        str | None,
        typer.Option(
            "--at",
            help="Replay these closed issues at this commit (one shared base); default: the "
            "target's live head, for open issues.",
        ),
    ] = None,
    out: Annotated[
        Path, typer.Option(help="Directory for campaign.csv, campaign.md, waves.json.")
    ] = Path("evals/fabric"),
    budget: Annotated[
        float, typer.Option(help="Skip a wave whose worst case would take spend over this (USD).")
    ] = 15.0,
    max_usd: Annotated[float | None, typer.Option(help="Override the per-issue cost cap.")] = None,
    name: Annotated[
        str | None,
        typer.Option(help="Campaign id (re-use one to resume its runs); default: a new run id."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Start and open draft PRs without asking.")
    ] = False,
) -> None:
    """Plan → waves → fix/verify/draft PR per wave in parallel → merge (D2) → gate → campaign.md.

    Every issue is an ordinary run (runs/<campaign>-<issue>/); WARDEN_MAX_FORKS bounds the
    parallel runs and the wave size. Rows land in campaign.csv (same issue + --at replaces)."""
    from warden.config import load_settings
    from warden.eval import campaign as cp
    from warden.runlog.events import new_run_id

    try:
        numbers = [int(x) for x in issues.replace(" ", "").split(",") if x]
    except ValueError:
        fail(f"--issues {issues!r} is not a list of numbers", fix="e.g. --issues 196,197", code=2)
    if not numbers or len(set(numbers)) != len(numbers):
        fail("--issues needs distinct issue numbers", fix="e.g. --issues 196,197", code=2)
    s = load_settings()
    if max_usd is not None:
        s.max_usd_per_issue = max_usd
    missing = s.missing_for("NEBIUS_API_KEY", "CONTREE_BASE_URL", "NEBIUS_PROJECT_ID")
    if missing:
        fail(f"missing {', '.join(missing)}", fix="run `warden doctor`", code=2)
    cid = name or f"campaign-{new_run_id()}"
    _err(
        f"campaign {cid} · {len(numbers)} issues · {at or 'live head'} · "
        f"cap ${s.max_usd_per_issue:.2f}/issue · budget ${budget:.2f} · "
        f"{s.max_forks} in parallel · → {out}"
    )
    if not yes and not typer.confirm(
        f"Start (spends Token Factory credit, opens draft PRs on {s.target_repo})?", err=True
    ):
        raise typer.Exit(1)
    rows = cp.run_campaign(
        numbers,
        s,
        cp.live_deps(s, at=at),
        campaign_id=cid,
        at=at,
        out_dir=out,
        yes=yes,
        budget_usd=budget,
    )
    for r in rows:
        print(json.dumps({k: v for k, v in r.__dict__.items()}), flush=True)
    t = cp.tally(rows)
    _err(
        f"done · PASS {t['PASS']} · PWC {t['PASS_WITH_CONCERNS']} · FAIL {t['FAIL']} · "
        f"merged {t['merged']} · PRs {t['PRs']} · ${t['USD']:.2f} · {out / cp.MD_NAME}"
    )


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="The run_id printed by `warden run`.")],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Stream JSONL events to stdout; final line is the result."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Open the PR without asking.")] = False,
    plain: Annotated[bool, typer.Option(help="No colour or animation.")] = False,
) -> None:
    """Continue an interrupted run from its last completed phase (artifacts decide, not events)."""
    from warden.config import load_settings
    from warden.pipeline import completed_phases, live_steps, run_pipeline
    from warden.runlog.events import EventLog, stdout_stream

    _plain_override(plain)
    s = load_settings()
    run_dir = s.runs_dir / run_id
    if not (run_dir / "events.jsonl").exists():
        fail(f"no run {run_id} under {s.runs_dir}", fix=f"ls {s.runs_dir}", run_id=run_id)
    if not (run_dir / "run.json").exists():
        fail(f"run {run_id} predates resume", fix="re-run: warden run --demo", run_id=run_id)
    done = completed_phases(run_dir)
    if "pr" in done:
        fail(
            f"run {run_id} already complete",
            fix=f"warden console --run {run_id}",
            run_id=run_id,
            code=2,
        )
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    mode = manifest.get("mode")
    if (run_dir / EXTERNAL_BASE).exists():  # a --repo run: same base, repo and test command
        from warden.sandbox.external import ExternalBase

        ext = ExternalBase.from_json((run_dir / EXTERNAL_BASE).read_text(encoding="utf-8"))
        s = s.model_copy(update=ext.overrides())
    log = EventLog(s.runs_dir, run_id, json_stream=stdout_stream() if json_out else None)
    _err(f"run_id {run_id}   (resuming after {done[-1] if done else 'start'})")
    progress = ui.PhaseLog()

    with _guarded(log):
        if mode == "demo":
            import warden.demo

            steps = warden.demo.demo_steps()
        else:
            steps = live_steps()
        result = run_pipeline(
            s,
            log,
            steps,
            start_after=done[-1] if done else None,
            json_out=json_out,
            yes=yes,
            progress=progress,
            requested_issue=manifest.get("issue"),
        )
        if not json_out:
            _err(result["evidence"])


@app.command()
def console(
    run_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Run ids to render (default: the latest run).", show_default=False),
    ] = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", "--run", help="Render one run to static HTML.")
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Write a deployable site here: index.html listing the runs, <id>/index.html "
            "per run with its events.jsonl, cost.jsonl, pr_body.md and timeline.gif.",
        ),
    ] = None,
    all_runs: Annotated[bool, typer.Option("--all", help="Every run under runs/.")] = False,
    gif: Annotated[
        bool, typer.Option(help="Also render runs/<id>/timeline.gif (needs the gif extra).")
    ] = True,
    static: Annotated[
        bool, typer.Option(help="Write runs/<id>/index.html instead of serving.")
    ] = True,
) -> None:
    """Render a run's timeline, branch tree and evidence table (stdout: the page written)."""
    from warden.config import load_settings
    from warden.runlog.site import list_runs

    s = load_settings()
    known = list_runs(s.runs_dir)
    ids = list(run_ids or []) + ([run_id] if run_id else [])
    if all_runs:
        ids = known
    if not known and not ids:
        fail("no runs yet", fix="warden run --demo")
    if not ids:
        ids = known[-1:]
    missing = [i for i in ids if i not in known]
    if missing:
        fail(f"no run {missing[0]} under {s.runs_dir}", fix=f"ls {s.runs_dir}", code=2)

    if out is not None:
        from warden.runlog.site import build_site

        for warning in build_site(s.runs_dir, ids, out, gif=gif):
            _err(f"warning: {warning}")
        _err(f"wrote {len(ids)} run page(s) to {out}")
        print(out / "index.html")
        return

    from warden.runlog.gif import GifUnavailableError
    from warden.runlog.render import render_run
    from warden.runlog.site import write_gif

    for rid in ids:
        has_gif = False
        if gif:
            try:
                _err(f"wrote {write_gif(s.runs_dir, rid)}")
                has_gif = True
            except GifUnavailableError as exc:
                _err(f"warning: {exc}; skipped timeline.gif → {exc.fix}")
        print(render_run(s.runs_dir, rid, has_gif=has_gif))


# ---------------------------------------------------------------------------- report
@app.command()
def report(
    csvs: Annotated[
        list[Path], typer.Argument(help="results CSVs (shards are merged, one row per task).")
    ],
    md: Annotated[bool, typer.Option("--md", help="Print the markdown table, not JSON.")] = False,
    label: Annotated[str, typer.Option(help="Column header of the markdown table.")] = "results",
    excluded: Annotated[
        Path | None,
        typer.Option(help="Tasks to report but not count (default: excluded.txt beside the CSV)."),
    ] = None,
    write_readme: Annotated[
        bool,
        typer.Option("--write-readme", help="Replace the table between the README's markers."),
    ] = False,
    readme: Annotated[Path, typer.Option(help="README to update.")] = Path("README.md"),
) -> None:
    """Metrics from eval results: resolved (95% CI), verifier precision, false PASS/FAIL, $/task."""
    from warden.eval import report as rep

    missing = [p for p in csvs if not p.exists()]
    if missing:
        fail(f"no such file: {missing[0]}", fix="pass the eval's results*.csv", code=2)
    if excluded is None and (csvs[0].parent / "excluded.txt").exists():
        excluded = csvs[0].parent / "excluded.txt"
    skip = rep.read_excluded(excluded) if excluded is not None else {}
    m = rep.compute(rep.read_results(csvs), excluded=skip)
    table = rep.markdown(m, label=label)
    if write_readme:
        try:
            readme.write_text(rep.replace_block(readme.read_text("utf-8"), table), "utf-8")
        except ValueError as e:
            fail(str(e), fix=f"add {rep.START} / {rep.END} around the Results table", code=2)
        _err(f"wrote {readme} ({m.resolved}/{m.n} resolved)")
    if md:
        sys.stdout.write(table)
    elif not write_readme:
        print(json.dumps(m.as_dict(), indent=2))


if __name__ == "__main__":
    app()
