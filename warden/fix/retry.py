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
"""

from __future__ import annotations

from typing import Any

from warden.fix.implementer import UNFIXED_TOOL_NAME, LLMLike, Progress, run_implementer
from warden.fix.tools import ToolDispatcher
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, Plan, Verdict

RETRY_TURNS_MAX = 20
BASE_OUTPUT_CHARS = 2500
DIFF_CHARS = 3000


def retry_budget(max_tool_turns: int) -> int:
    """Extra tool turns for one theater retry: a third of the main budget, at most 20."""
    return max(1, min(RETRY_TURNS_MAX, max_tool_turns // 3))


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "[…]\n" + text[-limit:]


def feedback(
    verdict: Verdict, *, attempt: int, retries: int, base_output: str, fix_diff: str
) -> str:
    """The user note for retry `attempt` of `retries`: what the verifier saw, what to do."""
    per_test = "\n".join(f"- {node}: {why}" for node, why in verdict.theater_tests.items())
    why = next((r for r in verdict.reasons if r.startswith("theater:")), "theater check failed")
    parts = [
        f"## Verifier feedback — theater retry {attempt} of {retries}",
        "You already finished this issue once and your fix is in this working copy. The verifier "
        f"rejected the candidate for one reason only: {why}. A new test must FAIL on the unfixed "
        "code, on an assertion about the behaviour the issue describes, and PASS with your fix; "
        "none of yours does, so nothing proves the fix.",
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
    **implementer_kw: Any,
) -> Candidate:
    """Retry `attempt`: the implementer continues on a fork of `cand`'s checkpoint with the
    verifier's feedback; return the new Candidate (tests and turns of both attempts, the cost of
    both). `base` is the unfixed base: `run_tests_unfixed` runs there."""
    start = base.sibling(cand.image_uuid)
    shown = dispatcher.run(start, "git add -A -N . && git diff HEAD", timeout=120)
    note = feedback(
        verdict,
        attempt=attempt,
        retries=retries,
        base_output=base_output,
        fix_diff=shown.stdout if shown.ok else "",
    )
    budget = retry_budget(max_tool_turns)
    log.emit(
        "fix",
        "note",
        {
            "text": f"theater retry {attempt}/{retries}: implementer continues from the "
            f"candidate, {budget} turns",
            "retry": "theater",
            "attempt": attempt + 1,
            "from": cand.image_uuid,
            "max_tool_turns": budget,
        },
    )
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
