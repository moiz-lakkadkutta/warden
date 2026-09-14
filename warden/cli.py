"""warden — hunt → plan → fix → verify → PR, on Nebius Sandboxes with Nemotron.

DX rules (clig.dev / Heroku): first output < 100 ms; `--help` and `doctor` never touch the
network; stdout is data, stderr is progress; `--json` streams JSONL events; every prompt has a flag.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="warden",
    help="Autonomous hunt → plan → fix → verify → PR agent on Nebius Token Factory Sandboxes.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)

PHASES = ("hunt", "plan", "fix", "verify", "pr")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _color_ok() -> bool:
    return (
        sys.stderr.isatty()
        and not os.environ.get("NO_COLOR")
        or bool(os.environ.get("FORCE_COLOR"))
    )


def _mark(ok: bool) -> str:
    if _color_ok():
        return "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    return "✓" if ok else "✗"


# --------------------------------------------------------------------------- doctor
@app.command()
def doctor(
    live: Annotated[
        bool, typer.Option(help="Also call each model once (costs a few cents).")
    ] = False,
) -> None:
    """Check local tools, credentials and (optionally) model reachability. No network by default."""
    from warden.config import load_settings

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
    checks.append((".env not tracked by git", not _env_tracked(), "git rm --cached .env"))

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
    from warden.llm.cost import CostMeter
    from warden.runlog.events import EventLog

    assert isinstance(s, Settings)
    out: list[tuple[str, bool, str]] = []
    log = EventLog(s.runs_dir, "doctor")
    llm = LLM(s, CostMeter(cap_usd=0.50), log)
    for role in (Role.PLANNER, Role.IMPLEMENTER, Role.TOOLS):
        try:
            r = llm.chat(
                role, "plan", [{"role": "user", "content": "Reply with the single word: ok"}]
            )
            out.append((f"model {s.model_for(role)}", bool(r.content), "check model id / quota"))
        except Exception as exc:  # noqa: BLE001
            out.append((f"model {s.model_for(role)}", False, str(exc)[:80]))
    log.close()
    return out


# ------------------------------------------------------------------------------ init
@app.command()
def init(
    repo: Annotated[Path, typer.Argument(help="Path to the target repository.")] = Path("."),
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept detected defaults.")] = False,
) -> None:
    """Detect the repo's test runner and write warden.toml (≤ 5 questions)."""
    repo = repo.resolve()
    runner = (
        "pytest"
        if (repo / "pyproject.toml").exists() or list(repo.glob("**/pytest.ini"))
        else "unknown"
    )
    app_dir = "backend" if (repo / "backend").is_dir() else "."
    cfg = repo / "warden.toml"
    if cfg.exists() and not yes and not typer.confirm(f"{cfg} exists. Overwrite?", default=False):
        raise typer.Exit(1)
    cfg.write_text(
        "\n".join(
            [
                "# warden.toml — project settings",
                "# precedence: flags > env > this file > ~/.config/warden/config.toml",
                f'test_cmd = "{runner} -q"',
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
) -> None:
    """Hunt → plan → fix → verify → PR for one issue. Prints run_id first (see `warden resume`)."""
    from warden.config import load_settings
    from warden.runlog.events import EventLog, stdout_stream

    s = load_settings()
    if max_usd is not None:
        s.max_usd_per_issue = max_usd
    log = EventLog(s.runs_dir, json_stream=stdout_stream() if json_out else None)
    _err(f"run_id {log.run_id}   (resume with: warden resume {log.run_id})")

    if demo:
        from warden.demo import run_demo

        result = run_demo(s, log, dry_run=dry_run)
        log.result(**result)
        if not json_out:
            _err(result["evidence"])
        log.close()
        raise typer.Exit(0)

    if issue is None:
        _err("pass --issue N, or --demo to try Warden without credentials")
        raise typer.Exit(2)
    missing = s.missing_for("NEBIUS_API_KEY", "CONTREE_BASE_URL", "NEBIUS_PROJECT_ID")
    if missing:
        _err(f"missing {', '.join(missing)} — run `warden doctor`")
        raise typer.Exit(2)
    _err("live pipeline lands in Phase B (see docs/warden-plan.md §5). Use --demo for now.")
    log.close()
    raise typer.Exit(3)


@app.command()
def resume(run_id: Annotated[str, typer.Argument()]) -> None:
    """Continue an interrupted run from its last completed phase."""
    from warden.config import load_settings
    from warden.runlog.events import EventLog

    s = load_settings()
    events = EventLog.read(s.runs_dir, run_id)
    if not events:
        _err(f"no run {run_id} under {s.runs_dir}")
        raise typer.Exit(1)
    last = events[-1].phase
    _err(f"{run_id}: {len(events)} events, last phase {last}. Resume lands in Phase B6.")
    raise typer.Exit(3)


@app.command()
def console(
    run_id: Annotated[str | None, typer.Option(help="Render one run to static HTML.")] = None,
    static: Annotated[
        bool, typer.Option(help="Write runs/<id>/index.html instead of serving.")
    ] = True,
) -> None:
    """Render a run's timeline, branch tree and evidence table."""
    from warden.config import load_settings
    from warden.runlog.render import render_run

    s = load_settings()
    if run_id is None:
        runs = (
            sorted(p.name for p in s.runs_dir.iterdir() if p.is_dir())
            if s.runs_dir.exists()
            else []
        )
        if not runs:
            _err("no runs yet — try: warden run --demo")
            raise typer.Exit(1)
        run_id = runs[-1]
    out = render_run(s.runs_dir, run_id)
    print(out)


if __name__ == "__main__":
    app()
