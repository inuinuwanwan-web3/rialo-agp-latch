import copy
import json
import multiprocessing
import os
import sqlite3
from collections import deque
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.gateway import McpResponseError, McpGateway
from agp_race_agent.observation_gateway import ObservationReader, ObservationTransport
from agp_race_agent.track_watcher import (
    Redactor, SnapshotStore, TrackWatcher, WatchOptions, main,
)


class FakeReader:
    def __init__(self, tracks=None):
        self.tracks = tracks if tracks is not None else [{'id': 'old', 'phase': 'unknown', 'over': False}]
        self.state = {'future_field': 1}
        self.race = {'run': None}
        self.balance = {'sigilBalance': {'creditStatus': 'failed'}}
        self.calls = []
        self.failures = deque()

    def list_tracks(self):
        self.calls.append('list_tracks')
        if self.failures:
            raise self.failures.popleft()
        return {'tracks': copy.deepcopy(self.tracks)}

    def my_race(self):
        self.calls.append('my_race')
        return copy.deepcopy(self.race)

    def sigil_balance(self):
        self.calls.append('sigil_balance')
        return copy.deepcopy(self.balance)

    def track_state(self, track_id):
        self.calls.append('track_state')
        return copy.deepcopy(self.state)

    def close(self):
        pass


def snapshots(store):
    return [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM snapshots ORDER BY id')]


@pytest.fixture
def observation(tmp_path):
    reader = FakeReader()
    watcher = TrackWatcher(lambda: reader, WatchOptions(tmp_path, .01, .08))
    store = SnapshotStore(tmp_path)
    yield reader, watcher, store
    store.close()


def test_baseline_new_track_and_deduplication(observation):
    reader, watcher, store = observation
    assert watcher.poll(reader, store) == ['BASELINE_TRACK']  # A
    reader.tracks.append({'id': 'new'})
    assert watcher.poll(reader, store) == ['NEW_TRACK_DETECTED']  # B
    assert watcher.poll(reader, store) == []  # C
    rows = snapshots(store)
    assert len(rows) == 2
    assert rows[-1]['trackId'] == 'new'
    assert rows[-1]['auto_join'] == 'BLOCKED'


@pytest.mark.parametrize('field,value', [
    ('phase', 'future-agp-phase'), ('status', 'future-status'), ('over', True),
    ('racerCount', 4), ('maxRacers', 9), ('prioritySlots', 2),
    ('priorityTaken', 1), ('queuedCount', 1), ('teamOnly', True),
    ('invitedOnly', True), ('verification', {'unknown': True}),
    ('startsAt', '2099-01-01'), ('registrationClosesAt', '2099-01-02'),
    ('greenFlagFutureField', 'unknown-time'), ('brand_new_schema', {'v': [1, 2]}),
])
def test_every_field_change_including_unknown_is_preserved(observation, field, value):
    reader, watcher, store = observation
    watcher.poll(reader, store)
    reader.tracks[0][field] = value
    assert watcher.poll(reader, store) == ['SNAPSHOT_CHANGED']
    row = snapshots(store)[-1]
    assert row['list_tracks']['tracks'][0][field] == value
    assert '/list_tracks/' + field in row['changed_fields']
    assert row['auto_join'] == 'BLOCKED'


def test_arbitrary_state_change_removal_and_absent_fields(observation):
    reader, watcher, store = observation
    watcher.poll(reader, store)
    reader.state = {'new': {'a': 1}}
    watcher.poll(reader, store)
    row = snapshots(store)[-1]
    assert row['track_state'] == reader.state
    assert '/track_state/future_field' in row['changed_fields']
    assert '/track_state/new' in row['changed_fields']
    assert 'status' not in row['list_tracks']['tracks'][0]


@pytest.mark.parametrize('error', [TimeoutError(), ConnectionResetError(), McpResponseError({'code': 429})])
def test_backoff_no_write_and_success_reset(tmp_path, error):
    reader = FakeReader()
    reader.failures.extend([error, error])
    watcher = TrackWatcher(lambda: reader, WatchOptions(tmp_path, .01, .08))
    sleep, notify = Mock(), Mock()
    assert watcher.run(sleep=sleep, notify=notify, max_polls=4) == 0
    assert [c.args[0] for c in sleep.call_args_list] == [.01, .02, .01]
    assert set(reader.calls) <= {'list_tracks', 'my_race', 'sigil_balance', 'track_state'}
    assert notify.call_args_list[0].args == ('READ_ERROR_BACKOFF',)


def test_retry_after_honored_and_startup_retried(tmp_path):
    error = McpResponseError({'code': 429, 'retryAfter': 2})
    reader = FakeReader()
    factory = Mock(side_effect=[error, reader])
    sleep = Mock()
    assert TrackWatcher(factory, WatchOptions(tmp_path, .01, .08)).run(sleep=sleep, notify=Mock(), max_polls=2) == 0
    sleep.assert_called_once_with(2)


def test_restart_recovers_last_snapshot_and_new_event(tmp_path):
    reader = FakeReader([])
    options = WatchOptions(tmp_path, .01, .08)
    first = TrackWatcher(lambda: reader, options)
    store = SnapshotStore(tmp_path)
    first.poll(reader, store)
    reader.tracks = [{'id': 'new', 'phase': 'unknown'}]
    assert first.poll(reader, store) == ['NEW_TRACK_DETECTED']
    store.close()
    again = SnapshotStore(tmp_path)
    try:
        assert TrackWatcher(lambda: reader, options).poll(reader, again) == []
        assert len(snapshots(again)) == 1
    finally:
        again.close()


def _concurrent_poll(path, barrier, results):
    reader = FakeReader([{'id': 'new'}])
    store = SnapshotStore(path)
    try:
        watcher = TrackWatcher(lambda: reader, WatchOptions(path, .01, .08))
        barrier.wait(timeout=10)
        results.put(watcher.poll(reader, store))
    finally:
        store.close()


def test_two_processes_no_corruption_or_duplicate_events(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save([])  # Initial baseline already observed.
    store.close()
    ctx = multiprocessing.get_context('fork')
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_concurrent_poll, args=(tmp_path, barrier, queue)) for _ in range(2)]
    try:
        for p in processes:
            p.start()
        events = [queue.get(timeout=15) for _ in processes]
        for p in processes:
            p.join(timeout=5)
            assert p.exitcode == 0
        assert sum(e.count('NEW_TRACK_DETECTED') for e in events) == 1
        with sqlite3.connect(tmp_path / 'observations.sqlite3') as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 1
        restarted = ctx.Process(target=_concurrent_poll, args=(tmp_path, ctx.Barrier(1), queue))
        processes.append(restarted)
        restarted.start()
        assert queue.get(timeout=15) == []
        restarted.join(timeout=5)
        assert restarted.exitcode == 0
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join()
        queue.close()


def test_secrets_redacted_before_any_persistence(observation):
    reader, watcher, store = observation
    watcher.redact = Redactor(['unlabelled-sensitive-value'])
    reader.state = {'token': 'token-value', 'nested': {'Authorization': 'Bearer private-value'},
                    'unknown_field': 'unlabelled-sensitive-value',
                    'text': 'Bearer another-value', 'serialized': '{"secret":"embedded-value"}'}
    reader.tracks[0]['password'] = 'password-value'
    watcher.poll(reader, store)
    row = snapshots(store)[0]
    text = json.dumps(row)
    for secret in ('token-value', 'private-value', 'unlabelled-sensitive-value', 'another-value', 'embedded-value', 'password-value'):
        assert secret not in text
        assert secret.encode() not in store.path.read_bytes()
    assert '<redacted>' in text
    assert os.stat(store.path).st_mode & 0o777 == 0o600


@pytest.mark.parametrize('name', ['start_track', 'ask', 'guess', 'practice_guess', 'practice_ask', 'bet', 'unknown'])
def test_no_write_can_cross_transport_boundary(name):
    transport = ObservationTransport.__new__(ObservationTransport)
    with patch.object(McpGateway, '_write') as wire:
        with pytest.raises(RuntimeError):
            transport._write({'method': 'tools/call', 'params': {'name': name, 'arguments': {}}}, 0)
        wire.assert_not_called()
    with pytest.raises(RuntimeError):
        transport.request('tools/call', {'name': name, 'arguments': {}})
    assert not hasattr(ObservationReader, name)
    assert not hasattr(ObservationReader, 'call')


def test_read_methods_use_exact_allowed_arguments():
    with patch('agp_race_agent.observation_gateway.ObservationTransport') as transport:
        reader = ObservationReader(object())
        reader.list_tracks()
        reader.my_race()
        reader.sigil_balance()
        reader.track_state('t')
        assert [c.args for c in transport.return_value.call.call_args_list] == [
            ('list_tracks',), ('my_race',), ('sigil_balance',), ('track_state', {'trackId': 't'})]


def test_partial_read_failure_preserves_new_track_and_backoff(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save([])
    store.close()
    reader = FakeReader([{'id': 'new'}])
    reader.track_state = Mock(side_effect=TimeoutError('secret-must-not-escape'))
    notify = Mock()
    assert TrackWatcher(lambda: reader, WatchOptions(tmp_path, .01, .08)).run(notify=notify, max_polls=1) == 1
    assert [c.args[0] for c in notify.call_args_list] == ['NEW_TRACK_DETECTED', 'READ_ERROR_BACKOFF']
    store = SnapshotStore(tmp_path)
    try:
        row = snapshots(store)[0]
        assert row['track_state'] is None
        assert row['read_errors'] == {'track_state/new': 'READ_ERROR'}
        assert 'secret-must-not-escape' not in json.dumps(row)
    finally:
        store.close()


def test_invalid_duplicate_list_does_not_initialize(observation):
    reader, watcher, store = observation
    reader.tracks = [{'id': 'same'}, {'id': 'same'}]
    with pytest.raises(ValueError):
        watcher.poll(reader, store)
    assert snapshots(store) == []
    assert store.db.execute('SELECT count(*) FROM metadata').fetchone()[0] == 0


def test_configurable_interval_and_safe_default_cli(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[observations]\ninterval_seconds = 12\nbackoff_max_seconds = 90\n')
    assert WatchOptions.load(config).interval == 12
    config.write_text('')
    assert WatchOptions.load(config).interval == 30
    with patch('agp_race_agent.track_watcher.load'), patch('agp_race_agent.track_watcher.credential_values', return_value=[]), patch('agp_race_agent.track_watcher.ObservationReader') as factory:
        assert main(['--config', str(config)]) == 0
        factory.assert_not_called()
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('interval,maximum', [(0, 30), (-1, 30), (float('nan'), 30), (30, 1), (1, float('inf'))])
def test_invalid_poll_configuration_is_rejected(tmp_path, interval, maximum):
    with pytest.raises(ValueError):
        WatchOptions(tmp_path, interval, maximum)


def test_backoff_cap(tmp_path):
    reader = FakeReader()
    reader.failures.extend(TimeoutError() for _ in range(6))
    sleep = Mock()
    assert TrackWatcher(lambda: reader, WatchOptions(tmp_path, .01, .04)).run(
        sleep=sleep, notify=Mock(), max_polls=6) == 1
    assert [c.args[0] for c in sleep.call_args_list] == [.01, .02, .04, .04, .04]


def test_event_snapshot_and_known_set_rollback_together(observation):
    reader, watcher, store = observation
    # Simulate disk/storage failure on the latest-state update after history insert.
    store.db.execute("CREATE TRIGGER fail_latest BEFORE INSERT ON latest BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.Error):
        watcher.poll(reader, store)
    assert snapshots(store) == []
    assert store.db.execute('SELECT count(*) FROM latest').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM metadata').fetchone()[0] == 0


def test_my_race_failure_preserves_list_and_skips_remaining_reads(tmp_path):
    reader = FakeReader()
    reader.my_race = Mock(side_effect=TimeoutError())
    watcher = TrackWatcher(lambda: reader, WatchOptions(tmp_path, .01, .08))
    assert watcher.run(notify=Mock(), max_polls=1) == 1
    store = SnapshotStore(tmp_path)
    try:
        row = snapshots(store)[0]
        assert row['list_tracks']['tracks'][0]['id'] == 'old'
        assert row['my_race'] is None
        assert row['sigil_balance'] is None
        assert row['read_errors']['sigil_balance'] == 'NOT_REQUESTED_AFTER_READ_ERROR'
        assert reader.calls == ['list_tracks']
    finally:
        store.close()
