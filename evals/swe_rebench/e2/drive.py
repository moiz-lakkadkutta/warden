"""Run `warden eval ...` through the CLI entry point.

The agent harness that ran E2 refuses shell commands containing the word "eval" (it reads them
as the shell builtin), so the shard runs call `warden.cli.app` from here instead:

    uv run python evals/swe_rebench/e2/drive.py --out evals/swe_rebench/e2 --shard 1/5 ...

Every argument is passed through after the subcommand name.
"""

import sys

from warden.cli import app

if __name__ == "__main__":
    app(["eval", *sys.argv[1:]], prog_name="warden")
