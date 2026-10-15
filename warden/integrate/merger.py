"""integration = integration.run('git merge --no-ff fix/N'); on conflict FAIL the issue and continue.

D2. A wave's candidates were all fixed on forks of the same integration image (the wave base).
Each candidate leaves its sandbox as a patch (`branch.export_patch`); here every patch becomes
branch `fix/N` on the wave base inside the integration image and is merged with `--no-ff` into
`warden/integration`, one kept run per issue, so the integration image is a fork chain:

    base ──merge fix/196──▶ i1 ──merge fix/197──▶ i2 ──(conflict on fix/200: i2 stays)──▶ …

A failed `git am` or a conflicting merge FAILs that issue (its child checkpoint is dropped, the
chain continues from the last good image). After the wave the gate runs on the integrated image,
and the next wave forks it. Nothing here touches the target repo: no push, no token.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Wave
from warden.verify.gate import GateCommands, GateResult, run_gate

INTEGRATION_BRANCH = "warden/integration"
PATCH_DIR = "/tmp/warden-merge"
_GIT = "git -c user.name=warden -c user.email=warden@localhost"
_SHA = "WARDEN_SHA="
_CONFLICT = "WARDEN_CONFLICT="
_STAGE = "WARDEN_STAGE="


@dataclass(frozen=True)
class MergeOutcome:
    issue: int
    merged: bool
    reason: str = ""  # why not: "no patch", "patch does not apply", "conflict in a.py, b.py"
    sha: str = ""  # integration HEAD after the merge
    image_uuid: str = ""  # integration checkpoint after the merge


@dataclass
class WaveResult:
    wave: Wave  # number, issues, integration_image_uuid, merged filled in
    outcomes: list[MergeOutcome]
    box: Box  # the integration image after the wave: the next wave forks it
    sha: str  # its HEAD
    gate: GateResult | None = None  # None when nothing merged (the image did not change)
    skipped: list[int] = field(default_factory=list)  # issues with no candidate to merge

    def outcome(self, issue: int) -> MergeOutcome | None:
        return next((o for o in self.outcomes if o.issue == issue), None)


def head_sha(box: Box, *, cwd: str) -> str:
    res = box.run("git rev-parse HEAD", cwd=cwd, keep=False)
    sha = res.stdout.strip()
    if not res.ok or not sha:
        raise RuntimeError(f"git rev-parse HEAD failed in {box.uuid}: {res.stderr.strip()[:200]}")
    return sha


def merge_command(issue: int, *, wave_base: str, integration: str) -> str:
    """One dash-safe shell line: fix/N := wave base + the candidate's patch, then
    `git merge --no-ff fix/N` into warden/integration. Prints stage markers so a failure says
    which step broke, the conflicting paths, and on success the new HEAD."""
    patch = shlex.quote(f"{PATCH_DIR}/fix-{issue}.patch")
    branch = f"fix/{issue}"
    msg = shlex.quote(f"Merge {branch} (#{issue}) into {INTEGRATION_BRANCH}")
    return (
        f"echo {_STAGE}am && git checkout -q -B {branch} {wave_base} && "
        f"{_GIT} am -q --3way {patch} && "
        f"echo {_STAGE}merge && git checkout -q -B {INTEGRATION_BRANCH} {integration} && "
        f"{{ {_GIT} merge --no-ff -q -m {msg} {branch} || "
        f"{{ echo {_CONFLICT}$(git diff --name-only --diff-filter=U | tr '\\n' ' '); exit 1; }}; }} "
        f"&& echo {_SHA}$(git rev-parse HEAD)"
    )


def _marker(out: str, prefix: str) -> str | None:
    found = [ln[len(prefix) :].strip() for ln in out.splitlines() if ln.startswith(prefix)]
    return found[-1] if found else None


def _failure(out: str, err: str) -> str:
    conflict = _marker(out, _CONFLICT)
    if conflict is not None:
        files = ", ".join(conflict.split()) or "unknown files"
        return f"merge conflict in {files}"
    if _marker(out, _STAGE) == "merge":
        return f"merge failed: {err.strip()[-200:] or 'no output'}"
    return f"patch does not apply on the wave base: {err.strip()[-200:] or 'no output'}"


def merge_wave(
    integration: Box,
    wave: Wave,
    patches: Mapping[int, str],
    log: EventLog,
    *,
    cwd: str,
    gate: GateCommands | None = None,
    wrap: str = "",
    gate_timeout: int = 1800,
) -> WaveResult:
    """Merge every issue of `wave` that has a patch, in issue order, onto `integration`; run
    `gate` on the result when at least one merged. Never raises on a conflict: that issue's
    outcome says why and the chain continues from the previous image."""
    wave_base = head_sha(integration, cwd=cwd)
    sha = wave_base
    box = integration
    outcomes: list[MergeOutcome] = []
    skipped: list[int] = []
    for issue in sorted(wave.issues):
        patch = patches.get(issue)
        if not patch:
            skipped.append(issue)
            outcomes.append(MergeOutcome(issue, False, "no candidate to merge (FAIL or error)"))
            continue
        res = box.run(
            merge_command(issue, wave_base=wave_base, integration=sha),
            cwd=cwd,
            keep=True,
            files={f"{PATCH_DIR}/fix-{issue}.patch": patch.encode()},
        )
        new_sha = _marker(res.stdout, _SHA)
        if res.ok and res.box is not None and new_sha:
            box, sha = res.box, new_sha
            outcome = MergeOutcome(issue, True, sha=sha, image_uuid=box.uuid)
        else:
            outcome = MergeOutcome(issue, False, _failure(res.stdout, res.stderr))
        outcomes.append(outcome)
        log.emit(
            "pr",
            "note",
            {
                "text": f"wave {wave.number}: fix/{issue} "
                + ("merged" if outcome.merged else f"not merged ({outcome.reason})"),
                "merge": {"issue": issue, "merged": outcome.merged, "reason": outcome.reason},
                "wave": wave.number,
            },
        )
    merged = [o.issue for o in outcomes if o.merged]
    result_gate = None
    if merged and gate is not None:
        result_gate = run_gate(box, log, commands=gate, cwd=cwd, wrap=wrap, timeout=gate_timeout)
    done = wave.model_copy(update={"integration_image_uuid": box.uuid, "merged": merged})
    return WaveResult(
        wave=done, outcomes=outcomes, box=box, sha=sha, gate=result_gate, skipped=skipped
    )
