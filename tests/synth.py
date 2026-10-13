"""Synthetic records for unit tests: a gate decision signed in Python, and case directories with chosen streams."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_evidence_vectors.run_vectors import ed25519_public_key, ed25519_sign

from checkout_runner.handoff import GateTrust, decision_ref_of, receipt_id_of, request_digest, signature_payload

GATE = "did:example:gate"
SEED = hashlib.sha256(b"tests/gate").digest()
TRUST = GateTrust(GATE, f"{GATE}#key-1", ed25519_public_key(SEED).hex())
REQUEST = {"request_id": "r1", "agent_id": "did:example:agent-a", "merchant_id": "m", "order_ref": "o1",
           "items": [{"sku": "s", "quantity": 1, "unit_price_minor": 100}], "amount_minor": 100, "currency": "USD"}


def permit(request: dict[str, Any] = REQUEST, valid_until: str = "2026-10-13T14:15:00.000Z",
           reservation: str | None = "res_1", verdict: str = "permit", seed: bytes = SEED) -> dict[str, Any]:
    action_ref = request_digest(request)
    output = {"profile": "aps-core-decision-output-v1", "verdict": verdict,
              "effective_authority_ref": "ab" * 32 if verdict == "permit" else None, "constraints": [],
              "valid_until": valid_until if verdict == "permit" else None}
    evidence = {
        "authority_state": {"grant_id": "sha256:" + "ab" * 32, "spend_unit": "iso4217:USD:minor",
                            "cumulative_minor": "1000", "held_minor_before": "0"},
        "policy_input": {"request_id": request["request_id"], "request_digest": action_ref,
                         "intent_receipt_id": "cd" * 32, "items_priced": True},
        "decision_context": {"profile": "aps-checkout-gate/decision-context-v1",
                             "reason": "permitted" if verdict == "permit" else "refused", "reasons": [],
                             "reservation_id": reservation, "gate_id": GATE},
        "decision_output": output,
    }
    receipt: dict[str, Any] = {
        "profile": "aps-receipt-v1", "receipt_type": "aps:policy-decision:v1", "issuer": GATE,
        "subject_agent": request["agent_id"], "action_ref": action_ref, "delegation_ref": "sha256:" + "ab" * 32,
        "decision_ref": decision_ref_of(action_ref, evidence), "prev": "cd" * 32,
        "issued_at": "2026-10-13T14:00:00.000Z", "evidence_refs": [], "result": output,
        "receipt_id": "0" * 64, "signatures": [],
    }
    receipt["receipt_id"] = receipt_id_of(receipt)
    descriptor = {"signer": GATE, "key_id": f"{GATE}#key-1", "alg": "Ed25519"}
    receipt["signatures"] = [{**descriptor, "value": ed25519_sign(seed, signature_payload(receipt, descriptor)).hex()}]
    return {"receipt": receipt, "evidence": evidence}


def case_dir(root: Path, streams: dict[str, list[dict[str, Any]]], **meta: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = {"case": "synthetic", "pin": "test", "summary": "", "cutoff_ms": 1_791_900_600_000,
            "expected_refusal": None, "expected_decisions": [], "expected_attempts": [], "expected_claims": {},
            "trust": {"gate_id": TRUST.gate_id, "key_id": TRUST.key_id, "public_key": TRUST.public_key}}
    base.update(meta)
    (root / "case.json").write_text(json.dumps(base), encoding="utf-8")
    for name, rows in streams.items():
        (root / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return root


def decision(record: dict[str, Any], valid: bool = True) -> dict[str, Any]:
    ctx = record["evidence"]["decision_context"]
    return {"kind": "decision", "at_ms": 1_791_900_000_000, "verdict": record["receipt"]["result"]["verdict"],
            "reason": ctx["reason"], "reservation_id": ctx["reservation_id"], "replay": False, "record": record,
            "gate_verify": {"valid": valid, "problems": []}}


def attempt(n: int, record: dict[str, Any], at_ms: int, digest: str | None = None, reached: int = 1,
            result: str = "response_ok", codes: str = "[]") -> dict[str, Any]:
    return {"attempt_id": n, "receipt_id": record["receipt"]["receipt_id"], "idempotency_key": f"k{n}",
            "request_digest": digest or record["receipt"]["action_ref"], "at_ms": at_ms, "result": result,
            "codes": codes, "provider_ref": f"p{n}", "reached_provider": reached}
