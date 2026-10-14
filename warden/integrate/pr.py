"""Render the evidence table and open the draft PR — the single artefact judges and reviewers read.

The console shows the same table; the PR embeds it. Rows are claims with the exact command and
result. An explicit "Not verified" row is always present.

Opening the PR is the product's one risky action (ux-brief §2.3): `confirm_pr` asks on stderr
(stdout stays pure data under `--json`), `--yes` bypasses, and a non-TTY refuses with the fix line.
`open_draft_pr` shells out to `gh` through an injectable runner so tests and `--demo` never call it.
"""

from __future__ import annotations

import re
import string
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from warden.schemas import Plan, Verdict

if TYPE_CHECKING:
    from warden.runlog.events import EventLog

PENDING_LABEL = "PENDING-HUMAN"

Runner = Callable[..., subprocess.CompletedProcess[str]]

_PR_URL = re.compile(r"https://\S+/pull/\d+")


def labels_for(plan: Plan) -> list[str]:
    """The one place that decides the PR label; C4 only adds a body reason."""
    return [PENDING_LABEL] if plan.needs_human else []


@dataclass(frozen=True)
class PrResult:
    url: str
    argv: list[str]
    exit_code: int


class PrOpenError(RuntimeError):
    """`str(err)` is the human sentence only; the caller prints fix and run_id after it."""

    def __init__(self, message: str, *, fix: str, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.fix = fix
        self.stderr_tail = stderr_tail


def open_draft_pr(
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    *,
    labels: list[str],
    runner: Runner = subprocess.run,
    log: EventLog | None = None,
) -> PrResult:
    """Run `gh pr create --draft` and return the PR URL; raise PrOpenError on any failure."""
    body_path: Path
    if log is not None:
        body_path = log.artifact("pr_body.md", body)  # same artefact the console renders
        cleanup = False
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as tmp:
            tmp.write(body)
            body_path = Path(tmp.name)
        cleanup = True
    argv = [
        "gh",
        "pr",
        "create",
        "--draft",
        "--repo",
        repo,
        "--head",
        head,
        "--base",
        base,
        "--title",
        title,
        "--body-file",
        str(body_path),
    ] + [tok for label in labels for tok in ("--label", label)]
    started = time.monotonic()
    try:
        completed = runner(argv, capture_output=True, text=True, check=False)
    except OSError as e:  # gh not installed, not executable, ...
        if log is not None:
            log.emit(
                "pr",
                "tool",
                {"name": "gh", "args": argv[1:], "exit_code": None, "url": None, "error": str(e)},
                ms=int((time.monotonic() - started) * 1000),
            )
        raise PrOpenError(
            f"could not run gh: {e}", fix="install gh (see `warden doctor`)", stderr_tail=""
        ) from e
    finally:
        if cleanup:
            body_path.unlink(missing_ok=True)
    rc = completed.returncode
    matches = _PR_URL.findall(completed.stdout or "")
    url = matches[-1] if matches else None
    if log is not None:
        log.emit(
            "pr",
            "tool",
            {"name": "gh", "args": argv[1:], "exit_code": rc, "url": url},
            ms=int((time.monotonic() - started) * 1000),
        )
    tail = "\n".join((completed.stderr or "").strip().splitlines()[-3:])
    if rc != 0:
        raise PrOpenError(
            f"gh could not open the draft PR for {head} on {repo} (exit {rc}).",
            fix=f"check `gh auth status` and that {head} is pushed; then re-run",
            stderr_tail=tail,
        )
    if url is None:
        raise PrOpenError(
            "gh exited 0 but printed no PR URL.",
            fix="run the gh command from the run log by hand",
            stderr_tail=tail,
        )
    return PrResult(url=url, argv=argv, exit_code=rc)


def confirm_pr(
    yes: bool,
    *,
    what: str,
    run_id: str | None = None,
    input: TextIO | None = None,
    err: TextIO | None = None,
) -> bool:
    """The product's only confirmation. Prompts on stderr so `--json` stdout stays valid JSONL.

    `input`/`err` default to the *current* sys.stdin/sys.stderr (resolved at call time, so
    redirections and capture work); tests inject StringIO.
    """
    input = sys.stdin if input is None else input
    err = sys.stderr if err is None else err
    if yes:
        err.write("opening draft PR without asking (--yes)\n")
        err.flush()
        return True
    if not input.isatty():
        err.write("Not opening the draft PR: no terminal to ask on.\n")
        err.write("fix: re-run with --yes (CI), or run interactively.\n")
        if run_id:
            err.write(f"run_id {run_id}\n")
        err.flush()
        return False
    err.write(f"Open draft PR? {what} [y/N] ")
    err.flush()
    answer = input.readline().strip().casefold()
    if answer in ("y", "yes"):
        return True
    err.write("not opening the PR\n")
    err.flush()
    return False


def best_of_n_cell(verdict: Verdict) -> str:
    """'#2 chosen from 3 candidates (#1 FAIL · #2 PASS · #3 skipped): <why>' (one table cell)."""
    each = " · ".join(
        f"#{c.index} {c.decision.replace('_', ' ') if c.decision else 'skipped'}"
        for c in verdict.candidates
    )
    why = verdict.chosen_why.replace("|", "/")
    return f"#{verdict.chosen} chosen from {len(verdict.candidates)} candidates ({each}): {why}"


def evidence_table(
    issue: int,
    title: str,
    plan: Plan,
    verdict: Verdict,
    *,
    cost_usd: float,
    calls_by_model: dict[str, int],
    log_url: str | None = None,
    note: str | None = None,
) -> str:
    link = f" [↗]({log_url})" if log_url else ""
    if verdict.theater_failed_on_base:
        theater = "FAIL — as required"
    elif verdict.theater_invalid:
        theater = "invalid repro — red for the wrong reason or with the fix"
    elif verdict.theater_inconclusive:
        theater = "inconclusive — not proof"
    else:
        theater = "PASSED — tests are theater"
    if verdict.theater_retry:  # WARDEN_THEATER_RETRIES: the implementer rewrote its tests
        n = verdict.theater_retry
        theater += (
            f" (repro found on retry {n})"
            if verdict.theater_failed_on_base
            else f" (after {n} theater {'retry' if n == 1 else 'retries'})"
        )
    theater += "".join(  # per new test: why it does or does not count as a repro
        f"<br>`{n.rsplit('/', 1)[-1]}`: {why}" for n, why in list(verdict.theater_tests.items())[:6]
    )
    suite = ", ".join(f"{v} {k}" for k, v in verdict.suite.items()) or "not run"
    race = (
        f"{verdict.race.threads} threads → {verdict.race.effects_on_candidate} effect(s)"
        + (
            f" (was {verdict.race.effects_on_base})"
            if verdict.race.effects_on_base is not None
            else ""
        )
        if verdict.race
        else "n/a"
    )
    gate = " · ".join(
        [
            "mypy clean" if verdict.mypy_ok else "mypy FAIL",
            "ruff clean" if verdict.ruff_ok else "ruff FAIL",
            "1 alembic head" if verdict.alembic_single_head else "alembic heads ≠ 1",
            "no openapi drift" if not verdict.openapi_drift else "openapi DRIFT",
        ]
    )
    risk = ", ".join(plan.risk_class) or "none"
    risk_cell = f"{risk} → **{PENDING_LABEL}**" if plan.needs_human else risk
    not_verified = ", ".join(verdict.not_verified) or "—"
    calls = " · ".join(f"{m.split('/')[-1]} {n}" for m, n in calls_by_model.items())

    def reason(*prefixes: str) -> str | None:
        """The verifier's own line for a check, minus its prefix; None = the check did not run."""
        for r in verdict.reasons:
            for p in prefixes:
                if r.startswith(p):
                    return r[len(p) :]
        return None

    existing = reason("existing tests: ", "regression: ")
    spec = reason("spec: ")

    lines = [
        f"## Warden evidence — #{issue} {title}",
        "",
        *([f"> {note}", ""] if note else []),
        f"**Verdict: {verdict.decision.replace('_', ' ')}**",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| New tests on unfixed base | {theater}{link} |",
        f"| Full suite on candidate | {suite}{link} |",
        *([f"| Existing tests on candidate | {existing} |"] if existing else []),
        *([f"| Issue's stated behaviours | {spec} |"] if spec else []),
        f"| Race probe | {race} |",
        f"| Gate | {gate} |",
        f"| Not verified | {not_verified} |",
        f"| Risk class | {risk_cell} |",
        *([f"| Best-of-N | {best_of_n_cell(verdict)} |"] if verdict.candidates else []),
        f"| Cost | ${cost_usd:.2f} · {calls} |",
    ]
    if verdict.concerns:
        lines += ["", "**Concerns**", *[f"- {c}" for c in verdict.concerns]]
    if plan.citations:
        lines += ["", "**Plan citations**", *[f"- [{c.claim}]({c.url})" for c in plan.citations]]
    return "\n".join(lines) + "\n"


WARDEN_URL = "https://github.com/moiz-lakkadkutta/warden"

# Built-in PR body: the evidence table, then the AI disclosure.
DEFAULT_TEMPLATE = "$evidence\n$disclosure"


def disclosure(routing: list[tuple[str, str]], calls_by_model: dict[str, int]) -> str:
    """The AI-disclosure section: what wrote this PR and with which models. Always in the body."""
    by_model: dict[str, list[str]] = {}
    for role, model in routing:
        by_model.setdefault(model, []).append(role)
    for model in calls_by_model:
        by_model.setdefault(model, [])
    rows = []
    for model, roles in by_model.items():
        n = calls_by_model.get(model)
        used = f", {n} call{'s' if n != 1 else ''}" if n else ""
        rows.append(f"- `{model}` ({' / '.join(roles) or 'other'}{used})")
    return "\n".join(
        [
            "## AI disclosure",
            "",
            f"This pull request was prepared end to end by [Warden]({WARDEN_URL}), an autonomous "
            "AI agent: the plan, the code, the tests and this description were generated by "
            "NVIDIA Nemotron models on Nebius Token Factory, and the tests were run in a Nebius "
            "sandbox. Models used:",
            "",
            *rows,
            "",
            "It is opened as a draft for a human maintainer to review; Warden never merges.",
            "",
        ]
    )


def render_pr_body(
    template: str | None,
    *,
    evidence: str,
    disclosure_md: str,
    issue: int,
    title: str,
    repo: str,
    run_id: str,
    models: str,
) -> str:
    """Fill a `string.Template` ($name; `$$` is a literal $). Unknown $names are left as they are.
    The evidence and the disclosure are never dropped: a template without them gets them appended.
    """
    text = DEFAULT_TEMPLATE if template is None else template
    fields = {
        "evidence": evidence,
        "disclosure": disclosure_md,
        "issue": str(issue),
        "title": title,
        "repo": repo,
        "run_id": run_id,
        "models": models,
    }
    tpl = string.Template(text)
    names = set(tpl.get_identifiers())
    body = tpl.safe_substitute(fields).rstrip("\n") + "\n"
    for name in ("evidence", "disclosure"):
        if name not in names:
            body += "\n" + fields[name].rstrip("\n") + "\n"
    return body
