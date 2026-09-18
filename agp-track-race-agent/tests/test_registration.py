import sqlite3
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.registration import Registration, sigil_balance
from agp_race_agent.monitor import TrackMonitor
from agp_race_agent.gateway import McpResponseError
from tests.test_monitor import settings
from tests.test_agent import available_track


BALANCE = {"sigilBalance": {"creditStatus": "failed", "creditRemainingUsd": 0,
    "creditUsedUsd": 0, "creditCapUsd": 50, "balanceUsd": 0}}


def lifecycle(tmp_path, replies):
    config = settings(tmp_path)
    gateway = Mock()
    gateway.call.side_effect = replies
    db = sqlite3.connect(config.monitor_seen_file)
    return Registration(config, gateway, db, Mock(), Mock()), gateway, db


def prefix():
    return [{"run": None}, BALANCE, {"tracks": [available_track()]}]


def seated(track="test-track"):
    return {"run": {"id": "run", "trackId": track}}


def test_real_nested_balance_only():
    assert sigil_balance({**BALANCE, "balanceUsd": 999}) == BALANCE["sigilBalance"]
    assert sigil_balance({"balanceUsd": 999}) is None


@pytest.mark.parametrize("remaining", [0, 50])
def test_zero_balance_failed_blocks_registration(tmp_path, remaining):
    from tests.test_write_safety import FakeWireGateway, writes
    config = settings(tmp_path)
    response = {'sigilBalance': {**BALANCE['sigilBalance'], 'creditRemainingUsd': remaining}}
    gateway = FakeWireGateway(config, replies={'sigil_balance': [response]})
    db = sqlite3.connect(config.monitor_seen_file)
    reg = Registration(config, gateway, db, Mock(), Mock())
    assert 'blocked' in reg.join('test-track')
    assert reg.balance['creditStatus'] == 'failed'
    assert writes(gateway) == []
    db.close()


@pytest.mark.parametrize("error", [TimeoutError("private-error"),
    McpResponseError({"code": 429, "Retry-After": 180}),
    {"isError": True, "error": {"code": 429, "retry-after": 180}},
    {}, {"run": None}])
def test_uncertain_registration_blocks_without_reconciliation(tmp_path, error):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [error])
    assert "blocked" in reg.join("test-track")
    assert reg.status("test-track") == "blocked"
    assert [c.args[0] for c in gateway.call.call_args_list].count("start_track") == 1
    assert [c.args[0] for c in gateway.call.call_args_list].count("my_race") == 1
    reg.sleep.assert_not_called()
    gateway.reset_mock()
    assert "blocked" in reg.reconcile("test-track")
    gateway.call.assert_not_called()
    db.close()


def test_explicit_rejection_is_terminal_without_paid_calls(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [{"isError": True, "error": {"code": "FORBIDDEN", "message": "private-value"}}])
    assert "blocked" in reg.join("test-track")
    assert reg.status("test-track") == "blocked"
    gateway.reset_mock()
    assert reg.join("test-track") is None
    gateway.call.assert_not_called()
    db.close()
    assert b"private-value" not in settings(tmp_path).monitor_seen_file.read_bytes()


def test_unknown_registration_survives_restart_as_blocked(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [TimeoutError()])
    assert "blocked" in reg.join("test-track")
    assert reg.status("test-track") == "blocked"
    db.close()
    reg, gateway, db = lifecycle(tmp_path, [seated()])
    assert "blocked" in reg.recover()
    assert reg.status("test-track") == "blocked"
    gateway.call.assert_not_called()
    db.close()


def test_other_track_never_resolves_unknown_attempt(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [TimeoutError()] + [seated("other")] * 3)
    assert "blocked" in reg.join("test-track")
    assert reg.status("test-track") == "blocked"
    assert not any(c.args[0] in {"track_state", "ask", "guess"} for c in gateway.call.call_args_list)
    db.close()


def test_waiting_only_registration_is_unconfirmed_and_blocked(tmp_path):
    waiting = {"registration": {"trackId": "test-track", "status": "waiting"}, "run": None}
    reg, gateway, db = lifecycle(tmp_path, prefix() + [waiting])
    assert "blocked" in reg.join("test-track")
    assert reg.status("test-track") == "blocked"
    assert not any(c.args[0] in {"track_state", "ask", "guess"} for c in gateway.call.call_args_list)
    db.close()


@pytest.mark.parametrize("state", [{"finished": False}, {"finished": False, "remainingUsd": 0, "spentUsd": 0}])
def test_postseat_missing_or_zero_budget_never_spends(tmp_path, state):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated(), state])
    assert reg.join("test-track") is not None
    assert reg.status("test-track") == "blocked"
    assert not any(c.args[0] in {"ask", "guess"} for c in gateway.call.call_args_list)
    db.close()


def test_unsent_read_failure_is_retryable_on_restart(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, [TimeoutError()])
    assert "登録前" in reg.join("test-track")
    assert reg.status("test-track") == "failed_retryable"
    db.close()
    reg, gateway, db = lifecycle(tmp_path, prefix() + [seated(), {"finished": True, "trackId": "test-track"}])
    assert reg.join("test-track") is None
    assert [c.args[0] for c in gateway.call.call_args_list].count("start_track") == 1
    db.close()


def test_detected_crash_restart_is_not_excluded(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, [])
    reg.set("test-track", "detected")
    db.close()
    gateway.call.side_effect = [{"tracks": [available_track()]}] + prefix() + [seated(), {"finished": True, "trackId": "test-track"}]
    TrackMonitor(settings(tmp_path), gateway).run(auto_join=True, sleep=Mock(side_effect=KeyboardInterrupt), notify=Mock())
    assert [c.args[0] for c in gateway.call.call_args_list].count("start_track") == 1


def test_persist_attempt_before_send_even_keyboard_interrupt(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        reg.join("test-track")
    assert reg.status("test-track") == "registration_attempted"
    db.close()
    reg, gateway, db = lifecycle(tmp_path, [{"run": None}] * 3)
    assert "blocked" in reg.recover()
    assert all(c.args[0] == "my_race" for c in gateway.call.call_args_list)
    db.close()


def test_solving_crash_never_repeats_paid_action(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, [])
    reg.set("test-track", "solving")
    assert "結果未確認" in reg.recover()
    gateway.call.assert_not_called()
    db.close()


def test_unknown_rejection_does_not_assume_no_side_effect(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [{"isError": True, "error": {"code": "INTERNAL_ERROR"}}])
    assert "blocked" in reg.join("test-track")
    assert reg.status("test-track") == "blocked"
    assert [c.args[0] for c in gateway.call.call_args_list].count("start_track") == 1
    db.close()


def test_waiting_restart_is_blocked_without_calls(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, [])
    reg.set("test-track", "registered_waiting")
    db.close()
    reg, gateway, db = lifecycle(tmp_path, [seated()])
    assert "blocked" in reg.recover()
    gateway.call.assert_not_called()
    db.close()


def test_429_write_failure_stays_blocked_after_restart(tmp_path):
    reg, gateway, db = lifecycle(tmp_path, prefix() + [McpResponseError({"code": 429, "Retry-After": 180})])
    assert "blocked" in reg.join("test-track")
    reg.sleep.assert_not_called()
    db.close()
    reg, gateway, db = lifecycle(tmp_path, [seated()])
    assert "blocked" in reg.recover()
    gateway.call.assert_not_called()
    reg.sleep.assert_not_called()
    db.close()


def test_post_timeout_read_reconnects_transport_without_resending_write():
    from tests.test_mcp_recovery import gateway_with_mock_transport
    from agp_race_agent.gateway import McpTransportError
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = [McpTransportError("private"), {"structuredContent": seated()}]
    with pytest.raises(McpTransportError):
        gateway.call("start_track", {"trackId": "test-track"})
    assert gateway.call("my_race") == seated()
    gateway._connect.assert_called_once()
    assert [c.args[1]["name"] for c in gateway.request.call_args_list] == ["start_track", "my_race"]


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
