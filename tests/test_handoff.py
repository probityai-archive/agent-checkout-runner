"""The independent handoff check, on decisions signed in Python with the APS receipt constructions."""

import copy
import hashlib

import pytest
from synth import REQUEST, TRUST, permit

from checkout_runner.handoff import GateTrust, check_handoff


def test_a_signed_permit_admits():
    c = check_handoff(permit(), TRUST)
    assert c.ok, c.codes
    assert c.reservation_id == "res_1"


@pytest.mark.parametrize("edit,code", [
    (lambda r: r["receipt"]["signatures"][0].update(value="00" * 64), "signature_invalid"),
    (lambda r: r["receipt"].update(issued_at="2026-10-13T14:00:00.001Z"), "receipt_id_mismatch"),
    (lambda r: r["evidence"]["decision_context"].update(reservation_id="res_2"), "decision_ref_mismatch"),
    (lambda r: r["receipt"]["signatures"][0].update(key_id="did:example:gate#key-2"), "signature_key_untrusted"),
    (lambda r: r["receipt"].update(signatures=[]), "signature_missing"),
    (lambda r: r.pop("evidence"), "handoff_shape"),
    (lambda r: r["receipt"].update(receipt_type="aps:action-intent:v1"), "handoff_type"),
])
def test_each_tamper_is_named(edit, code):
    r = copy.deepcopy(permit())
    edit(r)
    assert code in check_handoff(r, TRUST).codes


def test_a_different_key_under_the_same_gate_id_is_refused():
    rogue = permit(seed=hashlib.sha256(b"rogue").digest())
    assert "signature_invalid" in check_handoff(rogue, TRUST).codes


def test_a_refusal_is_not_a_permit():
    assert check_handoff(permit(verdict="deny", reservation=None), TRUST).codes == ["not_a_permit"]


def test_a_permit_without_reservation_is_refused():
    assert "permit_without_reservation" in check_handoff(permit(reservation=None), TRUST).codes


def test_trust_comes_from_configuration_not_the_record():
    other = GateTrust(TRUST.gate_id, TRUST.key_id, "11" * 32)
    assert "signature_invalid" in check_handoff(permit(), other).codes


def test_request_digest_is_the_gate_action_ref():
    assert permit()["receipt"]["action_ref"] == permit(dict(REQUEST))["receipt"]["action_ref"]
