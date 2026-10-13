"""One case's world: a gate process, an executor, a simulator, and four record streams.

The streams are written by different parts and never merged:

* ``decisions.jsonl``: what the gate returned (decisions, outcome and reconcile
  results, the gate's own verification of each decision), plus the gate's
  reservation events read back at the end;
* ``attempts.jsonl``: the executor's attempt rows, dumped from its own sqlite;
* ``effects.jsonl``: the simulator's ledger, dumped from its own sqlite;
* ``observations.jsonl``: report-time reads of the simulator ledger, each with
  the time it was made, so a reader can apply the report cutoff;
* ``runner.jsonl``: what the runner itself did (process kills, restarts).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .executor import idempotency_key
from .gate_client import GateProcess
from .handoff import GateTrust

T0 = 1_791_900_000_000  # 2026-10-13T14:00:00.000Z, the fixed start of every case clock
MINUTE = 60_000

GATE = "did:example:gate"
PRINCIPAL = "did:example:buyer"
AGENT = "did:example:agent-a"
OTHER_AGENT = "did:example:agent-b"
MERCHANT = "merchant-a"
PARTIES = [
    {"id": GATE, "role": "gate", "seed": "agent-checkout-runner/test-key/gate/v1"},
    {"id": PRINCIPAL, "role": "principal", "seed": "agent-checkout-runner/test-key/principal/v1"},
    {"id": AGENT, "role": "agent", "seed": "agent-checkout-runner/test-key/agent-a/v1"},
    {"id": OTHER_AGENT, "role": "agent", "seed": "agent-checkout-runner/test-key/agent-b/v1"},
]


class World:
    def __init__(self, case_dir: Path, gate_dir: Path, evidence_checks: list[dict[str, Any]] | None = None,
                 parties: list[dict[str, Any]] | None = None) -> None:
        self.dir = case_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.gate_dir = gate_dir
        self.evidence_checks = evidence_checks or []
        self.party_specs = parties or PARTIES
        self.gate_db = str(case_dir / "gate.db")
        self.exec_db = str(case_dir / "executor.db")
        self.ledger = str(case_dir / "simulator.db")
        self.streams: dict[str, list[dict[str, Any]]] = {"decisions": [], "observations": [], "runner": [],
                                                        "component_checks": []}
        self.reservations: set[str] = set()
        self.keys: dict[str, Any] = {}
        self.gate = self._start_gate(T0)

    # -- gate -------------------------------------------------------------

    def _start_gate(self, now: int) -> GateProcess:
        g = GateProcess(self.gate_dir)
        self.keys = g.call("parties", parties=self.party_specs)
        opened = g.call("open", db=self.gate_db, gate=GATE, trusted=[p["id"] for p in self.party_specs], now=now,
                        evidence_checks=self.evidence_checks)
        self.streams["runner"].append({"event": "gate_opened", "at_ms": now, "recovered": opened["recovered"]})
        return g

    @property
    def trust(self) -> GateTrust:
        return GateTrust(gate_id=GATE, key_id=f"{GATE}#key-1", public_key=self.keys[GATE]["publicKey"])

    def crash_gate(self, now: int) -> None:
        self.gate.kill()
        self.streams["runner"].append({"event": "gate_killed", "at_ms": now})

    def restart_gate(self, now: int) -> None:
        self.gate = self._start_gate(now)

    def grant(self, now: int = T0, agent: str = AGENT, merchant: str = MERCHANT, per_action: int = 10_000,
              cumulative: int = 10_000, currency: str = "USD") -> dict[str, Any]:
        return self.gate.call("grant", principal=PRINCIPAL, agent=agent, merchant=merchant, currency=currency,
                              per_action=per_action, cumulative=cumulative, nonce="00" * 16, now=now,
                              ttl_ms=24 * 60 * MINUTE)

    def decide(self, request: dict[str, Any], grant: dict[str, Any], now: int, signer: str = AGENT,
               evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        intent = self.gate.call("intent", request=request, agent=signer, delegation=grant, now=now)
        args: dict[str, Any] = {"request": request, "delegation": grant, "intent": intent, "now": now}
        if evidence is not None:
            args["evidence"] = evidence
        r = self.gate.call("decide", **args)
        own = self.gate.call("verify", record=r["record"], gate_id=GATE, public_key=self.keys[GATE]["publicKey"])
        self.streams["decisions"].append({"kind": "decision", "at_ms": now, "request": request, "verdict": r["verdict"],
                                          "reason": r["reason"], "reservation_id": r["reservation_id"],
                                          "replay": r["replay"], "record": r["record"], "gate_verify": own})
        if r["reservation_id"]:
            self.reservations.add(r["reservation_id"])
        return r

    def outcome(self, reservation_id: str, outcome: str, now: int, platform_ref: str | None = None) -> dict[str, Any]:
        conf = {"platform_ref": platform_ref} if platform_ref else None
        r = self.gate.call("outcome", reservation_id=reservation_id, outcome=outcome, confirmation=conf, now=now)
        self.streams["decisions"].append({"kind": "outcome", "at_ms": now, "reservation_id": reservation_id,
                                          "outcome": outcome, "confirmation": conf, "result": r})
        return r

    def reconcile(self, reservation_id: str, outcome: str, platform_ref: str, now: int) -> dict[str, Any]:
        r = self.gate.call("reconcile", reservation_id=reservation_id, outcome=outcome,
                           confirmation={"platform_ref": platform_ref}, now=now)
        self.streams["decisions"].append({"kind": "reconcile", "at_ms": now, "reservation_id": reservation_id,
                                          "outcome": outcome, "confirmation": {"platform_ref": platform_ref},
                                          "result": r})
        return r

    # -- executor and simulator --------------------------------------------

    def dispatch(self, handoff: Any, request: dict[str, Any], now: int, fault: str = "none",
                 crash_at: str | None = None, trust: GateTrust | None = None) -> dict[str, Any] | None:
        t = trust or self.trust
        msg = {"handoff": handoff, "request": request, "now": now, "fault": fault, "crash_at": crash_at,
               "trust": {"gate_id": t.gate_id, "key_id": t.key_id, "public_key": t.public_key}}
        p = subprocess.run([sys.executable, "-m", "checkout_runner.executor", self.exec_db, self.ledger],
                           input=json.dumps(msg), capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            self.streams["runner"].append({"event": "executor_exited", "at_ms": now, "exit": p.returncode,
                                           "crash_at": crash_at})
            return None
        return json.loads(p.stdout)

    def observe(self, receipt_id: str, now: int) -> dict[str, Any] | None:
        """A report-time read of the simulator ledger for one operation."""
        key = idempotency_key(GATE, receipt_id)
        p = subprocess.run([sys.executable, "-m", "checkout_runner.simulator", self.ledger],
                           input=json.dumps({"op": "query", "idempotency_key": key}), capture_output=True, text=True,
                           timeout=60, check=True)
        row = json.loads(p.stdout)["row"]
        self.streams["observations"].append({"at_ms": now, "receipt_id": receipt_id, "idempotency_key": key,
                                             "row": row})
        return row

    def component_check(self, name: str, result: Any, now: int) -> None:
        self.streams["component_checks"].append({"check": name, "at_ms": now, "result": result})

    # -- closing ---------------------------------------------------------------

    def finish(self) -> None:
        events: list[dict[str, Any]] = []
        for rid in sorted(self.reservations):
            for e in self.gate.call("events", reservation_id=rid):
                events.append({"reservation_id": rid, **e})
            state = self.gate.call("reservation", reservation_id=rid)
            self.streams["decisions"].append({"kind": "reservation_final", "reservation_id": rid, "state": state})
        self.gate.close()
        self.streams["gate_events"] = events
        self.streams["attempts"] = _dump(self.exec_db, "SELECT * FROM attempts ORDER BY attempt_id")
        self.streams["operations"] = _dump(self.exec_db, "SELECT * FROM operations ORDER BY receipt_id")
        self.streams["effects"] = _dump(self.ledger, "SELECT * FROM ledger ORDER BY at_ms, idempotency_key") \
            if Path(self.ledger).exists() else []
        for name, rows in self.streams.items():
            with open(self.dir / f"{name}.jsonl", "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")


def _dump(path: str, sql: str) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    db = sqlite3.connect(path)
    try:
        cur = db.execute(sql)
    except sqlite3.OperationalError:
        return []
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def request(n: int, **over: Any) -> dict[str, Any]:
    amount = over.pop("amount_minor", 3000)
    base = {
        "request_id": f"req-{n:04d}", "agent_id": AGENT, "merchant_id": MERCHANT, "order_ref": f"order-{n:04d}",
        "items": [{"sku": "TEE-BLK-M", "quantity": 1, "unit_price_minor": amount}], "amount_minor": amount,
        "currency": "USD",
    }
    base.update(over)
    return base
