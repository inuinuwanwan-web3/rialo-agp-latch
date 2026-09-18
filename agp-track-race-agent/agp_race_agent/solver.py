from __future__ import annotations

import json
import shlex
import subprocess
import os
from typing import Any
from .config import reject_secret_argv


class SolverError(RuntimeError):
    """A solver failure that must stop the race before another paid action."""


class Solver:
    def __init__(self, command: str) -> None:
        self.command = shlex.split(command)
        reject_secret_argv(self.command, os.environ)

    def decide(self, state: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
        payload = json.dumps({"state": state, "history": history}, ensure_ascii=False)
        try:
            result = subprocess.run(
                self.command, input=payload, text=True, capture_output=True,
                check=True, timeout=120,
            )
            decision = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise SolverError("Solver execution failed") from None
        if not isinstance(decision, dict) or set(decision) not in ({"question"}, {"guess"}):
            raise SolverError("Solver must return exactly one question or guess")
        value = next(iter(decision.values()))
        if not isinstance(value, str) or not value.strip():
            raise SolverError("Solver returned an empty or invalid decision")
        if "guess" in decision and any(
            item.get("guess") == decision["guess"]
            and isinstance(item.get("result"), dict)
            and item["result"].get("correct") is False
            for item in history
        ):
            raise SolverError("Solver repeated an incorrect guess")
        return decision
