"""Fake-only ALLOW and failed-decision contracts; never creates real clients."""

from collections import Counter
from copy import deepcopy
from pathlib import Path
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from test_latch_deny_contract import FakeAudit, FakeGateway, FakeModel, LatchCheckedSolver


class FakeAllowLatch:
    def __init__(self, events):
        self.events, self.requests = events, []

    def evaluate(self, payload):
        self.events.append("allow")
        self.requests.append(deepcopy(payload))
        return "ALLOW"


class FakeAnswerModel:
    def __init__(self, events):
        self.events, self.requests = events, []

    def decide(self, payload):
        self.events.append("model")
        self.requests.append(deepcopy(payload))
        return {"guess": "synthetic-answer"}


class FakeCompletionGateway:
    def __init__(self, events, state):
        self.events, self.state = events, state
        self.calls, self.arguments = Counter(), []

    def call(self, name, arguments=None):
        self.calls[name] += 1
        self.arguments.append((name, deepcopy(arguments)))
        self.events.append(name)
        if name == "guess" and self.calls[name] == 1:
            return {"correct": True}
        if name == "track_state" and self.calls[name] == 1:
            return {**self.state, "finished": True, "status": "completed"}
        raise AssertionError("Unexpected FakeGateway call")


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network, subprocess or database access forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "agp-track-race-agent"))
    from agp_race_agent import agent
    from agp_race_agent.solver import SolverError
    return agent, SolverError


def synthetic_state():
    return {
        "trackId": "synthetic-track", "started": True, "finished": False,
        "status": "running", "remainingUsd": 1.0, "spentUsd": 0.0,
        "questionCostUsd": 0.01, "guessCostUsd": 0.01,
    }


def make_race(monkeypatch, agent_module, solver, gateway, state):
    monkeypatch.setattr(agent_module, "Solver", lambda command: solver)
    settings = SimpleNamespace(
        solver_command="offline-fake-only", max_usd=1.0,
        max_questions=2, max_guesses=2,
    )
    audit = FakeAudit()
    race = agent_module.RaceAgent(settings, gateway, audit)
    race.track_id = state["trackId"]
    race.require_economics = True
    race.initial_state = deepcopy(state)
    return race, audit


def test_allow_calls_model_once_and_race_consumes_guess(offline, monkeypatch):
    agent_module, error_type = offline
    events, state = [], synthetic_state()
    latch, model = FakeAllowLatch(events), FakeAnswerModel(events)
    solver = LatchCheckedSolver(latch, model, error_type)
    gateway = FakeCompletionGateway(events, state)
    race, audit = make_race(monkeypatch, agent_module, solver, gateway, state)

    assert race.solve() == "レースを完走しました。"
    expected = [{"state": state, "history": []}]
    assert latch.requests == expected  # Exactly one decision, exact input.
    assert model.requests == expected  # Exactly one model call, exact input.
    assert solver.calls == 1
    assert events == ["allow", "model", "guess", "track_state"]
    assert gateway.arguments == [("guess", {"guess": "synthetic-answer"}), ("track_state", None)]
    assert gateway.calls == Counter(guess=1, track_state=1)
    for name in ("start_track", "ask", "finish"):
        assert gateway.calls[name] == 0
    assert race.completed is True
    assert race.spent == 0.01  # Existing AGP cost accounting remains active.
    assert not any(event == "safe_exit" for event, _ in audit.events)


@pytest.mark.parametrize("outcome", ["exception", None, "", "allow", True, {"decision": "allow"}],
                         ids=["exception", "undecidable", "empty", "wrong-case", "boolean", "object"])
def test_failed_latch_decision_is_closed(offline, monkeypatch, outcome):
    agent_module, error_type = offline

    class FakeFailedLatch:
        def __init__(self):
            self.calls = 0

        def evaluate(self, payload):
            self.calls += 1
            if outcome == "exception":
                raise RuntimeError("Synthetic decision failure")
            return outcome

    latch, model, gateway = FakeFailedLatch(), FakeModel(), FakeGateway()
    solver = LatchCheckedSolver(latch, model, error_type)
    race, audit = make_race(monkeypatch, agent_module, solver, gateway, synthetic_state())
    assert race.solve() == "Solverの安全な出力を取得できないため終了しました。"
    assert latch.calls == solver.calls == 1
    assert model.calls == 0
    assert sum(gateway.calls.values()) == 0
    assert audit.events == [("safe_exit", {"reason": "solver_failure"})]
    assert race.completed is False
    assert race.spent == 0.0
