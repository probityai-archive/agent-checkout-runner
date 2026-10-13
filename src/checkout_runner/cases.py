"""The case matrix. Each case drives the real components and declares what the
report must derive from the retained streams afterwards.

E, C and N stand for ESTABLISHED, CONTRADICTED and NOT_ESTABLISHED. A claim a
case does not list must not be derivable for it; the report includes every
claim for every case, and the run passes only when every derived verdict equals
the declared one.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .world import AGENT, MINUTE, OTHER_AGENT, T0, World, request

E, C, N = "ESTABLISHED", "CONTRADICTED", "NOT_ESTABLISHED"

PUBLISHED_VERDICT = "invinoveritas-verdict-b2cd036a.json"


@dataclass
class Case:
    name: str
    summary: str
    run: Callable[[World], None]
    expect: dict[str, str]
    pins: tuple[str, ...] = ("main", "pr1")
    cutoff_ms: int = T0 + 10 * MINUTE
    refusal: str | None = None
    decisions: list[list[str]] = field(default_factory=list)
    #: The executor's attempt results, in order.
    attempts: list[str] = field(default_factory=list)
    #: Called with the synthetic reviewer's public key before the gate opens.
    evidence_checks: Callable[[str], list[dict[str, Any]]] | None = None
    parties: list[dict[str, Any]] | None = None


def _permit(w: World, n: int = 1, **over: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    req = request(n, **over)
    r = w.decide(req, w.grant(), T0)
    return req, r


def _settle_ok(w: World, r: dict[str, Any], a: dict[str, Any] | None, at: int) -> None:
    w.outcome(r["reservation_id"], "placed", at, a["provider_ref"] if a else None)
    w.observe(r["record"]["receipt"]["receipt_id"], at + MINUTE)


def _refused_and_released(w: World, r: dict[str, Any], at: int) -> None:
    w.outcome(r["reservation_id"], "failed", at, "executor-refused-before-dispatch")


# -- happy path ---------------------------------------------------------------

def match_dispatch(w: World) -> None:
    req, r = _permit(w)
    a = w.dispatch(r["record"], req, T0 + 1000)
    _settle_ok(w, r, a, T0 + 2000)


# -- changed after approval ------------------------------------------------------

def _changed(field_name: str, value: Any) -> Callable[[World], None]:
    def run(w: World) -> None:
        req, r = _permit(w)
        changed = copy.deepcopy(req)
        changed[field_name] = value
        if field_name == "amount_minor":
            changed["items"][0]["unit_price_minor"] = value
        w.dispatch(r["record"], changed, T0 + 1000)
        _refused_and_released(w, r, T0 + 2000)
    return run


def changed_items(w: World) -> None:
    req, r = _permit(w)
    changed = copy.deepcopy(req)
    changed["items"] = [{"sku": "TEE-WHT-L", "quantity": 1, "unit_price_minor": 3000}]
    w.dispatch(r["record"], changed, T0 + 1000)
    _refused_and_released(w, r, T0 + 2000)


# -- handoff integrity -----------------------------------------------------------

def forged_signature(w: World) -> None:
    req, r = _permit(w)
    forged = copy.deepcopy(r["record"])
    sig = forged["receipt"]["signatures"][0]["value"]
    forged["receipt"]["signatures"][0]["value"] = ("0" if sig[0] != "0" else "1") + sig[1:]
    w.dispatch(forged, req, T0 + 1000)
    _refused_and_released(w, r, T0 + 2000)


def tampered_evidence(w: World) -> None:
    req, r = _permit(w)
    tampered = copy.deepcopy(r["record"])
    tampered["evidence"]["decision_context"]["reservation_id"] = "res_" + "0" * 32
    w.dispatch(tampered, req, T0 + 1000)
    _refused_and_released(w, r, T0 + 2000)


def untrusted_signer(w: World) -> None:
    """A permit from a gate that claims the trusted gate id but signs with another key."""
    req, r = _permit(w)
    rogue_dir = w.dir / "rogue"
    rogue = World(rogue_dir, w.gate_dir, parties=[
        {**p, "seed": p["seed"] + "/rogue"} if p["role"] == "gate" else p for p in w.party_specs])
    rr = rogue.decide(req, rogue.grant(), T0)
    rogue.finish()
    w.dispatch(rr["record"], req, T0 + 1000)
    _refused_and_released(w, r, T0 + 2000)


def refusal_as_handoff(w: World) -> None:
    req = request(1, amount_minor=20_000)
    r = w.decide(req, w.grant(), T0)
    w.dispatch(r["record"], req, T0 + 1000)


# -- replay -------------------------------------------------------------------------

def replay_after_success(w: World) -> None:
    req, r = _permit(w)
    a = w.dispatch(r["record"], req, T0 + 1000)
    w.dispatch(r["record"], req, T0 + 3000)
    _settle_ok(w, r, a, T0 + 4000)


def replay_after_expiry(w: World) -> None:
    req, r = _permit(w)
    a = w.dispatch(r["record"], req, T0 + 1000)
    _settle_ok(w, r, a, T0 + 2000)
    w.dispatch(r["record"], req, T0 + 16 * MINUTE)


def gate_replay(w: World) -> None:
    req, r = _permit(w)
    again = w.decide(req, w.grant(), T0 + 500)
    a = w.dispatch(r["record"], req, T0 + 1000)
    w.dispatch(again["record"], req, T0 + 2000)
    _settle_ok(w, r, a, T0 + 3000)


# -- expiry ----------------------------------------------------------------------------

def expired_permit(w: World) -> None:
    req, r = _permit(w)
    w.dispatch(r["record"], req, T0 + 15 * MINUTE + 1)
    _refused_and_released(w, r, T0 + 15 * MINUTE + 2)


# -- crash and lost response -----------------------------------------------------------

def executor_crash_before_call(w: World) -> None:
    req, r = _permit(w)
    w.dispatch(r["record"], req, T0 + 1000, crash_at="after_pending")
    a = w.dispatch(r["record"], req, T0 + 5000)
    _settle_ok(w, r, a, T0 + 6000)


def provider_crash_before_commit(w: World) -> None:
    req, r = _permit(w)
    w.dispatch(r["record"], req, T0 + 1000, fault="crash_before_commit")
    w.outcome(r["reservation_id"], "timeout", T0 + 2000)
    a = w.dispatch(r["record"], req, T0 + 3000)
    rid = r["record"]["receipt"]["receipt_id"]
    row = w.observe(rid, T0 + 4000)
    w.reconcile(r["reservation_id"], "placed", (row or {}).get("provider_ref") or (a or {}).get("provider_ref"), T0 + 5000)


def lost_response_retry(w: World) -> None:
    req, r = _permit(w)
    w.dispatch(r["record"], req, T0 + 1000, fault="lose_response")
    w.outcome(r["reservation_id"], "timeout", T0 + 2000)
    a = w.dispatch(r["record"], req, T0 + 3000)
    w.observe(r["record"]["receipt"]["receipt_id"], T0 + 4000)
    w.reconcile(r["reservation_id"], "placed", a["provider_ref"] if a else "missing", T0 + 5000)


def lost_response_reconciled(w: World) -> None:
    req, r = _permit(w)
    w.dispatch(r["record"], req, T0 + 1000, fault="lose_response")
    w.outcome(r["reservation_id"], "timeout", T0 + 2000)
    row = w.observe(r["record"]["receipt"]["receipt_id"], T0 + 2 * MINUTE)
    if row and row["kind"] == "effect":
        w.reconcile(r["reservation_id"], "placed", row["provider_ref"], T0 + 3 * MINUTE)


def lost_response_after_cutoff(w: World) -> None:
    req, r = _permit(w)
    w.dispatch(r["record"], req, T0 + 1000, fault="lose_response")
    w.outcome(r["reservation_id"], "timeout", T0 + 2000)
    row = w.observe(r["record"]["receipt"]["receipt_id"], T0 + 20 * MINUTE)
    if row and row["kind"] == "effect":
        w.reconcile(r["reservation_id"], "placed", row["provider_ref"], T0 + 21 * MINUTE)


def crash_then_expired_retry(w: World) -> None:
    req, r = _permit(w)
    w.dispatch(r["record"], req, T0 + 1000, fault="crash_before_commit")
    w.outcome(r["reservation_id"], "timeout", T0 + 2000)
    w.dispatch(r["record"], req, T0 + 16 * MINUTE)
    row = w.observe(r["record"]["receipt"]["receipt_id"], T0 + 16 * MINUTE + 1000)
    if row is None:
        w.reconcile(r["reservation_id"], "failed", "simulator-ledger-has-no-row", T0 + 16 * MINUTE + 2000)


def provider_declined(w: World) -> None:
    req, r = _permit(w)
    a = w.dispatch(r["record"], req, T0 + 1000, fault="decline")
    w.outcome(r["reservation_id"], "failed", T0 + 2000, a["provider_ref"] if a else None)
    w.observe(r["record"]["receipt"]["receipt_id"], T0 + 3000)


def gate_crash_recovery(w: World) -> None:
    req, r = _permit(w)
    w.crash_gate(T0 + 1000)
    w.restart_gate(T0 + 16 * MINUTE)
    w.dispatch(r["record"], req, T0 + 16 * MINUTE + 1000)
    w.reconcile(r["reservation_id"], "failed", "executor-refused-before-dispatch", T0 + 16 * MINUTE + 2000)


# -- optional required evidence (aps-checkout-gate pull request 1) ------------------------

REVIEWER = "agent-checkout-runner/reviewer"


def _reviewer_checks(reviewer_pub: str) -> list[dict[str, Any]]:
    return [{"type": "verdict", "verifierPubkey": reviewer_pub, "proceedOn": ["approve"]}]


def verdict_present(w: World) -> None:
    req = request(1)
    v = w.gate.call("sign_test_verdict", label=REVIEWER, verdict="approve", at=T0 - 1000, request=req)["event"]
    r = w.decide(req, w.grant(), T0, evidence={"verdict": v})
    a = w.dispatch(r["record"], req, T0 + 1000)
    _settle_ok(w, r, a, T0 + 2000)


def verdict_missing(w: World) -> None:
    req = request(1)
    r = w.decide(req, w.grant(), T0)
    w.dispatch(r["record"], req, T0 + 1000)


def verdict_other_request(w: World) -> None:
    req = request(1)
    other = request(1, amount_minor=2900)
    v = w.gate.call("sign_test_verdict", label=REVIEWER, verdict="approve", at=T0 - 1000, request=other)["event"]
    r = w.decide(req, w.grant(), T0, evidence={"verdict": v})
    w.dispatch(r["record"], req, T0 + 1000)


def published_verdict(w: World) -> None:
    """The real invinoveritas verdict from the gate's pull request, through the whole gate."""
    fixture = json.loads((w.gate_dir / "test" / "vectors" / PUBLISHED_VERDICT).read_text(encoding="utf-8"))
    ev, req = fixture["event"], fixture["request"]
    at = json.loads(ev["content"])["verified_at"] * 1000 + 1000
    cfg = {"verifierPubkey": ev["pubkey"], "proceedOn": ["approve", "approve_with_concerns"]}
    action_ref = w.gate.call("digest", request=req)
    w.component_check("verdict_problems", w.gate.call("verdict_problems", event=ev, request=req,
                                                      action_ref=action_ref, now=at, cfg=cfg), at)
    grant = w.grant(at, agent=req["agent_id"], merchant=req["merchant_id"])
    r = w.decide(req, grant, at, signer=req["agent_id"], evidence={"verdict": ev})
    w.dispatch(r["record"], req, at + 1000)


def _published_checks(_reviewer_pub: str) -> list[dict[str, Any]]:
    return [{"type": "verdict", "verifierPubkey": "6786e18a864893a900bd9858e650f67ccc3513f248fed374b591e2ff6922fbb7",
             "proceedOn": ["approve", "approve_with_concerns"]}]


def _published_parties() -> list[dict[str, Any]]:
    from .world import PARTIES
    return PARTIES + [{"id": "agent:demo-shopper", "role": "agent", "seed": "agent-checkout-runner/test-key/demo-shopper/v1"}]


# -- the matrix ---------------------------------------------------------------------------

HAPPY = {"admission.decisions_verify": E, "admission.expected_decisions": E, "dispatch.expected_attempts": E,
         "dispatch.provider_calls_bound": E,
         "dispatch.refused_before_provider": N, "effect.at_most_once": E, "budget.held_until_confirmed": E,
         "outcome.resolved_at_cutoff": E}
REFUSED = {"admission.decisions_verify": E, "admission.expected_decisions": E, "dispatch.expected_attempts": E,
           "dispatch.provider_calls_bound": N,
           "dispatch.refused_before_provider": E, "effect.at_most_once": N, "budget.held_until_confirmed": E,
           "outcome.resolved_at_cutoff": E}
PERMIT = [["permit", "permitted"]]


def matrix() -> list[Case]:
    ok, ref = ["response_ok"], ["refused"]
    no_res = {"budget.held_until_confirmed": N, "outcome.resolved_at_cutoff": N}
    return [
        Case("match_dispatch", "a verified permit, then the exact approved request is dispatched once", match_dispatch,
             HAPPY, decisions=PERMIT, attempts=ok),
        Case("changed_amount", "the amount changes after approval", _changed("amount_minor", 2999), REFUSED,
             refusal="request_mismatch", decisions=PERMIT, attempts=ref),
        Case("changed_merchant", "the merchant changes after approval", _changed("merchant_id", "merchant-b"), REFUSED,
             refusal="request_mismatch", decisions=PERMIT, attempts=ref),
        Case("changed_currency", "the currency changes after approval", _changed("currency", "EUR"), REFUSED,
             refusal="request_mismatch", decisions=PERMIT, attempts=ref),
        Case("changed_agent", "the agent changes after approval", _changed("agent_id", OTHER_AGENT), REFUSED,
             refusal="request_mismatch", decisions=PERMIT, attempts=ref),
        Case("changed_items", "the line items change after approval, same total", changed_items, REFUSED,
             refusal="request_mismatch", decisions=PERMIT, attempts=ref),
        Case("forged_signature", "one hex digit of the gate signature is changed", forged_signature, REFUSED,
             refusal="signature_invalid", decisions=PERMIT, attempts=ref),
        Case("tampered_evidence", "the reservation id in the committed evidence is changed", tampered_evidence,
             REFUSED, refusal="decision_ref_mismatch", decisions=PERMIT, attempts=ref),
        Case("untrusted_signer", "a permit signed under the gate's id by a key the executor does not trust",
             untrusted_signer, REFUSED, refusal="signature_invalid", decisions=PERMIT, attempts=ref),
        Case("refusal_as_handoff", "a signed refusal (amount over the per-request limit) is presented for dispatch",
             refusal_as_handoff, {**REFUSED, **no_res}, refusal="not_a_permit",
             decisions=[["deny", "per_request_limit_exceeded"]], attempts=ref),
        Case("replay_after_success", "the same permit is dispatched again after a confirmed effect",
             replay_after_success, HAPPY, decisions=PERMIT, attempts=ok + ["recovered_existing"]),
        Case("replay_after_expiry", "the same permit is dispatched again after its valid_until; the finished "
             "operation is returned, nothing is dispatched", replay_after_expiry, HAPPY, decisions=PERMIT,
             attempts=ok + ["recovered_existing"], cutoff_ms=T0 + 20 * MINUTE),
        Case("gate_replay", "the same request is decided twice; the gate returns its stored permit and both are "
             "dispatched", gate_replay, HAPPY, decisions=PERMIT + PERMIT, attempts=ok + ["recovered_existing"]),
        Case("expired_permit", "dispatch is attempted after the permit's valid_until", expired_permit, REFUSED,
             refusal="permit_expired", decisions=PERMIT, attempts=ref, cutoff_ms=T0 + 20 * MINUTE),
        Case("executor_crash_before_call", "the executor dies after recording dispatch_pending, then the call is "
             "retried", executor_crash_before_call, HAPPY, decisions=PERMIT, attempts=ok),
        Case("provider_crash_before_commit", "the provider dies before committing; the retry under the same key "
             "commits once", provider_crash_before_commit, HAPPY, decisions=PERMIT,
             attempts=["response_lost", "response_ok"]),
        Case("lost_response_retry", "the provider commits and the response is lost; the retry returns the same "
             "effect", lost_response_retry, HAPPY, decisions=PERMIT, attempts=["response_lost", "response_ok"]),
        Case("lost_response_reconciled", "the response is lost; a report-time read finds the effect before the "
             "cutoff", lost_response_reconciled, HAPPY, decisions=PERMIT, attempts=["response_lost"]),
        Case("lost_response_after_cutoff", "the response is lost and the only read comes after the report cutoff",
             lost_response_after_cutoff, {**HAPPY, "outcome.resolved_at_cutoff": N}, decisions=PERMIT,
             attempts=["response_lost"]),
        Case("crash_then_expired_retry", "the provider dies before committing and the retry comes after "
             "valid_until", crash_then_expired_retry,
             {**REFUSED, "dispatch.provider_calls_bound": E, "dispatch.refused_before_provider": N},
             decisions=PERMIT, attempts=["response_lost", "refused"], cutoff_ms=T0 + 20 * MINUTE),
        Case("provider_declined", "the provider declines; the confirmed decline releases the budget",
             provider_declined, {**HAPPY, "effect.at_most_once": N}, decisions=PERMIT, attempts=["declined"]),
        Case("gate_crash_recovery", "the gate process is killed after a permit; its restart moves the reservation "
             "to unresolved", gate_crash_recovery, REFUSED, refusal="permit_expired", decisions=PERMIT,
             attempts=ref, cutoff_ms=T0 + 20 * MINUTE),
        Case("verdict_present", "the gate requires a reviewer verdict and gets one on this exact request",
             verdict_present, HAPPY, pins=("pr1",), decisions=PERMIT, attempts=ok,
             evidence_checks=_reviewer_checks),
        Case("verdict_missing", "the gate requires a reviewer verdict and gets none", verdict_missing,
             {**REFUSED, **no_res}, pins=("pr1",), refusal="not_a_permit",
             decisions=[["deny", "evidence_missing"]], attempts=ref, evidence_checks=_reviewer_checks),
        Case("verdict_other_request", "the verdict binds a different request", verdict_other_request,
             {**REFUSED, **no_res}, pins=("pr1",), refusal="not_a_permit",
             decisions=[["deny", "verdict_request_mismatch"]], attempts=ref, evidence_checks=_reviewer_checks),
        Case("published_verdict", "the real invinoveritas verdict published with the gate's pull request, through "
             "decide()", published_verdict,
             {**REFUSED, **no_res, "evidence.offline_verdict_check": E, "evidence.verdict_permits_through_gate": C},
             pins=("pr1",), refusal="not_a_permit", decisions=[["deny", "malformed_request"]], attempts=ref,
             evidence_checks=_published_checks, parties=_published_parties()),
    ]


def write_case_meta(case: Case, w: World, pin: str) -> None:
    meta = {
        "case": case.name, "pin": pin, "summary": case.summary, "cutoff_ms": case.cutoff_ms,
        "expected_refusal": case.refusal, "expected_decisions": case.decisions,
        "expected_attempts": case.attempts, "expected_claims": case.expect,
        "trust": {"gate_id": w.trust.gate_id, "key_id": w.trust.key_id, "public_key": w.trust.public_key},
    }
    (w.dir / "case.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def case_dir(root: Path, pin: str, case: Case) -> Path:
    return root / "cases" / pin / case.name
