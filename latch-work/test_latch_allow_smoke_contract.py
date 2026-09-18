"""Offline-only ALLOW entry contracts; never launches registered live MCP."""
import json
import threading
from unittest.mock import Mock
import pytest
from test_latch_allow_contract import offline


def result(payload=None):
    if payload is None:
        payload = {'status': 200, 'headers': {'x-test': 'synthetic-private'}, 'data': {
            'object': 'chat.completion', 'choices': [{'finish_reason': 'length',
            'message': {'role': 'assistant', 'content': 'synthetic-private'}}],
            'usage': {'prompt_tokens': 8, 'completion_tokens': 1, 'total_tokens': 9}}}
    return {'content': [{'type': 'text', 'text': json.dumps(payload)}]}


def test_no_flag_zero_dispatch(offline, capsys):
    from latch_allow_smoke import main
    factory = Mock(side_effect=AssertionError('must not create client'))
    assert main([], client_factory=factory) == 0
    factory.assert_not_called()
    assert json.loads(capsys.readouterr().out)['dispatch_count'] == 0


@pytest.mark.parametrize('change', ['path', 'method', 'model', 'bound', 'bool_bound', 'extra'])
def test_fixed_request_rejects_changes(offline, change):
    from latch_smoke_request import allow_request
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    request = allow_request()
    if change == 'path': request['path'] = '/v1/responses'
    if change == 'method': request['method'] = 'GET'
    if change == 'model': request['body']['model'] = 'other'
    if change == 'bound': request['body']['max_completion_tokens'] = 2
    if change == 'bool_bound': request['body']['max_completion_tokens'] = True
    if change == 'extra': request['body']['stream'] = False
    client = OfflineMcpClient(result())
    with pytest.raises(offline[1]):
        LatchMcpTransport(client).smoke_allow(request, execute_live_allow=True)
    assert client.calls == []


def test_guard_required_and_retry_immutable(offline):
    from latch_registered_client import RegisteredAllowLatchClient
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    from latch_smoke_request import allow_request
    with pytest.raises(offline[1]): RegisteredAllowLatchClient()
    client = RegisteredAllowLatchClient(execute_live_allow=True)
    assert client.retries == 0
    with pytest.raises(AttributeError): client.retries = 1
    fixture = OfflineMcpClient(result())
    with pytest.raises(offline[1]): LatchMcpTransport(fixture).smoke_allow(allow_request())
    assert fixture.calls == []


@pytest.mark.parametrize('payload', [None, {},
    {'authorized': False, 'deniedBy': 'endpoint_0', 'reason': 'synthetic-private'},
    {'authorized': 'unknown'},
    {'status': 200, 'headers': {}, 'data': {}, 'authorized': True}])
def test_exactly_one_attempt_success_or_failure(offline, payload):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    from latch_smoke_request import allow_request
    client = OfflineMcpClient(result(payload))
    transport = LatchMcpTransport(client)
    if payload is None:
        assert transport.smoke_allow(allow_request(), execute_live_allow=True).status == 200
    else:
        with pytest.raises(offline[1]): transport.smoke_allow(allow_request(), execute_live_allow=True)
    with pytest.raises(offline[1]): transport.smoke_allow(allow_request(), execute_live_allow=True)
    assert len(client.calls) == 1


def test_timeout_after_dispatch_no_second_attempt(offline, monkeypatch):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    from latch_smoke_request import allow_request
    client = OfflineMcpClient(result())
    original = client.call_tool
    release, entered, completed = threading.Event(), threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        response = original(*args, **kwargs)
        entered.set()
        release.wait(2)
        completed.set()
        return response
    monkeypatch.setattr(client, 'call_tool', delayed)
    transport = LatchMcpTransport(client, timeout_seconds=0.03)
    try:
        with pytest.raises(offline[1]): transport.smoke_allow(allow_request(), execute_live_allow=True)
        assert entered.is_set()
        with pytest.raises(offline[1]): transport.smoke_allow(allow_request(), execute_live_allow=True)
    finally:
        release.set()
        assert completed.wait(1)
    assert len(client.calls) == 1


@pytest.mark.parametrize('payload,expected', [(None, 0), ({}, 2),
    ({'authorized': False, 'deniedBy': 'endpoint_0', 'reason': 'synthetic-private'}, 2)])
def test_entry_only_sanitized_summary(offline, capsys, monkeypatch, payload, expected):
    from latch_allow_smoke import main
    from latch_mcp_transport import OfflineMcpClient
    monkeypatch.setenv('LATCH_TOKEN', 'synthetic-private')
    client = OfflineMcpClient(result(payload))
    factory = lambda **kwargs: client
    assert main(['--execute-live-allow'], client_factory=factory, window_check=lambda: True) == expected
    output = capsys.readouterr()
    assert 'synthetic-private' not in output.out + output.err
    assert len(client.calls) == 1


def test_window_and_unknown_flags_no_client(offline, capsys):
    from latch_allow_smoke import main
    factory = Mock()
    assert main(['--execute-live-allow'], client_factory=factory, window_check=lambda: False) == 2
    assert main(['--retry=1'], client_factory=factory) == 2
    factory.assert_not_called()


@pytest.mark.parametrize('grant', [{'authorized': True}, {'authorized': False},
    {'authorized': 'true'}, {'authorized': True, 'deniedBy': 'endpoint_0'}])
def test_fetch_guard_allows_only_auth_then_one_proxy(grant):
    import os
    import subprocess
    from pathlib import Path
    script = r'''
const assert = require('node:assert/strict');
let calls = 0;
globalThis.fetch = async (url, init) => {
  calls++;
  assert.equal(init.redirect, 'error');
  assert.ok(init.signal);
  return {ok: true, clone: () => ({json: async () => JSON.parse(process.env.SYNTHETIC_GRANT)})};
};
require('./latch_allow_fetch_guard.cjs');
(async () => {
  const root = process.env.LATCH_URL;
  const body = {model:'gpt-4o-mini',messages:[{role:'user',content:'Hi'}],max_completion_tokens:1};
  const proxy = {method:'POST',body:JSON.stringify(body)};
  await assert.rejects(fetch(root+'/proxy/v1/chat/completions', proxy));
  await fetch(root+'/proxy/.well-known/latch-self/authorize', {method:'POST',body:JSON.stringify({method:'POST',path:'/v1/chat/completions',headers:{},body})});
  await assert.rejects(fetch('https://api.openai.com/v1/chat/completions', proxy));
  await assert.rejects(fetch(root+'/proxy/v1/chat/completions', {method:'POST',body:JSON.stringify({...body,max_completion_tokens:2})}));
  const grant = JSON.parse(process.env.SYNTHETIC_GRANT);
  const allowed = grant.authorized === true && !('deniedBy' in grant);
  if (allowed) await fetch(root+'/proxy/v1/chat/completions', proxy);
  else await assert.rejects(fetch(root+'/proxy/v1/chat/completions', proxy));
  await assert.rejects(fetch(root+'/proxy/v1/chat/completions', proxy));
  assert.equal(calls, allowed ? 2 : 1);
})().catch(() => process.exitCode=1);
'''
    completed = subprocess.run(['node', '-e', script], cwd=Path(__file__).parent,
        env={'PATH': os.environ['PATH'], 'LATCH_URL':'https://offline.invalid', 'LATCH_SMOKE_TIMEOUT_MS':'1000', 'SYNTHETIC_GRANT':json.dumps(grant)},
        capture_output=True, timeout=3)
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == b''


def test_registered_allow_client_rejects_changed_request_and_reuse(offline):
    from latch_registered_client import RegisteredAllowLatchClient
    from latch_smoke_request import allow_request
    client = RegisteredAllowLatchClient(execute_live_allow=True)
    wrong = allow_request()
    wrong['body']['max_completion_tokens'] = True
    with pytest.raises(offline[1]):
        client.call_tool('latch_authorize', wrong, timeout_seconds=1)
    assert client.dispatch_count == 0
    client._used = True
    with pytest.raises(offline[1]):
        client.call_tool('latch_authorize', allow_request(), timeout_seconds=1)
    assert client.dispatch_count == 0
