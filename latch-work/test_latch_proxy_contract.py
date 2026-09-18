"""Fake proxy model requests only; no preflight, token, SDK or network."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from test_latch_allow_contract import offline, make_race, synthetic_state, FakeCompletionGateway
from test_latch_deny_contract import FakeGateway


def response(content='{"guess":"synthetic-answer"}', **choice_updates):
    from latch_proxy_solver import ProxyResponse
    choice = {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
    choice.update(choice_updates)
    return ProxyResponse(200, json.dumps({"choices": [choice]}))


def test_proxy_success_one_model_request_no_preflight(offline, monkeypatch):
    from latch_proxy_solver import BASE_URL, CHAT_PATH, LatchProxySolver, FakeProxyTransport
    state = synthetic_state()
    transport = FakeProxyTransport(response())
    solver = LatchProxySolver(model="synthetic-model", transport=transport)
    gateway = FakeCompletionGateway([], state)
    race, _ = make_race(monkeypatch, offline[0], solver, gateway, state)
    assert race.solve() == "レースを完走しました。"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["url"] == BASE_URL + CHAT_PATH == "https://onlatch.com/proxy/chat/completions"
    assert request["method"] == "POST" and request["timeout_seconds"] == 60.0
    assert request["json"]["model"] == "synthetic-model"
    assert request["json"]["stream"] is False
    assert json.loads(request["json"]["messages"][1]["content"]) == {"state": state, "history": []}
    assert set(request) == {"url", "method", "json", "timeout_seconds"}
    assert gateway.calls["guess"] == gateway.calls["track_state"] == 1
    assert all(gateway.calls[n] == 0 for n in ("start_track", "ask", "finish"))
    assert race.spent == 0.01


@pytest.mark.parametrize("case", [
    "403", "401", "429", "500", "503", "302", "timeout", "connection",
    "malformed-json", "missing-choices", "empty-choices", "multiple-choices",
    "bad-content", "both-keys", "empty-guess", "truncated", "tool-call", "refusal", "blocked",
])
def test_proxy_failure_stops_race_without_retry(offline, monkeypatch, case):
    from latch_proxy_solver import LatchProxySolver, FakeProxyTransport, ProxyResponse
    outcomes = {
        "malformed-json": ProxyResponse(200, "not-json"),
        "missing-choices": ProxyResponse(200, '{}'),
        "empty-choices": ProxyResponse(200, '{"choices":[]}'),
        "multiple-choices": ProxyResponse(200, '{"choices":[{},{}]}'),
        "bad-content": response("not-json"), "both-keys": response('{"question":"q","guess":"g"}'),
        "empty-guess": response('{"guess":""}'), "truncated": response(finish_reason="length"),
        "tool-call": response(message={"role":"assistant", "content":"{}", "tool_calls":[{}]}),
        "refusal": response(message={"role":"assistant", "content":"{}", "refusal":"synthetic refusal"}),
    }
    result = ProxyResponse(int(case), "synthetic-error-body") if case.isdigit() else outcomes.get(case, response())
    transport = None if case == "blocked" else FakeProxyTransport(result, fault=case if case in {"timeout", "connection"} else None)
    solver = LatchProxySolver(model="synthetic-model", transport=transport)
    gateway = FakeGateway()
    race, audit = make_race(monkeypatch, offline[0], solver, gateway, synthetic_state())
    assert race.solve() == "Solverの安全な出力を取得できないため終了しました。"
    if transport is not None:
        assert len(transport.requests) == 1
    assert all(gateway.calls[n] == 0 for n in ("start_track", "ask", "guess", "finish"))
    assert sum(gateway.calls.values()) == 0
    assert audit.events == [("safe_exit", {"reason": "solver_failure"})]
    assert race.spent == 0 and race.completed is False


@pytest.mark.parametrize("content", ['{"question":"q"}', '{"guess":"g"}', '{}', '{"question":null}', '{"guess":"wrong"}'])
def test_proxy_contract_matches_existing_solver(offline, monkeypatch, content):
    from agp_race_agent.solver import Solver, SolverError
    import agp_race_agent.solver as module
    from latch_proxy_solver import LatchProxySolver, FakeProxyTransport
    history = [{"guess":"wrong", "result":{"correct":False}}]
    proxy = LatchProxySolver(model="synthetic-model", transport=FakeProxyTransport(response(content)))
    monkeypatch.setattr(module.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=content)))
    existing = Solver("offline-fake")
    try:
        expected = existing.decide(synthetic_state(), history)
    except SolverError:
        with pytest.raises(SolverError, match="Proxy Solver failed closed"):
            proxy.decide(synthetic_state(), history)
    else:
        assert proxy.decide(synthetic_state(), history) == expected


def test_proxy_rejects_arbitrary_transport(offline):
    from latch_proxy_solver import LatchProxySolver
    transport = Mock()
    with pytest.raises(TypeError):
        LatchProxySolver(model="synthetic-model", transport=transport)
    transport.send.assert_not_called()
