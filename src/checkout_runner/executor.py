"""The dispatch boundary: the only path from a gate permit to the simulator.

Runs as its own process for every dispatch (``python -m checkout_runner.executor
<state.db> <ledger.db>``), so a crash at any point is a real process exit and the
next call starts from what sqlite holds. One call:

1. checks the handoff with :mod:`checkout_runner.handoff` against the trust it is
   given (gate id, key id, public key), never against keys inside the record;
2. binds the request it is asked to dispatch to the permit: sha256(JCS(request))
   must equal the receipt's ``action_ref``, so a changed merchant, item, amount,
   currency or agent is refused before any effect;
3. looks up the operation for this permit (``receipt_id``). A finished operation
   is returned as it is: no dispatch, and its validity is not extended;
4. refuses a new or retried dispatch at or after the permit's ``valid_until``;
5. writes ``dispatch_pending`` durably before the provider call, which is the
   consumption of the permit: a second dispatch of the same permit is the same
   operation under the same idempotency key, never a second admission;
6. calls the simulator and records the reply, or ``response_lost`` when the
   provider process ends without answering. A lost response never means no effect.

Every call appends one attempt row. Attempts are a separate stream from the
gate's decisions, the simulator's ledger and report-time observations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from agent_evidence_vectors.run_vectors import jcs_dumps

from .handoff import GateTrust, check_handoff, request_digest, sha256_hex

FINAL = ("response_ok", "declined")

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
  receipt_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_digest TEXT NOT NULL,
  reservation_id TEXT NOT NULL,
  valid_until TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('dispatch_pending', 'response_ok', 'declined', 'response_lost')),
  provider_ref TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id TEXT,
  idempotency_key TEXT,
  request_digest TEXT,
  at_ms INTEGER NOT NULL,
  result TEXT NOT NULL,
  codes TEXT NOT NULL,
  provider_ref TEXT,
  reached_provider INTEGER NOT NULL
);
"""


def idempotency_key(gate_id: str, receipt_id: str) -> str:
    """Scoped to the trusted gate, the permit and the simulator namespace."""
    return sha256_hex(jcs_dumps({
        "profile": "agent-checkout-runner/idempotency-v1",
        "gate": gate_id,
        "receipt_id": receipt_id,
        "provider_namespace": "local-simulator",
    }))


def iso_ms(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def open_state(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None, timeout=20)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = FULL")
    db.executescript(SCHEMA)
    return db


def _attempt(db: sqlite3.Connection, now: int, result: str, codes: list[str], receipt_id: str | None = None,
             key: str | None = None, digest: str | None = None, provider_ref: str | None = None,
             reached: bool = False) -> dict[str, Any]:
    db.execute(
        "INSERT INTO attempts (receipt_id, idempotency_key, request_digest, at_ms, result, codes, provider_ref, reached_provider)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (receipt_id, key, digest, now, result, json.dumps(codes), provider_ref, int(reached)),
    )
    return {"result": result, "codes": codes, "receipt_id": receipt_id, "idempotency_key": key,
            "provider_ref": provider_ref, "reached_provider": reached}


def call_simulator(ledger: str, msg: dict[str, Any]) -> dict[str, Any] | None:
    """One provider call in its own process. None when it ended without answering."""
    p = subprocess.run([sys.executable, "-m", "checkout_runner.simulator", ledger],
                       input=json.dumps(msg), capture_output=True, text=True, timeout=60)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return json.loads(p.stdout)


def dispatch(db: sqlite3.Connection, ledger: str, msg: dict[str, Any]) -> dict[str, Any]:
    now: int = msg["now"]
    trust = GateTrust(**msg["trust"])
    request = msg["request"]
    check = check_handoff(msg["handoff"], trust)
    codes = list(check.codes)
    try:
        digest = request_digest(request)
    except Exception:  # noqa: BLE001 - an unencodable request is refused, never dispatched
        return _attempt(db, now, "refused", codes + ["request_unencodable"], check.receipt_id)
    if not codes and digest != check.action_ref:
        codes.append("request_mismatch")
    if codes:
        return _attempt(db, now, "refused", codes, check.receipt_id, digest=digest)
    assert check.receipt_id and check.reservation_id and check.valid_until
    key = idempotency_key(trust.gate_id, check.receipt_id)
    op = db.execute("SELECT state, provider_ref, request_digest FROM operations WHERE receipt_id = ?",
                    (check.receipt_id,)).fetchone()
    if op is not None and op[0] in FINAL:
        return _attempt(db, now, "recovered_existing", [], check.receipt_id, key, digest, op[1])
    if now >= iso_ms(check.valid_until):
        return _attempt(db, now, "refused", ["permit_expired"], check.receipt_id, key, digest)
    if op is None:
        db.execute(
            "INSERT INTO operations (receipt_id, idempotency_key, request_digest, reservation_id, valid_until, state)"
            " VALUES (?, ?, ?, ?, ?, 'dispatch_pending')",
            (check.receipt_id, key, digest, check.reservation_id, check.valid_until),
        )
    if msg.get("crash_at") == "after_pending":
        os._exit(137)
    reply = call_simulator(ledger, {"op": "apply", "idempotency_key": key, "request": request,
                                    "fault": msg.get("fault", "none"), "now": now})
    if reply is None:
        db.execute("UPDATE operations SET state = 'response_lost' WHERE receipt_id = ?", (check.receipt_id,))
        return _attempt(db, now, "response_lost", [], check.receipt_id, key, digest, reached=True)
    state = "declined" if reply["status"] == "declined" else "response_ok"
    db.execute("UPDATE operations SET state = ?, provider_ref = ? WHERE receipt_id = ?",
               (state, reply["provider_ref"], check.receipt_id))
    return _attempt(db, now, state, [], check.receipt_id, key, digest, reply["provider_ref"], reached=True)


def main() -> None:
    db = open_state(sys.argv[1])
    msg = json.loads(sys.stdin.read())
    sys.stdout.write(json.dumps(dispatch(db, sys.argv[2], msg)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
