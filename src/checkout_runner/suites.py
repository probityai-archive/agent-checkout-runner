"""Each component's own published cases, run at its pin inside the same sealed bundle.

The matrix drives one component (the checkout gate) through the runner's executor
and simulator. The other components in the refund workflow publish their own
case suites: PIC's signed refund approval cases, the APS exact-refund admission
candidate, the C1 to C3 refund predicate readers, and the refund-handoff/0.1
conformance corpus. This stage fetches each at the commit in
``components.lock.json``, runs the commands its README documents, and keeps
every step's command, stdout, stderr and exit status under
``components/<name>/``.

Each suite's verdict comes from those retained exit files alone:

* ESTABLISHED: every step exited 0;
* CONTRADICTED: setup finished and a cases step exited non-zero;
* NOT_ESTABLISHED: the checkout or a setup step failed, so the cases never ran.

Suites run side by side on their own fixtures. Passing them together says each
component's published cases pass at its pin on this machine; it does not say
the components were connected to each other.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

E, C, N = "ESTABLISHED", "CONTRADICTED", "NOT_ESTABLISHED"
KINDS = ("setup", "cases")


def checkout(cache: Path, repo: str, commit: str, fetch_ref: str) -> Path:
    """A full-history partial clone at exactly ``commit``; refuses anything else."""
    dest = cache / "suites" / commit
    if not (dest / ".git").exists():
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", repo, str(dest)], check=True,
                       capture_output=True)
        have = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=dest, capture_output=True)
        if have.returncode != 0:
            subprocess.run(["git", "fetch", "-q", "origin", fetch_ref], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-q", "--force", commit], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "clean", "-q", "-ffdx"], cwd=dest, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, check=True, capture_output=True,
                          text=True).stdout.strip()
    if head != commit:
        raise RuntimeError(f"checked out {head}, pinned {commit}")
    return dest


def _expand(arg: str, results: Path, scratch: Path) -> str:
    """``{python}`` is this interpreter, ``{results}`` the suite's retained directory, ``{scratch}`` a
    throwaway directory outside the bundle (virtual environments go there, not into the seal)."""
    return arg.replace("{python}", sys.executable).replace("{results}", str(results)).replace("{scratch}", str(scratch))


def run_suite(name: str, spec: dict[str, Any], cache: Path, out: Path) -> dict[str, Any]:
    """Run one suite's steps in order, stopping at the first failure, and retain every step."""
    sdir = out / "components" / name
    sdir.mkdir(parents=True, exist_ok=True)
    scratch = cache / "scratch" / name
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    steps: list[dict[str, Any]] = []
    try:
        root = checkout(cache, spec["repo"], spec["commit"], spec.get("fetch", "HEAD"))
        steps.append({"n": 0, "kind": "setup", "argv": ["git", "checkout", spec["commit"]], "exit": 0})
    except Exception as e:  # noqa: BLE001 - a checkout that fails is recorded, never skipped silently
        steps.append({"n": 0, "kind": "setup", "argv": ["git", "checkout", spec["commit"]], "exit": 1,
                      "error": f"{type(e).__name__}: {e}"})
        return _finish(name, spec, sdir, steps)
    for n, step in enumerate(spec["steps"], start=1):
        if step["kind"] not in KINDS:
            raise ValueError(f"{name} step {n}: kind must be one of {KINDS}")
        cwd = root / step.get("cwd", ".")
        argv = [_expand(a, sdir, scratch) for a in step["argv"]]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **step.get("env", {}))
        try:
            p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                               timeout=step.get("timeout", 1800))
            code, stdout, stderr = p.returncode, p.stdout, p.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            code, stdout, stderr = 127, "", f"{type(e).__name__}: {e}"
        (sdir / f"{n:02d}.stdout").write_text(stdout, encoding="utf-8")
        (sdir / f"{n:02d}.stderr").write_text(stderr, encoding="utf-8")
        steps.append({"n": n, "kind": step["kind"], "cwd": step.get("cwd", "."), "argv": step["argv"],
                      "exit": code})
        if code != 0:
            break
    for rel in spec.get("retain", []):
        src = root / rel
        if src.is_file():
            shutil.copyfile(src, sdir / Path(rel).name)
    tree = spec.get("retain_tree")
    if tree:
        src = Path(_expand(tree["from"], sdir, scratch))
        if src.is_dir():
            skip = set(tree.get("exclude", []))
            shutil.copytree(src, sdir / tree["to"], symlinks=True,
                            ignore=lambda d, names: [n for n in names if Path(d) == src and n in skip])
    return _finish(name, spec, sdir, steps)


def _finish(name: str, spec: dict[str, Any], sdir: Path, steps: list[dict[str, Any]]) -> dict[str, Any]:
    record = {"suite": name, "repo": spec["repo"], "commit": spec["commit"], "what": spec["what"],
              "planned_steps": len(spec["steps"]), "steps": steps}
    (sdir / "steps.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**record, **verdict(record)}


def verdict(record: dict[str, Any]) -> dict[str, str]:
    """The suite's claim, from its retained step record and nothing else."""
    steps = record["steps"]
    failed = [s for s in steps if s["exit"] != 0]
    if not failed:
        if len(steps) != record["planned_steps"] + 1:
            return {"verdict": N, "why": f"{len(steps) - 1} of {record['planned_steps']} steps ran"}
        cases = sum(1 for s in steps if s["kind"] == "cases")
        if cases == 0:
            return {"verdict": N, "why": "no cases step ran"}
        return {"verdict": E, "why": f"{cases} cases steps exited 0 at {record['commit'][:12]}"}
    first = failed[0]
    if first["kind"] == "setup":
        return {"verdict": N, "why": f"setup step {first['n']} exited {first['exit']}; the cases did not run"}
    return {"verdict": C, "why": f"cases step {first['n']} ({' '.join(first['argv'])}) exited {first['exit']}"}


def run_all(suites: dict[str, Any], cache: Path, out: Path, only: set[str] | None = None) -> list[dict[str, Any]]:
    return [run_suite(name, spec, cache, out) for name, spec in suites.items() if not only or name in only]
