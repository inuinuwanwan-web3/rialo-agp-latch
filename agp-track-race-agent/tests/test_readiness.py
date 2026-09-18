from unittest.mock import Mock, patch

import pytest

from agp_race_agent.registration import registration_state
from agp_race_agent.read_recovery import ReadRecovery
from agp_race_agent.gateway import McpResponseError
from agp_race_agent.monitor import TrackMonitor
from tests.test_registration import lifecycle, prefix, seated
from tests.test_monitor import settings


READY = {"trackId": "test-track", "started": True, "finished": False, "remainingUsd": 0.6, "spentUsd": 0, "guessCostUsd": 0.003}
ZERO = {**READY, "remainingUsd": 0}


def test_tokens_delayed_then_solving(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated(), ZERO, {**READY, "started": False}, READY, {"correct": True}, {"finished": True, "trackId": "test-track"}])
    statuses = []
    reg.sleep.side_effect = lambda _: statuses.append(reg.status("test-track"))
    with patch("agp_race_agent.agent.Solver") as solver:
        solver.return_value.decide.side_effect = lambda *args: (statuses.append(reg.status("test-track")) or {"guess": "test"})
        assert reg.join("test-track") is None
    assert statuses == ["token_waiting", "token_waiting", "solving"]
    assert reg.status("test-track") == "completed"
    assert [c.args[0] for c in gateway.call.call_args_list].count("start_track") == 1
    db.close()


def test_token_timeout_is_bounded_and_never_resends(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated()] + [ZERO] * 11)
    assert "待機期限" in reg.join("test-track")
    assert reg.status("test-track") == "failed_terminal"
    assert reg.sleep.call_count == 10
    assert not any(c.args[0] in {"ask", "guess"} for c in gateway.call.call_args_list)
    db.close()


@pytest.mark.parametrize("status", ["seated", "registered_waiting", "token_waiting"])
def test_restart_readiness_states(tmp_path, status):
    reg, gateway, db = lifecycle(tmp_path, [])
    reg.set("test-track", status)
    db.close()
    reg, gateway, db = lifecycle(tmp_path, [])
    assert "blocked" in reg.recover()
    assert reg.status("test-track") == "blocked"
    gateway.call.assert_not_called()
    db.close()


def test_deadline_preserved_on_restart(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated(), ZERO])
    reg.sleep.side_effect = KeyboardInterrupt
    with patch("agp_race_agent.registration.time.time", return_value=1000):
        with pytest.raises(KeyboardInterrupt):
            reg.join("test-track")
    db.close()
    reg, gateway, db = lifecycle(tmp_path, [seated()])
    with patch("agp_race_agent.registration.time.time", return_value=1601):
        assert "blocked" in reg.recover()
    gateway.call.assert_not_called()
    db.close()


@pytest.mark.parametrize("error", [TimeoutError(), McpResponseError({"code": 429, "Retry-After": 180})])
def test_monitor_recovers_read_without_exit(tmp_path, error):
    gateway = Mock()
    gateway.call.side_effect = [error, {"tracks": []}]
    sleep = Mock(side_effect=[None, KeyboardInterrupt])
    result = TrackMonitor(settings(tmp_path), gateway).run(auto_join=True, sleep=sleep)
    assert "監視を停止" in result
    assert [c.args[0] for c in gateway.call.call_args_list] == ["list_tracks"] * 2
    assert sleep.call_args_list[0].args[0] >= 60


def test_recovery_never_retries_write():
    gateway, sleep = Mock(), Mock()
    gateway.call.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        ReadRecovery(gateway, sleep).call("start_track", {"trackId": "test"})
    gateway.call.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("response", [
    {"registration": {"trackId": "t", "status": "waiting"}},
    {"queue": {"trackId": "t", "status": "registered"}},
    {"trackId": "t", "status": "waitlisted"},
])
def test_waiting_shapes(response):
    assert registration_state(response, "t") == "registered_waiting"


@pytest.mark.parametrize("status", ["seated", "started", "running", "finished"])
def test_explicit_run_statuses(status):
    response = {"state": {"track": {"id": "t"}, "run": {"id": "r", "status": status}}}
    assert registration_state(response, "t") == ("completed" if status == "finished" else "seated")


@pytest.mark.parametrize("state", [{"remainingUsd": 50}, {**READY, "started": None}, {**READY, "trackId": "other"}])
def test_unknown_readiness_never_solves(tmp_path, state):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated(), state])
    assert reg.join("test-track") is not None
    assert not any(c.args[0] in {"ask", "guess"} for c in gateway.call.call_args_list)
    db.close()


def test_auth_error_is_not_retried():
    gateway, sleep = Mock(), Mock()
    gateway.call.side_effect = McpResponseError({"code": 401, "message": "private"})
    with pytest.raises(McpResponseError):
        ReadRecovery(gateway, sleep).call("my_race")
    gateway.call.assert_called_once()
    sleep.assert_not_called()


def test_read_failure_during_solving_preserves_solver_history(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated(), READY, {"correct": False}, TimeoutError(), READY, {"correct": True}, {"finished": True, "trackId": "test-track"}])
    reg.gateway = ReadRecovery(gateway, Mock())
    with patch("agp_race_agent.agent.Solver") as solver:
        solver.return_value.decide.side_effect = [{"guess": "one"}, {"guess": "two"}]
        assert reg.join("test-track") is None
        assert solver.return_value.decide.call_count == 2
    assert [c.args[0] for c in gateway.call.call_args_list].count("guess") == 2
    db.close()


def test_gateway_retry_after_cannot_exceed_token_deadline():
    from tests.test_mcp_recovery import gateway_with_mock_transport
    from agp_race_agent.read_recovery import ReadDeadline
    gateway = gateway_with_mock_transport()
    gateway.sleep = Mock()
    gateway.request.side_effect = McpResponseError({"code": 429, "Retry-After": 86400})
    recovery = ReadRecovery(gateway, Mock())
    recovery.deadline = 1600
    with patch("time.time", return_value=1000):
        with pytest.raises(ReadDeadline):
            recovery.call("track_state")
    gateway.request.assert_called_once()
    gateway.sleep.assert_not_called()


def test_unknown_phase_with_started_true_is_not_ready(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated(), {**READY, "phase": "unrecognized"}])
    assert "blocked" in reg.join("test-track")
    assert not any(c.args[0] in {"ask", "guess"} for c in gateway.call.call_args_list)
    db.close()


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
