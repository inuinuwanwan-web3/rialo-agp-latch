import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.gateway import McpGateway, McpResponseError, McpTransportError
from agp_race_agent.monitor import TrackMonitor
from tests.test_agent import available_track
from tests.test_monitor import settings
from tests.test_mcp_recovery import gateway_with_mock_transport


def test_two_completed_races_return_to_monitor_and_do_not_repeat(tmp_path):
    a, b = available_track(id="a"), available_track(id="b")
    gateway = Mock()
    gateway.call.side_effect = [
        {"tracks": [a]}, {"run": None}, {"sigilBalance": {"creditRemainingUsd": 50}},
        {"tracks": [a]}, {"run": {"id": "run-a", "trackId": "a"}}, {"finished": True, "trackId": "a"},
        {"tracks": [a, b]}, {"run": None}, {"sigilBalance": {"creditRemainingUsd": 49}},
        {"tracks": [a, b]}, {"run": {"id": "run-b", "trackId": "b"}}, {"finished": True, "trackId": "b"},
        {"tracks": [a, b]},
    ]
    sleep = Mock(side_effect=[None, None, KeyboardInterrupt])
    result = TrackMonitor(settings(tmp_path), gateway).run(auto_join=True, sleep=sleep, notify=Mock())
    assert "監視を停止" in result
    assert [c.args[1] for c in gateway.call.call_args_list if c.args[0] == "start_track"] == [{"trackId": "a"}, {"trackId": "b"}]
    assert [c.args for c in sleep.call_args_list] == [(60.0,)] * 3


def test_24_hours_virtual_monitoring(tmp_path):
    gateway = Mock()
    gateway.call.return_value = {"tracks": [available_track()]}
    elapsed = 0
    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds
        if elapsed >= 86400:
            raise KeyboardInterrupt
    notify = Mock()
    TrackMonitor(settings(tmp_path), gateway).run(sleep=sleep, notify=notify)
    assert gateway.call.call_count == 1440
    assert notify.call_count == 1


@pytest.mark.parametrize("label", ["429", "rate limit", "throttle", "No valid session", "retry-after: 180"])
def test_read_errors_retry_with_delay_without_diagnostics(label):
    gateway = gateway_with_mock_transport()
    gateway.sleep = Mock()
    gateway.request.side_effect = [
        {"isError": True, "content": [{"type": "text", "text": label + "\nprivate-value"}]},
        {"structuredContent": {"tracks": []}},
    ]
    assert gateway.call("list_tracks") == {"tracks": []}
    assert gateway.request.call_count == 2
    assert gateway.sleep.call_args_list[0].args[0] >= (180 if "180" in label else 60)


@pytest.mark.parametrize("tool", ["start_track", "ask", "guess"])
@pytest.mark.parametrize("label", ["429", "rate limit", "throttled", "retry-after: 90", "No valid session"])
def test_write_errors_never_retry(tool, label):
    gateway = gateway_with_mock_transport()
    gateway.sleep = Mock()
    gateway.request.return_value = {"isError": True, "content": [{"type": "text", "text": label + " private-value"}], "structuredContent": {"run": {"id": "misleading"}}}
    with pytest.raises(McpResponseError) as error:
        gateway.call(tool)
    assert "private-value" not in str(error.value)
    gateway.request.assert_called_once()
    gateway.sleep.assert_not_called()


def test_retry_after_http_date():
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=300), usegmt=True)
    error = McpResponseError({"code": 429, "headers": {"Retry-After": date}})
    assert 298 <= error.retry_after <= 300


def test_list_spacing_includes_eligibility_recheck():
    gateway = gateway_with_mock_transport()
    gateway.sleep = Mock()
    gateway.request.return_value = {"structuredContent": {"tracks": []}}
    with patch("agp_race_agent.gateway.time.monotonic", return_value=100):
        gateway.call("list_tracks")
        gateway.call("list_tracks")
    gateway.sleep.assert_called_once_with(60)


def test_silent_pipe_times_out_without_network():
    reader, writer = os.pipe()
    gateway = McpGateway.__new__(McpGateway)
    gateway._buffer = b""
    with os.fdopen(reader) as stream:
        gateway.proc = Mock(stdout=stream)
        try:
            with pytest.raises(McpTransportError):
                gateway._readline(0)
        finally:
            os.close(writer)


def test_failed_credit_status_blocks_registration(tmp_path):
    from tests.test_write_safety import FakeWireGateway, writes
    config = settings(tmp_path)
    failed_balance = {'sigilBalance': {'creditUsedUsd': 0, 'creditRemainingUsd': 50,
                      'creditCapUsd': 50, 'balanceUsd': 0, 'creditStatus': 'failed'}}
    gateway = FakeWireGateway(config, replies={'sigil_balance': [failed_balance]})
    result = TrackMonitor(config, gateway).run(auto_join=True, notify=Mock(), sleep=Mock())
    assert 'blocked' in result
    assert writes(gateway) == []


def test_joined_but_unfinished_restart_stops_without_tools(tmp_path):
    import sqlite3
    config = settings(tmp_path)
    with sqlite3.connect(config.monitor_seen_file) as db:
        db.execute("CREATE TABLE participation (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        db.execute("INSERT INTO participation VALUES ('a', 'joined')")
    gateway = Mock()
    assert "結果未確認" in TrackMonitor(config, gateway).run(auto_join=True, sleep=Mock())
    gateway.call.assert_not_called()


def test_audit_does_not_save_solver_content_or_response_keys(tmp_path):
    from agp_race_agent.agent import RaceAgent
    from agp_race_agent.audit import AuditLog
    audit = AuditLog(tmp_path)
    gateway = Mock()
    gateway.call.return_value = {"answer": "yes", "private-response-key": "private-response-value"}
    RaceAgent(settings(tmp_path), gateway, audit).call("ask", {"question": "private-question"})
    assert "private-" not in audit.path.read_text()


def test_read_rate_limit_exhaustion_is_bounded():
    gateway = gateway_with_mock_transport()
    gateway.sleep = Mock()
    gateway.request.return_value = {"isError": True, "content": [{"type": "text", "text": "429 private-diagnostic"}]}
    with pytest.raises(McpResponseError):
        gateway.call("track_state")
    assert gateway.request.call_count == 3
    assert [c.args[0] for c in gateway.sleep.call_args_list] == [60, 120]


def test_explicitly_finished_existing_run_allows_next_race(tmp_path):
    from agp_race_agent.agent import RaceAgent
    from agp_race_agent.audit import AuditLog
    gateway = Mock()
    gateway.call.side_effect = [
        {"run": {"id": "old", "finished": True}}, {"tracks": [available_track()]},
        {"creditRemainingUsd": 1}, {"run": {"id": "new", "trackId": "test-track"}}, {"finished": True, "trackId": "test-track"},
    ]
    agent = RaceAgent(settings(tmp_path), gateway, AuditLog(tmp_path))
    agent.run(require_no_existing=True)
    assert agent.completed
    assert [c.args[0] for c in gateway.call.call_args_list].count("start_track") == 1


@pytest.mark.parametrize("field", ["remainingUsd", "spentUsd", "guessCostUsd"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_budget_never_sends_paid_action(tmp_path, field, value):
    from agp_race_agent.agent import RaceAgent
    from agp_race_agent.audit import AuditLog
    gateway = Mock()
    state = {"finished": False, "remainingUsd": 1, "spentUsd": 0, "guessCostUsd": 0.01}
    state[field] = value
    gateway.call.return_value = state
    agent = RaceAgent(settings(tmp_path), gateway, AuditLog(tmp_path))
    agent.solver = Mock()
    agent.solver.decide.return_value = {"guess": "test"}
    agent.solve()
    gateway.call.assert_called_once_with("track_state", None)


def test_blocked_send_has_deadline():
    reader, writer = os.pipe()
    gateway = McpGateway.__new__(McpGateway)
    with os.fdopen(writer, "w") as stream:
        gateway.proc = Mock(stdin=stream)
        try:
            with pytest.raises(McpTransportError):
                gateway._write({"test": True}, 0)
        finally:
            os.close(reader)


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
