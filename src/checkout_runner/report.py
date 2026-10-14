"""Claims, derived from a case's retained streams and nothing else.

``derive(case_dir)`` reads the JSON-lines files a case left behind and returns
one verdict per claim: ESTABLISHED, CONTRADICTED or NOT_ESTABLISHED. It never
sees a component object, so the same function gives the same answer when a
reader runs it later over a copied bundle. The streams stay separate here too:
admission claims read decisions, dispatch claims read attempts against
decisions, effect claims read the simulator ledger, and the cutoff claim reads
gate events and observations made at or before the report cutoff.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .executor import iso_ms
from .handoff import GateTrust, check_handoff

E, C, N = "ESTABLISHED", "CONTRADICTED", "NOT_ESTABLISHED"

CLAIMS = (
    "admission.decisions_verify",
    "admission.expected_decisions",
    "dispatch.expected_attempts",
    "dispatch.provider_calls_bound",
    "dispatch.refused_before_provider",
    "effect.at_most_once",
    "budget.held_until_confirmed",
    "outcome.resolved_at_cutoff",
    "evidence.offline_verdict_check",
    "evidence.verdict_request_admissible",
    "evidence.verdict_permits_through_gate",
)

#: Said about every run, whatever the cases show. Each needs evidence this
#: runner does not produce.
RUN_LIMITS = {
    "provider.live_behaviour": "the provider is a local simulator; nothing here exercised a real payment or refund API",
    "gate.multi_host": "one gate process and one sqlite file; two hosts were not run",
    "admission.signed_handoff_format": "the handoff is the gate's own signed decision receipt and its committed evidence; "
                                        "no component driven here emits a separate signed admission record yet",
    "run.independent": "this run was operated by the runner's author on fixtures it generated; it is not an independent "
                       "run and not a host or merchant adoption",
    "settlement_and_fulfilment": "a permit and a simulator effect say nothing about settlement or delivery",
}


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load(case_dir: Path) -> dict[str, Any]:
    s: dict[str, Any] = {name: _lines(case_dir / f"{name}.jsonl") for name in (
        "decisions", "attempts", "effects", "observations", "gate_events", "operations", "component_checks")}
    s["meta"] = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    return s


def _integrity_codes(record: Any, trust: GateTrust) -> list[str]:
    ignore = {"not_a_permit", "permit_without_reservation", "permit_without_validity"}
    return [c for c in check_handoff(record, trust).codes if c not in ignore]


def derive(case_dir: Path) -> dict[str, dict[str, str]]:
    s = load(case_dir)
    trust = GateTrust(**s["meta"]["trust"])
    decisions = [d for d in s["decisions"] if d["kind"] == "decision"]
    out: dict[str, dict[str, str]] = {}

    def put(claim: str, verdict: str, why: str) -> None:
        out[claim] = {"verdict": verdict, "why": why}

    # admission.decisions_verify: our check and the gate's own verifier, on every decision it returned.
    if not decisions:
        put("admission.decisions_verify", N, "the gate returned no decision")
    else:
        bad = [d["record"]["receipt"].get("receipt_id") for d in decisions
               if _integrity_codes(d["record"], trust) or not d["gate_verify"]["valid"]]
        put("admission.decisions_verify", C if bad else E,
            f"{len(decisions)} decisions; failing: {bad}" if bad else f"{len(decisions)} decisions verify both ways")

    expected = s["meta"]["expected_decisions"]
    got = [[d["verdict"], d["reason"]] for d in decisions]
    put("admission.expected_decisions", E if got == expected else C, f"decisions {got}, expected {expected}")

    want_attempts = s["meta"]["expected_attempts"]
    got_attempts = [a["result"] for a in s["attempts"]]
    put("dispatch.expected_attempts", E if got_attempts == want_attempts else C,
        f"attempts {got_attempts}, expected {want_attempts}")

    permits = {d["record"]["receipt"]["receipt_id"]: d["record"] for d in decisions if d["verdict"] == "permit"}

    # dispatch.provider_calls_bound: every attempt that reached the provider stands on a verified permit
    # for exactly the dispatched request, made before the permit's valid_until.
    reached = [a for a in s["attempts"] if a["reached_provider"]]
    if not reached:
        put("dispatch.provider_calls_bound", N, "no attempt reached the provider")
    else:
        unbound = []
        for a in reached:
            rec = permits.get(a["receipt_id"])
            ok = (rec is not None and not check_handoff(rec, trust).codes
                  and rec["receipt"]["action_ref"] == a["request_digest"]
                  and a["at_ms"] < iso_ms(rec["receipt"]["result"]["valid_until"]))
            if not ok:
                unbound.append(a["attempt_id"])
        put("dispatch.provider_calls_bound", C if unbound else E,
            f"unbound attempts {unbound}" if unbound else f"{len(reached)} provider calls, each bound to its permit")

    # dispatch.refused_before_provider: the case expects a refusal; it must be the expected one,
    # no attempt may reach the provider, and the ledger must hold no row.
    want = s["meta"]["expected_refusal"]
    if want is None:
        put("dispatch.refused_before_provider", N, "this case expects no refusal")
    else:
        ok = (bool(s["attempts"]) and all(a["result"] == "refused" and not a["reached_provider"]
                                           and want in json.loads(a["codes"]) for a in s["attempts"])
              and not s["effects"])
        put("dispatch.refused_before_provider", E if ok else C,
            f"attempts {[(a['result'], a['codes']) for a in s['attempts']]}, ledger rows {len(s['effects'])}")

    # effect.at_most_once: per reservation, at most one effect row, and every row belongs to an operation.
    op_by_key = {o["idempotency_key"]: o for o in s["operations"]}
    effects = [e for e in s["effects"] if e["kind"] == "effect"]
    if not effects:
        put("effect.at_most_once", N, "the simulator recorded no effect")
    else:
        per_res: dict[str, int] = defaultdict(int)
        orphan = []
        for e in effects:
            op = op_by_key.get(e["idempotency_key"])
            if op is None:
                orphan.append(e["provider_ref"])
            else:
                per_res[op["reservation_id"]] += 1
        dup = {r: n for r, n in per_res.items() if n > 1}
        put("effect.at_most_once", C if dup or orphan else E,
            f"duplicates {dup}, effects without an operation {orphan}" if dup or orphan
            else f"{len(effects)} effects over {len(per_res)} reservations")

    res_ids = {d["reservation_id"] for d in decisions if d["reservation_id"]}
    effect_res = {op_by_key[e["idempotency_key"]]["reservation_id"] for e in effects if e["idempotency_key"] in op_by_key}

    # budget.held_until_confirmed: nothing released without a confirmation, and nothing released that has an effect.
    if not res_ids:
        put("budget.held_until_confirmed", N, "no reservation was made")
    else:
        bad = [ev for ev in s["gate_events"] if ev["to_state"] == "released"
               and (ev["evidence"] is None or ev["reservation_id"] in effect_res)]
        put("budget.held_until_confirmed", C if bad else E,
            f"released without confirmation or with an effect: {bad}" if bad
            else f"{len(res_ids)} reservations; every release carried a confirmation and no effect")

    out["outcome.resolved_at_cutoff"] = _resolved(s, res_ids)

    checks = {c["check"]: c["result"] for c in s["component_checks"]}
    if "verdict_problems" in checks:
        put("evidence.offline_verdict_check", E if checks["verdict_problems"] == [] else C,
            f"verdictProblems returned {checks['verdict_problems']}")
        if "request_problems" in checks:
            probs, ctl = checks["request_problems"], checks.get("request_problems_segment_control")
            put("evidence.verdict_request_admissible", E if probs == [] else C,
                f"requestShapeProblems on the signed request returned {probs}; "
                f"with the merchant id as one scope segment it returned {ctl}")
        else:
            put("evidence.verdict_request_admissible", N, "the request check was not run")
        through = any(d["verdict"] == "permit" for d in decisions)
        put("evidence.verdict_permits_through_gate", E if through else C,
            f"gate decisions {got}")
    else:
        put("evidence.offline_verdict_check", N, "no published verdict in this case")
        put("evidence.verdict_request_admissible", N, "no published verdict in this case")
        put("evidence.verdict_permits_through_gate", N, "no published verdict in this case")
    return out


def _resolved(s: dict[str, Any], res_ids: set[str]) -> dict[str, str]:
    """At the cutoff, is every reservation's outcome known and consistent with what was observed?"""
    if not res_ids:
        return {"verdict": N, "why": "no reservation was made"}
    cutoff = s["meta"]["cutoff_ms"]
    ops = defaultdict(list)
    for o in s["operations"]:
        ops[o["reservation_id"]].append(o)
    seen: dict[str, list[Any]] = defaultdict(list)
    for ob in s["observations"]:
        if ob["at_ms"] <= cutoff:
            seen[ob["receipt_id"]].append(ob["row"])
    verdicts, why = [], []
    for rid in sorted(res_ids):
        state = "reserved"
        for ev in s["gate_events"]:
            if ev["reservation_id"] == rid and iso_ms(ev["at"]) <= cutoff:
                state = ev["to_state"]
        rows = [r for o in ops[rid] for r in seen[o["receipt_id"]]]
        kinds = {r["kind"] if r else "none" for r in rows}
        if state == "committed":
            v = E if kinds == {"effect"} else (C if kinds else N)
        elif state == "released":
            never_dispatched = not ops[rid]
            v = E if never_dispatched or (kinds and kinds <= {"none", "declined"}) else (C if "effect" in kinds else N)
        else:
            v = N
        verdicts.append(v)
        why.append(f"{rid}: {state} at cutoff, observed {sorted(kinds) or 'nothing'} -> {v}")
    v = C if C in verdicts else (N if N in verdicts else E)
    return {"verdict": v, "why": "; ".join(why)}
