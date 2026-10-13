"""Component suite verdicts, against a local repository whose steps pass or fail on purpose."""

import json
import subprocess

import pytest

from checkout_runner.suites import C, E, N, run_suite, verdict


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("component")
    (root / "ok.py").write_text("print('cases pass')\n")
    (root / "bad.py").write_text("import sys\nprint('one case fails')\nsys.exit(3)\n")
    (root / "result.json").write_text('{"cases": 2}\n')
    for argv in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c"]):
        subprocess.run(["git", *argv], cwd=root, check=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                            text=True).stdout.strip()
    return root, commit


def spec(repo, steps, retain=()):
    root, commit = repo
    return {"what": "test suite", "repo": str(root), "commit": commit, "fetch": "HEAD", "steps": steps,
            "retain": list(retain)}


OK = {"kind": "cases", "argv": ["{python}", "ok.py"]}
BAD = {"kind": "cases", "argv": ["{python}", "bad.py"]}
BAD_SETUP = {"kind": "setup", "argv": ["{python}", "bad.py"]}


def test_every_step_passing_is_established(repo, tmp_path):
    r = run_suite("s", spec(repo, [{"kind": "setup", "argv": ["{python}", "ok.py"]}, OK], ["result.json"]),
                  tmp_path / "cache", tmp_path / "out")
    assert r["verdict"] == E, r["why"]
    sdir = tmp_path / "out" / "components" / "s"
    assert (sdir / "02.stdout").read_text() == "cases pass\n"
    assert json.loads((sdir / "result.json").read_text()) == {"cases": 2}


def test_a_failing_cases_step_contradicts(repo, tmp_path):
    r = run_suite("s", spec(repo, [OK, BAD, OK]), tmp_path / "cache", tmp_path / "out")
    assert r["verdict"] == C
    assert [s["exit"] for s in r["steps"]] == [0, 0, 3]


def test_a_failing_setup_step_is_not_established(repo, tmp_path):
    r = run_suite("s", spec(repo, [BAD_SETUP, OK]), tmp_path / "cache", tmp_path / "out")
    assert r["verdict"] == N
    assert len(r["steps"]) == 2


def test_an_unknown_commit_is_not_established(repo, tmp_path):
    r = run_suite("s", {**spec(repo, [OK]), "commit": "0" * 40}, tmp_path / "cache", tmp_path / "out")
    assert r["verdict"] == N
    assert r["steps"][0]["exit"] != 0


def test_a_missing_program_contradicts_rather_than_passing(repo, tmp_path):
    r = run_suite("s", spec(repo, [{"kind": "cases", "argv": ["no-such-program-acr"]}]), tmp_path / "cache",
                  tmp_path / "out")
    assert r["verdict"] == C
    assert r["steps"][-1]["exit"] == 127


def test_a_suite_with_no_cases_step_is_not_established(repo, tmp_path):
    r = run_suite("s", spec(repo, [{"kind": "setup", "argv": ["{python}", "ok.py"]}]), tmp_path / "cache",
                  tmp_path / "out")
    assert r["verdict"] == N


def test_a_record_missing_a_step_is_not_established():
    record = {"commit": "a" * 40, "planned_steps": 2,
              "steps": [{"n": 0, "kind": "setup", "argv": [], "exit": 0}, {"n": 1, "kind": "cases", "argv": [], "exit": 0}]}
    assert verdict(record)["verdict"] == N


def test_the_verdict_reads_only_the_record(repo, tmp_path):
    r = run_suite("s", spec(repo, [OK]), tmp_path / "cache", tmp_path / "out")
    record = json.loads((tmp_path / "out" / "components" / "s" / "steps.json").read_text())
    assert verdict(record)["verdict"] == r["verdict"] == E
    record["steps"][-1]["exit"] = 1
    assert verdict(record)["verdict"] == C


def test_the_pinned_commit_is_what_runs(repo, tmp_path):
    root, commit = repo
    (root / "ok.py").write_text("import sys\nsys.exit(9)\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "break"], cwd=root,
                   check=True)
    try:
        r = run_suite("s", spec(repo, [OK]), tmp_path / "cache", tmp_path / "out")
        assert r["verdict"] == E
    finally:
        subprocess.run(["git", "reset", "-q", "--hard", commit], cwd=root, check=True)


def test_verify_catches_an_edited_suite_record(repo, tmp_path):
    from checkout_runner.runner import seal, verify_bundle

    out = tmp_path / "bundle"
    r = run_suite("s", spec(repo, [OK]), tmp_path / "cache", out)
    seal(out, {"matrix": {"cases": []}, "component_suites": [r]})
    assert verify_bundle(out) == []
    steps = out / "components" / "s" / "steps.json"
    record = json.loads(steps.read_text())
    record["steps"][-1]["exit"] = 1
    steps.write_text(json.dumps(record))
    problems = verify_bundle(out)
    assert any("sum mismatch" in p for p in problems)
    assert "suite s: verdict does not re-derive from its retained steps" in problems


def test_a_retained_tree_leaves_out_the_excluded_top_level_names(repo, tmp_path):
    make = ("import pathlib, sys; r = pathlib.Path(sys.argv[1]); (r / '.venv' / 'lib').mkdir(parents=True); "
            "(r / '.venv' / 'lib' / 'big').write_text('x'); (r / 'keep').mkdir(); (r / 'keep' / '.venv').write_text('k'); "
            "(r / 'summary.json').write_text('{}')")
    s = {**spec(repo, [{"kind": "cases", "argv": ["{python}", "-c", make, "{scratch}/res"]}]),
         "retain_tree": {"from": "{scratch}/res", "to": "res", "exclude": [".venv"]}}
    run_suite("s", s, tmp_path / "cache", tmp_path / "out")
    kept = tmp_path / "out" / "components" / "s" / "res"
    assert sorted(p.relative_to(kept).as_posix() for p in kept.rglob("*")) == ["keep", "keep/.venv", "summary.json"]
