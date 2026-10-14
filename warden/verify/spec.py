"""Spec review: does the diff do what the issue *says*, not what the implementer read into it?

The theater check proves the new tests bite; it cannot prove they test the behaviour the issue
asked for. One VERDICT call reads the issue (title + body; comments never, they can hold the
human's plan) beside the candidate diff and lists each behaviour the issue states, with the
issue's own words as a quote. The model's word is not evidence by itself: a "no" blocks only when
its quote is found in the issue text (whitespace and case ignored); an unquotable "no" or an
"unclear" is a concern. It can only add FAIL reasons or concerns, never upgrade a decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from warden.config import Role
from warden.hunt.issue import UNTRUSTED_RULE, IssueText, fence_untrusted
from warden.plan.planner import StructuredLLM
from warden.schemas import SpecReview

MAX_DIFF_CHARS = 40_000


@dataclass(frozen=True)
class SpecResult:
    review: SpecReview
    unmet: list[str] = field(default_factory=list)  # quoted from the issue and not implemented
    unclear: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        n = len(self.review.behaviours)
        if self.unmet:
            return f"{len(self.unmet)} of {n} stated behaviours not implemented"
        return f"{n - len(self.unclear)} of {n} stated behaviours implemented" + (
            f", {len(self.unclear)} unclear" if self.unclear else ""
        )


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def spec_user_message(issue: IssueText, diff: str, tests: list[str], max_diff_chars: int) -> str:
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n... (diff truncated at {max_diff_chars} chars)"
    return (
        f"# Issue\n{fence_untrusted(f'{issue.title}{chr(10) * 2}{issue.body}')}\n\n"
        f"# New tests\n{chr(10).join(tests) or '(none)'}\n\n"
        f"# Candidate diff\n```diff\n{diff}\n```\n"
    )


def spec_review(
    llm: StructuredLLM,
    issue: IssueText,
    *,
    diff: str,
    tests: list[str],
    system: str,
    max_diff_chars: int = MAX_DIFF_CHARS,
) -> SpecResult:
    review = llm.structured(
        Role.VERDICT,
        "verify",
        f"{system.rstrip()}\n\n{UNTRUSTED_RULE}\n",
        spec_user_message(issue, diff, tests, max_diff_chars),
        SpecReview,
    )
    stated = _norm(f"{issue.title}\n{issue.body}")
    unmet: list[str] = []
    unclear: list[str] = []
    for b in review.behaviours:
        if b.met == "yes":
            continue
        quoted = bool(_norm(b.quote)) and _norm(b.quote) in stated
        if b.met == "no" and quoted:
            unmet.append(b.behaviour)
        elif b.met == "no":
            unclear.append(f"{b.behaviour} (quote not found in the issue)")
        else:
            unclear.append(b.behaviour)
    return SpecResult(review, unmet, unclear)
