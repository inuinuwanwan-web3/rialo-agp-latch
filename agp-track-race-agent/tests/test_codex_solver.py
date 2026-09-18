from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.agent import RaceAgent
from agp_race_agent.audit import AuditLog
from agp_race_agent.codex_solver import _safe_stderr_tail, decide
from agp_race_agent.solver import Solver, SolverError
from tests.test_solver_flow import settings_with_limits


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_codex_solver_uses_chatgpt_auth_isolation_and_schema() -> None:
    runs = Mock(side_effect=[completed("Logged in using ChatGPT\n"), completed('{"question":"Which era?","guess":null}\n')])
    payload = {"state": {"clue": "fictional"}, "history": [{"question": "Where?", "answer": "Japan"}]}

    with patch("agp_race_agent.codex_solver.subprocess.run", runs), patch.dict(
        "agp_race_agent.codex_solver.os.environ", {"OPENAI_API_KEY": "must-not-be-used"}, clear=True
    ):
        assert decide(payload) == {"question": "Which era?"}

    status_call, exec_call = runs.call_args_list
    assert status_call.args[0] == ["codex", "login", "status"]
    command = exec_call.args[0]
    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--output-schema" in command
    assert "OPENAI_API_KEY" not in exec_call.kwargs["env"]
    prompt = exec_call.kwargs["input"]
    assert '"state":{"clue":"fictional"}' in prompt
    assert '"history":[{"question":"Where?","answer":"Japan"}]' in prompt
    assert "Do not inspect files" in prompt


def test_codex_solver_accepts_chatgpt_status_on_stderr() -> None:
    runs = Mock(side_effect=[
        completed(stderr="Logged in using ChatGPT\n"),
        completed('{"question":null,"guess":"632"}\n'),
    ])

    with patch("agp_race_agent.codex_solver.subprocess.run", runs):
        assert decide({"state": {"clue": "fictional"}, "history": []}) == {"guess": "632"}


def test_authentication_failure_does_not_expose_status_output() -> None:
    with patch(
        "agp_race_agent.codex_solver.subprocess.run",
        return_value=completed(stderr="token=private-value\n"),
    ):
        with pytest.raises(RuntimeError) as caught:
            decide({"state": {"clue": "fictional"}, "history": []})

    assert str(caught.value) == "Codex is not logged in using ChatGPT"
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize(
    "output",
    [
        "not-json", '{}', '{"question":"q","guess":"g"}',
        '{"question":"","guess":null}', '{"question":null,"guess":null}',
        '{"question":"q"}', '{"question":"q","guess":""}',
    ],
)
def test_codex_solver_rejects_abnormal_output(output: str) -> None:
    with patch(
        "agp_race_agent.codex_solver.subprocess.run",
        side_effect=[completed("Logged in using ChatGPT\n"), completed(output)],
    ):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            decide({"state": {"clue": "fictional"}, "history": []})


def test_codex_failure_reports_sanitized_returncode_and_stderr_tail() -> None:
    error_text = "request failed Authorization: Bearer secret-token sk-private123 final detail"
    with patch(
        "agp_race_agent.codex_solver.subprocess.run",
        side_effect=[
            completed("Logged in using ChatGPT\n"),
            subprocess.CompletedProcess([], 7, "", error_text),
        ],
    ):
        with pytest.raises(RuntimeError) as caught:
            decide({"state": {"clue": "fictional"}, "history": []})

    diagnostic = str(caught.value)
    assert "returncode=7" in diagnostic
    assert "final detail" not in diagnostic
    assert "secret-token" not in diagnostic
    assert "sk-private123" not in diagnostic
    assert "<redacted>" in diagnostic


def test_stderr_diagnostic_is_limited_to_tail() -> None:
    diagnostic = _safe_stderr_tail("old-sensitive-context " + "x" * 2_100)
    assert "old-sensitive-context" not in diagnostic
    assert len(diagnostic) <= 2_000


def test_solver_timeout_becomes_safe_solver_error() -> None:
    solver = Solver("fake-solver")
    with patch("agp_race_agent.solver.subprocess.run", side_effect=subprocess.TimeoutExpired("fake-solver", 120)):
        with pytest.raises(SolverError):
            solver.decide({"clue": "fictional"}, [])


def test_repeated_incorrect_guess_is_rejected() -> None:
    solver = Solver("fake-solver")
    history = [{"guess": "wrong answer", "result": {"correct": False}}]
    with patch(
        "agp_race_agent.solver.subprocess.run",
        return_value=completed('{"guess":"wrong answer"}'),
    ):
        with pytest.raises(SolverError):
            solver.decide({"clue": "fictional"}, history)


def test_agent_passes_incorrect_guess_to_next_solver_decision(tmp_path: Path) -> None:
    gateway = Mock()
    gateway.call.side_effect = [
        {"finished": False, "remainingUsd": 1, "spentUsd": 0, "guessCostUsd": 0.01},
        {"correct": False},
        {"finished": False, "remainingUsd": 0.99, "spentUsd": 0.01, "guessCostUsd": 0.01},
    ]
    seen_history: list[list[dict[str, object]]] = []

    def choose(_state: dict[str, object], history: list[dict[str, object]]) -> dict[str, str]:
        seen_history.append(list(history))
        if not history:
            return {"guess": "first wrong answer"}
        raise SolverError("stop test before another guess")

    agent = RaceAgent(settings_with_limits(tmp_path), gateway, AuditLog(tmp_path))
    agent.solver = Mock()
    agent.solver.decide.side_effect = choose

    assert agent.solve() == "Solverの安全な出力を取得できないため終了しました。"
    assert seen_history[1] == [{"guess": "first wrong answer", "result": {"correct": False}}]
    assert [call.args[0] for call in gateway.call.call_args_list].count("guess") == 1


def test_agent_safely_stops_on_abnormal_solver_output(tmp_path: Path) -> None:
    gateway = Mock()
    gateway.call.return_value = {"finished": False, "remainingUsd": 1, "spentUsd": 0}
    agent = RaceAgent(settings_with_limits(tmp_path), gateway, AuditLog(tmp_path))
    agent.solver = Mock()
    agent.solver.decide.side_effect = SolverError("invalid output")

    assert agent.solve() == "Solverの安全な出力を取得できないため終了しました。"
    assert all(call.args[0] not in {"ask", "guess"} for call in gateway.call.call_args_list)
