"""Failure injection only; sockets and real subprocesses are forbidden."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_latch_allow_contract import offline
from test_latch_allow_smoke_contract import result

PRIVATE = 'synthetic-credential-request-response-environment'


def run(capsys, factory):
    from latch_allow_smoke import main
    code = main(['--execute-live-allow'], client_factory=factory, window_check=lambda: True)
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err
    assert code == 2
    assert json.loads(output.out) == dict(result='FAILED_CLOSED', retries=0, fallback='NONE')
    data = json.loads(output.err)
    assert set(data) == {'failure_stage', 'exception_class', 'error_category',
                         'dispatch_started', 'mcp_call_started', 'mcp_response_received',
                         'timeout', 'exit_code', 'retries', 'fallback'}
    assert data['exit_code'] == 2 and data['retries'] == 0 and data['fallback'] == 'NONE'
    return data


def test_init_failure(offline, capsys):
    def factory(**kwargs):
        raise PermissionError(PRIVATE)
    data = run(capsys, factory)
    assert data['failure_stage'] == 'CLIENT_INIT'
    assert data['exception_class'] == 'PermissionError'
    assert not data['dispatch_started']


def test_local_request_guard(offline):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    transport = LatchMcpTransport(OfflineMcpClient(result()))
    with pytest.raises(offline[1]):
        transport.smoke_allow({'body': PRIVATE}, execute_live_allow=True)
    data = transport.diagnostic.snapshot()
    assert data['failure_stage'] == 'LOCAL_PRECHECK'
    assert data['exception_class'] == 'ValueError'
    assert not data['dispatch_started']
    assert transport.client.calls == []


@pytest.mark.parametrize('payload,stage,category', [
    ({'isError': True, 'content': [{'type': 'text', 'text': 'Error: Request blocked'}]}, 'FETCH_GUARD', 'FETCH_BLOCKED'),
    ({'isError': True, 'content': [{'type': 'text', 'text': 'Error: fetch failed'}]}, 'MCP_RESPONSE', 'FETCH_FAILED'),
    ({'isError': True, 'content': [{'type': 'text', 'text': 'Error: Request blocked '+PRIVATE}]}, 'MCP_RESPONSE', 'MCP_ERROR'),
    ({'content': [{'type': 'text', 'text': PRIVATE}]}, 'MCP_RESPONSE', 'SCHEMA_MISMATCH'),
    (result({'authorized': False, 'deniedBy': 'endpoint_0', 'reason': PRIVATE}), 'MCP_RESPONSE', 'POLICY_DENY'),
    (result({'status': 200, 'headers': {}, 'data': {'private': PRIVATE}}), 'RESPONSE_VALIDATE', 'COMPLETION_SCHEMA'),
])
def test_response_categories(offline, capsys, payload, stage, category):
    from latch_mcp_transport import OfflineMcpClient
    client = OfflineMcpClient(payload)
    data = run(capsys, lambda **kwargs: client)
    assert data['failure_stage'] == stage and data['error_category'] == category
    assert data['dispatch_started'] and len(client.calls) == 1


@pytest.mark.parametrize('fault,stage,category,called,received', [
    ('config', 'LOCAL_PRECHECK', 'CONFIG', False, False),
    ('spawn', 'MCP_START', 'PROCESS_START', False, False),
    ('handshake', 'MCP_START', 'HANDSHAKE', False, False),
    ('timeout', 'MCP_START', 'TIMEOUT', False, False),
    ('write', 'MCP_DISPATCH', 'MCP_WRITE', True, False),
    ('read', 'MCP_RESPONSE', 'MCP_READ', True, False),
    ('guard', 'FETCH_GUARD', 'FETCH_BLOCKED', True, True),
])
def test_registered_client_stages(offline, monkeypatch, capsys, fault, stage, category, called, received):
    import latch_registered_client as module
    # No real config, cache, credential or process is accessed.
    monkeypatch.setattr(Path, 'read_text', lambda self: PRIVATE)
    monkeypatch.setattr(Path, 'read_bytes', lambda self: b'fixture')
    monkeypatch.setattr(Path, 'glob', lambda *args: iter([Path('/offline/package-lock.json')]))
    registration = dict(command='npx', args=['-y', 'offline'], env={k: PRIVATE for k in
                        ('LATCH_ID', 'LATCH_LINK', 'LATCH_TOKEN', 'LATCH_URL')})
    def config(_):
        if fault == 'config':
            raise ValueError(PRIVATE)
        return {'mcp_servers': {module.SERVER: registration}}
    monkeypatch.setattr(module.tomllib, 'loads', config)
    original_loads = json.loads
    monkeypatch.setattr(json, 'loads', lambda s, **kw: {'packages': {'node_modules/latch-mcp-server': {'resolved': 'offline'}}}
                        if s == PRIVATE else original_loads(s, **kw))
    monkeypatch.setattr(module.hashlib, 'sha256', lambda _: SimpleNamespace(hexdigest=lambda: module.SERVER_HASH))
    monkeypatch.setattr(module.shutil, 'which', lambda _: '/offline/node')
    class Input(io.BytesIO):
        def write(self, value):
            if fault == 'write' and b'tools/call' in value:
                raise BrokenPipeError(PRIVATE)
            return super().write(value)
    proc = SimpleNamespace(stdin=Input(), stdout=io.BytesIO(), poll=lambda: 0, wait=lambda **kw: 0)
    def spawn(*args, **kwargs):
        if fault == 'spawn':
            raise PermissionError(PRIVATE)
        return proc
    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    class Selector:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def register(self, *args): pass
        def select(self, timeout): return [] if fault == 'timeout' else [True]
    monkeypatch.setattr(module.selectors, 'DefaultSelector', Selector)
    proc.stdout = SimpleNamespace(fileno=lambda: 999, close=lambda: None)
    hello = {'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {}}}
    reply = {'jsonrpc': '2.0', 'id': 2, 'result': {'isError': True, 'content': [{'type': 'text', 'text': 'Error: Request blocked'}]}}
    chunks = iter([b'' if fault == 'handshake' else (json.dumps(hello)+'\n').encode(),
                   b'' if fault == 'read' else (json.dumps(reply)+'\n').encode()])
    monkeypatch.setattr(module.os, 'read', lambda *args: next(chunks))
    data = run(capsys, module.RegisteredAllowLatchClient)
    assert data['failure_stage'] == stage and data['error_category'] == category
    assert data['dispatch_started']
    assert data['mcp_call_started'] is called
    assert data['mcp_response_received'] is received
    assert data['timeout'] is (fault == 'timeout')


def test_timeout_and_late_worker_cannot_overwrite(offline, monkeypatch):
    import threading
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    from latch_smoke_request import allow_request
    client = OfflineMcpClient(result())
    release, done = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        release.wait(2)
        done.set()
        raise ValueError(PRIVATE)
    monkeypatch.setattr(client, 'call_tool', delayed)
    transport = LatchMcpTransport(client, timeout_seconds=0.02)
    try:
        with pytest.raises(offline[1]):
            transport.smoke_allow(allow_request(), execute_live_allow=True)
        data = transport.diagnostic.snapshot()
        assert data['timeout'] and data['exception_class'] == 'TimeoutError'
        assert data['error_category'] == 'TIMEOUT'
        with pytest.raises(offline[1]):
            transport.smoke_allow(allow_request(), execute_live_allow=True)
    finally:
        release.set()
        assert done.wait(1)
    assert transport.diagnostic.snapshot() == data


def test_custom_exception_name_is_not_logged():
    from latch_safe_diagnostic import SafeDiagnostic
    diagnostic = SafeDiagnostic()
    diagnostic.fail(type(PRIVATE, (Exception,), {})(PRIVATE))
    assert PRIVATE not in json.dumps(diagnostic.snapshot())
    assert diagnostic.snapshot()['exception_class'] == 'UNKNOWN'


def test_success_stderr_unchanged(offline, capsys):
    from latch_allow_smoke import main
    from latch_mcp_transport import OfflineMcpClient
    client = OfflineMcpClient(result())
    assert main(['--execute-live-allow'], client_factory=lambda **kw: client, window_check=lambda: True) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out) == dict(result='PASS', dispatch_count=1, retries=0)
