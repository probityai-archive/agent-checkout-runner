"""Independent check of the gate's signed decision before anything is dispatched.

The gate signs every decision as an ``aps:policy-decision:v1`` receipt and keeps
the decision evidence beside it, committed through ``decision_ref``. The
executor treats that pair as the admission handoff. This module re-derives every
binding from the bytes, with the JCS encoder and the Ed25519 verifier from
``agent-evidence-vectors`` 0.18.1, and never calls the gate's code or SDK: a
check that reused the producer's verifier would only show the producer agrees
with itself. The gate's own ``verifyDecision`` result is recorded separately by
the runner, and the two are compared.

Constructions, read from agent-passport-system 7.2.1 (receipt-core):

* ``receipt_id`` = sha256 of ``"APS-RECEIPT-ID-V1\\0" + JCS(receipt without
  receipt_id and signatures)``.
* each signature is Ed25519 over ``"APS-RECEIPT-SIG-V1\\0" + JCS({"receipt":
  receipt without signatures, "signer": {signer, key_id, alg}})``, hex encoded.
* ``decision_ref`` = sha256 of ``"APS-DECISION-REF-V1\\0" + JCS(ref input)``,
  where the ref input holds the action_ref and one tagged digest per evidence
  component.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from agent_evidence_vectors.run_vectors import ed25519_verify, jcs_dumps

RECEIPT_ID_TAG = b"APS-RECEIPT-ID-V1\x00"
RECEIPT_SIG_TAG = b"APS-RECEIPT-SIG-V1\x00"
DECISION_REF_TAG = b"APS-DECISION-REF-V1\x00"
COMPONENT_TAGS = {
    "authority_state": b"APS-DECISION-AUTHORITY-V1\x00",
    "policy_input": b"APS-DECISION-POLICY-V1\x00",
    "decision_context": b"APS-DECISION-CONTEXT-V1\x00",
    "decision_output": b"APS-DECISION-OUTPUT-V1\x00",
}
REF_NAMES = {
    "authority_state": "authority_state_ref",
    "policy_input": "policy_ref",
    "decision_context": "context_ref",
    "decision_output": "decision_output_ref",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX128 = re.compile(r"^[0-9a-f]{128}$")


@dataclass(frozen=True)
class GateTrust:
    """What the executor trusts, from its own configuration, never from a record."""

    gate_id: str
    key_id: str
    public_key: str


@dataclass
class HandoffCheck:
    codes: list[str] = field(default_factory=list)
    receipt_id: str | None = None
    action_ref: str | None = None
    reservation_id: str | None = None
    valid_until: str | None = None
    verdict: str | None = None

    @property
    def ok(self) -> bool:
        return not self.codes


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def receipt_id_of(receipt: dict[str, Any]) -> str:
    body = {k: v for k, v in receipt.items() if k not in ("receipt_id", "signatures")}
    return sha256_hex(RECEIPT_ID_TAG + jcs_dumps(body))


def signature_payload(receipt: dict[str, Any], descriptor: dict[str, Any]) -> bytes:
    body = {k: v for k, v in receipt.items() if k != "signatures"}
    return RECEIPT_SIG_TAG + jcs_dumps({"receipt": body, "signer": descriptor})


def decision_ref_of(action_ref: str, evidence: dict[str, Any]) -> str:
    ref_input: dict[str, Any] = {"profile": "aps-decision-ref-v1", "action_ref": action_ref}
    for component, tag in COMPONENT_TAGS.items():
        ref_input[REF_NAMES[component]] = sha256_hex(tag + jcs_dumps(evidence[component]))
    return sha256_hex(DECISION_REF_TAG + jcs_dumps(ref_input))


def check_handoff(record: Any, trust: GateTrust) -> HandoffCheck:
    """Every reason this record does not admit a dispatch. Empty codes = it does."""
    out = HandoffCheck()
    if not isinstance(record, dict) or sorted(record) != ["evidence", "receipt"]:
        out.codes.append("handoff_shape")
        return out
    receipt, evidence = record["receipt"], record["evidence"]
    if not isinstance(receipt, dict) or not isinstance(evidence, dict):
        out.codes.append("handoff_shape")
        return out
    if receipt.get("receipt_type") != "aps:policy-decision:v1":
        out.codes.append("handoff_type")
    if receipt.get("issuer") != trust.gate_id:
        out.codes.append("handoff_issuer_untrusted")
    out.receipt_id = receipt.get("receipt_id")
    out.action_ref = receipt.get("action_ref")
    try:
        if receipt_id_of(receipt) != receipt.get("receipt_id"):
            out.codes.append("receipt_id_mismatch")
    except Exception:  # noqa: BLE001 - any encoding failure is a refusal, never a pass
        out.codes.append("receipt_unencodable")
        return out
    out.codes.extend(_signature_codes(receipt, trust))
    out.codes.extend(_evidence_codes(receipt, evidence))
    result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
    out.verdict = result.get("verdict")
    out.valid_until = result.get("valid_until")
    context = evidence.get("decision_context") if isinstance(evidence.get("decision_context"), dict) else {}
    out.reservation_id = context.get("reservation_id")
    if out.verdict != "permit":
        out.codes.append("not_a_permit")
    elif not isinstance(out.reservation_id, str) or context.get("reason") != "permitted":
        out.codes.append("permit_without_reservation")
    if out.verdict == "permit" and not isinstance(out.valid_until, str):
        out.codes.append("permit_without_validity")
    return out


def _signature_codes(receipt: dict[str, Any], trust: GateTrust) -> list[str]:
    sigs = receipt.get("signatures")
    if not isinstance(sigs, list):
        return ["signature_missing"]
    mine = [s for s in sigs if isinstance(s, dict) and s.get("signer") == trust.gate_id]
    if len(mine) != 1:
        return ["signature_missing"]
    proof = mine[0]
    if proof.get("key_id") != trust.key_id or proof.get("alg") != "Ed25519":
        return ["signature_key_untrusted"]
    value = proof.get("value")
    if not isinstance(value, str) or not HEX128.match(value):
        return ["signature_invalid"]
    descriptor = {k: v for k, v in proof.items() if k != "value"}
    ok = ed25519_verify(bytes.fromhex(trust.public_key), signature_payload(receipt, descriptor), bytes.fromhex(value))
    return [] if ok else ["signature_invalid"]


def _evidence_codes(receipt: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    if sorted(evidence) != sorted(COMPONENT_TAGS):
        return ["evidence_shape"]
    action_ref = receipt.get("action_ref")
    if not isinstance(action_ref, str) or not HEX64.match(action_ref):
        return ["action_ref_malformed"]
    codes = []
    try:
        if decision_ref_of(action_ref, evidence) != receipt.get("decision_ref"):
            codes.append("decision_ref_mismatch")
        if jcs_dumps(receipt.get("result")) != jcs_dumps(evidence["decision_output"]):
            codes.append("result_mismatch")
    except Exception:  # noqa: BLE001
        codes.append("evidence_unencodable")
    return codes


def request_digest(request: Any) -> str:
    """sha256 over the JCS form of a request: the gate's action_ref construction."""
    return sha256_hex(jcs_dumps(request))
