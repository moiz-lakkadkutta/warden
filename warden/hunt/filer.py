"""Group findings by root_cause_key and open GitHub issues via `gh` with the strict finding format.

Lands in Phase C6 of docs/warden-plan.md. Findings with a shared ``root_cause_key`` become one
issue (so eight firm-spoof endpoints file as one bug, not eight). The body is the exact format from
the flow-bug-finder skill: title, severity, a copy-paste curl repro a human can paste against a
local Fabric, expected vs actual (+ request_id if present) and impact. ``gh`` is reached through an
injectable runner so tests and dry runs never touch the network.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from warden.schemas import Finding

if TYPE_CHECKING:
    from warden.runlog.events import EventLog

Runner = Callable[..., subprocess.CompletedProcess[str]]

HUNT_LABEL = "warden-hunt"
_LABEL_COLOR = "b60205"
_LABEL_DESC = "Filed autonomously by the Warden hunter"
_ISSUE_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/issues/(\d+)")


@dataclass(frozen=True)
class FiledIssue:
    """One issue the filer created (or would have, with ``--no-file``)."""

    root_cause_key: str
    title: str
    body: str
    severity: str
    number: int | None = None
    url: str | None = None
    dry_run: bool = False
    regression_of: str | None = None


def group_findings(findings: list[Finding]) -> dict[str, list[Finding]]:
    """Group by ``root_cause_key``, preserving first-seen order of keys and findings."""
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(f.root_cause_key, []).append(f)
    return groups


def _head(group: list[Finding]) -> Finding:
    """The group's representative: prefer a finding carrying a regression id (most recognizable)."""
    return next((f for f in group if f.regression_of), group[0])


def issue_title(group: list[Finding], *, prefix: str = "") -> str:
    """The group's title: the representative finding's, with severity and a regression marker."""
    head = _head(group)
    reg = f"[regression {head.regression_of}] " if head.regression_of else ""
    return f"{prefix}[{head.severity}] {reg}{head.title}"


def attacker_section(attacker: dict[str, object]) -> str:
    """Render the decoded-JWT evidence block (empty string when there is nothing to show)."""
    if not attacker:
        return ""
    order = ("role", "roles", "is_owner", "firm_id", "org_id", "permissions", "sub",
             "signup_firm_id", "signup_org_id")  # fmt: skip
    rows = [(k, attacker[k]) for k in order if k in attacker]
    rows += [(k, v) for k, v in attacker.items() if k not in dict(rows)]
    body = "\n".join(f"- `{k}`: `{v}`" for k, v in rows)
    return (
        "## Attacker token (decoded JWT claims of the caller)\n"
        "The write was sent by a token Warden signed up for itself. Its own claims:\n"
        f"{body}"
    )


def format_body(group: list[Finding], *, preamble: str = "", evidence: str = "") -> str:
    """The strict finding format. One section per endpoint when a cause spans several."""
    head = _head(group)
    lines: list[str] = []
    if preamble:
        lines.append(preamble)
        lines.append("")
    lines.append(f"**Severity:** {head.severity}")
    lines.append(f"**Axis:** {head.axis}  ·  **root_cause_key:** `{head.root_cause_key}`")
    if head.regression_of:
        lines.append(
            f"**Regression of:** `{head.regression_of}` "
            "(already on record in the known-issues register)."
        )
    confirmed = [f for f in group if f.confirmed]
    reps = max((f.reproduced_times for f in group), default=0)
    lines.append(
        f"**Status:** {'confirmed' if confirmed else 'plausible'} "
        f"(reproduced {reps}× before filing)."
    )
    lines.append("")
    lines.append("## Repro")
    lines.append("Paste against a local Fabric (`make dev` / `uvicorn app.main:app`):")
    lines.append("")
    lines.append("```sh")
    for f in group:
        lines.append(f.repro.strip())
        lines.append("")
    lines.append("```")
    lines.append("")
    lines.append("## Expected vs actual")
    for f in group:
        rid = f" (request_id `{f.request_id}`)" if f.request_id else ""
        lines.append(f"- **{_probe_label(f)}**")
        lines.append(f"  - expected: {f.expected}")
        lines.append(f"  - actual: {f.actual}{rid}")
    lines.append("")
    if evidence:
        lines.append(evidence)
        lines.append("")
    lines.append("## Impact")
    lines.append(head.impact or "Cross-tenant data integrity / isolation.")
    lines.append("")
    lines.append(
        "---\n_Filed autonomously by the Warden hunter "
        f"(`{head.axis}` axis). Findings reproduce twice before filing._"
    )
    return "\n".join(lines)


def _probe_label(f: Finding) -> str:
    """A short label for one finding's expected/actual block (first line of its title)."""
    return f.title.split("\n", 1)[0].strip()


def ensure_label(repo: str, label: str = HUNT_LABEL, *, runner: Runner = subprocess.run) -> None:
    """Create the scratch label if missing. `gh` exits non-zero when it already exists — fine."""
    runner(
        [
            "gh", "label", "create", label, "-R", repo,
            "--color", _LABEL_COLOR, "--description", _LABEL_DESC,
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip


def file_findings(
    findings: list[Finding],
    repo: str,
    *,
    no_file: bool = False,
    label: str = HUNT_LABEL,
    preamble: str = "",
    title_prefix: str = "",
    evidence: str = "",
    runner: Runner = subprocess.run,
    log: EventLog | None = None,
) -> list[FiledIssue]:
    """File one issue per ``root_cause_key``. With ``no_file`` nothing is sent (dry run).

    ``preamble`` is prepended to each body and ``title_prefix`` to each title — used by a
    historical replay to mark the issue as already-fixed-on-main up front. ``evidence`` is an
    extra body section (e.g. the decoded attacker-token claims).
    """
    groups = group_findings(findings)
    out: list[FiledIssue] = []
    if groups and not no_file:
        ensure_label(repo, label, runner=runner)
    for key, group in groups.items():
        title = issue_title(group, prefix=title_prefix)
        body = format_body(group, preamble=preamble, evidence=evidence)
        head = _head(group)
        if no_file:
            out.append(
                FiledIssue(key, title, body, head.severity, dry_run=True,
                           regression_of=head.regression_of)
            )  # fmt: skip
            if log is not None:
                log.emit(
                    "hunt", "note", {"filer": "dry-run", "root_cause_key": key, "title": title}
                )
            continue
        number, url = _gh_issue_create(repo, title, body, label, runner=runner, log=log)
        out.append(FiledIssue(key, title, body, head.severity, number, url,
                              regression_of=head.regression_of))  # fmt: skip
    return out


def _gh_issue_create(
    repo: str,
    title: str,
    body: str,
    label: str,
    *,
    runner: Runner,
    log: EventLog | None,
) -> tuple[int | None, str | None]:
    """`gh issue create` via a body file; return (number, url). Raises on a hard failure."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as tmp:
        tmp.write(body)
        body_path = Path(tmp.name)
    argv = [
        "gh", "issue", "create", "-R", repo,
        "--title", title, "--body-file", str(body_path), "--label", label,
    ]  # fmt: skip
    try:
        res = runner(argv, capture_output=True, text=True, check=False)
    finally:
        body_path.unlink(missing_ok=True)
    stdout = res.stdout or ""
    m = _ISSUE_URL.search(stdout)
    url = m.group(0) if m else None
    number = int(m.group(1)) if m else None
    if log is not None:
        log.emit(
            "hunt",
            "tool",
            {"name": "gh", "args": ["issue", "create", "-R", repo], "exit_code": res.returncode,
             "url": url, "number": number},
        )  # fmt: skip
    if res.returncode != 0:
        tail = "\n".join((res.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"gh issue create failed on {repo} (exit {res.returncode}): {tail}")
    return number, url
