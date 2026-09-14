"""Box: the only place Warden talks to Nebius Token Factory Sandboxes (Contree).

Model: an *image* is an immutable filesystem checkpoint. `run(keep=True)` executes a command
and returns a NEW Box (the child checkpoint). Calling `run` twice on the same Box forks it —
that single property gives us worktrees, template databases, the theater check and best-of-N.

The Contree SDK is imported lazily and wrapped behind `SandboxBackend` so tests use FakeBackend.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from warden.runlog.events import EventLog
from warden.schemas import Phase


@dataclass(frozen=True)
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    box: Box | None  # child checkpoint when keep=True
    ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxBackend(Protocol):
    """Minimal surface we need from a sandbox provider."""

    def use(self, ref: str) -> Any: ...
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
    ) -> tuple[str, str, int, Any | None, str]: ...
    def tag(self, image: Any, name: str) -> None: ...
    def uuid_of(self, image: Any) -> str: ...


class ContreeBackend:
    """Real backend. Constructed only when CONTREE_BASE_URL and NEBIUS_API_KEY are set."""

    def __init__(self, api_key: str, base_url: str) -> None:
        from contree_client.httpx import ContreeAsyncClient
        from contree_sdk import ContreeSync

        self._sdk = ContreeSync(ContreeAsyncClient(api_key, base_url=base_url))

    def use(self, ref: str) -> Any:
        if ref.startswith(("docker://", "oci://")):
            return self._sdk.images.oci(ref)
        return self._sdk.images.use(ref)

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
        kwargs: dict[str, Any] = {"shell": shell, "cwd": cwd, "disposable": not keep}
        if env:
            kwargs["env"] = env
        if files:
            kwargs["files"] = files
        op = image.run(**kwargs)
        res = op.wait() if hasattr(op, "wait") else op
        stdout = res.stdout.decode() if isinstance(res.stdout, bytes) else str(res.stdout or "")
        stderr = res.stderr.decode() if isinstance(res.stderr, bytes) else str(res.stderr or "")
        child = res if keep else None
        return stdout, stderr, int(res.exit_code), child, str(getattr(res, "uuid", ""))

    def tag(self, image: Any, name: str) -> None:
        image.tag_as(name)

    def uuid_of(self, image: Any) -> str:
        return str(getattr(image, "uuid", ""))


class Box:
    def __init__(
        self, backend: SandboxBackend, image: Any, log: EventLog, *, phase: Phase = "fix"
    ) -> None:
        self._b = backend
        self._image = image
        self._log = log
        self.phase: Phase = phase

    @classmethod
    def from_ref(
        cls, backend: SandboxBackend, ref: str, log: EventLog, *, phase: Phase = "fix"
    ) -> Box:
        return cls(backend, backend.use(ref), log, phase=phase)

    @property
    def uuid(self) -> str:
        return self._b.uuid_of(self._image)

    def run(
        self,
        shell: str,
        *,
        cwd: str = "/app",
        env: dict[str, str] | None = None,
        timeout: int = 900,
        keep: bool = True,
        files: dict[str, bytes] | None = None,
    ) -> RunResult:
        t0 = time.perf_counter()
        stdout, stderr, code, child, child_uuid = self._b.run(
            self._image, shell=shell, cwd=cwd, env=env, timeout=timeout, files=files, keep=keep
        )
        ms = int((time.perf_counter() - t0) * 1000)
        self._log.emit(
            self.phase,
            "sandbox",
            {
                "shell": shell[:200],
                "exit_code": code,
                "from": self.uuid,
                "to": child_uuid,
                "cwd": cwd,
            },
            ms=ms,
        )
        box = Box(self._b, child, self._log, phase=self.phase) if child is not None else None
        return RunResult(stdout, stderr, code, box, ms)

    def copy_in(self, files: Mapping[str, bytes | Path], *, cwd: str = "/app") -> Box:
        """Fork with files added. Keys are paths relative to cwd."""
        payload = {k: (v.read_bytes() if isinstance(v, Path) else v) for k, v in files.items()}
        res = self.run("true", cwd=cwd, files=payload, keep=True)
        if res.box is None:
            raise RuntimeError("copy_in produced no checkpoint")
        return res.box

    def read_file(self, path: str) -> str:
        res = self.run(f"cat {path}", keep=False)
        if not res.ok:
            raise FileNotFoundError(f"{path}: {res.stderr.strip()}")
        return res.stdout

    def tag(self, name: str) -> None:
        self._b.tag(self._image, name)
