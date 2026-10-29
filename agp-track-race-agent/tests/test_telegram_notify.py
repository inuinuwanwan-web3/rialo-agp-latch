"""Offline Telegram checks; no AGP imports or network requests."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock
import pytest

spec = importlib.util.spec_from_file_location('telegram_notify', Path(__file__).parents[1] / 'scripts/telegram_notify.py')
notifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notifier)


def test_only_new_events_read_without_db_changes(tmp_path):
    path = tmp_path / 'observations.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE snapshots (id INTEGER, event TEXT)')
        db.executemany('INSERT INTO snapshots VALUES (?, ?)', [
            (1, 'NEW_TRACK_DETECTED'), (2, 'BASELINE_TRACK'),
            (3, 'SNAPSHOT_CHANGED'), (4, 'NEW_TRACK_DETECTED')])
    before = path.read_bytes()
    assert notifier.read_events(path, 1) == [(4,)]
    assert path.read_bytes() == before


def test_private_storage(tmp_path):
    path = tmp_path / 'credentials.json'
    notifier.save(path, {'token': 'dummy'})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {'token': 'dummy'}


def test_single_test_attempt_uses_shared_send(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, 'PRIVATE', tmp_path)
    notifier.save(tmp_path / 'credentials.json', {'token': 'dummy', 'chat_id': 123})
    send = Mock()
    monkeypatch.setattr(notifier, 'send', send)
    monkeypatch.setattr(notifier.sys, 'argv', ['telegram_notify.py', 'test'])
    notifier.main()
    notifier.main()
    assert send.call_count == 1


def test_uncertain_test_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, 'PRIVATE', tmp_path)
    notifier.save(tmp_path / 'credentials.json', {'token': 'dummy', 'chat_id': 123})
    send = Mock(side_effect=RuntimeError('transport failure'))
    monkeypatch.setattr(notifier, 'send', send)
    monkeypatch.setattr(notifier.sys, 'argv', ['telegram_notify.py', 'test'])
    try:
        notifier.main()
    except RuntimeError:
        pass
    notifier.main()
    assert send.call_count == 1


def safe_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, 'PRIVATE', tmp_path)
    database = tmp_path / 'observations.sqlite3'
    monkeypatch.setattr(notifier, 'DATABASE', database)
    notifier.save(tmp_path / 'credentials.json', {'token': 'synthetic-token', 'chat_id': 123})
    notifier.save(tmp_path / 'cursor.json', 7)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE snapshots (id INTEGER, event TEXT)')
        db.execute("INSERT INTO snapshots VALUES (8, 'NEW_TRACK_DETECTED')")
    monkeypatch.setattr(notifier.http.client, 'HTTPSConnection', Mock(side_effect=AssertionError('Network forbidden')))
    return database


def test_safe_once_preserves_production_and_uses_existing_send(tmp_path, monkeypatch):
    database = safe_fixture(tmp_path, monkeypatch)
    cursor = (tmp_path / 'cursor.json').read_bytes()
    credentials = (tmp_path / 'credentials.json').read_bytes()
    def accepted(config, message):
        assert config == {'token': 'synthetic-token', 'chat_id': 123}
        assert message == 'AGP Watch TEST: Telegram notification path OK'
        with sqlite3.connect(database) as db:
            assert db.execute('SELECT status FROM telegram_test_once').fetchone() == ('attempted',)
    send = Mock(side_effect=accepted)
    monkeypatch.setattr(notifier, 'send', send)
    monkeypatch.setattr(notifier.sys, 'argv', ['telegram_notify.py', 'test-once'])
    notifier.main()
    notifier.main()
    assert send.call_count == 1
    assert (tmp_path / 'cursor.json').read_bytes() == cursor
    assert (tmp_path / 'credentials.json').read_bytes() == credentials
    assert not (tmp_path / 'notifier.lock').exists()
    assert notifier.read_events(database, 7) == [(8,)]
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT * FROM snapshots').fetchall() == [(8, 'NEW_TRACK_DETECTED')]
        assert db.execute('SELECT event,status FROM telegram_test_once').fetchall() == [('TELEGRAM_TEST', 'accepted')]


def test_safe_failure_never_retries_or_exposes_error(tmp_path, monkeypatch, capsys):
    import pytest
    safe_fixture(tmp_path, monkeypatch)
    send = Mock(side_effect=RuntimeError('synthetic-sensitive-error'))
    monkeypatch.setattr(notifier, 'send', send)
    with pytest.raises(RuntimeError, match='delivery unconfirmed') as error:
        notifier.test_once()
    assert 'synthetic-sensitive-error' not in str(error.value)
    notifier.test_once()
    assert send.call_count == 1
    captured = capsys.readouterr()
    assert 'synthetic-sensitive-error' not in captured.out + captured.err


def test_safe_crash_after_claim_never_retries(tmp_path, monkeypatch):
    import pytest
    safe_fixture(tmp_path, monkeypatch)
    send = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(notifier, 'send', send)
    with pytest.raises(KeyboardInterrupt):
        notifier.test_once()
    notifier.test_once()
    assert send.call_count == 1


def test_safe_concurrent_invocations_dispatch_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    safe_fixture(tmp_path, monkeypatch)
    send = Mock()
    monkeypatch.setattr(notifier, 'send', send)
    start = threading.Barrier(2)
    def run():
        start.wait()
        notifier.test_once()
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(run) for _ in range(2)]
        for task in tasks:
            task.result()
    assert send.call_count == 1


def test_safe_missing_database_fails_without_dispatch(tmp_path, monkeypatch):
    import pytest
    database = safe_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(notifier, 'DATABASE', tmp_path / 'missing.sqlite3')
    send = Mock()
    monkeypatch.setattr(notifier, 'send', send)
    with pytest.raises(sqlite3.OperationalError):
        notifier.test_once()
    send.assert_not_called()
    assert not (tmp_path / 'missing.sqlite3').exists()


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    safe_fixture(tmp_path, monkeypatch)
    store = notifier.DeliveryStore()
    yield store
    store.close()


def delivery(store):
    return store.db.execute('SELECT event_id,status,attempts,reason FROM deliveries ORDER BY event_id').fetchall()


def test_new_track_through_watcher_outbox_and_mocked_telegram(tmp_path, monkeypatch):
    from agp_race_agent.track_watcher import SnapshotStore, TrackWatcher, WatchOptions
    from tests.test_track_watcher import FakeReader
    private = tmp_path / 'private'
    private.mkdir()
    monkeypatch.setattr(notifier, 'PRIVATE', private)
    database = tmp_path / 'observations' / 'observations.sqlite3'
    monkeypatch.setattr(notifier, 'DATABASE', database)
    reader = FakeReader()
    watcher = TrackWatcher(lambda: reader, WatchOptions(database.parent))
    snapshots = SnapshotStore(database.parent)
    try:
        assert watcher.poll(reader, snapshots) == ['BASELINE_TRACK']
        store = notifier.DeliveryStore()
        send = Mock()
        monkeypatch.setattr(notifier, 'send', send)
        try:
            notifier.relay_once({}, store, clock=lambda: 100)
            send.assert_not_called()
            reader.tracks.append({'id': 'new'})
            assert watcher.poll(reader, snapshots) == ['NEW_TRACK_DETECTED']
            before = database.read_bytes()
            notifier.relay_once({}, store, clock=lambda: 101)
            notifier.relay_once({}, store, clock=lambda: 102)
            assert database.read_bytes() == before
            assert delivery(store) == [(2, 'sent', 1, '')]
            send.assert_called_once()
            assert 'イベント: 2' in send.call_args.args[1]
            assert 'AUTO-JOIN: BLOCKED' in send.call_args.args[1]
        finally:
            store.close()
    finally:
        snapshots.close()


def test_safe_retry_waits_and_survives_reopen(outbox, monkeypatch):
    clock = [100]
    send = Mock(side_effect=[notifier.SafeRetry('connect_failed'), {}])
    monkeypatch.setattr(notifier, 'send', send)
    notifier.relay_once({}, outbox, clock=lambda: clock[0])
    assert delivery(outbox) == [(8, 'retry', 1, 'connect_failed')]
    reopened = notifier.DeliveryStore()
    try:
        reopened.recover(101)
        clock[0] = 104
        notifier.relay_once({}, reopened, clock=lambda: clock[0])
        assert send.call_count == 1
        clock[0] = 105
        notifier.relay_once({}, reopened, clock=lambda: clock[0])
        notifier.relay_once({}, reopened, clock=lambda: 200)
        assert send.call_count == 2
        assert delivery(reopened) == [(8, 'sent', 2, '')]
        assert reopened.db.execute('SELECT outcome FROM attempts ORDER BY id').fetchall() == [('retry',), ('sent',)]
    finally:
        reopened.close()


def test_rate_limit_respects_retry_after(outbox, monkeypatch):
    send = Mock(side_effect=notifier.SafeRetry('rate_limit', 600))
    monkeypatch.setattr(notifier, 'send', send)
    notifier.relay_once({}, outbox, clock=lambda: 100)
    notifier.relay_once({}, outbox, clock=lambda: 699)
    assert send.call_count == 1
    assert outbox.db.execute('SELECT next_attempt FROM deliveries').fetchone() == (700,)


def test_backoff_is_capped_without_limiting_attempt_count(outbox, monkeypatch):
    send = Mock(side_effect=notifier.SafeRetry('connect_failed'))
    monkeypatch.setattr(notifier, 'send', send)
    now, delays = 100, []
    for _ in range(10):
        notifier.relay_once({}, outbox, clock=lambda: now)
        due = outbox.db.execute('SELECT next_attempt FROM deliveries').fetchone()[0]
        delays.append(due - now)
        now = due
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300, 300]


def test_slow_failed_backlog_does_not_retry_same_event_within_pass(outbox, monkeypatch):
    with sqlite3.connect(notifier.DATABASE) as source:
        source.executemany('INSERT INTO snapshots VALUES (?,?)', [(9, 'NEW_TRACK_DETECTED'), (10, 'NEW_TRACK_DETECTED')])
    now = [100]
    def slow_failure(*args):
        now[0] += 20
        raise notifier.SafeRetry('connect_failed')
    send = Mock(side_effect=slow_failure)
    monkeypatch.setattr(notifier, 'send', send)
    notifier.relay_once({}, outbox, clock=lambda: now[0])
    assert send.call_count == 3
    assert [r[2] for r in delivery(outbox)] == [1, 1, 1]
    assert outbox.db.execute('SELECT started_at FROM attempts ORDER BY id').fetchall() == [(100,), (120,), (140,)]


@pytest.mark.parametrize('error,status,reason', [
    (RuntimeError('synthetic-sensitive-value'), 'unconfirmed', 'delivery_unknown'),
    (notifier.Rejected('synthetic-sensitive-value'), 'failed', 'rejected'),
])
def test_uncertain_or_permanent_failure_is_retained_not_resent(outbox, monkeypatch, capsys, error, status, reason):
    send = Mock(side_effect=error)
    monkeypatch.setattr(notifier, 'send', send)
    notifier.relay_once({}, outbox, clock=lambda: 100)
    outbox.recover(101)
    notifier.relay_once({}, outbox, clock=lambda: 999)
    assert send.call_count == 1
    assert delivery(outbox) == [(8, status, 1, reason)]
    assert 'synthetic-sensitive-value' not in capsys.readouterr().out
    assert b'synthetic-sensitive-value' not in (notifier.PRIVATE / 'deliveries.sqlite3').read_bytes()


def test_crash_after_claim_is_unconfirmed_and_does_not_block_later_events(outbox, monkeypatch):
    send = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(notifier, 'send', send)
    with pytest.raises(KeyboardInterrupt):
        notifier.relay_once({}, outbox, clock=lambda: 100)
    assert delivery(outbox) == [(8, 'sending', 1, '')]
    outbox.recover(101)
    with sqlite3.connect(notifier.DATABASE) as source:
        source.execute("INSERT INTO snapshots VALUES (9,'NEW_TRACK_DETECTED')")
    send.side_effect = None
    notifier.relay_once({}, outbox, clock=lambda: 102)
    assert delivery(outbox) == [(8, 'unconfirmed', 1, 'interrupted'), (9, 'sent', 1, '')]
    assert outbox.db.execute('SELECT outcome,reason FROM attempts ORDER BY id').fetchall() == [('unconfirmed', 'interrupted'), ('sent', '')]


def test_crash_after_api_acceptance_before_commit_never_resends(outbox, monkeypatch):
    send = Mock()
    monkeypatch.setattr(notifier, 'send', send)
    with monkeypatch.context() as patcher:
        patcher.setattr(outbox, 'finish', Mock(side_effect=KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            notifier.relay_once({}, outbox, clock=lambda: 100)
    outbox.recover(101)
    notifier.relay_once({}, outbox, clock=lambda: 102)
    send.assert_called_once()
    assert delivery(outbox)[0][1] == 'unconfirmed'


def test_ingest_and_cursor_rollback_together(outbox):
    outbox.db.execute("CREATE TRIGGER refuse BEFORE INSERT ON metadata BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(sqlite3.Error):
        outbox.ingest(100)
    assert delivery(outbox) == []
    assert outbox.db.execute('SELECT * FROM metadata').fetchall() == []
    outbox.db.execute('DROP TRIGGER refuse')
    outbox.ingest(101)
    assert delivery(outbox)[0][:2] == (8, 'pending')


def test_legacy_cursor_import_is_non_destructive(outbox, monkeypatch):
    legacy = (notifier.PRIVATE / 'cursor.json').read_bytes()
    credentials = (notifier.PRIVATE / 'credentials.json').read_bytes()
    monkeypatch.setattr(notifier, 'send', Mock())
    notifier.relay_once({}, outbox, clock=lambda: 100)
    assert (notifier.PRIVATE / 'cursor.json').read_bytes() == legacy
    assert (notifier.PRIVATE / 'credentials.json').read_bytes() == credentials
    assert (notifier.PRIVATE / 'deliveries.sqlite3').stat().st_mode & 0o777 == 0o600
    # Once initialized, a stale/missing legacy cursor cannot replay old events.
    (notifier.PRIVATE / 'cursor.json').unlink()
    notifier.relay_once({}, outbox, clock=lambda: 101)
    notifier.send.assert_called_once()


def test_invalid_legacy_cursor_fails_without_sending(outbox, monkeypatch):
    notifier.save(notifier.PRIVATE / 'cursor.json', True)
    send = Mock()
    monkeypatch.setattr(notifier, 'send', send)
    with pytest.raises(ValueError):
        notifier.relay_once({}, outbox, clock=lambda: 100)
    send.assert_not_called()


def test_atomic_claim_excludes_second_connection(outbox):
    outbox.ingest(100)
    other = notifier.DeliveryStore()
    try:
        assert outbox.claim(100) is not None
        assert other.claim(100) is None
    finally:
        other.close()


def test_status_is_read_only_without_credentials(outbox, monkeypatch, capsys):
    monkeypatch.setattr(notifier, 'send', Mock())
    notifier.relay_once({}, outbox, clock=lambda: 100)
    (notifier.PRIVATE / 'credentials.json').unlink()
    before = (notifier.PRIVATE / 'deliveries.sqlite3').read_bytes()
    monkeypatch.setattr(notifier.sys, 'argv', ['telegram_notify.py', 'status'])
    capsys.readouterr()
    notifier.main()
    status = json.loads(capsys.readouterr().out)
    assert status['counts'] == {'sent': 1}
    assert status['last_api_accepted_unix'] == 100
    assert status['delivery_verified'] is False
    assert (notifier.PRIVATE / 'deliveries.sqlite3').read_bytes() == before


def test_status_before_initialization_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(notifier, 'PRIVATE', tmp_path / 'absent')
    notifier.delivery_status()
    assert json.loads(capsys.readouterr().out)['delivery_store'] == 'not_initialized'
    assert not notifier.PRIVATE.exists()


def test_relay_lock_blocks_duplicate_instance(outbox, monkeypatch):
    import fcntl
    monkeypatch.setattr(notifier.sys, 'argv', ['telegram_notify.py', 'run'])
    send = Mock()
    monkeypatch.setattr(notifier, 'send', send)
    with (notifier.PRIVATE / 'notifier.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            notifier.main()
    send.assert_not_called()


@pytest.mark.parametrize('case,expected', [
    ('connect', notifier.SafeRetry), ('rate_limit', notifier.SafeRetry),
    ('rejected', notifier.Rejected), ('request_timeout', TimeoutError),
    ('response_timeout', TimeoutError), ('server_error', RuntimeError),
    ('malformed', ValueError), ('accepted', None),
])
def test_api_failure_classification_never_uses_network(monkeypatch, case, expected):
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = {'rate_limit': 429, 'rejected': 403, 'server_error': 500}.get(case, 200)
    data = {'ok': True, 'result': {'message_id': 1}}
    if case == 'rate_limit':
        data = {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 42}}
    if case in {'rejected', 'server_error'}:
        data = {'ok': False, 'description': 'synthetic-sensitive-description'}
    response.read.return_value = b'not-json' if case == 'malformed' else json.dumps(data).encode()
    if case == 'connect':
        connection.connect.side_effect = OSError('synthetic-sensitive-error')
    if case == 'request_timeout':
        connection.request.side_effect = TimeoutError()
    if case == 'response_timeout':
        response.read.side_effect = TimeoutError()
    monkeypatch.setattr(notifier.http.client, 'HTTPSConnection', Mock(return_value=connection))
    if expected:
        with pytest.raises(expected) as raised:
            notifier.api('synthetic-token', 'sendMessage', {})
        assert 'synthetic-sensitive' not in str(raised.value)
        if case == 'connect':
            connection.request.assert_not_called()
        if case == 'rate_limit':
            assert raised.value.retry_after == 42
    else:
        assert notifier.api('synthetic-token', 'sendMessage', {}) == {'message_id': 1}
    connection.close.assert_called_once()
