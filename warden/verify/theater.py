"""Theater check: the new tests must FAIL on the unfixed base.

Fork `base`, copy in ONLY the new/changed test files, run them. If they pass, the tests do not
exercise the change and the candidate cannot be trusted. This is the row judges will screenshot.
"""

from __future__ import annotations

from dataclasses import dataclass

from warden.sandbox.box import Box


@dataclass(frozen=True)
class TheaterResult:
    failed_on_base: bool
    exit_code: int
    stdout: str
    base_uuid: str
    probe_uuid: str

    @property
    def label(self) -> str:
        return (
            "FAIL on unfixed base — as required"
            if self.failed_on_base
            else "PASSED on unfixed base — tests are theater"
        )


def theater_check(
    base: Box, new_tests: dict[str, bytes], *, test_cmd: str = "pytest -q"
) -> TheaterResult:
    if not new_tests:
        raise ValueError("theater check needs at least one new test file")
    probe = base.copy_in(new_tests)
    targets = " ".join(sorted(new_tests))
    res = probe.run(f"{test_cmd} {targets}", keep=False)
    return TheaterResult(
        failed_on_base=not res.ok,
        exit_code=res.exit_code,
        stdout=res.stdout[-4000:],
        base_uuid=base.uuid,
        probe_uuid=probe.uuid,
    )
