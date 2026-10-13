"""agent-checkout-runner run | verify"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .runner import main_verify, run


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-checkout-runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch the pinned components, run every case, seal the bundle")
    r.add_argument("--out", type=Path, default=Path("bundle"))
    r.add_argument("--cache", type=Path,
                   default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agent-checkout-runner")
    r.add_argument("--case", action="append", help="run only this case (repeatable)")
    r.add_argument("--reader", help="external reader command; {bundle} is replaced with the bundle path")
    r.add_argument("--suite", action="append", help="run only this component suite (repeatable)")
    r.add_argument("--no-suites", action="store_true", help="run the matrix only, not the component suites")
    v = sub.add_parser("verify", help="re-check a sealed bundle and re-derive every claim from its streams")
    v.add_argument("bundle", type=Path)
    a = ap.parse_args()
    if a.cmd == "run":
        raise SystemExit(run(a.out.resolve(), a.cache.resolve(), set(a.case) if a.case else None, a.reader,
                             set(a.suite) if a.suite else None, not a.no_suites))
    raise SystemExit(main_verify(a.bundle.resolve()))


if __name__ == "__main__":
    main()
