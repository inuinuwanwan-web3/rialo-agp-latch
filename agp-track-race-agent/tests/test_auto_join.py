import sqlite3
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.monitor import TrackMonitor
from tests.test_monitor import settings, run_polls
from tests.test_agent import available_track


def responses(registration=None):
    return [{"tracks": [available_track()]}, {"run": None},
            {"sigilBalance": {"creditRemainingUsd": 1}}, {"tracks": [available_track()]},
            registration if registration is not None else {"run": {"id": "fake-run", "trackId": "test-track"}},
            {"finished": True, "trackId": "test-track"}]


def run_auto(config, replies):
    gateway = Mock()
    gateway.call.side_effect = replies
    with patch("agp_race_agent.agent.Solver") as solver:
        result = TrackMonitor(config, gateway).run(auto_join=True, notify=Mock(), sleep=Mock(side_effect=KeyboardInterrupt))
    return result, gateway, solver


def test_auto_join_persists_before_send_and_uses_race_flow(tmp_path):
    config = settings(tmp_path)
    replies = iter(responses())
    gateway = Mock()

    def call(tool, *args):
        if tool in {"start_track", "track_state"}:
            with sqlite3.connect(config.monitor_seen_file) as db:
                assert db.execute("SELECT status FROM registration_state").fetchone()[0] == (
                    "registration_attempted" if tool == "start_track" else "token_waiting")
        return next(replies)

    gateway.call.side_effect = call
    result = TrackMonitor(config, gateway).run(auto_join=True, notify=Mock(), sleep=Mock(side_effect=KeyboardInterrupt))
    assert result == "監視を停止しました。"
    assert [c.args[0] for c in gateway.call.call_args_list] == [
        "list_tracks", "my_race", "sigil_balance", "list_tracks", "start_track", "track_state"]
    # Restart sees the same ID but never registers again.
    gateway.call.side_effect = [{"tracks": [available_track()]}]
    gateway.reset_mock()
    TrackMonitor(config, gateway).run(auto_join=True, notify=Mock(), sleep=Mock(side_effect=KeyboardInterrupt))
    gateway.call.assert_called_once_with("list_tracks")


@pytest.mark.parametrize("registration", [{}, {"run": None}, {"run": {"id": ""}}, RuntimeError("private-test-value")])
def test_unknown_registration_stops_and_blocks_restart(tmp_path, registration):
    config = settings(tmp_path)
    result, gateway, solver = run_auto(config, responses(registration))
    assert "停止" in result
    assert [c.args[0] for c in gateway.call.call_args_list].count("start_track") == 1
    assert "track_state" not in [c.args[0] for c in gateway.call.call_args_list]
    solver.return_value.decide.assert_not_called()
    gateway.reset_mock()
    gateway.call.side_effect = None
    gateway.call.return_value = {"run": None}
    assert "blocked" in TrackMonitor(config, gateway).run(auto_join=True, sleep=Mock())
    gateway.call.assert_not_called()
    assert "private-test-value" not in result


@pytest.mark.parametrize("race", [{"run": {"id": "existing"}}, {}, None, {"run": False}])
def test_existing_or_unknown_race_blocks_registration(tmp_path, race):
    result, gateway, solver = run_auto(settings(tmp_path), [{"tracks": [available_track()]}, race])
    assert "既存レース" in result
    assert [c.args[0] for c in gateway.call.call_args_list] == ["list_tracks", "my_race"]


def test_fresh_eligibility_failure_blocks_start(tmp_path):
    result, gateway, _ = run_auto(settings(tmp_path), [
        {"tracks": [available_track()]}, {"run": None},
        {"sigilBalance": {}}, {"tracks": [available_track(racerCount=50)]},
    ])
    assert "停止" in result
    assert gateway.call.call_count == 4


@pytest.mark.parametrize("balance", [{}, {"balanceUsd": 0}, {"creditRemainingUsd": float("nan")}, None])
def test_unavailable_preseat_budget_blocks_wire(tmp_path, balance):
    from tests.test_write_safety import FakeWireGateway, writes
    config = settings(tmp_path)
    gateway = FakeWireGateway(config, replies={'sigil_balance': [balance]})
    result = TrackMonitor(config, gateway).run(auto_join=True, notify=Mock(), sleep=Mock())
    assert 'blocked' in result
    assert writes(gateway) == []


def test_monitor_only_detection_does_not_exclude_auto_join(tmp_path):
    config = settings(tmp_path)
    run_polls(config, [{"tracks": [available_track()]}])
    gateway = Mock()
    gateway.call.side_effect = responses()
    TrackMonitor(config, gateway).run(auto_join=True, notify=Mock(), sleep=Mock(side_effect=KeyboardInterrupt))
    assert any(c.args[0] == "start_track" for c in gateway.call.call_args_list)


def test_parallel_auto_monitor_cannot_join(tmp_path):
    config = settings(tmp_path)
    other_gateway = Mock()
    gateway = Mock()

    def call(*args):
        assert "停止" in TrackMonitor(config, other_gateway).run(auto_join=True)
        return {"tracks": []}

    gateway.call.side_effect = call
    TrackMonitor(config, gateway).run(auto_join=True, sleep=Mock(side_effect=KeyboardInterrupt))
    other_gateway.call.assert_not_called()


@pytest.mark.parametrize("tool", ["ask", "guess"])
def test_auto_join_keeps_paid_error_safe_stop(tmp_path, tool):
    replies = responses()[:-1] + [
        {"trackId": "test-track", "started": True, "finished": False, "remainingUsd": 1, "spentUsd": 0, "questionCostUsd": 0.01, "guessCostUsd": 0.01},
        RuntimeError("private-test-value"),
    ]
    gateway = Mock()
    gateway.call.side_effect = replies
    with patch("agp_race_agent.agent.Solver") as solver:
        solver.return_value.decide.return_value = {"question" if tool == "ask" else "guess": "test"}
        result = TrackMonitor(settings(tmp_path), gateway).run(auto_join=True, notify=Mock())
    assert "MCP操作の応答を確認できない" in result
    assert [c.args[0] for c in gateway.call.call_args_list].count(tool) == 1


def test_participation_write_failure_prevents_start(tmp_path):
    config = settings(tmp_path)
    with sqlite3.connect(config.monitor_seen_file) as db:
        db.execute("CREATE TABLE registration_state (id TEXT PRIMARY KEY, status TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0)")
        db.execute("CREATE TRIGGER refuse_insert BEFORE INSERT ON registration_state BEGIN SELECT RAISE(ABORT, 'test'); END")
    result, gateway, _ = run_auto(config, responses())
    assert "停止" in result
    assert "start_track" not in [c.args[0] for c in gateway.call.call_args_list]


def test_joined_write_failure_prevents_state_and_leaves_unknown(tmp_path):
    config = settings(tmp_path)
    with sqlite3.connect(config.monitor_seen_file) as db:
        db.execute("CREATE TABLE registration_state (id TEXT PRIMARY KEY, status TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0)")
        db.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON registration_state WHEN NEW.status = 'seated' BEGIN SELECT RAISE(ABORT, 'test'); END")
    result, gateway, _ = run_auto(config, responses())
    assert "停止" in result
    assert "track_state" not in [c.args[0] for c in gateway.call.call_args_list]
    with sqlite3.connect(config.monitor_seen_file) as db:
        assert db.execute("SELECT status FROM registration_state").fetchone() == ("registration_attempted",)


def test_auto_join_cli_defaults_to_dry_run(tmp_path):
    from agp_race_agent.cli import main
    with patch("sys.argv", ["agent", "--watch", "--auto-join"]), \
         patch("agp_race_agent.cli.load", return_value=settings(tmp_path)), \
         patch("agp_race_agent.cli.McpGateway") as gateway:
        assert main() == 0
    gateway.assert_not_called()


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
