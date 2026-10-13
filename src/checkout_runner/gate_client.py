"""The runner's side of driver/gate_driver.ts: one gate process, killable."""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from typing import Any

DRIVER = Path(__file__).resolve().parent / "driver" / "gate_driver.ts"


class GateError(RuntimeError):
    pass


class GateProcess:
    def __init__(self, gate_dir: Path, node: str = "node") -> None:
        self.gate_dir = gate_dir
        self.node = node
        self.proc: subprocess.Popen[str] | None = None
        self.start()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [self.node, "--disable-warning=ExperimentalWarning", str(DRIVER), str(self.gate_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )

    def call(self, op: str, **args: Any) -> Any:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"op": op, "args": args}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise GateError(f"gate driver ended during {op} (exit {self.proc.wait()})")
        reply = json.loads(line)
        if not reply["ok"]:
            raise GateError(f"{op}: {reply['error']}")
        return reply["result"]

    def kill(self) -> None:
        """SIGKILL: the gate gets no chance to close its database."""
        assert self.proc
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.call("close")
            finally:
                assert self.proc.stdin
                self.proc.stdin.close()
                self.proc.wait(timeout=30)
