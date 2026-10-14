"""Remove one safeguard at a time; the test suite must fail for each.

Each mutant copies src/ to a scratch directory, applies one textual change
(which must match exactly once), and runs the full suite with that copy first
on PYTHONPATH, so the executor and simulator child processes load it too. A
mutant whose suite still passes is a survivor, and the check exits 1.

    python scripts/mutation_check.py --jobs 4 --out mutation-results.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = "src/checkout_runner"

MUTANTS = [
    ("handoff: skip the signature check", "handoff.py",
     "    out.codes.extend(_signature_codes(receipt, trust))\n", "\n"),
    ("handoff: skip the decision_ref binding", "handoff.py",
     'if decision_ref_of(action_ref, evidence) != receipt.get("decision_ref"):', "if False:"),
    ("handoff: skip the receipt_id recompute", "handoff.py",
     'if receipt_id_of(receipt) != receipt.get("receipt_id"):', "if False:"),
    ("handoff: accept a refusal as a permit", "handoff.py",
     '    if out.verdict != "permit":\n        out.codes.append("not_a_permit")\n    elif', "    if"),
    ("executor: skip the request binding", "executor.py",
     "if not codes and digest != check.action_ref:", "if False:"),
    ("executor: skip the expiry check", "executor.py",
     "if now >= iso_ms(check.valid_until):", "if False:"),
    ("executor: re-dispatch a finished operation", "executor.py",
     "if op is not None and op[0] in FINAL:", "if False:"),
    ("executor: idempotency key per attempt", "executor.py",
     "key = idempotency_key(trust.gate_id, check.receipt_id)",
     "key = idempotency_key(trust.gate_id, check.receipt_id + str(now))"),
    ("executor: record a lost response as success", "executor.py",
     'return _attempt(db, now, "response_lost", [], check.receipt_id, key, digest, reached=True)',
     'return _attempt(db, now, "response_ok", [], check.receipt_id, key, digest, reached=True)'),
    ("executor: no durable operation before the call", "executor.py",
     "    if op is None:\n        db.execute(", "    if False:\n        db.execute("),
    ("simulator: no idempotency", "simulator.py",
     'key = msg["idempotency_key"]', 'key = msg["idempotency_key"] + str(msg.get("now"))'),
    ("report: count observations after the cutoff", "report.py",
     'if ob["at_ms"] <= cutoff:', "if True:"),
    ("report: count gate transitions after the cutoff", "report.py",
     'if ev["reservation_id"] == rid and iso_ms(ev["at"]) <= cutoff:', 'if ev["reservation_id"] == rid:'),
    ("report: accept a release without confirmation", "report.py",
     'and (ev["evidence"] is None or ev["reservation_id"] in effect_res)]',
     'and ev["reservation_id"] in effect_res]'),
    ("report: ignore valid_until when binding attempts", "report.py",
     'and a["at_ms"] < iso_ms(rec["receipt"]["result"]["valid_until"]))', ")"),
    ("report: ignore the gate's own verifier", "report.py",
     'if _integrity_codes(d["record"], trust) or not d["gate_verify"]["valid"]]',
     'if _integrity_codes(d["record"], trust)]'),
    ("report: count duplicate effects as one", "report.py",
     "dup = {r: n for r, n in per_res.items() if n > 1}", "dup = {}"),
    ("report: a refused verdict request counts as admissible", "report.py",
     'put("evidence.verdict_request_admissible", E if probs == [] else C,',
     'put("evidence.verdict_request_admissible", E,'),
    ("cases: the segment control keeps the colon", "cases.py",
     'control = {**req, "merchant_id": req["merchant_id"].replace(":", "-")}', "control = req"),
    ("suites: a failed setup counts as a contradiction", "suites.py",
     'if first["kind"] == "setup":', "if False:"),
    ("suites: a record missing steps still passes", "suites.py",
     'if len(steps) != record["planned_steps"] + 1:', "if False:"),
    ("suites: run whatever the clone's HEAD is", "suites.py",
     'subprocess.run(["git", "checkout", "-q", "--force", commit]', 'subprocess.run(["git", "checkout", "-q", "--force", "HEAD"]'),
    ("suites: a program that cannot start counts as passing", "suites.py",
     "code, stdout, stderr = 127,", "code, stdout, stderr = 0,"),
    ("suites: a suite with no cases step passes", "suites.py",
     "        if cases == 0:\n", "        if False:\n"),
    ("runner: a host checkout is reported as the pin", "runner.py",
     '"components": {"aps-checkout-gate@main": host_gate(gate_dir)} if gate_dir else pinned,',
     '"components": pinned,'),
    ("runner: verify skips the suite verdicts", "runner.py",
     'if suites_mod.verdict(record)["verdict"] != s["verdict"]:', "if False:"),
]

TESTS = ["tests/test_handoff.py", "tests/test_report.py", "tests/test_suites.py", "tests/test_matrix.py"]


def run_suite(src_parent: Path) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(src_parent), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-3:])
    return p.returncode, tail


def one(mutant: tuple[str, str, str, str]) -> dict[str, object]:
    name, file, old, new = mutant
    with tempfile.TemporaryDirectory(prefix="acr-mutant-") as tmp:
        dst = Path(tmp) / "src"
        shutil.copytree(ROOT / "src", dst)
        target = dst / "checkout_runner" / file
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return {"mutant": name, "status": "not_applied", "detail": f"pattern found {text.count(old)} times"}
        target.write_text(text.replace(old, new), encoding="utf-8")
        rc, tail = run_suite(dst)
    return {"mutant": name, "status": "killed" if rc != 0 else "SURVIVED", "exit": rc, "detail": tail}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    rc, tail = run_suite(ROOT / "src")
    print(f"baseline: exit {rc}: {tail.splitlines()[-1] if tail else ''}", flush=True)
    if rc != 0:
        raise SystemExit("the unmutated suite fails; nothing to measure")
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        results = list(pool.map(one, MUTANTS))
    for r in results:
        print(f"{r['status']:12} {r['mutant']}", flush=True)
    killed = sum(r["status"] == "killed" for r in results)
    print(f"{killed}/{len(results)} mutants killed")
    if a.out:
        a.out.write_text(json.dumps({"baseline_exit": rc, "results": results}, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if killed == len(results) else 1)


if __name__ == "__main__":
    main()
