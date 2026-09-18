"""Exercise the adapter with fake Latch and fake model; real AGP validators."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_latch_allow_contract import offline, make_race, synthetic_state, FakeCompletionGateway
from test_latch_deny_contract import FakeGateway


def setup_adapter(offline, monkeypatch, response=None, fault=None, output='{"guess":"synthetic-answer"}', blocked=False):
    from agp_race_agent.solver import Solver
    from latch_adapter import FakeTransport, LatchGuardedSolver, LocalResponse
    import agp_race_agent.solver as module

    calls = []
    def model(command, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(stdout=output)
    monkeypatch.setattr(module.subprocess, "run", model)
    transport = None if blocked else FakeTransport(response, fault=fault)
    adapter = LatchGuardedSolver(Solver("offline-fake-model"), transport)
    return adapter, transport, calls


def test_adapter_allow_uses_existing_solver_once(offline, monkeypatch):
    from latch_adapter import LocalResponse
    adapter, transport, calls = setup_adapter(offline, monkeypatch, LocalResponse(200, '{"decision":"allow"}'))
    state = synthetic_state()
    history = [{"question": "synthetic", "answer": {"answer": "clue"}}]
    assert adapter.decide(state, history) == {"guess": "synthetic-answer"}
    assert calls == [{"state": state, "history": history}]
    assert transport.requests == [(calls[0], 5.0)]


def test_adapter_allow_race_completion(offline, monkeypatch):
    from latch_adapter import LocalResponse
    adapter, transport, calls = setup_adapter(offline, monkeypatch, LocalResponse(200, '{"decision":"allow"}'))
    state = synthetic_state()
    gateway = FakeCompletionGateway([], state)
    race, _ = make_race(monkeypatch, offline[0], adapter, gateway, state)
    assert race.solve() == "レースを完走しました。"
    assert len(calls) == len(transport.requests) == 1
    assert gateway.calls["guess"] == 1  # In-memory only.
    assert race.spent == 0.01


@pytest.mark.parametrize("case", [
    "deny", "connection", "timeout", "http-401", "http-429", "http-500",
    "redirect", "malformed-json", "empty", "missing-decision", "wrong-type",
    "unknown-decision", "uppercase", "extra-key", "duplicate-key", "bad-envelope",
    "blocked-default",
])
def test_adapter_failure_stops_race_without_retry(offline, monkeypatch, case):
    from latch_adapter import LocalResponse
    bodies = {
        "deny": '{"decision":"deny"}', "malformed-json": "not-json", "empty": "",
        "missing-decision": '{}', "wrong-type": '{"decision":true}',
        "unknown-decision": '{"decision":"pending"}', "uppercase": '{"decision":"ALLOW"}',
        "extra-key": '{"decision":"allow","extra":1}',
        "duplicate-key": '{"decision":"deny","decision":"allow"}',
    }
    status = int(case[5:]) if case.startswith("http-") else 302 if case == "redirect" else 200
    response = None if case == "bad-envelope" else LocalResponse(status, bodies.get(case, '{"decision":"allow"}'))
    adapter, transport, calls = setup_adapter(
        offline, monkeypatch, response, fault=case if case in {"connection", "timeout"} else None,
        blocked=case == "blocked-default",
    )
    gateway = FakeGateway()
    race, audit = make_race(monkeypatch, offline[0], adapter, gateway, synthetic_state())
    assert race.solve() == "Solverの安全な出力を取得できないため終了しました。"
    assert calls == []
    if transport is not None:
        assert len(transport.requests) == 1
    for name in ("start_track", "ask", "guess", "finish"):
        assert gateway.calls[name] == 0
    assert sum(gateway.calls.values()) == 0
    assert audit.events == [("safe_exit", {"reason": "solver_failure"})]
    assert race.completed is False


@pytest.mark.parametrize("output", ["", "not-json", '{}', '{"question":"q","guess":"g"}', '{"guess":""}'])
def test_adapter_preserves_solver_output_rejection(offline, monkeypatch, output):
    from latch_adapter import LocalResponse
    adapter, transport, calls = setup_adapter(offline, monkeypatch, LocalResponse(200, '{"decision":"allow"}'), output=output)
    gateway = FakeGateway()
    race, audit = make_race(monkeypatch, offline[0], adapter, gateway, synthetic_state())
    assert race.solve() == "Solverの安全な出力を取得できないため終了しました。"
    assert len(calls) == len(transport.requests) == 1
    assert sum(gateway.calls.values()) == 0
    assert audit.events == [("safe_exit", {"reason": "solver_failure"})]


def test_adapter_rejects_arbitrary_transport_before_any_call(offline):
    from agp_race_agent.solver import Solver
    from latch_adapter import LatchGuardedSolver
    transport = Mock()
    with pytest.raises(TypeError):
        LatchGuardedSolver(Solver("unused"), transport)
    transport.exchange.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_adapter_rejects_invalid_timeout_budget(offline, timeout):
    from agp_race_agent.solver import Solver
    from latch_adapter import LatchGuardedSolver
    with pytest.raises(ValueError):
        LatchGuardedSolver(Solver("unused"), timeout_seconds=timeout)
