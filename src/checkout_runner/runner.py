"""Fetch the pinned components, run the matrix, seal the bundle, hand it to a reader."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from agent_evidence_vectors.run_vectors import ed25519_public_key, ed25519_sign, ed25519_verify, jcs_dumps, pae

from . import cases as cases_mod
from .gate_client import GateProcess
from .report import CLAIMS, RUN_LIMITS, derive
from .world import T0, World

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "components.lock.json"
REPORT_TYPE = "application/vnd.agent-checkout-runner.report+json"
#: Test-only signing seed for the report envelope. It shows the bundle was not
#: edited after sealing; it carries no authority of any kind.
REPORT_SEED = hashlib.sha256(b"agent-checkout-runner/test-key/report/v1").digest()


def components() -> dict[str, Any]:
    return json.loads(LOCK.read_text(encoding="utf-8"))["components"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fetch(cache: Path) -> dict[str, Path]:
    """Check out every pinned component at its exact commit and install its locked dependencies."""
    out = {}
    for name, spec in components().items():
        dest = cache / spec["commit"]
        if not (dest / "node_modules").exists():
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            _git(dest, "init", "-q")
            _git(dest, "remote", "add", "origin", spec["repo"])
            try:
                _git(dest, "fetch", "-q", "--depth", "1", "origin", spec["commit"])
            except subprocess.CalledProcessError:
                _git(dest, "fetch", "-q", "origin", spec["fetch"])
            _git(dest, "checkout", "-q", spec["commit"])
            subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=dest, check=True,
                           capture_output=True)
        head = _git(dest, "rev-parse", "HEAD")
        if head != spec["commit"]:
            raise SystemExit(f"{name}: checked out {head}, pinned {spec['commit']}")
        out[name.split("@", 1)[1]] = dest
    return out


def _reviewer_pub(gate_dir: Path) -> str:
    g = GateProcess(gate_dir)
    try:
        return g.call("sign_test_verdict", label=cases_mod.REVIEWER, verdict="approve", at=T0,
                      request=cases_mod.request(0))["pubkey"]
    finally:
        g.close()


def run_matrix(out: Path, gates: dict[str, Path], only: set[str] | None = None) -> dict[str, Any]:
    results = []
    for pin, gate_dir in gates.items():
        reviewer = None
        for case in cases_mod.matrix():
            if pin not in case.pins or (only and case.name not in only):
                continue
            checks: list[dict[str, Any]] = []
            if case.evidence_checks:
                reviewer = reviewer or _reviewer_pub(gate_dir)
                checks = case.evidence_checks(reviewer)
            d = cases_mod.case_dir(out, pin, case)
            w = World(d, gate_dir, evidence_checks=checks, parties=case.parties)
            try:
                case.run(w)
            finally:
                w.finish()
            cases_mod.write_case_meta(case, w, pin)
            derived = derive(d)
            mismatched = {c: {"derived": derived[c]["verdict"], "expected": case.expect.get(c, "NOT_ESTABLISHED")}
                          for c in CLAIMS if derived[c]["verdict"] != case.expect.get(c, "NOT_ESTABLISHED")}
            results.append({"pin": pin, "case": case.name, "summary": case.summary, "claims": derived,
                            "matches_expected": not mismatched, "mismatched": mismatched})
    return {"cases": results}


def provenance(gates: dict[str, Path]) -> dict[str, Any]:
    node = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    comps = components()
    return {
        "runner": {"name": "agent-checkout-runner", "version": metadata.version("agent-checkout-runner")},
        "agent_evidence_vectors": metadata.version("agent-evidence-vectors"),
        "python": platform.python_version(), "node": node, "platform": platform.platform(),
        "components": {k: {"repo": v["repo"], "commit": v["commit"], "role": v["role"]} for k, v in comps.items()},
        "operated_by": os.environ.get("RUNNER_OPERATOR", "the runner's author"),
        "classification": "runner-operated fixture run; not an independent run and not a host adoption",
    }


def seal(out: Path, report: dict[str, Any]) -> None:
    body = jcs_dumps(report)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sig = ed25519_sign(REPORT_SEED, pae(REPORT_TYPE, body))
    pub = ed25519_public_key(REPORT_SEED)
    env = {"payloadType": REPORT_TYPE, "payload": base64.b64encode(body).decode(),
           "signatures": [{"keyid": hashlib.sha256(pub).hexdigest(), "sig": base64.b64encode(sig).decode()}]}
    (out / "report.dsse.json").write_text(json.dumps(env, indent=2) + "\n", encoding="utf-8")
    sums = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS" and "reader" not in p.relative_to(out).parts \
                and not p.name.endswith(("-wal", "-shm")):
            sums.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out).as_posix()}")
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")


def verify_bundle(out: Path) -> list[str]:
    """Re-check a sealed bundle: sums, envelope, and every claim re-derived from the streams."""
    problems = []
    for line in (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        if hashlib.sha256((out / rel).read_bytes()).hexdigest() != digest:
            problems.append(f"sum mismatch: {rel}")
    env = json.loads((out / "report.dsse.json").read_text(encoding="utf-8"))
    body = base64.b64decode(env["payload"])
    if not ed25519_verify(ed25519_public_key(REPORT_SEED), pae(env["payloadType"], body),
                          base64.b64decode(env["signatures"][0]["sig"])):
        problems.append("report envelope signature does not verify")
    report = json.loads(body)
    if jcs_dumps(report) != jcs_dumps(json.loads((out / "report.json").read_text(encoding="utf-8"))):
        problems.append("report.json differs from the signed payload")
    for c in report["matrix"]["cases"]:
        again = derive(out / "cases" / c["pin"] / c["case"])
        if jcs_dumps(again) != jcs_dumps(c["claims"]):
            problems.append(f"{c['pin']}/{c['case']}: claims do not re-derive from the retained streams")
    return problems


def run_reader(out: Path, command: str) -> dict[str, Any]:
    """Hand the sealed bundle to an external reader. Its result is kept apart and decides nothing here."""
    rdir = out / "reader"
    rdir.mkdir(exist_ok=True)
    argv = [a.replace("{bundle}", str(out)) for a in shlex.split(command)]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    (rdir / "stdout.txt").write_text(p.stdout, encoding="utf-8")
    (rdir / "stderr.txt").write_text(p.stderr, encoding="utf-8")
    result = {"argv": argv, "exit": p.returncode}
    (rdir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def run(out: Path, cache: Path, only: set[str] | None = None, reader: str | None = None) -> int:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    gates = fetch(cache)
    matrix = run_matrix(out, gates, only)
    passed = all(c["matches_expected"] for c in matrix["cases"])
    report = {"format": "agent-checkout-runner/report-v1", "provenance": provenance(gates), "matrix": matrix,
              "run_limits": {k: {"verdict": "NOT_ESTABLISHED", "why": v} for k, v in RUN_LIMITS.items()},
              "all_cases_match_expected": passed}
    seal(out, report)
    for c in matrix["cases"]:
        mark = "ok  " if c["matches_expected"] else "FAIL"
        line = " ".join(f"{k.split('.', 1)[1]}={v['verdict'][0]}" for k, v in c["claims"].items())
        print(f"{mark} {c['pin']:4} {c['case']:28} {line}")
        for claim, m in c["mismatched"].items():
            print(f"       {claim}: derived {m['derived']}, expected {m['expected']}: {c['claims'][claim]['why']}")
    n = len(matrix["cases"])
    print(f"{sum(c['matches_expected'] for c in matrix['cases'])}/{n} cases match their declared claims")
    if reader:
        r = run_reader(out, reader)
        print(f"reader exit {r['exit']} (kept in reader/, not part of the runner's verdicts)")
    return 0 if passed else 1


def main_verify(out: Path) -> int:
    problems = verify_bundle(out)
    for p in problems:
        print(p, file=sys.stderr)
    print("bundle verifies" if not problems else f"{len(problems)} problems")
    return 0 if not problems else 1
