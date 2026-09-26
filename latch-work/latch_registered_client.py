"""Single-use registered stdio client; DENY default, explicit fixed ALLOW opt-in."""
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time
import tomllib

from agp_race_agent.solver import SolverError
from latch_mcp_transport import _unique, _invalid_constant
from latch_safe_diagnostic import SafeDiagnostic

PROBE = {"method": "GET", "path": "/__latch_policy_denial_probe__"}
SERVER = os.environ.get("LATCH_MCP_SERVER", "latch")
SERVER_HASH = "753a72c0402f91998a5c9c28938b426ed189d60ee079ac41d7b8bc0209365c13"


class RegisteredLatchClient:
    def __init__(self):
        self.dispatch_count = 0
        self.connected = False
        self._used = False
        self._lock = threading.Lock()
        self.diagnostic = SafeDiagnostic()

    @property
    def retries(self):
        return 0

    def _expected_request(self):
        return dict(PROBE)

    def _guard_file(self):
        return "latch_deny_fetch_guard.cjs"

    def call_tool(self, name, arguments, *, timeout_seconds):
        self.diagnostic.stage('LOCAL_PRECHECK', 'LOCAL_GUARD')
        if not self._lock.acquire(blocking=False):
            raise SolverError("Registered Latch MCP failed closed") from None
        proc = None
        try:
            expected = self._expected_request()
            canonical = lambda value: json.dumps(value, sort_keys=True, allow_nan=False)
            if (self._used or name != "latch_authorize" or canonical(arguments) != canonical(expected)
                    or type(timeout_seconds) not in {int, float}
                    or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
                raise ValueError()
            self._used = True
            deadline = time.monotonic() + timeout_seconds
            self.diagnostic.stage('LOCAL_PRECHECK', 'CONFIG')
            config = tomllib.loads((Path.home() / '.codex/config.toml').read_text())
            registration = config['mcp_servers'][SERVER]
            if registration.get('enabled', True) is not True or registration['command'] != 'npx':
                raise ValueError()
            args = registration['args']
            if len(args) != 2 or args[0] != '-y':
                raise ValueError()
            # Resolve only an already cached copy of the registered package.
            matches = []
            self.diagnostic.stage('LOCAL_PRECHECK', 'CACHE')
            for lock in (Path.home() / '.npm/_npx').glob('*/package-lock.json'):
                metadata = json.loads(lock.read_text())
                package = metadata.get('packages', {}).get('node_modules/latch-mcp-server', {})
                if package.get('resolved') == args[1]:
                    script = lock.parent / 'node_modules/latch-mcp-server/server.js'
                    if hashlib.sha256(script.read_bytes()).hexdigest() == SERVER_HASH:
                        matches.append(script)
            if len(matches) != 1:
                raise ValueError()
            env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
            configured = registration['env']
            self.diagnostic.stage('LOCAL_PRECHECK', 'ENVIRONMENT')
            if set(configured) != {'LATCH_ID', 'LATCH_LINK', 'LATCH_TOKEN', 'LATCH_URL'}:
                raise ValueError()
            if any(type(v) is not str or not v for v in configured.values()):
                raise ValueError()
            env.update(configured)
            env['LATCH_SMOKE_TIMEOUT_MS'] = str(max(1, int(timeout_seconds * 1000)))
            self.diagnostic.stage('MCP_START', 'PROCESS_START')
            node = shutil.which('node')
            if not node:
                raise ValueError()
            proc = subprocess.Popen([node, '--require', str(Path(__file__).with_name(self._guard_file())),
                                     str(matches[0])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=env, cwd=Path(__file__).parent)
            buffer = b''
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)

                def write(message):
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    proc.stdin.write(json.dumps(message).encode() + b'\n')
                    proc.stdin.flush()

                def read(identifier):
                    nonlocal buffer
                    while True:
                        if b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            message = json.loads(line, object_pairs_hook=_unique, parse_constant=_invalid_constant)
                            if (type(message) is not dict or message.get('jsonrpc') != '2.0'
                                    or type(message.get('id')) is not int or message['id'] != identifier
                                    or set(message) != {'jsonrpc', 'id', 'result'}
                                    or type(message['result']) is not dict):
                                raise ValueError()
                            return message['result']
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise TimeoutError()
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk or len(buffer) + len(chunk) > 1_000_000:
                            raise ValueError()
                        buffer += chunk

                self.diagnostic.stage('MCP_START', 'HANDSHAKE')
                write({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2024-11-05', 'capabilities': {},
                    'clientInfo': {'name': 'isolated-latch-smoke', 'version': '1'}}})
                hello = read(1)
                if (hello.get('protocolVersion') != '2024-11-05'
                        or type(hello.get('capabilities')) is not dict
                        or type(hello.get('serverInfo')) is not dict):
                    raise ValueError()
                self.connected = True
                write({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
                self.dispatch_count += 1
                self.diagnostic.stage('MCP_DISPATCH', 'MCP_WRITE')
                # Means write attempted, not proof of server receipt.
                self.diagnostic.mark('mcp_call_started')
                write({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                       'params': {'name': name, 'arguments': expected}})
                self.diagnostic.stage('MCP_RESPONSE', 'MCP_READ')
                result = read(2)
                self.diagnostic.mark('mcp_response_received')
                return result
        except Exception as error:
            self.diagnostic.fail(error)
            raise SolverError("Registered Latch MCP failed closed") from None
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)
                proc.stdin.close()
                proc.stdout.close()
            self._lock.release()


class RegisteredAllowLatchClient(RegisteredLatchClient):
    """Explicit opt-in; same credential loader, handshake, timeout and one-shot logic."""
    def __init__(self, *, execute_live_allow=False):
        if execute_live_allow is not True:
            raise SolverError("Live ALLOW disabled")
        super().__init__()

    def _expected_request(self):
        from latch_smoke_request import allow_request
        return allow_request()

    def _guard_file(self):
        return "latch_allow_fetch_guard.cjs"
