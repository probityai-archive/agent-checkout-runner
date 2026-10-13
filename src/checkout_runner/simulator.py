"""A local provider simulator: the only thing in a run that creates an effect.

It runs as its own process (``python -m checkout_runner.simulator <ledger>``),
reads one JSON request on stdin, writes one JSON reply on stdout and exits. Its
ledger is a sqlite file nobody else writes. An effect is a ledger row; at most
one row exists per idempotency key, enforced by the primary key, so a retry
under the same key returns the original row instead of a second effect.

Fault modes, chosen by the caller per call, model what a real provider does to
its client:

* ``none``: commit, then answer.
* ``decline``: record a declined attempt (no effect), then answer.
* ``lose_response``: commit, then exit without answering. The client sees EOF.
* ``crash_before_commit``: exit before the ledger is touched.

``query`` reads the ledger for one key. It is the report-time read path, kept
separate from ``apply`` so an observation is never the answer to a dispatch.
This is a simulator: what it records says nothing about a live provider.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any

from .handoff import request_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  idempotency_key TEXT PRIMARY KEY,
  provider_ref TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('effect', 'declined')),
  request_digest TEXT NOT NULL,
  at_ms INTEGER NOT NULL
);
"""


def _open(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None, timeout=20)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = FULL")
    db.executescript(SCHEMA)
    return db


def _row(db: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    r = db.execute(
        "SELECT idempotency_key, provider_ref, kind, request_digest, at_ms FROM ledger WHERE idempotency_key = ?", (key,)
    ).fetchone()
    if r is None:
        return None
    return dict(zip(("idempotency_key", "provider_ref", "kind", "request_digest", "at_ms"), r))


def apply(db: sqlite3.Connection, msg: dict[str, Any]) -> dict[str, Any] | None:
    key = msg["idempotency_key"]
    fault = msg.get("fault", "none")
    if fault == "crash_before_commit":
        os._exit(137)
    digest = request_digest(msg["request"])
    db.execute("BEGIN IMMEDIATE")
    existing = _row(db, key)
    if existing is None:
        kind = "declined" if fault == "decline" else "effect"
        ref = "sim_" + digest[:16] + "_" + key[:16]
        db.execute(
            "INSERT INTO ledger (idempotency_key, provider_ref, kind, request_digest, at_ms) VALUES (?, ?, ?, ?, ?)",
            (key, ref, kind, digest, msg["now"]),
        )
        existing = _row(db, key)
        replayed = False
    else:
        replayed = True
    db.execute("COMMIT")
    if fault == "lose_response":
        os._exit(137)
    assert existing is not None
    return {"status": existing["kind"], "provider_ref": existing["provider_ref"], "replayed": replayed,
            "request_digest": existing["request_digest"]}


def query(db: sqlite3.Connection, msg: dict[str, Any]) -> dict[str, Any]:
    return {"row": _row(db, msg["idempotency_key"])}


def dump(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT idempotency_key, provider_ref, kind, request_digest, at_ms FROM ledger ORDER BY at_ms, idempotency_key"
    ).fetchall()
    return [dict(zip(("idempotency_key", "provider_ref", "kind", "request_digest", "at_ms"), r)) for r in rows]


def main() -> None:
    db = _open(sys.argv[1])
    msg = json.loads(sys.stdin.read())
    if msg["op"] == "apply":
        reply = apply(db, msg)
    elif msg["op"] == "query":
        reply = query(db, msg)
    elif msg["op"] == "dump":
        reply = {"rows": dump(db)}
    else:
        raise SystemExit(f"unknown op {msg['op']}")
    sys.stdout.write(json.dumps(reply))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
