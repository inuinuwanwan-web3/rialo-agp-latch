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
        connection.request('POST', '/bot' + token + '/' + method,
                           json.dumps(payload), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = json.loads(response.read())
        if response.status != 200 or data.get('ok') is not True:
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


def relay(config):
    state_path = PRIVATE / 'cursor.json'
    if not state_path.exists():
        with sqlite3.connect(DATABASE.resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as db:
            latest = db.execute('SELECT coalesce(max(id),0) FROM snapshots').fetchone()[0]
        save(state_path, latest)
    cursor = json.loads(state_path.read_text())
    print('TELEGRAM_RELAY_STARTED; AUTO_JOIN_BLOCKED', flush=True)
    while True:
        for (event_id,) in read_events(DATABASE, cursor):
            # Claim before sending: never automatically duplicate an uncertain delivery.
            save(state_path, event_id)
            cursor = event_id
            try:
                send(config, 'AGP Track Watcher\n新Trackを検知しました。\nイベント: ' + str(event_id) + '\nAUTO-JOIN: BLOCKED')
                print('TELEGRAM_EVENT_SENT', flush=True)
            except Exception:
                print('TELEGRAM_EVENT_DELIVERY_UNCONFIRMED; event=' + str(event_id), flush=True)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'test', 'run'])
    args = parser.parse_args()
    os.umask(0o077)
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
