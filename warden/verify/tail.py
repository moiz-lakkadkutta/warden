"""Keep the END of long test output.

contree_sdk truncates stdout and stderr at `default_truncate_output_at` (65535 bytes) and keeps
the HEAD. pytest prints its verdict last (the `-rA` short summary, the `N passed in Xs` line), so a
long suite loses exactly the lines Warden parses — run 20261014-123938-6f4c reported "no summary"
on a suite that had one. `keep_tail` sends the output to a file and prints only its last
`max_bytes`, under the cap, with the command's own exit status.
"""

from __future__ import annotations

TAIL_BYTES = 60_000  # below contree_sdk's 65535-byte cap, so the sandbox never cuts it again
LOG = "/tmp/warden-out.log"


def keep_tail(shell: str, max_bytes: int = TAIL_BYTES, *, log: str = LOG) -> str:
    """`shell` with stdout+stderr reduced to their last `max_bytes`; exit status preserved.

    The redirect binds to the last simple command (`cd x && pytest -q` → pytest's output), and the
    command stays first so a reader (and `runlog/render.py`) still sees what ran.
    """
    return f"{shell} > {log} 2>&1; rc=$?; tail -c {max_bytes} {log}; exit $rc"


def was_cut(output: str, max_bytes: int = TAIL_BYTES) -> bool:
    """True when `output` is a `keep_tail` tail that hit its limit (its head was dropped)."""
    return len(output.encode()) >= max_bytes
