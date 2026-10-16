"""Theater retry: the fix is right, the tests do not prove it — rewrite the tests, keep the fix.

Run 20261014-131452-cd9b (icalendar#1928): the implementer made the correct one-line fix, the full
suite was green (21315 passed), but all four new tests built the rule from "FREQ=DAILY;COUNT=10",
where FREQ is already first, so every one passed on the unfixed base and the verdict was FAIL. A
correct verdict and a wasted run: one more short implementer pass, told exactly that, can write
the test that bites.

`retry_implementer` continues from the candidate checkpoint (a fork of it, never a fresh base),
with the verifier's per-test reasons, the base pytest output and the fix diff as its user note,
a bounded turn budget, and `run_tests_unfixed` to check a test against the unfixed source without
touching its working copy. The machine (pipeline.verify_with_retries) decides when to retry and
re-verifies; nothing here catches CostCapExceededError.

Verify retry (WARDEN_VERIFY_RETRIES): the other way a good candidate was thrown away. Fabric #196
and #206 (camp-replay-212-d) had a valid repro (fails on base, passes with the fix) but 2 suite
failures plus mypy / ruff errors. The implementer continues once from the candidate with a
compact note built from the verify artifacts: failing test node ids with their one-line error,
mypy / ruff errors as file:line, regressions, unmet spec behaviours. The checks are unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from warden.fix.implementer import UNFIXED_TOOL_NAME, LLMLike, Progress, run_implementer
from warden.fix.tools import ToolDispatcher
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, Plan, Verdict

RETRY_TURNS_MAX = 20
BASE_OUTPUT_CHARS = 2500
DIFF_CHARS = 3000


VERIFY_RETRY_TURNS_MAX = 8  # a verify retry fixes listed errors; it does not re-explore
VERIFY_RETRY_MIN_SHARE = 0.2  # skip the verify retry when less of the per-issue cap is left
FEEDBACK_ITEMS = 10  # per section of the verify-retry note
ITEM_CHARS = 220
VERIFY_DIFF_CHARS = 2000

RetryKind = Literal["theater", "verify"]


def retry_budget(max_tool_turns: int) -> int:
    """Extra tool turns for one theater retry: a third of the main budget, at most 20."""
    return max(1, min(RETRY_TURNS_MAX, max_tool_turns // 3))


def verify_retry_budget(max_tool_turns: int) -> int:
    """Extra tool turns for the verify retry: a third of the main budget, at most 8."""
    return max(1, min(VERIFY_RETRY_TURNS_MAX, max_tool_turns // 3))


def budget_for(kind: RetryKind, max_tool_turns: int) -> int:
    return verify_retry_budget(max_tool_turns) if kind == "verify" else retry_budget(max_tool_turns)


# --------------------------------------------------------------- verify-retry feedback

_SECTION = re.compile(r"^## (\w+) \(exit (-?\d+), [\d.]+ s\)\n(?:\$ [^\n]*\n?)?", re.MULTILINE)
_PYTEST_RED = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$", re.MULTILINE)
_MYPY = re.compile(r"^(\S+?:\d+(?::\d+)?): error: (.*)$", re.MULTILINE)
_RUFF_FULL = re.compile(r"^([A-Z]+\d+) (.+)\n\s*--> (\S+?:\d+:\d+)", re.MULTILINE)
_RUFF_CONCISE = re.compile(r"^(\S+?:\d+:\d+): ([A-Z]+\d+) (.+)$", re.MULTILINE)
_REFORMAT = re.compile(r"^Would reformat: (\S+)$", re.MULTILINE)


def _clip(text: str, limit: int = ITEM_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def gate_sections(gate_text: str) -> dict[str, tuple[int, str]]:
    """gate.txt → {step: (exit code, output tail)} (pipeline.verify_candidate writes it)."""
    found = list(_SECTION.finditer(gate_text))
    out: dict[str, tuple[int, str]] = {}
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(gate_text)
        out[m.group(1)] = (int(m.group(2)), gate_text[m.end() : end].strip())
    return out


def pytest_failures(output: str, only: list[str] | None = None) -> list[str]:
    """`FAILED node - msg` / `ERROR node - msg` lines → "node — msg" (pytest's short summary);
    `only` keeps those node ids."""
    seen: dict[str, str] = {}
    for node, msg in _PYTEST_RED.findall(output):
        if only is not None and node not in only:
            continue
        seen.setdefault(node, msg or "")
    return [f"{n} — {_clip(m)}" if m else n for n, m in seen.items()]


def lint_errors(step: str, output: str) -> list[str]:
    """mypy / ruff output → "file:line: message" lines."""
    if step == "mypy":
        return [f"{loc}: {_clip(msg)}" for loc, msg in _MYPY.findall(output)]
    if step == "ruff_check":
        full = [f"{loc}: {code} {_clip(msg)}" for code, msg, loc in _RUFF_FULL.findall(output)]
        concise = [
            f"{loc}: {code} {_clip(msg)}" for loc, code, msg in _RUFF_CONCISE.findall(output)
        ]
        return full or concise
    if step == "ruff_format":
        return [f"{path}: not formatted (ruff format)" for path in _REFORMAT.findall(output)]
    return []


def _items(lines: list[str]) -> str:
    more = len(lines) - FEEDBACK_ITEMS
    body = "\n".join(f"- {ln}" for ln in lines[:FEEDBACK_ITEMS])
    return body + (f"\n- … and {more} more" if more > 0 else "")


def verify_feedback(
    verdict: Verdict,
    *,
    attempt: int,
    retries: int,
    gate_text: str,
    regression_text: str = "",
    fix_diff: str = "",
    lint: str = "",
    turns: int = VERIFY_RETRY_TURNS_MAX,
) -> str:
    """The user note for verify retry `attempt`: the repro is accepted; these checks are red."""
    from warden.verify.verifier import verify_fixable

    red = verify_fixable(verdict)
    sections = gate_sections(gate_text)
    parts = [
        f"## Verifier feedback — verify retry {attempt} of {retries}",
        "You already worked on this issue and your change is in this working copy. Your new "
        "test reproduces the issue (it fails on the unfixed code and passes with your fix): "
        "the verifier accepted that part. It rejected the candidate only for: "
        + ", ".join(red)
        + f". Fix exactly these, then call finish. You have {turns} tool turns: do not explore.",
    ]
    if "suite" in red:
        code, out = sections.get("suite", (1, ""))
        fails = pytest_failures(out)
        detail = _items(fails) if fails else "```\n" + _tail(out, 1200) + "\n```"
        counts = ", ".join(f"{v} {k}" for k, v in verdict.suite.items()) or "no summary"
        parts.append(f"### Full suite red ({counts}, exit {code})\n{detail}")
    for step, title in (
        ("mypy", "mypy errors"),
        ("ruff_check", "ruff check errors"),
        ("ruff_format", "ruff format"),
    ):
        code, out = sections.get(step, (0, ""))
        if code == 0:
            continue
        errs = lint_errors(step, out)
        detail = _items(errs) if errs else "```\n" + _tail(out, 800) + "\n```"
        parts.append(f"### {title} (exit {code})\n{detail}")
    for check, steps in (
        ("alembic", ("db_create", "alembic_upgrade", "alembic_heads")),
        ("openapi", ("openapi_snapshot", "openapi_diff")),
    ):
        if check not in red:
            continue
        bad = [(s, sections[s]) for s in steps if s in sections and sections[s][0] != 0]
        for step, (code, out) in bad[:1]:
            parts.append(f"### {step} (exit {code})\n```\n{_tail(out, 600)}\n```")
    if verdict.regressions:
        _, _, cand_out = regression_text.partition("# candidate")
        found = pytest_failures(cand_out, only=list(verdict.regressions))
        named = {f.split(" — ", 1)[0] for f in found}
        lines = found + [n for n in verdict.regressions if n not in named]
        parts.append(
            "### Existing tests: green on the unfixed code, red with your change\n" + _items(lines)
        )
    if verdict.spec_unmet:
        parts.append(
            "### Issue behaviours the spec review found not implemented\n"
            + _items([_clip(u, 400) for u in verdict.spec_unmet])
        )
    if fix_diff.strip():
        shown = _tail(fix_diff, VERIFY_DIFF_CHARS)
        parts.append(f"Your change so far (diff):\n```diff\n{shown}\n```")
    steps_text = [
        "What to do now:",
        "1. Keep the fix and the repro test: they are accepted. Do not delete, skip or xfail any "
        "test, do not edit tests that existed before your change, and do not silence a check "
        "(`# type: ignore`, `# noqa`, config changes).",
        "2. A red existing test means your change broke behaviour it relies on: fix the source. "
        "A red test of your own: fix the test or the code it exercises.",
        "3. Fix each error at the file:line given (read only those lines).",
        "4. Re-run the failing tests by node id with run_shell"
        + (f", then `{lint}`" if lint else "")
        + ". Then call finish.",
    ]
    parts.append("\n".join(steps_text))
    return "\n\n".join(parts)


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "[…]\n" + text[-limit:]


def feedback(
    verdict: Verdict, *, attempt: int, retries: int, base_output: str, fix_diff: str
) -> str:
    """The user note for retry `attempt` of `retries`: what the verifier saw, what to do."""
    per_test = "\n".join(f"- {node}: {why}" for node, why in verdict.theater_tests.items())
    why = next((r for r in verdict.reasons if r.startswith("theater:")), "theater check failed")
    if verdict.decision != "FAIL" and verdict.main_guard:
        intro = (
            "You already finished this issue once and your fix is in this working copy. The "
            "verifier did not accept the evidence: the tests for the issue's MAIN behaviour ("
            + ", ".join(verdict.main_guard[:4])
            + ") pass on the unfixed code, so they are guards, not a reproduction; whatever "
            "test does fail on the unfixed code exercises something else. Write a test of the "
            "issue's own scenario — its exact inputs, the exception class its traceback shows, "
            "the YAML kind / command line it uses — that FAILS on the unfixed code and PASSES "
            "with your fix. If it still fails with your fix, the fix is incomplete: fix the code."
        )
    else:
        intro = (
            "You already finished this issue once and your fix is in this working copy. The "
            f"verifier rejected the candidate for one reason only: {why}. A new test must FAIL "
            "on the unfixed code, on an assertion about the behaviour the issue describes, and "
            "PASS with your fix; none of yours does, so nothing proves the fix."
        )
    parts = [
        f"## Verifier feedback — theater retry {attempt} of {retries}",
        intro,
        f"Your new tests on the unfixed base:\n{per_test or '- (no per-test results)'}",
    ]
    if base_output.strip():
        parts.append(
            "pytest on the unfixed base (tail):\n```\n"
            + _tail(base_output, BASE_OUTPUT_CHARS)
            + "\n```"
        )
    if fix_diff.strip():
        diff = _tail(fix_diff, DIFF_CHARS)
        parts.append(f"Your change so far (diff):\n```diff\n{diff}\n```")
    parts.append(
        "What to do now:\n"
        "1. Keep the fix: do not revert, rewrite or 'simplify' the source change.\n"
        "2. Find the input that the bug actually mishandles (a test whose input already "
        "satisfies the fixed behaviour passes either way) and add or adjust a test that asserts "
        "the issue's expected behaviour on it. Do not delete existing tests.\n"
        f"3. Check it bites: `{UNFIXED_TOOL_NAME}` runs your current test files on the unfixed "
        "source (your working copy is untouched) — the new test must fail there, on an assertion, "
        "not on a NameError/ImportError/TypeError for an API that does not exist yet.\n"
        "4. Run it with run_shell here: it must pass with your fix. Then call finish."
    )
    return "\n\n".join(parts)


def merge_tests(prior: list[str], new: list[str]) -> list[str]:
    """Both attempts' new tests, in order; a node under a file already listed is not repeated."""
    out: list[str] = []
    for t in [*prior, *new]:
        if t in out or t.split("::", 1)[0] in out:
            continue
        out.append(t)
    return out


def retry_implementer(
    llm: LLMLike,
    base: Box,
    cand: Candidate,
    plan: Plan,
    verdict: Verdict,
    log: EventLog,
    *,
    dispatcher: ToolDispatcher,
    attempt: int,
    retries: int,
    max_tool_turns: int,
    base_output: str = "",
    progress: Progress | None = None,
    kind: RetryKind = "theater",
    seq: int | None = None,
    gate_text: str = "",
    regression_text: str = "",
    **implementer_kw: Any,
) -> Candidate:
    """Retry `attempt` (of `retries` of this `kind`): the implementer continues on a fork of
    `cand`'s checkpoint with the verifier's feedback; return the new Candidate (tests and turns
    of both attempts, the cost of both). `base` is the unfixed base: `run_tests_unfixed` runs
    there. `seq` is the candidate's retry number across kinds (its *.attempt<n>.* artifacts).
    A verify retry's note comes from `gate_text` (gate.txt) and `regression_text`."""
    start = base.sibling(cand.image_uuid)
    shown = dispatcher.run(start, "git add -A -N . && git diff HEAD", timeout=120)
    diff = shown.stdout if shown.ok else ""
    budget = budget_for(kind, max_tool_turns)
    if kind == "verify":
        note = verify_feedback(
            verdict,
            attempt=attempt,
            retries=retries,
            gate_text=gate_text,
            regression_text=regression_text,
            fix_diff=diff,
            lint=str(implementer_kw.get("lint", "")),
            turns=budget,
        )
    else:
        note = feedback(
            verdict, attempt=attempt, retries=retries, base_output=base_output, fix_diff=diff
        )
    data: dict[str, Any] = {
        "text": f"{kind} retry {attempt}/{retries}: implementer continues from the "
        f"candidate, {budget} turns",
        "retry": kind,
        "attempt": (seq or attempt) + 1,
        "from": cand.image_uuid,
        "max_tool_turns": budget,
    }
    if kind == "verify":
        data["verify_retry"] = "start"
        data["feedback_chars"] = len(note)
    log.emit("fix", "note", data)
    new = run_implementer(
        llm,
        start,
        plan,
        log,
        dispatcher=dispatcher,
        max_tool_turns=budget,
        progress=progress,
        variant=note,
        unfixed=base,
        prior_tests=list(cand.tests_added),
        **implementer_kw,
    )
    return new.model_copy(
        update={
            "tests_added": merge_tests(cand.tests_added, new.tests_added),
            "tool_turns": cand.tool_turns + new.tool_turns,
            "cost_usd": round(cand.cost_usd + new.cost_usd, 6),
        }
    )
