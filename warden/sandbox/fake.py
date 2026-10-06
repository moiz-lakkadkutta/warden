"""In-memory sandbox backend for tests and `warden run --demo`.

Each image is a dict of files plus an id; `run` understands a tiny command vocabulary that is
enough to exercise fork semantics and the theater check without any network.
"""

from __future__ import annotations

import itertools
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeImage:
    uuid: str
    files: dict[str, bytes] = field(default_factory=dict)
    # test outcome table: path -> exit code that `pytest <path>` returns
    test_results: dict[str, int] = field(default_factory=dict)
    # gate vocabulary: command key ("mypy", "ruff check", "git diff", ...) -> exit code
    gate_results: dict[str, int] = field(default_factory=dict)
    # pytest summary counts, e.g. {"passed": 5, "failed": 2}; empty = derive from exit code
    suite_counts: dict[str, int] = field(default_factory=dict)
    # lines printed by `alembic heads`
    alembic_heads: list[str] = field(default_factory=lambda: ["abc123 (head)"])


class FakeBackend:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.images: dict[str, FakeImage] = {}
        self.calls: list[str] = []
        self.timeouts: list[int] = []  # parallel to `calls`

    def _new(self, parent: FakeImage | None = None) -> FakeImage:
        img = FakeImage(f"img-{next(self._ids):04d}")
        if parent is not None:
            img.files = dict(parent.files)
            img.test_results = dict(parent.test_results)
            img.gate_results = dict(parent.gate_results)
            img.suite_counts = dict(parent.suite_counts)
            img.alembic_heads = list(parent.alembic_heads)
        self.images[img.uuid] = img
        return img

    def use(self, ref: str) -> Any:
        img = self._new()
        img.files["/app/REF"] = ref.encode()
        return img

    def run(
        self,
        image: Any,
        *,
        shell: str,
        cwd: str,
        env: dict[str, str] | None,
        timeout: int,
        files: dict[str, bytes] | None,
        keep: bool,
    ) -> tuple[str, str, int, Any | None, str]:
        self.calls.append(shell)
        self.timeouts.append(timeout)
        child = self._new(image) if keep else image
        if files:
            for rel, data in files.items():
                child.files[f"{cwd.rstrip('/')}/{rel}"] = data
        stdout, stderr, code = self._interpret(child, shell, cwd)
        return stdout, stderr, code, (child if keep else None), child.uuid

    def _interpret(self, img: FakeImage, shell: str, cwd: str) -> tuple[str, str, int]:
        try:
            parts = shlex.split(shell) if shell else []
        except ValueError:
            return "", "fake backend: cannot parse shell", 127
        # `|`-tails are no-ops (e.g. `find … | sort`): tokens from the first bare `|` are dropped.
        if "||" in parts:
            return "", "fake backend: unknown command ||", 127
        if "|" in parts:
            parts = parts[: parts.index("|")]
        if not parts or parts[0] == "true":
            return "", "", 0
        if parts[0] == "false":
            return "", "", 1
        if parts[0] == "echo":
            text = " ".join(parts[1:])
            if ">>" in shell or ">" in shell:
                op = ">>" if ">>" in shell else ">"
                lhs, target = shell.split(op, 1)
                text = " ".join(shlex.split(lhs)[1:])
                path = target.strip()
                path = path if path.startswith("/") else f"{cwd.rstrip('/')}/{path}"
                prev = img.files.get(path, b"") if op == ">>" else b""
                img.files[path] = prev + text.encode() + b"\n"
                return "", "", 0
            return text + "\n", "", 0
        if parts[0] == "cat":
            path = parts[1] if parts[1].startswith("/") else f"{cwd.rstrip('/')}/{parts[1]}"
            if path in img.files:
                return img.files[path].decode(), "", 0
            return "", f"cat: {parts[1]}: No such file", 1
        if parts[0] == "pytest":
            targets = [p for p in parts[1:] if not p.startswith("-")] or ["<suite>"]
            code = max(img.test_results.get(t, 0) for t in targets)
            counts = dict(img.suite_counts)
            if counts.get("failed", 0) > 0 or counts.get("errors", 0) > 0:
                code = 1
            if not counts:
                counts = {"failed": 1} if code else {"passed": 1}
            summary = ", ".join(f"{n} {k}" for k, n in counts.items())
            return f"{' '.join(targets)}\n===== {summary} in 0.10s =====\n", "", code
        if parts[0] == "find":
            return self._find(img, parts, cwd)
        if parts[0] == "ls":
            return self._ls(img, parts, cwd)
        if parts[0] == "grep":
            return self._grep(img, parts, cwd)
        return self._interpret_gate(img, parts)

    # -- read-only vocabulary used by fix/tools.py -----------------------------------------

    @staticmethod
    def _abs(p: str, cwd: str) -> str:
        return posixpath.normpath(p if p.startswith("/") else f"{cwd.rstrip('/')}/{p}")

    @staticmethod
    def _under(img: FakeImage, root: str) -> list[str]:
        """Absolute keys equal to `root` or under `root/`, sorted."""
        root = root.rstrip("/") or "/"
        return sorted(k for k in img.files if k == root or k.startswith(root.rstrip("/") + "/"))

    @staticmethod
    def _shown(key: str, root: str, arg: str) -> str:
        """Re-spell an absolute key relative to the argument as the user typed it."""
        root = root.rstrip("/")
        return arg if key == root else f"{arg.rstrip('/')}{key[len(root) :]}"

    def _find(self, img: FakeImage, parts: list[str], cwd: str) -> tuple[str, str, int]:
        arg = next((p for p in parts[1:] if not p.startswith("-")), ".")
        root = self._abs(arg, cwd)
        keys = self._under(img, root)
        if not keys:
            return "", f"find: '{arg}': No such file or directory\n", 1
        return "".join(self._shown(k, root, arg) + "\n" for k in keys), "", 0

    def _ls(self, img: FakeImage, parts: list[str], cwd: str) -> tuple[str, str, int]:
        arg = next((p for p in parts[1:] if not p.startswith("-")), ".")
        root = self._abs(arg, cwd)
        keys = self._under(img, root)
        if not keys:
            return "", f"ls: cannot access '{arg}': No such file or directory\n", 2
        if keys == [root.rstrip("/")]:
            return arg + "\n", "", 0
        prefix = root.rstrip("/") + "/"
        names = sorted({k[len(prefix) :].split("/", 1)[0] for k in keys})
        return "".join(n + "\n" for n in names), "", 0

    def _grep(self, img: FakeImage, parts: list[str], cwd: str) -> tuple[str, str, int]:
        pattern: str | None = None
        positional: list[str] = []
        extended = False
        it = iter(parts[1:])
        for tok in it:
            if tok == "-e":
                pattern = next(it, "")
            elif tok == "--":
                positional.extend(it)
            elif tok.startswith("-"):
                extended = extended or "E" in tok
                continue
            else:
                positional.append(tok)
        if pattern is None:
            pattern, positional = (positional[0], positional[1:]) if positional else ("", [])
        arg = positional[0] if positional else "."
        root = self._abs(arg, cwd)
        keys = self._under(img, root)
        if not keys:
            return "", f"grep: {arg}: No such file or directory\n", 2
        out: list[str] = []
        for key in keys:
            try:
                text = img.files[key].decode()
            except UnicodeDecodeError:
                continue
            shown = self._shown(key, root, arg)
            out.extend(
                f"{shown}:{n}:{line}\n"
                for n, line in enumerate(text.splitlines(), 1)
                if (re.search(pattern, line) if extended else pattern in line)
            )
        return "".join(out), "", 0 if out else 1

    def _interpret_gate(self, img: FakeImage, parts: list[str]) -> tuple[str, str, int]:
        key = " ".join(parts[:2]) if " ".join(parts[:2]) in img.gate_results else parts[0]
        code = img.gate_results.get(key, 0)
        if parts[0] == "mypy":
            return (
                "Success: no issues found\n" if code == 0 else f"Found {code} errors\n",
                "",
                code,
            )
        if parts[0] == "ruff" and parts[1:2] in (["check"], ["format"]):
            return ("All checks passed!\n" if code == 0 else "", "", code)
        if parts[0] in ("createdb", "dropdb"):
            return "", "", code
        if parts[0] == "alembic":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "heads":
                return "".join(f"{h}\n" for h in img.alembic_heads), "", 0
            if sub in ("upgrade", "downgrade"):
                return "", "", code
        if parts[0] == "make":
            return "", "", code
        if parts[0] == "git" and parts[1:2] == ["diff"]:
            return "", "", code
        if parts[0] == "git" and parts[1:2] == ["status"]:
            return "", "", 0
        return "", f"fake backend: unknown command {parts[0]}", 127

    def tag(self, image: Any, name: str) -> None:
        image.files["/app/.tag"] = name.encode()

    def uuid_of(self, image: Any) -> str:
        return str(image.uuid)
