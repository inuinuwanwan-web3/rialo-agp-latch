"""Offline Telegram checks; no AGP imports or network requests."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

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
