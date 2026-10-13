"""The whole matrix against the pinned components, then the sealed bundle re-checked.

Needs git, Node 24 and npm on PATH: the components are fetched at their pinned
commits into the runner's cache on first use.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from checkout_runner import cases
from checkout_runner.runner import run, verify_bundle

CACHE = Path(os.environ.get("RUNNER_CACHE", Path.home() / ".cache" / "agent-checkout-runner"))


@pytest.fixture(scope="session")
def bundle(tmp_path_factory):
    out = tmp_path_factory.mktemp("run") / "bundle"
    rc = run(out, CACHE)
    return out, rc


def test_every_case_matches_its_declared_claims(bundle):
    out, rc = bundle
    report = json.loads((out / "report.json").read_text())
    bad = [(c["pin"], c["case"], c["mismatched"]) for c in report["matrix"]["cases"] if not c["matches_expected"]]
    assert bad == []
    assert rc == 0


def test_every_case_ran_on_every_pin_it_names(bundle):
    out, _ = bundle
    report = json.loads((out / "report.json").read_text())
    ran = {(c["pin"], c["case"]) for c in report["matrix"]["cases"]}
    want = {(p, c.name) for c in cases.matrix() for p in c.pins}
    assert ran == want


def test_the_sealed_bundle_verifies(bundle):
    out, _ = bundle
    assert verify_bundle(out) == []


def test_an_edited_stream_is_caught(bundle, tmp_path):
    out, _ = bundle
    copy = tmp_path / "bundle"
    shutil.copytree(out, copy)
    attempts = copy / "cases" / "main" / "changed_amount" / "attempts.jsonl"
    rows = [json.loads(line) for line in attempts.read_text().splitlines()]
    rows[0]["reached_provider"] = 1
    attempts.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    problems = verify_bundle(copy)
    assert any("sum mismatch" in p for p in problems)
    assert any("do not re-derive" in p for p in problems)


def test_streams_stay_separate(bundle):
    out, _ = bundle
    d = out / "cases" / "main" / "lost_response_retry"
    names = {p.name for p in d.iterdir() if p.suffix == ".jsonl"}
    assert {"decisions.jsonl", "attempts.jsonl", "effects.jsonl", "observations.jsonl"} <= names
    decisions = [json.loads(line) for line in (d / "decisions.jsonl").read_text().splitlines()]
    assert {r["kind"] for r in decisions} <= {"decision", "outcome", "reconcile", "reservation_final"}
