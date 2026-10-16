"""Spec review: does the diff do what the issue *says*, not what the implementer read into it?

The theater check proves the new tests bite; it cannot prove they test the behaviour the issue
asked for. One VERDICT call reads the issue (title + body; comments never, they can hold the
human's plan) beside the candidate diff and lists each behaviour the issue states, with the
issue's own words as a quote. The model's word is not evidence by itself: a "no" blocks only when
its quote is found in the issue text (whitespace and case ignored); an unquotable "no" or an
"unclear" is a concern. It can only add FAIL reasons or concerns, never upgrade a decision.

The same call lists the issue's concrete *artefacts* (the exception class its traceback raises,
the YAML `kind` it uses, its command line, its input values) and whether the new tests use them.
E1's two verifier misses were tests of look-alikes: pre-commit-1997's repro mocked
`subprocess.CalledProcessError` where the traceback shows `pre_commit.util.CalledProcessError`,
and every checkov-831 test used `kind: Pod` for an issue about `kind: Deployment`. A quoted "not
used" is unmet, like a behaviour; the same quote rule applies.

Each behaviour also names the new tests that exercise it; the first is the issue's main one.
`main_tests` feeds the verifier's guard rule (verifier.main_guard): when every test mapped to
the main behaviour passes on the unfixed base, the evidence for it is a guard, not a repro.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from warden.config import Role
from warden.hunt.issue import UNTRUSTED_RULE, IssueText, fence_untrusted
from warden.plan.planner import StructuredLLM
from warden.schemas import SpecReview

MAX_DIFF_CHARS = 40_000
_ELISION = re.compile(r"\.{3,}|…")


@dataclass(frozen=True)
class SpecResult:
    review: SpecReview
    # quoted from the issue and not implemented, then artefacts the tests do not use
    unmet: list[str] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)
    artefacts_unmet: int = 0  # how many of `unmet` (its tail) are artefacts

    @property
    def main_tests(self) -> list[str]:
        """The new tests the review maps to the issue's main (first) behaviour."""
        return list(self.review.behaviours[0].tests) if self.review.behaviours else []

    @property
    def label(self) -> str:
        n = len(self.review.behaviours)
        behaviours = len(self.unmet) - self.artefacts_unmet
        if self.unmet:
            parts = []
            if behaviours:
                parts.append(f"{behaviours} of {n} stated behaviours not implemented")
            if self.artefacts_unmet:
                k = len(self.review.artefacts)
                parts.append(f"{self.artefacts_unmet} of {k} issue artefacts not used by the tests")
            return "; ".join(parts)
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
    stated = _norm(f"{issue.title}\n{issue.body}".replace("`", " "))

    def quoted(quote: str) -> bool:
        """Every fragment between elisions (`...`, `…`) is in the issue: the model quotes long
        YAML with its middle elided (E1 checkov-831), never words the issue lacks."""
        parts = [_norm(p) for p in _ELISION.split(quote.replace("`", " "))]
        parts = [p for p in parts if p]
        return bool(parts) and all(p in stated for p in parts)

    unmet: list[str] = []
    unclear: list[str] = []
    for b in review.behaviours:
        if b.met == "yes":
            continue
        if b.met == "no" and quoted(b.quote):
            unmet.append(b.behaviour)
        elif b.met == "no":
            unclear.append(f"{b.behaviour} (quote not found in the issue)")
        else:
            unclear.append(b.behaviour)
    artefacts: list[str] = []
    for a in review.artefacts:
        if a.used == "yes":
            continue
        what = f"the tests do not use the issue's {a.artefact}"
        if a.used == "no" and quoted(a.quote):
            artefacts.append(what)
        elif a.used == "no":
            unclear.append(f"{what} (quote not found in the issue)")
        else:
            unclear.append(f"unclear whether the tests use the issue's {a.artefact}")
    return SpecResult(review, unmet + artefacts, unclear, artefacts_unmet=len(artefacts))
