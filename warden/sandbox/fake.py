"""In-memory sandbox backend for tests and `warden run --demo`.

Each image is a dict of files plus an id; `run` understands a tiny command vocabulary that is
enough to exercise fork semantics and the theater check without any network.
"""

from __future__ import annotations

import itertools
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
        child = self._new(image) if keep else image
        if files:
            for rel, data in files.items():
                child.files[f"{cwd.rstrip('/')}/{rel}"] = data
        stdout, stderr, code = self._interpret(child, shell, cwd)
        return stdout, stderr, code, (child if keep else None), child.uuid

    def _interpret(self, img: FakeImage, shell: str, cwd: str) -> tuple[str, str, int]:
        parts = shlex.split(shell) if shell else []
        if not parts or parts[0] == "true":
            return "", "", 0
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
        return self._interpret_gate(img, parts)

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
