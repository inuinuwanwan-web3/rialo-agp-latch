from pathlib import Path
from unittest.mock import Mock

from agp_race_agent.agent import RaceAgent
from agp_race_agent.audit import AuditLog
from agp_race_agent.config import Settings, load


def settings_with_limits(tmp_path: Path, *, max_usd: float = 1.0, questions: int = 12, guesses: int = 3) -> Settings:
    return Settings("cmd", [], [], {}, "direct", None, "2025-03-26", max_usd, questions, guesses, "solver", tmp_path)


def test_solver_flow_without_real_mcp(tmp_path: Path):
    settings = load(Path("config.toml"))

    gateway = Mock()
    gateway.call.side_effect = [
        {
            "finished": False,
            "remainingUsd": 1,
            "spentUsd": 0,
            "questionCostUsd": 0.005,
            "guessCostUsd": 0.005,
        },
        {"answer": "test clue"},
        {"correct": True},
        {"finished": True},
    ]

    solver = Mock()
    solver.decide.side_effect = [
        {"question": "What is the most important clue?", "confidence": 1.0},
        {"guess": "test-answer", "confidence": 1.0},
    ]
    audit = AuditLog(tmp_path)
    agent = RaceAgent(settings, gateway, audit)
    agent.solver = solver 
    result = agent.solve() 

    assert result == "レースを完走しました。"


def test_guess_limit_is_preserved_when_same_point_is_refetched(tmp_path: Path):
    gateway = Mock()
    gateway.call.side_effect = [
        {"finished": False, "remainingUsd": 1, "spentUsd": 0, "guessCostUsd": 0.01},
        {"correct": False},
        {"finished": False, "remainingUsd": 0.99, "spentUsd": 0.01, "guessCostUsd": 0.01},
        {"correct": False},
        {"finished": False, "remainingUsd": 0.98, "spentUsd": 0.02, "guessCostUsd": 0.01},
    ]
    audit = AuditLog(tmp_path)
    agent = RaceAgent(settings_with_limits(tmp_path, guesses=2), gateway, audit)
    agent.solver = Mock()
    agent.solver.decide.return_value = {"guess": "wrong"}

    assert agent.solve() == "ポイントごとの操作上限に達したため終了しました。"
    assert [call.args[0] for call in gateway.call.call_args_list].count("guess") == 2


def test_multiple_questions_cannot_exceed_server_remaining_budget(tmp_path: Path):
    gateway = Mock()
    gateway.call.side_effect = [
        {"finished": False, "remainingUsd": 0.05, "spentUsd": 0, "questionCostUsd": 0.03},
        {"answer": "clue"},
    ]
    audit = AuditLog(tmp_path)
    agent = RaceAgent(settings_with_limits(tmp_path), gateway, audit)
    agent.solver = Mock()
    agent.solver.decide.return_value = {"question": "question"}

    assert agent.solve() == "予算上限に達したため終了しました。"
    assert [call.args[0] for call in gateway.call.call_args_list].count("ask") == 1


def test_server_spent_and_local_spent_are_not_double_counted(tmp_path: Path):
    gateway = Mock()
    gateway.call.side_effect = [
        {"finished": False, "remainingUsd": 0.4, "spentUsd": 0.6, "guessCostUsd": 0.2},
        {"correct": False},
        {"finished": False, "remainingUsd": 0.2, "spentUsd": 0.8, "guessCostUsd": 0.1},
        {"correct": True},
        {"finished": True},
    ]
    audit = AuditLog(tmp_path)
    agent = RaceAgent(settings_with_limits(tmp_path), gateway, audit)
    agent.solver = Mock()
    agent.solver.decide.return_value = {"guess": "answer"}

    assert agent.solve() == "レースを完走しました。"
    assert [call.args[0] for call in gateway.call.call_args_list].count("guess") == 2


def test_missing_question_cost_stops_before_ask(tmp_path: Path):
    gateway = Mock()
    gateway.call.return_value = {"finished": False, "remainingUsd": 1, "spentUsd": 0}
    audit = AuditLog(tmp_path)
    agent = RaceAgent(settings_with_limits(tmp_path), gateway, audit)
    agent.solver = Mock()
    agent.solver.decide.return_value = {"question": "question"}

    assert agent.solve() == "質問費用を確認できないため終了しました。"
    assert "ask" not in [call.args[0] for call in gateway.call.call_args_list]
