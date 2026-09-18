"""Confirm an iPhone private chat with a one-time message, then send one test."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from telegram_notify import PRIVATE, api, save


def match_chat(updates, challenge, since):
    for update in updates:
        message = update.get('message', {})
        chat = message.get('chat', {})
        sender = message.get('from', {})
        if (message.get('text') == challenge and message.get('date', 0) >= since
                and chat.get('type') == 'private' and not sender.get('is_bot', True)
                and sender.get('id') == chat.get('id')):
            return chat['id']
    return None


def main():
    os.umask(0o077)
    with (PRIVATE / 'pair.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (PRIVATE / 'credentials.json').exists():
            print('CHAT_ALREADY_CONFIGURED; NOT_SENDING', flush=True)
            return
        token = json.loads((PRIVATE / 'pending-token.json').read_text())['token']
        bot = api(token, 'getMe', {})
        challenge = 'AGP-' + secrets.token_hex(4)
        since = int(time.time())
        print(json.dumps({'bot': bot['username'], 'send_from_iphone': challenge}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            updates = api(token, 'getUpdates', {'timeout': 0, 'limit': 100})
            chat_id = match_chat(updates, challenge, since)
            if chat_id is not None:
                with (PRIVATE / 'notifier.lock').open('a') as notifier_lock:
                    fcntl.flock(notifier_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    save(PRIVATE / 'credentials.json', {'token': token, 'chat_id': chat_id})
                print('IPHONE_PRIVATE_CHAT_VERIFIED', flush=True)
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('telegram_notify.py')), 'test'])
                return result.returncode
            time.sleep(3)
        print('PAIRING_TIMED_OUT; NO_TEST_SENT', flush=True)
        return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception:
        print('TELEGRAM_PAIR_FAILED; NO_SECRET_DETAILS', flush=True)
        raise SystemExit(1)
