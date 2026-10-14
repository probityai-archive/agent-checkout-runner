"""Each claim's CONTRADICTED and NOT_ESTABLISHED paths, on streams written by hand.

The matrix shows the components behaving; these show the report would say so
if they did not. Without them a broken claim function could pass the matrix by
answering ESTABLISHED for everything the components happen to do right.
"""

import json

from synth import attempt, case_dir, decision, permit

from checkout_runner.handoff import request_digest
from checkout_runner.report import C, E, N, derive

T = 1_791_900_000_000
P = permit()
RID = P["receipt"]["receipt_id"]
OP = {"receipt_id": RID, "idempotency_key": "k1", "request_digest": P["receipt"]["action_ref"],
      "reservation_id": "res_1", "valid_until": "2026-10-13T14:15:00.000Z", "state": "response_ok",
      "provider_ref": "p1"}
EFFECT = {"idempotency_key": "k1", "provider_ref": "p1", "kind": "effect", "request_digest": "x", "at_ms": T + 1000}


def ev(to_state, at, evidence='{"platform_ref":"p1"}', frm="reserved"):
    return {"reservation_id": "res_1", "from_state": frm, "to_state": to_state, "cause": "t", "evidence": evidence,
            "at": at}


def claims(tmp_path, **streams):
    meta = streams.pop("meta", {})
    return {k: v["verdict"] for k, v in derive(case_dir(tmp_path, streams, **meta)).items()}


def test_a_good_case(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], attempts=[attempt(1, P, T + 1000)], operations=[OP],
               effects=[EFFECT], gate_events=[ev("committed", "2026-10-13T14:00:02.000Z")],
               observations=[{"at_ms": T + 3000, "receipt_id": RID, "row": EFFECT}],
               meta={"expected_decisions": [["permit", "permitted"]], "expected_attempts": ["response_ok"]})
    assert c["admission.decisions_verify"] == E
    assert c["admission.expected_decisions"] == E
    assert c["dispatch.expected_attempts"] == E
    assert c["dispatch.provider_calls_bound"] == E
    assert c["effect.at_most_once"] == E
    assert c["budget.held_until_confirmed"] == E
    assert c["outcome.resolved_at_cutoff"] == E


def test_the_gate_disagreeing_with_us_contradicts(tmp_path):
    assert claims(tmp_path, decisions=[decision(P, valid=False)])["admission.decisions_verify"] == C


def test_unexpected_attempts_contradict(tmp_path):
    c = claims(tmp_path, attempts=[attempt(1, P, T)], meta={"expected_attempts": ["refused"]})
    assert c["dispatch.expected_attempts"] == C


def test_an_attempt_after_valid_until_is_unbound(tmp_path):
    late = T + 16 * 60_000
    assert claims(tmp_path, decisions=[decision(P)], attempts=[attempt(1, P, late)])[
        "dispatch.provider_calls_bound"] == C


def test_an_attempt_for_another_request_is_unbound(tmp_path):
    other = request_digest({"x": 1})
    assert claims(tmp_path, decisions=[decision(P)], attempts=[attempt(1, P, T, digest=other)])[
        "dispatch.provider_calls_bound"] == C


def test_an_attempt_without_a_permit_is_unbound(tmp_path):
    assert claims(tmp_path, attempts=[attempt(1, P, T)])["dispatch.provider_calls_bound"] == C


def test_no_provider_call_is_not_established(tmp_path):
    assert claims(tmp_path, decisions=[decision(P)])["dispatch.provider_calls_bound"] == N


def test_a_refusal_that_reached_the_provider_contradicts(tmp_path):
    c = claims(tmp_path, attempts=[attempt(1, P, T, reached=1, result="refused", codes='["request_mismatch"]')],
               meta={"expected_refusal": "request_mismatch"})
    assert c["dispatch.refused_before_provider"] == C


def test_a_refusal_with_another_code_contradicts(tmp_path):
    c = claims(tmp_path, attempts=[attempt(1, P, T, reached=0, result="refused", codes='["permit_expired"]')],
               meta={"expected_refusal": "request_mismatch"})
    assert c["dispatch.refused_before_provider"] == C


def test_a_refusal_with_a_ledger_row_contradicts(tmp_path):
    c = claims(tmp_path, attempts=[attempt(1, P, T, reached=0, result="refused", codes='["request_mismatch"]')],
               effects=[EFFECT], meta={"expected_refusal": "request_mismatch"})
    assert c["dispatch.refused_before_provider"] == C


def test_two_effects_for_one_reservation_contradict(tmp_path):
    op2 = {**OP, "receipt_id": "other", "idempotency_key": "k2"}
    c = claims(tmp_path, operations=[OP, op2], effects=[EFFECT, {**EFFECT, "idempotency_key": "k2", "provider_ref": "p2"}])
    assert c["effect.at_most_once"] == C


def test_an_effect_without_an_operation_contradicts(tmp_path):
    assert claims(tmp_path, effects=[EFFECT])["effect.at_most_once"] == C


def test_a_release_without_confirmation_contradicts(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], gate_events=[ev("released", "2026-10-13T14:00:02.000Z", None)])
    assert c["budget.held_until_confirmed"] == C


def test_a_release_with_an_effect_contradicts(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], operations=[OP], effects=[EFFECT],
               gate_events=[ev("released", "2026-10-13T14:00:02.000Z")])
    assert c["budget.held_until_confirmed"] == C


def test_committed_without_an_observed_effect_contradicts(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], operations=[OP],
               gate_events=[ev("committed", "2026-10-13T14:00:02.000Z")],
               observations=[{"at_ms": T + 3000, "receipt_id": RID, "row": None}])
    assert c["outcome.resolved_at_cutoff"] == C


def test_released_with_an_observed_effect_contradicts(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], operations=[OP],
               gate_events=[ev("released", "2026-10-13T14:00:02.000Z")],
               observations=[{"at_ms": T + 3000, "receipt_id": RID, "row": EFFECT}])
    assert c["outcome.resolved_at_cutoff"] == C


def test_an_observation_after_the_cutoff_does_not_count(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], operations=[OP],
               gate_events=[ev("committed", "2026-10-13T14:00:02.000Z")],
               observations=[{"at_ms": T + 3_600_000, "receipt_id": RID, "row": EFFECT}])
    assert c["outcome.resolved_at_cutoff"] == N


def test_a_transition_after_the_cutoff_does_not_count(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], operations=[OP],
               gate_events=[ev("committed", "2026-10-13T15:00:00.000Z")],
               observations=[{"at_ms": T + 3000, "receipt_id": RID, "row": EFFECT}])
    assert c["outcome.resolved_at_cutoff"] == N


def test_unresolved_at_cutoff_is_not_established(tmp_path):
    c = claims(tmp_path, decisions=[decision(P)], operations=[OP],
               gate_events=[ev("unresolved", "2026-10-13T14:00:02.000Z", None)])
    assert c["outcome.resolved_at_cutoff"] == N


def test_a_failing_component_verdict_check_contradicts(tmp_path):
    c = claims(tmp_path, component_checks=[{"check": "verdict_problems", "at_ms": T,
                                            "result": [{"code": "verdict_invalid", "message": "x"}]}])
    assert c["evidence.offline_verdict_check"] == C
    assert c["evidence.verdict_permits_through_gate"] == C


def test_report_reads_only_files(tmp_path):
    d = case_dir(tmp_path, {"decisions": [decision(P)]})
    first = derive(d)
    assert json.dumps(first, sort_keys=True) == json.dumps(derive(d), sort_keys=True)


def test_an_inadmissible_verdict_request_contradicts(tmp_path):
    probs = ["merchant_id is not a valid scope segment"]
    c = claims(tmp_path, component_checks=[
        {"check": "verdict_problems", "at_ms": T, "result": []},
        {"check": "request_problems", "at_ms": T, "result": probs},
        {"check": "request_problems_segment_control", "at_ms": T, "result": []}])
    assert c["evidence.verdict_request_admissible"] == C
    assert c["evidence.offline_verdict_check"] == E


def test_an_admissible_verdict_request_is_established(tmp_path):
    c = claims(tmp_path, component_checks=[
        {"check": "verdict_problems", "at_ms": T, "result": []},
        {"check": "request_problems", "at_ms": T, "result": []}])
    assert c["evidence.verdict_request_admissible"] == E


def test_a_verdict_without_the_request_check_is_not_established(tmp_path):
    c = claims(tmp_path, component_checks=[{"check": "verdict_problems", "at_ms": T, "result": []}])
    assert c["evidence.verdict_request_admissible"] == N
