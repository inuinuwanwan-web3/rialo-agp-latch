"""Loopback-only, one-time token entry. No AGP access or request logging."""
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import secrets
import signal

from telegram_notify import PRIVATE, api, save


class SetupServer(HTTPServer):
    def handle_error(self, request, client_address):
        pass  # Never expose request data or exceptions.


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type='text/html; charset=utf-8'):
        payload = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'nonce-" + self.server.key + "'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def valid_host(self):
        return self.headers.get('Host') == self.server.address

    def do_GET(self):
        if not self.valid_host() or self.path != '/' + self.server.key:
            self.reply(404, 'Not found')
            return
        self.reply(200, '''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AGP Track Watcher — Telegram設定</title>
<style>body{font:18px system-ui;max-width:620px;margin:12vh auto;padding:24px;color:#172b40;background:#f4f8fc}input{box-sizing:border-box;width:100%;padding:18px;font-size:20px}p{line-height:1.8}</style>
<h1>Telegram通知の設定</h1>
<p>Bot Tokenを下の欄に1回貼り付けてください。自動で安全に保存します。</p>
<input id="token" type="password" autocomplete="off" spellcheck="false" autofocus aria-label="Bot Token">
<p id="status" role="status">Tokenはこのパソコンに保存されます。チャットに貼り付ける必要はありません。</p>
<p>通知先の確認とテスト通知は、Token保存後に進めます。</p>
<script nonce="''' + self.server.key + '''">
const field=document.getElementById('token'), status=document.getElementById('status');
field.addEventListener('paste', async event => {
event.preventDefault(); const token=event.clipboardData.getData('text').trim();
field.value=''; field.disabled=true; status.textContent='確認しています…';
try { const response=await fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Key':location.pathname.slice(1)},body:JSON.stringify({token})});
const result=await response.json(); status.textContent=result.message;
if(!response.ok)field.disabled=false;
} catch {status.textContent='保存結果を確認できません。Tokenをチャットに貼らず、この表示をCodexへ伝えてください。';}
});</script></html>''')

    def do_POST(self):
        if (not self.valid_host() or self.path != '/' + self.server.key
                or self.headers.get('Origin') != 'http://' + self.server.address
                or not hmac.compare_digest(self.headers.get('X-Setup-Key', ''), self.server.key)):
            self.reply(403, '{}', 'application/json')
            return
        try:
            if self.server.saved:
                raise ValueError()
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 512:
                raise ValueError()
            token = json.loads(self.rfile.read(length))['token']
            if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
                raise ValueError()
            bot = api(token, 'getMe', {})
            if not bot.get('is_bot'):
                raise ValueError()
            save(PRIVATE / 'pending-token.json', {'token': token})
            self.server.saved = True
            message = 'Tokenを保存しました。この画面を閉じて構いません。Codexへ「保存できた」と伝えてください。'
            self.reply(200, json.dumps({'message': message}), 'application/json')
        except Exception:
            self.reply(400, json.dumps({'message': '保存できませんでした。Tokenまたは接続を確認してください。秘密情報は表示していません。'}), 'application/json')


def main():
    os.umask(0o077)
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PRIVATE, 0o700)
    if any((PRIVATE / name).exists() for name in ('credentials.json', 'pending-token.json')):
        print('TELEGRAM_TOKEN_ALREADY_SAVED', flush=True)
        return
    server = SetupServer(('127.0.0.1', 0), Handler)
    server.key = secrets.token_urlsafe(32)
    server.address = '127.0.0.1:' + str(server.server_port)
    server.saved = False
    signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
    signal.alarm(3600)
    print('http://' + server.address + '/' + server.key, flush=True)
    server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('TELEGRAM_SETUP_UNAVAILABLE', flush=True)
        raise SystemExit(1)
