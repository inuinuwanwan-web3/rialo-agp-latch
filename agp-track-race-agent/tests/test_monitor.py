from dataclasses import replace
from unittest.mock import Mock, patch
import sqlite3

import pytest

from agp_race_agent.cli import main
from agp_race_agent.config import load, preflight
from agp_race_agent.monitor import TrackMonitor
from tests.test_agent import available_track, local_settings


def settings(tmp_path, **updates):
    return replace(local_settings(tmp_path), monitor_seen_file=tmp_path / "seen.sqlite3", **updates)


def run_polls(config, responses):
    gateway = Mock()
    gateway.call.side_effect = responses
    notify = Mock()
    sleep = Mock(side_effect=[None] * (len(responses) - 1) + [KeyboardInterrupt()])
    result = TrackMonitor(config, gateway).run(sleep=sleep, notify=notify)
    assert all(call.args == ("list_tracks",) for call in gateway.call.call_args_list)
    return result, gateway, notify, sleep


def test_detects_new_candidates_once_and_preserves_ids_across_restart(tmp_path):
    config = settings(tmp_path)
    a, b = available_track(id="a"), available_track(id="b")
    responses = [
        {"tracks": [a, a, available_track(id="b", invitedOnly=True)]},
        {"tracks": []}, {"tracks": [a, b]},
    ]
    result, gateway, notify, sleep = run_polls(config, responses)
    assert result == "監視を停止しました。"
    assert gateway.call.call_count == 3
    assert [c.args[0] for c in notify.call_args_list] == [
        '参加候補Trackを検知しました。', '参加候補Trackを検知しました。',
    ]
    assert [c.args for c in sleep.call_args_list] == [(60.0,)] * 3
    _, _, notify, _ = run_polls(config, [{"tracks": [a, b]}])
    notify.assert_not_called()
    assert config.monitor_seen_file.stat().st_mode & 0o777 == 0o600


def test_notification_contains_only_id_and_persistence_precedes_output(tmp_path):
    config = settings(tmp_path)
    gateway = Mock()
    gateway.call.return_value = {"tracks": [available_track(id="test-id", name="private-test-value")],
                                 "extra": "private-test-value"}

    def notify(message):
        assert "private-test-value" not in message
        with sqlite3.connect(config.monitor_seen_file) as database:
            assert database.execute("SELECT id FROM seen").fetchall() == [("test-id",)]
        raise KeyboardInterrupt

    TrackMonitor(config, gateway).run(notify=notify)
    assert b"private-test-value" not in config.monitor_seen_file.read_bytes()


@pytest.mark.parametrize("response", [None, {}, {"tracks": None}, {"tracks": {}}])
def test_bad_response_stops_without_polling_again(tmp_path, response):
    gateway, sleep, notify = Mock(), Mock(), Mock()
    gateway.call.return_value = response
    result = TrackMonitor(settings(tmp_path), gateway).run(sleep=sleep, notify=notify)
    assert "停止" in result
    gateway.call.assert_called_once_with("list_tracks")
    sleep.assert_not_called()
    notify.assert_not_called()


def test_corrupt_history_stops_before_mcp_without_reset(tmp_path):
    config = settings(tmp_path)
    config.monitor_seen_file.write_bytes(b"invalid database")
    gateway = Mock()
    assert "停止" in TrackMonitor(config, gateway).run()
    gateway.call.assert_not_called()
    assert config.monitor_seen_file.read_bytes() == b"invalid database"


def test_storage_failure_stops_before_notification(tmp_path):
    gateway, notify = Mock(), Mock()
    with patch("agp_race_agent.monitor.sqlite3.connect", side_effect=sqlite3.OperationalError("private-test-value")):
        result = TrackMonitor(settings(tmp_path), gateway).run(notify=notify)
    gateway.call.assert_not_called()
    notify.assert_not_called()
    assert "private-test-value" not in result


def test_communication_failure_stops_without_unbounded_outer_retry(tmp_path):
    gateway = Mock()
    gateway.call.side_effect = RuntimeError("private-test-value")
    sleep = Mock()
    result = TrackMonitor(settings(tmp_path), gateway).run(sleep=sleep)
    gateway.call.assert_called_once_with("list_tracks")
    sleep.assert_not_called()
    assert "private-test-value" not in result


@pytest.mark.parametrize("interval", [0, -1, 9, float("inf"), float("nan")])
def test_invalid_interval_rejected_without_mcp(tmp_path, interval):
    config = settings(tmp_path, monitor_interval_seconds=interval)
    assert preflight(config, monitor=True)
    gateway = Mock()
    assert "不正" in TrackMonitor(config, gateway).run()
    gateway.call.assert_not_called()


def test_monitor_config_load_and_no_solver_requirement(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[mcp]\ncommand="unused"\n[monitor]\ninterval_seconds=60\nseen_file="state/seen.db"\n')
    config = load(path)
    assert config.monitor_interval_seconds == 60
    assert config.monitor_seen_file == tmp_path / "state/seen.db"
    assert preflight(config, monitor=True) == []
    _, _, _, sleep = run_polls(config, [{"tracks": []}])
    sleep.assert_called_once_with(60)


@pytest.mark.parametrize("flags", [["--watch"], ["--watch", "--execute", "--dry-run"]])
def test_watch_dry_run_never_constructs_gateway_or_monitor(tmp_path, flags):
    with patch("sys.argv", ["agent", *flags]), patch("agp_race_agent.cli.load", return_value=settings(tmp_path)), \
         patch("agp_race_agent.cli.McpGateway") as gateway, patch("agp_race_agent.cli.TrackMonitor") as monitor:
        assert main() == 0
    gateway.assert_not_called()
    monitor.assert_not_called()


def test_watch_execute_uses_only_monitor_and_closes_gateway(tmp_path):
    with patch("sys.argv", ["agent", "--watch", "--execute"]), \
         patch("agp_race_agent.cli.load", return_value=settings(tmp_path)), \
         patch("agp_race_agent.cli.McpGateway") as gateway, \
         patch("agp_race_agent.cli.TrackMonitor") as monitor, \
         patch("agp_race_agent.cli.RaceAgent") as race, patch("agp_race_agent.cli.AuditLog") as audit:
        assert main() == 0
    monitor.return_value.run.assert_called_once()
    gateway.return_value.close.assert_called_once()
    race.assert_not_called()
    audit.assert_not_called()


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
