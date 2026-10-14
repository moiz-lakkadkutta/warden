"""Spec review: an LLM lists the issue's stated behaviours; only quoted, verifiable 'no' blocks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from warden.config import Role
from warden.hunt.issue import IssueText
from warden.schemas import SpecBehaviour, SpecReview
from warden.verify.spec import spec_review

ISSUE = IssueText(
    number=0,
    title="qml.simplify removes num_steps from Exp",
    body="Expected behavior:\n>>> qml.simplify(op1).num_steps\n2\n\nActual: None",
    comments=["maintainer: the fix is in Exp.simplify"],
)


class FakeLLM:
    def __init__(self, out: BaseModel) -> None:
        self.out = out
        self.calls: list[tuple[Role, str, str, str]] = []

    def structured(
        self, role: Role, phase: str, system: str, user: str, schema: type[BaseModel], **_: Any
    ) -> BaseModel:
        self.calls.append((role, phase, system, user))
        assert isinstance(self.out, schema)
        return self.out


def _b(
    met: str, quote: str = "qml.simplify(op1).num_steps\n2", text: str = "keeps num_steps"
) -> SpecBehaviour:
    return SpecBehaviour(behaviour=text, quote=quote, met=met, evidence="diff line 12")  # type: ignore[arg-type]


def test_met_behaviours_are_clean_and_the_call_is_a_verdict_call() -> None:
    llm = FakeLLM(SpecReview(behaviours=[_b("yes")], summary="ok"))
    res = spec_review(
        llm, ISSUE, diff="+ return Exp(..., num_steps=self.num_steps)", tests=["t::a"], system="SYS"
    )
    assert res.unmet == [] and res.unclear == []
    role, phase, system, user = llm.calls[0]
    assert role is Role.VERDICT and phase == "verify" and system == "SYS"
    assert "num_steps=self.num_steps" in user and "t::a" in user and ISSUE.title in user


def test_an_unmet_behaviour_quoted_from_the_issue_blocks() -> None:
    res = spec_review(
        FakeLLM(SpecReview(behaviours=[_b("no")], summary="")),
        ISSUE,
        diff="+x",
        tests=[],
        system="S",
    )
    assert res.unmet == ["keeps num_steps"]


def test_quote_matching_ignores_whitespace_and_case() -> None:
    b = _b("no", quote="QML.simplify(op1).num_steps   2")
    res = spec_review(
        FakeLLM(SpecReview(behaviours=[b], summary="")), ISSUE, diff="+x", tests=[], system="S"
    )
    assert res.unmet == ["keeps num_steps"]


def test_an_unmet_behaviour_the_issue_never_states_is_only_unclear() -> None:
    b = _b("no", quote="must also support Hamiltonians", text="supports Hamiltonians")
    res = spec_review(
        FakeLLM(SpecReview(behaviours=[b], summary="")), ISSUE, diff="+x", tests=[], system="S"
    )
    assert res.unmet == []
    assert res.unclear == ["supports Hamiltonians (quote not found in the issue)"]


def test_unclear_is_a_concern_not_a_block() -> None:
    res = spec_review(
        FakeLLM(SpecReview(behaviours=[_b("unclear")], summary="")),
        ISSUE,
        diff="+x",
        tests=[],
        system="S",
    )
    assert res.unmet == [] and res.unclear == ["keeps num_steps"]


def test_comments_are_never_sent_and_the_diff_is_capped() -> None:
    llm = FakeLLM(SpecReview(behaviours=[], summary=""))
    spec_review(llm, ISSUE, diff="+" + "x" * 100_000, tests=[], system="S", max_diff_chars=1000)
    user = llm.calls[0][3]
    assert "maintainer" not in user
    assert len(user) < 3000 and "diff truncated" in user
