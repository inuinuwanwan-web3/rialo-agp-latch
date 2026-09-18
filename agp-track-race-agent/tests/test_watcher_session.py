import json
import os
import signal
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from agp_race_agent.track_watcher import (
    TrackWatcher, WatchOptions, SnapshotStore, manual_session, show_status,
)
from tests.test_track_watcher import FakeReader, snapshots


def test_ctrl_c_finishes_current_poll_and_reopens_without_duplicate(tmp_path):
    reader = FakeReader()
    original = reader.track_state
    def stop_during_poll(track):
        os.kill(os.getpid(), signal.SIGINT)
        return original(track)
    reader.track_state = stop_during_poll
    reader.close = Mock()
    watcher = TrackWatcher(lambda: reader, WatchOptions(tmp_path, .01, .08))
    previous = signal.getsignal(signal.SIGINT)
    assert manual_session(watcher, hours=24) == 0
    assert signal.getsignal(signal.SIGINT) == previous
    assert reader.calls.count('list_tracks') == 1
    reader.close.assert_called_once()
    store = SnapshotStore(tmp_path)
    try:
        assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert len(snapshots(store)) == 1
        reader.track_state = original
        assert watcher.poll(reader, store) == []
    finally:
        store.close()


def test_deadline_stops_before_next_poll(tmp_path):
    watcher = SimpleNamespace(options=WatchOptions(tmp_path))
    clock = [0]
    def run(**kwargs):
        clock[0] = 3599
        assert kwargs['should_stop']() is False
        clock[0] = 3600
        assert kwargs['should_stop']() is True
        return 0
    watcher.run = run
    with patch('agp_race_agent.track_watcher.time.monotonic', side_effect=lambda: clock[0]):
        assert manual_session(watcher, hours=1) == 0


def test_status_is_local_and_reports_session_lock(tmp_path, capsys):
    watcher = SimpleNamespace(options=WatchOptions(tmp_path))
    def run(**kwargs):
        show_status(tmp_path)
        assert json.loads(capsys.readouterr().out.splitlines()[-1])['watcher_active'] is True
        return 0
    watcher.run = run
    manual_session(watcher, hours=24)
    show_status(tmp_path)
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['watcher_active'] is False


def test_stop_during_backoff_does_not_reconnect(tmp_path):
    reader = FakeReader()
    reader.failures.append(TimeoutError())
    factory = Mock(return_value=reader)
    watcher = TrackWatcher(factory, WatchOptions(tmp_path, 30, 300))
    actual_run = watcher.run
    def run(**kwargs):
        kwargs['notify'] = lambda message: os.kill(os.getpid(), signal.SIGTERM)
        return actual_run(**kwargs)
    watcher.run = run
    assert manual_session(watcher, hours=24) == 0
    factory.assert_called_once()


def test_database_closes_even_if_transport_close_fails(tmp_path):
    reader = FakeReader()
    reader.close = Mock(side_effect=RuntimeError('test close failure'))
    watcher = TrackWatcher(lambda: reader, WatchOptions(tmp_path))
    with patch('agp_race_agent.track_watcher.SnapshotStore') as store:
        store.return_value.lock = 0
        with patch.object(watcher, 'poll', return_value=[]):
            with pytest.raises(RuntimeError):
                watcher.run(max_polls=1)
        store.return_value.close.assert_called_once()
