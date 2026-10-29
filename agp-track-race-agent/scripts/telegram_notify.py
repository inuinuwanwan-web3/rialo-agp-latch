"""Telegram-only notifier. Never imports or calls the AGP gateway."""
import argparse
import fcntl
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time

PRIVATE = Path.home() / '.config' / 'agp-track-telegram'
DATABASE = Path(__file__).resolve().parents[1] / 'data/track_observations/observations.sqlite3'


class SafeRetry(RuntimeError):
    """Only a failure known not to have delivered a message may be retried."""
    def __init__(self, reason, retry_after=0):
        super().__init__('Telegram retry required')
        self.reason, self.retry_after = reason, retry_after


class Rejected(RuntimeError):
    pass


def save(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def api(token, method, payload):
    connection = http.client.HTTPSConnection('api.telegram.org', timeout=20)
    try:
        try:
            # No HTTP request bytes have been sent if this step fails.
            connection.connect()
        except OSError:
            raise SafeRetry('connect_failed') from None
        connection.request('POST', '/bot' + token + '/' + method,
                           json.dumps(payload), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = json.loads(response.read())
        if response.status != 200 or data.get('ok') is not True:
            if response.status == 429 and data.get('ok') is False and data.get('error_code') == 429:
                delay = data.get('parameters', {}).get('retry_after', 60)
                if type(delay) is not int or delay < 1:
                    delay = 60
                raise SafeRetry('rate_limit', delay)
            if response.status in {400, 401, 403, 404} and data.get('ok') is False:
                raise Rejected('Telegram request rejected')
            raise RuntimeError('Telegram request failed')
        return data['result']
    finally:
        connection.close()


def send(config, message):
    return api(config['token'], 'sendMessage', {
        'chat_id': config['chat_id'], 'text': message,
    })


def setup():
    if not sys.stdin.isatty():
        raise RuntimeError('Interactive terminal required')
    if (PRIVATE / 'credentials.json').exists():
        print('設定済みです。上書きしていません。')
        return
    token = getpass.getpass('Bot Token（非表示）: ').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('Invalid token')
    bot = api(token, 'getMe', {})
    print('接続先Bot: @' + bot['username'])
    pairing = 'agp-' + secrets.token_hex(8)
    print('iPhoneで対象Botを開き、次の文字列を送信してください: ' + pairing, flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        updates = api(token, 'getUpdates', {'timeout': 0, 'limit': 100})
        for update in updates:
            message = update.get('message', {})
            chat = message.get('chat', {})
            if message.get('text') == pairing and chat.get('type') == 'private':
                save(PRIVATE / 'credentials.json', {'token': token, 'chat_id': chat['id']})
                print('設定保存済み。Tokenは共有せず、Codexへ「設定済み」と伝えてください。')
                return
        time.sleep(2)
    print('ペアリング時間切れ。設定は保存されていません。')


def read_events(database, after):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as db:
        return db.execute("SELECT id FROM snapshots WHERE id > ? AND event = 'NEW_TRACK_DETECTED' ORDER BY id", (after,)).fetchall()


class DeliveryStore:
    """Private durable outbox, independent of the watcher's read-only database.

    main's notifier.lock must be held for the entire relay lifetime. An uncertain
    delivery is retained for review, never automatically duplicated.
    """
    def __init__(self):
        path = PRIVATE / 'deliveries.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                event_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL, reason TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL,
                started_at REAL NOT NULL, finished_at REAL,
                outcome TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
        ''')

    def close(self):
        self.db.close()

    def recover(self, now):
        with self.db:
            recovered = self.db.execute("UPDATE deliveries SET status='unconfirmed',updated_at=?,reason='interrupted' WHERE status='sending'", (now,)).rowcount
            self.db.execute("UPDATE attempts SET outcome='unconfirmed',finished_at=?,reason='interrupted' WHERE outcome='sending'", (now,))
        if recovered:
            print('TELEGRAM_RECOVERED_UNCONFIRMED; count=' + str(recovered), flush=True)

    def ingest(self, now):
        row = self.db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()
        if row is None:
            legacy = PRIVATE / 'cursor.json'
            if legacy.exists():
                cursor = json.loads(legacy.read_text())
                if type(cursor) is not int or cursor < 0:
                    raise ValueError('Invalid legacy cursor')
            else:
                with sqlite3.connect(DATABASE.resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as source:
                    cursor = source.execute('SELECT coalesce(max(id),0) FROM snapshots').fetchone()[0]
        else:
            cursor = row[0]
        events = read_events(DATABASE, cursor)
        # Discovery cursor and pending events are committed together. The legacy
        # cursor is import-only: do not let a second state file lose pending work.
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO deliveries(event_id,status,updated_at) VALUES (?,'pending',?)",
                                [(event_id, now) for (event_id,) in events])
            self.db.execute("INSERT INTO metadata VALUES ('cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (events[-1][0] if events else cursor,))
            self.db.execute("INSERT INTO metadata VALUES ('last_scan_unix',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))

    def claim(self, now, *, due_before=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute("SELECT event_id,attempts FROM deliveries WHERE status IN ('pending','retry') AND next_attempt<=? ORDER BY event_id LIMIT 1", (now if due_before is None else due_before,)).fetchone()
            if row is None:
                return None
            event_id, attempts = row
            self.db.execute("UPDATE deliveries SET status='sending',attempts=attempts+1,updated_at=? WHERE event_id=?", (now, event_id))
            attempt_id = self.db.execute("INSERT INTO attempts(event_id,started_at,outcome) VALUES (?,?,'sending')", (event_id, now)).lastrowid
        return event_id, attempts + 1, attempt_id

    def finish(self, claim, status, reason, now, delay=0):
        event_id, _, attempt_id = claim
        with self.db:
            self.db.execute('UPDATE deliveries SET status=?,reason=?,updated_at=?,next_attempt=? WHERE event_id=?',
                            (status, reason, now, now + delay, event_id))
            self.db.execute('UPDATE attempts SET outcome=?,reason=?,finished_at=? WHERE id=?',
                            (status, reason, now, attempt_id))


def relay_once(config, store, *, clock=time.time):
    due_before = clock()
    store.ingest(due_before)
    # Try each due event at most once this pass. Retrying failures cannot spin.
    while (claim := store.claim(clock(), due_before=due_before)) is not None:
        event_id, attempts, _ = claim
        status, reason, delay = 'sent', '', 0
        try:
            send(config, 'AGP Track Watcher\n新Trackを検知しました。\nイベント: ' + str(event_id) + '\nAUTO-JOIN: BLOCKED')
        except SafeRetry as error:
            status, reason = 'retry', error.reason
            delay = max(min(5 * 2 ** min(attempts - 1, 6), 300), error.retry_after)
        except Rejected:
            status, reason = 'failed', 'rejected'
        except Exception:
            status, reason = 'unconfirmed', 'delivery_unknown'
        # Exceptions/crashes before this commit leave sending -> unconfirmed on
        # recovery. Never assume the remote API did not accept the request.
        store.finish(claim, status, reason, clock(), delay)
        label = {'sent': 'SENT', 'retry': 'RETRY_SCHEDULED',
                 'failed': 'REJECTED', 'unconfirmed': 'DELIVERY_UNCONFIRMED'}[status]
        print('TELEGRAM_EVENT_' + label + '; event=' + str(event_id), flush=True)


def delivery_status():
    """Local read-only health/history summary; never reads credentials or sends."""
    path = PRIVATE / 'deliveries.sqlite3'
    if not path.exists():
        print(json.dumps({'delivery_store': 'not_initialized', 'delivery_verified': False}))
        return
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        counts = dict(db.execute('SELECT status,count(*) FROM deliveries GROUP BY status'))
        last = db.execute('SELECT max(finished_at) FROM attempts WHERE outcome=\'sent\'').fetchone()[0]
        due = db.execute("SELECT min(next_attempt) FROM deliveries WHERE status='retry'").fetchone()[0]
        scan = db.execute("SELECT value FROM metadata WHERE key='last_scan_unix'").fetchone()
    print(json.dumps({'counts': counts, 'last_api_accepted_unix': last,
                      'next_retry_unix': due, 'last_scan_unix': scan[0] if scan else None,
                      'delivery_verified': False}))


def relay(config):
    store = DeliveryStore()
    print('TELEGRAM_RELAY_STARTED; AUTO_JOIN_BLOCKED', flush=True)
    try:
        store.recover(time.time())
        while True:
            relay_once(config, store)
            time.sleep(5)
    finally:
        store.close()


def test_once():
    """Claim a separate DB test event durably before one shared send attempt."""
    config = json.loads((PRIVATE / 'credentials.json').read_text())
    # Existing DB only; no snapshots, production cursor, or notifier lock changes.
    with sqlite3.connect(DATABASE.resolve().as_uri() + '?mode=rw', uri=True, timeout=10) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS telegram_test_once (
            id INTEGER PRIMARY KEY CHECK(id=1),
            event TEXT NOT NULL CHECK(event='TELEGRAM_TEST'),
            status TEXT NOT NULL CHECK(status IN ('attempted','accepted','unconfirmed')))
        ''')
        if db.execute('SELECT 1 FROM telegram_test_once WHERE id=1').fetchone():
            print('TELEGRAM_TEST_ALREADY_ATTEMPTED; NO_DISPATCH', flush=True)
            return
        db.execute("INSERT INTO telegram_test_once VALUES (1, 'TELEGRAM_TEST', 'attempted')")
        db.commit()
        # A crash or ambiguous API failure permanently consumes this single attempt.
        try:
            send(config, 'AGP Watch TEST: Telegram notification path OK')
        except Exception:
            db.execute("UPDATE telegram_test_once SET status='unconfirmed' WHERE id=1")
            db.commit()
            raise RuntimeError('Telegram test delivery unconfirmed; no retry') from None
        db.execute("UPDATE telegram_test_once SET status='accepted' WHERE id=1")
        db.commit()
        print('TELEGRAM_TEST_API_ACCEPTED; DISPATCH_COUNT=1', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'test', 'run', 'test-once', 'status'])
    args = parser.parse_args()
    if args.action == 'status':
        delivery_status()
        return
    os.umask(0o077)
    if args.action == 'test-once':
        test_once()
        return
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PRIVATE, 0o700)
    with (PRIVATE / 'notifier.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'setup':
            setup()
            return
        config = json.loads((PRIVATE / 'credentials.json').read_text())
        if args.action == 'test':
            marker = PRIVATE / 'test-attempt.json'
            if marker.exists():
                print('テスト送信は実行済みです。自動再送しません。')
                return
            save(marker, {'status': 'attempted'})
            send(config, 'AGP Track Watcher\nTelegramテスト通知（1通のみ）\nAGPアクセスなし / AUTO-JOIN: BLOCKED')
            save(marker, {'status': 'sent'})
            print('TELEGRAM_TEST_SENT; iPhone到着確認待ち')
        else:
            relay(config)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception:
        # Never print exceptions: HTTP errors can contain token-bearing URLs.
        print('TELEGRAM_OPERATION_FAILED; 秘密情報を含む詳細は表示しません。', file=sys.stderr)
        raise SystemExit(1)
