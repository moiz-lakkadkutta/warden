"""Spec review: an LLM lists the issue's stated behaviours; only quoted, verifiable 'no' blocks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from warden.config import Role
from warden.hunt.issue import UNTRUSTED_RULE, IssueText
from warden.schemas import Candidate, SpecArtefact, SpecBehaviour, SpecReview
from warden.verify.spec import spec_review
from warden.verify.verifier import decide

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
    assert role is Role.VERDICT and phase == "verify" and system.startswith("SYS\n")
    assert UNTRUSTED_RULE in system  # the issue is data, never instructions
    assert "num_steps=self.num_steps" in user and "t::a" in user and ISSUE.title in user
    assert user.index("<untrusted-issue>") < user.index(ISSUE.title) < user.index("</untrusted")


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


# ------------------------------------------- the issue's own artefacts (E1 verifier misses)

# Recorded from the E1 tasks (problem statements shortened, artefact lines verbatim).
PRE_COMMIT = IssueText(
    number=0,
    title="Docker inspect fails when running with Docker in Docker",
    body=(
        "Could the code for this be modified to handle the docker inspect failing?\n\n"
        "```\nTraceback (most recent call last):\n"
        '  File "/usr/local/lib/python3.6/dist-packages/pre_commit/util.py", line 154, in '
        "cmd_output_b\n    raise CalledProcessError(returncode, cmd, retcode, stdout_b, "
        "stderr_b)\npre_commit.util.CalledProcessError: command: ('/usr/bin/docker', 'inspect', "
        "'runner-dmrvfsu-project-35395-concurrent-0')\nreturn code: 1\n```"
    ),
    comments=[],
)
CHECKOV = IssueText(
    number=0,
    title="CKV_K8S_31 failure with RuntimeDefault configured for workloads",
    body=(
        "The bug reported in #710 needs to be fixed for workloads in Seccomp.py as well.\n\n"
        "```yaml\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n....\n  template:\n"
        "    spec:\n      securityContext:\n        seccompProfile:\n"
        "          type: RuntimeDefault\n```"
    ),
    comments=[],
)


def _a(used: str, artefact: str, quote: str) -> SpecArtefact:
    return SpecArtefact(artefact=artefact, quote=quote, used=used, evidence="t")  # type: ignore[arg-type]


def _met(quote: str, tests: list[str] | None = None) -> SpecBehaviour:
    return SpecBehaviour(behaviour="b", quote=quote, met="yes", evidence="e", tests=tests or [])


def test_pre_commit_1997_a_mocked_lookalike_exception_is_unmet() -> None:
    review = SpecReview(
        behaviours=[_met("handle the docker inspect failing")],
        artefacts=[
            _a("no", "pre_commit.util.CalledProcessError", "pre_commit.util.CalledProcessError")
        ],
    )
    res = spec_review(FakeLLM(review), PRE_COMMIT, diff="+x", tests=["t::a"], system="S")
    assert res.unmet == ["the tests do not use the issue's pre_commit.util.CalledProcessError"]
    assert res.label == "1 of 1 issue artefacts not used by the tests"


def test_checkov_831_a_pod_test_for_a_deployment_issue_is_unmet() -> None:
    review = SpecReview(
        behaviours=[_met("needs to be fixed for workloads", ["test_workload"])],
        artefacts=[_a("no", "kind: Deployment", "kind: Deployment")],
    )
    res = spec_review(FakeLLM(review), CHECKOV, diff="+x", tests=["test_workload"], system="S")
    assert res.unmet == ["the tests do not use the issue's kind: Deployment"]
    assert res.main_tests == ["test_workload"]
    v = decide(
        Candidate(issue=0, image_uuid="c", tool_turns=1, tests_added=["t"], cost_usd=0, done=True),
        None,
        None,
        spec=res,
    )
    assert v.spec_unmet == res.unmet and v.decision == "FAIL"


def test_an_elided_yaml_quote_counts_when_every_fragment_is_in_the_issue() -> None:
    """Live replay of checkov-831: the model quoted the YAML with its middle elided."""
    good = "kind: Deployment\n...\n      securityContext:\n        seccompProfile:\n" + (
        "          type: RuntimeDefault"
    )
    bad = "kind: Deployment\n...\n      hostNetwork: true"
    review = SpecReview(
        behaviours=[_met("needs to be fixed for workloads")],
        artefacts=[_a("no", "kind: Deployment", good), _a("no", "host network", bad)],
    )
    res = spec_review(FakeLLM(review), CHECKOV, diff="+x", tests=[], system="S")
    assert res.unmet == ["the tests do not use the issue's kind: Deployment"]
    assert res.unclear == [
        "the tests do not use the issue's host network (quote not found in the issue)"
    ]


def test_artefacts_follow_the_quote_rule_and_used_is_clean() -> None:
    review = SpecReview(
        behaviours=[_met("handle the docker inspect failing")],
        artefacts=[
            _a("yes", "pre_commit.util.CalledProcessError", "pre_commit.util.CalledProcessError"),
            _a("no", "docker run flags", "docker run --rm -v"),  # not in the issue
            _a("unclear", "return code 1", "return code: 1"),
        ],
    )
    res = spec_review(FakeLLM(review), PRE_COMMIT, diff="+x", tests=[], system="S")
    assert res.unmet == [] and res.artefacts_unmet == 0
    assert res.unclear == [
        "the tests do not use the issue's docker run flags (quote not found in the issue)",
        "unclear whether the tests use the issue's return code 1",
    ]


def test_a_recorded_spec_json_without_artefacts_still_parses() -> None:
    old = '{"behaviours": [{"behaviour": "b", "quote": "q", "met": "yes", "evidence": "e"}]}'
    review = SpecReview.model_validate_json(old)
    assert review.artefacts == [] and review.behaviours[0].tests == []


def test_the_prompt_asks_for_artefacts_and_the_main_behaviour_first() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "prompts" / "spec.md").read_text()
    assert "ARTEFACTS" in text and "MAIN behaviour" in text and "`tests`" in text
