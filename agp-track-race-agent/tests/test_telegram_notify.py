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
