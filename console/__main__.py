"""`python -m console [ROOT] [--port N]`: serve a site/ or runs/ directory read-only."""

from __future__ import annotations

import argparse
from pathlib import Path

from console.app import DEFAULT_ROOT, create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m console", description=__doc__)
    p.add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT, help="site/ or runs/")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args()
    if not a.root.is_dir():
        p.error(f"no such directory: {a.root}")
    import uvicorn

    uvicorn.run(create_app(a.root), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
