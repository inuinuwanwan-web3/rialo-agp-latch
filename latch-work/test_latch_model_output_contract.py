"""Feed fake model stdout through the unmodified AGP Solver validator."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from test_latch_allow_contract import FakeAllowLatch, make_race, offline, synthetic_state
from test_latch_deny_contract import FakeGateway, LatchCheckedSolver


BAD_OUTPUTS = [
    pytest.param("", id="empty-stdout"),
    pytest.param("not-json", id="invalid-json"),
    pytest.param("{}", id="missing-decision"),
    pytest.param('{"question":"q","guess":"g"}', id="both-keys"),
    pytest.param('{"question":""}', id="empty-question"),
    pytest.param('{"guess":""}', id="empty-guess"),
    pytest.param('{"question":"  "}', id="whitespace-question"),
    pytest.param('{"guess":"  "}', id="whitespace-guess"),
    pytest.param('{"question":null}', id="null-question"),
    pytest.param('{"guess":42}', id="number-guess"),
    pytest.param('{"question":true}', id="boolean-question"),
    pytest.param('{"guess":[]}', id="array-guess"),
    pytest.param('{"question":{}}', id="object-question"),
    pytest.param('{"guess":"g","extra":1}', id="extra-key"),
    pytest.param("[]", id="top-level-array"),
    pytest.param("null", id="top-level-null"),
    pytest.param('"answer"', id="top-level-string"),
]


class FakeRawModel:
    def __init__(self, output, events):
        self.output, self.events, self.requests = output, events, []

    def run(self, command, *, input, text, capture_output, check, timeout):
        assert command == ["offline-fake-model"]
        assert text is capture_output is check is True
        assert timeout == 120
        self.events.append("model")
        self.requests.append(json.loads(input))
        return SimpleNamespace(stdout=self.output)


class ValidatedModel:
    def __init__(self, solver):
        self.solver = solver
        self.errors = []

    def decide(self, payload):
        try:
            return self.solver.decide(payload["state"], payload["history"])
        except Exception as error:
            self.errors.append(error)
            raise


@pytest.mark.parametrize("output", BAD_OUTPUTS)
def test_allow_invalid_model_output_stops_real_race(offline, monkeypatch, output):
    agent_module, error_type = offline
    from agp_race_agent import solver as solver_module

    events, state = [], synthetic_state()
    latch = FakeAllowLatch(events)
    raw_model = FakeRawModel(output, events)
    # Only the subprocess result is fake; JSON parsing and all validation are real.
    monkeypatch.setattr(solver_module.subprocess, "run", raw_model.run)
    validator = ValidatedModel(solver_module.Solver("offline-fake-model"))
    solver = LatchCheckedSolver(latch, validator, error_type)
    gateway = FakeGateway()
    race, audit = make_race(monkeypatch, agent_module, solver, gateway, state)

    assert race.solve() == "Solverの安全な出力を取得できないため終了しました。"
    expected = [{"state": state, "history": []}]
    assert latch.requests == raw_model.requests == expected
    assert events == ["allow", "model"]
    assert solver.calls == 1
    assert len(validator.errors) == 1
    assert isinstance(validator.errors[0], error_type)
    for name in ("start_track", "ask", "guess", "finish"):
        assert gateway.calls[name] == 0
    assert sum(gateway.calls.values()) == 0
    assert audit.events == [("safe_exit", {"reason": "solver_failure"})]
    assert race.completed is False
    assert race.spent == 0.0


def test_allow_repeated_wrong_guess_rejected_at_solver_boundary(offline, monkeypatch):
    _, error_type = offline
    from agp_race_agent import solver as solver_module

    events, state = [], synthetic_state()
    history = [{"guess": "wrong", "result": {"correct": False}}]
    latch = FakeAllowLatch(events)
    raw_model = FakeRawModel('{"guess":"wrong"}', events)
    monkeypatch.setattr(solver_module.subprocess, "run", raw_model.run)
    solver = LatchCheckedSolver(
        latch, ValidatedModel(solver_module.Solver("offline-fake-model")), error_type,
    )
    # RaceAgent starts with empty history; this history-dependent rule is tested
    # directly, without inventing prior AGP actions or changing RaceAgent.
    with pytest.raises(error_type, match="Solver repeated an incorrect guess"):
        solver.decide(state, deepcopy(history))
    assert latch.requests == raw_model.requests == [{"state": state, "history": history}]
    assert events == ["allow", "model"]
