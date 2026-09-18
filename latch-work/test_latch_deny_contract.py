"""Offline-only DENY contract; no real Latch client or AGP transport is created.

Exercises the existing RaceAgent.solve() exit path, not registration or live
policy enforcement. All doubles and audit records stay in memory.
"""

from collections import Counter
from copy import deepcopy
from pathlib import Path
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest


class FakeLatch:
    def __init__(self):
        self.requests = []

    def evaluate(self, payload):
        self.requests.append(deepcopy(payload))
        return "DENY"


class FakeModel:
    def __init__(self):
        self.calls = 0

    def decide(self, payload):
        self.calls += 1
        raise AssertionError("External model must not be called")


class FakeGateway:
    def __init__(self):
        self.calls = Counter()

    def call(self, name, arguments=None):
        self.calls[name] += 1
        raise AssertionError("No gateway operation is permitted in this test")


class FakeAudit:
    def __init__(self):
        self.events = []

    def write(self, event, **data):
        self.events.append((event, data))


class LatchCheckedSolver:
    """Test-local design prototype, not a production Latch integration."""

    def __init__(self, latch, model, error_type):
        self.latch, self.model, self.error_type = latch, model, error_type
        self.calls = 0

    def decide(self, state, history):
        self.calls += 1
        payload = {"state": state, "history": history}
        try:
            decision = self.latch.evaluate(payload)
        except Exception:
            raise self.error_type("Latch decision unavailable") from None
        if type(decision) is not str or decision != "ALLOW":
            raise self.error_type("Latch denied solver request")
        return self.model.decide(payload)


def test_latch_deny_stops_race_before_model_or_agp_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network, subprocess or database access forbidden")

    # Guards only affect this test process; no environment or safety gate changes.
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "agp-track-race-agent"))

    from agp_race_agent import agent as agent_module
    from agp_race_agent.solver import SolverError

    latch, model = FakeLatch(), FakeModel()
    gateway, audit = FakeGateway(), FakeAudit()
    solver = LatchCheckedSolver(latch, model, SolverError)
    # Replace only the Solver factory; RaceAgent and its budget checks stay real.
    monkeypatch.setattr(agent_module, "Solver", lambda command: solver)
    settings = SimpleNamespace(
        solver_command="offline-fake-only", max_usd=1.0,
        max_questions=2, max_guesses=2,
    )
    state = {
        "trackId": "synthetic-track", "started": True, "finished": False,
        "status": "running", "remainingUsd": 1.0, "spentUsd": 0.0,
        "questionCostUsd": 0.01, "guessCostUsd": 0.01,
    }
    race = agent_module.RaceAgent(settings, gateway, audit)
    race.track_id = state["trackId"]
    race.require_economics = True
    race.initial_state = deepcopy(state)

    result = race.solve()

    assert result == "Solverの安全な出力を取得できないため終了しました。"
    assert latch.requests == [{"state": state, "history": []}]
    assert solver.calls == 1
    assert audit.events == [("safe_exit", {"reason": "solver_failure"})]
    assert race.completed is False
    assert race.spent == 0.0
    assert model.calls == 0
    for operation in ("start_track", "ask", "guess", "finish"):
        assert gateway.calls[operation] == 0
    assert sum(gateway.calls.values()) == 0  # No subsequent read/poll either.

    # Also prove DENY maps to the actual SolverError with nonempty history.
    history = [{"question": "synthetic question", "answer": {"answer": "synthetic clue"}}]
    with pytest.raises(SolverError, match="Latch denied solver request"):
        solver.decide(state, history)
    assert latch.requests[-1] == {"state": state, "history": history}
    assert len(latch.requests) == 2  # Race exit plus separate error-mapping probe.
    assert model.calls == 0
    assert sum(gateway.calls.values()) == 0
