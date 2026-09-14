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
            codes = [img.test_results.get(t, 0) for t in targets]
            code = max(codes)
            return (f"{'FAILED' if code else 'passed'} {' '.join(targets)}\n", "", code)
        return "", f"fake backend: unknown command {parts[0]}", 127

    def tag(self, image: Any, name: str) -> None:
        image.files["/app/.tag"] = name.encode()

    def uuid_of(self, image: Any) -> str:
        return str(image.uuid)
