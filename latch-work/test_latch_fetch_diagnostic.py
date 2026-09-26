"""Synthetic fetch failures only; the MCP server is never launched."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from test_latch_allow_contract import offline

PRIVATE = 'synthetic-secret-url-header-body-credential'
CATEGORIES = ['DNS_FAILED', 'CONNECT_FAILED', 'TLS_FAILED', 'FETCH_TIMEOUT', 'UNKNOWN_FETCH_FAILED']


@pytest.mark.parametrize('phase', ['AUTHORIZE', 'PROXY'])
@pytest.mark.parametrize('cause,category', [
    ({'cause': {'code': 'ENOTFOUND'}}, 'DNS_FAILED'),
    ({'cause': {'code': 'EAI_AGAIN'}}, 'DNS_FAILED'),
    ({'cause': {'code': 'ECONNREFUSED'}}, 'CONNECT_FAILED'),
    ({'cause': {'code': 'EPERM'}}, 'CONNECT_FAILED'),
    ({'cause': {'code': 'ERR_TLS_CERT_ALTNAME_INVALID'}}, 'TLS_FAILED'),
    ({'cause': {'code': 'CERT_HAS_EXPIRED'}}, 'TLS_FAILED'),
    ({'cause': {'code': 'UND_ERR_CONNECT_TIMEOUT'}}, 'FETCH_TIMEOUT'),
    ({'name': 'TimeoutError'}, 'FETCH_TIMEOUT'),
    ({'cause': {'errors': [{'code': 'ECONNREFUSED'}, {'code': 'ENETUNREACH'}]}}, 'CONNECT_FAILED'),
    ({'cause': {'errors': [{'code': 'ENOTFOUND'}, {'code': 'ECONNREFUSED'}]}}, 'UNKNOWN_FETCH_FAILED'),
    ({'cause': {'code': PRIVATE}, 'message': 'ENOTFOUND '+PRIVATE}, 'UNKNOWN_FETCH_FAILED'),
    ({}, 'UNKNOWN_FETCH_FAILED'),
])
def test_guard_emits_only_fixed_marker(phase, cause, category):
    script = r'''
const assert = require('node:assert/strict');
const net = require('node:net');
net.Socket.prototype.connect = () => { throw new Error('Real network forbidden'); };
const phase = process.env.SYNTHETIC_PHASE;
const privateValue = process.env.SYNTHETIC_PRIVATE;
let calls = 0;
globalThis.fetch = async (url, init) => {
  calls++;
  assert.equal(init.redirect, 'error');
  assert.ok(init.signal);
  if (phase === 'PROXY' && calls === 1) {
    return {ok: true, clone: () => ({json: async () => ({authorized: true})})};
  }
  const error = Object.assign(new TypeError(privateValue), JSON.parse(process.env.SYNTHETIC_CAUSE));
  error.url = privateValue;
  error.headers = {Authorization: privateValue};
  throw error;
};
require('./latch_allow_fetch_guard.cjs');
(async () => {
  const base = process.env.LATCH_URL;
  const body = {model:'gpt-4o-mini',messages:[{role:'user',content:'Hi'}],max_completion_tokens:1};
  const authorize = () => fetch(base+'/proxy/.well-known/latch-self/authorize', {
    method:'POST', body:JSON.stringify({method:'POST',path:'/v1/chat/completions',headers:{},body})});
  const proxy = () => fetch(base+'/proxy/v1/chat/completions', {method:'POST',body:JSON.stringify(body)});
  if (phase === 'PROXY') await authorize();
  let caught;
  try { await (phase === 'AUTHORIZE' ? authorize() : proxy()); } catch (error) { caught = error; }
  assert.ok(caught);
  assert.equal(caught.message, `LATCH_SAFE_FETCH:${phase}:${process.env.SYNTHETIC_CATEGORY}`);
  assert.equal(caught.cause, undefined);
  assert.ok(!caught.stack.includes(privateValue));
  // Neither retry nor proxy fallback can reach originalFetch after failure.
  await assert.rejects(authorize(), {message:'Request blocked'});
  await assert.rejects(proxy(), {message:'Request blocked'});
  assert.equal(calls, phase === 'AUTHORIZE' ? 1 : 2);
  process.stdout.write('Error: '+caught.message);
})().catch(() => { process.exitCode = 1; });
'''
    completed = subprocess.run(['node', '-e', script], cwd=Path(__file__).parent,
        env={'PATH': os.environ['PATH'], 'LATCH_URL': 'https://offline.invalid',
             'LATCH_SMOKE_TIMEOUT_MS': '1000', 'SYNTHETIC_PHASE': phase,
             'SYNTHETIC_CAUSE': json.dumps(cause), 'SYNTHETIC_CATEGORY': category,
             'SYNTHETIC_PRIVATE': PRIVATE}, capture_output=True, timeout=3)
    assert completed.returncode == 0
    assert completed.stderr == b''
    assert completed.stdout.decode() == f'Error: LATCH_SAFE_FETCH:{phase}:{category}'
    assert PRIVATE.encode() not in completed.stdout + completed.stderr


@pytest.mark.parametrize('phase', ['AUTHORIZE', 'PROXY'])
@pytest.mark.parametrize('category', CATEGORIES)
def test_python_preserves_safe_fetch_category(offline, capsys, phase, category):
    from latch_allow_smoke import main
    from latch_mcp_transport import OfflineMcpClient
    client = OfflineMcpClient({'isError': True, 'content': [
        {'type': 'text', 'text': f'Error: LATCH_SAFE_FETCH:{phase}:{category}'}]})
    assert main(['--execute-live-allow'], client_factory=lambda **kw: client, window_check=lambda: True) == 2
    output = capsys.readouterr()
    data = json.loads(output.err)
    assert data['failure_stage'] == 'MCP_RESPONSE'
    assert data['exception_class'] == 'ValueError'
    assert data['error_category'] == category and data['fetch_phase'] == phase
    assert data['timeout'] is (category == 'FETCH_TIMEOUT')
    assert data['retries'] == 0 and data['fallback'] == 'NONE'
    assert len(client.calls) == 1
    assert json.loads(output.out) == dict(result='FAILED_CLOSED', retries=0, fallback='NONE')


@pytest.mark.parametrize('text', [
    f'Error: LATCH_SAFE_FETCH:AUTHORIZE:{PRIVATE}',
    f'Error: LATCH_SAFE_FETCH:{PRIVATE}:DNS_FAILED',
    f'Error: LATCH_SAFE_FETCH:AUTHORIZE:DNS_FAILED {PRIVATE}',
])
def test_untrusted_marker_not_forwarded(offline, capsys, text):
    from latch_allow_smoke import main
    from latch_mcp_transport import OfflineMcpClient
    client = OfflineMcpClient({'isError': True, 'content': [{'type': 'text', 'text': text}]})
    assert main(['--execute-live-allow'], client_factory=lambda **kw: client, window_check=lambda: True) == 2
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err
    data = json.loads(output.err)
    assert data['error_category'] == 'MCP_ERROR'
    assert 'fetch_phase' not in data
