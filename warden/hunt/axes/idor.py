"""IDOR / cross-tenant access axis.

Two probe families run against a forked, seeded Fabric (docs/runbook.md §3.4):

* **cross_org_read** — org A's token against every id-taking GET endpoint enumerated from
  ``/openapi.json`` with org B's UUIDs. Fabric filters ``WHERE org_id = <token org>`` in every
  query, so the *expected* result is 404/422; a 200 would be a cross-org leak (P0).
* **cross_firm_write** — a create endpoint (``POST /parties``, ``POST /items``) called with a
  ``firm_id`` belonging to a *different* tenant's firm. Several services take ``firm_id`` straight
  from the body with no "firm belongs to org" guard, so a 201 persists a row whose
  ``firm.org_id`` differs from the token's org — a firm-level isolation bypass with no DB backstop
  (RLS is inert). This is the SEC-1 / AUTHZ-2 class on record.

The HTTP work happens *inside* the sandbox (one dash script per run, so the booted server and all
curls share a process); :mod:`warden.hunt.hunter` runs it and re-runs for reproduction. Everything
below the :class:`IdorRun` boundary is pure and unit-tested with canned runs — no network.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from warden.schemas import Finding, Severity

if TYPE_CHECKING:
    from warden.runlog.events import EventLog
    from warden.sandbox.box import Box

ProbeKind = Literal["cross_org_read", "cross_firm_write"]

# Confirmed only when a vulnerable observation repeats at least this many times (runbook: "twice").
CONFIRM_THRESHOLD = 2

_SEVERITY: dict[ProbeKind, Severity] = {
    "cross_org_read": "P0",  # a real cross-org leak: another tenant's rows readable by id
    "cross_firm_write": "P1",  # cross-firm isolation bypass (matches the register's SEC-1 P1)
}
_IMPACT: dict[ProbeKind, str] = {
    "cross_org_read": (
        "A tenant can read another tenant's records by guessing or enumerating their UUIDs — a "
        "direct confidentiality breach of org isolation."
    ),
    "cross_firm_write": (
        "Within a multi-firm org (and across the tenant boundary, since the body `firm_id` may "
        "belong to another org's firm) a caller persists rows stamped with a firm they do not own. "
        "RLS is inert (app DB role is BYPASSRLS), so no database backstop catches it: firm-level "
        "isolation rests entirely on a hand-written guard that these endpoints omit."
    ),
}


class Observation(BaseModel):
    """One probe's result in one run. ``vulnerable`` is the harness's own per-run verdict."""

    probe: str = Field(description="Stable probe key, e.g. 'cross_firm_write POST /parties'.")
    kind: ProbeKind
    method: str
    path: str
    status: int = Field(description="HTTP status observed (-1 if the request itself failed).")
    vulnerable: bool
    request_id: str | None = None
    repro: str = Field(default="", description="Copy-paste curl sequence that reproduced it.")
    expected: str = ""
    actual: str = ""


class IdorRun(BaseModel):
    """The parsed outcome of one sandbox probe run."""

    observations: list[Observation] = Field(default_factory=list)
    endpoints_enumerated: int = 0
    tenants: int = 0
    error: str = Field(default="", description="Non-empty if the harness could not boot or log in.")


def findings_from_runs(
    runs: list[IdorRun], *, threshold: int = CONFIRM_THRESHOLD
) -> tuple[list[Finding], list[Finding]]:
    """Fold N runs into (confirmed, plausible) findings.

    A probe is vulnerable in a run when its observation says so. ``reproduced_times`` counts the
    runs in which it was vulnerable; ``threshold`` or more makes it confirmed (filed), fewer makes
    it plausible (logged, not filed). Findings are keyed by ``root_cause_key`` so the filer groups
    one cause's endpoints into a single issue.
    """
    first: dict[str, Observation] = {}
    counts: dict[str, int] = {}
    order: list[str] = []
    for run in runs:
        for obs in run.observations:
            if not obs.vulnerable:
                continue
            if obs.probe not in first:
                first[obs.probe] = obs
                order.append(obs.probe)
                counts[obs.probe] = 0
            counts[obs.probe] += 1
    confirmed: list[Finding] = []
    plausible: list[Finding] = []
    for probe in order:
        obs = first[probe]
        reps = counts[probe]
        finding = _finding(obs, reproduced=reps, confirmed=reps >= threshold)
        (confirmed if reps >= threshold else plausible).append(finding)
    return confirmed, plausible


def _finding(obs: Observation, *, reproduced: int, confirmed: bool) -> Finding:
    return Finding(
        axis="idor",
        severity=_SEVERITY[obs.kind],
        title=f"{obs.kind.replace('_', ' ')}: {obs.method} {obs.path}",
        repro=obs.repro,
        expected=obs.expected or _default_expected(obs.kind),
        actual=obs.actual or f"HTTP {obs.status}",
        request_id=obs.request_id,
        confirmed=confirmed,
        reproduced_times=reproduced,
        root_cause_key=f"idor:{obs.kind}",
        impact=_IMPACT[obs.kind],
    )


def _default_expected(kind: ProbeKind) -> str:
    if kind == "cross_org_read":
        return "404/422 — another tenant's resource must not be readable by id."
    return (
        "422/403 — a body `firm_id` that does not belong to the caller's org must be rejected "
        "(the firm-in-org guard that `sales_service._ensure_firm_in_org` applies elsewhere)."
    )


# ---------------------------------------------------------------- live sandbox prober

# Booting uvicorn, waiting for /ready, two signups and the full openapi sweep: give it room.
PROBE_TIMEOUT = 600


@dataclass
class SandboxProber:
    """Runs the IDOR harness on a fresh fork of the seeded base image, once per :meth:`probe`.

    Two ``run`` calls on one :class:`Box` fork it, so every probe starts from the same seeded DB —
    throwaway signups never accumulate across reproductions. ``shell_wrap`` (``with-services sh
    -c``) brings Postgres/Redis up around the boot script.
    """

    box: Box
    log: EventLog
    shell_wrap: str = "with-services sh -c"
    probe_path: str = "/tmp/warden_idor_probe.py"
    timeout: int = PROBE_TIMEOUT

    def probe(self) -> IdorRun:
        from warden.hunt.axes.probe_script import boot_command, parse_probe_output, probe_py

        command = boot_command(probe_path=self.probe_path)
        shell = f"{self.shell_wrap} {shlex.quote(command)}" if self.shell_wrap else command
        res = self.box.run(
            shell,
            cwd="/app",
            timeout=self.timeout,
            keep=False,
            files={self.probe_path: probe_py().encode()},
        )
        run = parse_probe_output(res.stdout)
        if run.error:
            self.log.emit(
                "hunt",
                "note",
                {"probe_error": run.error, "exit_code": res.exit_code,
                 "stderr_tail": res.stderr[-500:]},
            )  # fmt: skip
        return run
