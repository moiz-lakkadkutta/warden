"""Fetch one GitHub issue as plain text for the planner (the `hunt` step for a given issue)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

Runner = Callable[..., subprocess.CompletedProcess[str]]


class IssueText(BaseModel):
    number: int
    title: str
    body: str
    comments: list[str]


def fetch_issue(
    repo: str, number: int, *, include_comments: bool, runner: Runner = subprocess.run
) -> IssueText:
    """`gh issue view N -R repo --json ...`. Comments are optional: when replaying a closed issue,
    a comment may already contain the human's plan, which would leak the answer."""
    cmd = ["gh", "issue", "view", str(number), "-R", repo, "--json", "number,title,body,comments"]
    res = runner(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"gh issue view {number} -R {repo} failed: {res.stderr.strip()}")
    data: dict[str, Any] = json.loads(res.stdout)
    comments = (
        [str(c.get("body", "")) for c in data.get("comments", [])] if include_comments else []
    )
    return IssueText(
        number=int(data["number"]),
        title=str(data.get("title", "")),
        body=str(data.get("body") or ""),
        comments=comments,
    )
