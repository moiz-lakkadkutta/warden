"""Host-side campaign git: the integration branch a later wave stacks on, and the base bundle."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_branch import _patch_against, git, origin  # noqa: F401 — fixture re-export
from warden.integrate.branch import bundle_rev, push_fix_branch, push_integration_branch


def _second_patch(bare: Path, rev: str, tmp_path: Path) -> str:
    w = tmp_path / "box2"
    subprocess.run(["git", "clone", "-q", str(bare), str(w)], check=True)
    git(w, "checkout", "-q", "--detach", rev)
    (w / "other.py").write_text("x = 2\n")
    git(w, "add", "-A")
    git(w, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-q", "-m", "fix #197")
    return git(w, "format-patch", "-1", "--stdout") + "\n"


def test_integration_branch_merges_fix_branches_and_a_later_pr_stacks_on_it(
    origin: tuple[Path, str, str],  # noqa: F811
    tmp_path: Path,
) -> None:
    bare, _, c2 = origin
    cache = tmp_path / "cache"
    kw = dict(base_rev=None, cache_dir=cache, remote_url=str(bare))
    a = push_fix_branch("o/r", _patch_against(bare, c2, tmp_path), issue=196, run_id="c-196", **kw)  # type: ignore[arg-type]
    b = push_fix_branch("o/r", _second_patch(bare, c2, tmp_path), issue=197, run_id="c-197", **kw)  # type: ignore[arg-type]

    sha = push_integration_branch(
        "o/r",
        start=c2,
        heads=[a.head, b.head],
        name="warden/campaign-c-w1",
        cache_dir=cache,
        remote_url=str(bare),
    )
    assert git(bare, "rev-parse", "warden/campaign-c-w1") == sha
    assert git(bare, "rev-list", "--count", "--merges", f"{c2}..{sha}") == "2"
    assert "FINALIZED',)" in git(bare, "show", f"{sha}:svc.py")
    assert git(bare, "show", f"{sha}:other.py") == "x = 2"
    assert git(bare, "rev-parse", "main") == c2  # main untouched

    # a wave-2 fix made on the integrated code applies on the integration branch
    w = tmp_path / "box3"
    subprocess.run(
        ["git", "clone", "-q", "-b", "warden/campaign-c-w1", str(bare), str(w)], check=True
    )
    (w / "svc.py").write_text("STATUSES = ('FINALIZED', 'PAID')\n")
    git(w, "add", "-A")
    git(w, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-q", "-m", "fix #200")
    patch = git(w, "format-patch", "-1", "--stdout") + "\n"
    stacked = push_fix_branch(
        "o/r",
        patch,
        issue=200,
        run_id="c-200",
        base_rev=None,
        cache_dir=cache,
        remote_url=str(bare),
        base_branch="warden/campaign-c-w1",
    )
    assert stacked.base == "warden/campaign-c-w1"
    assert git(bare, "rev-parse", f"{stacked.head}^") == sha


def test_bundle_carries_the_rev_minus_what_the_image_has(
    origin: tuple[Path, str, str],  # noqa: F811
    tmp_path: Path,
) -> None:
    bare, c1, c2 = origin
    sha, data = bundle_rev(
        "o/r", rev=None, exclude=c1, cache_dir=tmp_path / "cache", remote_url=str(bare)
    )
    assert sha == c2
    # an "image" at c1 fetches the bundle and can check out c2
    img = tmp_path / "img"
    subprocess.run(["git", "clone", "-q", str(bare), str(img)], check=True)
    git(img, "checkout", "-q", "--detach", c1)
    git(img, "update-ref", "-d", "refs/remotes/origin/main")
    git(img, "reflog", "expire", "--expire=now", "--all")
    bundle = tmp_path / "b.bundle"
    bundle.write_bytes(data)
    git(img, "fetch", "-q", str(bundle), "refs/warden/campaign:refs/warden/campaign")
    git(img, "checkout", "-q", "--detach", c2)
    assert git(img, "rev-parse", "HEAD") == c2
    # an explicit rev resolves too (a replay commit)
    sha1, _ = bundle_rev(
        "o/r", rev=f"{c2}^", exclude="0" * 40, cache_dir=tmp_path / "cache", remote_url=str(bare)
    )
    assert sha1 == c1
