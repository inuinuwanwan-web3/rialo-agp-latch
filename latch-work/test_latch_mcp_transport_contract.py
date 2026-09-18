"""Offline MCP fixtures only. No real MCP client is instantiated."""
import io
import json
import threading
import time

import pytest
from test_latch_allow_contract import offline


def envelope(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def success():
    return envelope({"status": 200, "headers": {"content-type": "application/json"}, "data": {
        "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": '{"guess":"synthetic-answer"}'}}]}})


def request():
    from latch_proxy_solver import BASE_URL, CHAT_PATH
    return {"method": "POST", "url": BASE_URL + CHAT_PATH,
            "json": {"model": "synthetic-model"}, "timeout_seconds": 1.0}


@pytest.mark.parametrize("result", [
    envelope({"authorized": False, "deniedBy": "endpoint_0", "reason": "synthetic"}),
    {"isError": True, "content": []},
    {"content": [{"type": "text", "text": "not-json"}]},
    envelope({}), envelope({"authorized": "ALLOW"}),
    envelope({"authorized": True, "decision": "DENY"}),
    {**success(), "structuredContent": {"authorized": False}},
    envelope({"authorized": True, "response": {"status": 200, "body": "bad"}}),
    envelope({"authorized": True}),
    {"content": [{"type": "text", "text": '{"authorized":false,"authorized":true}'}]},
    {**success(), "isError": 0},
    {"content": success()["content"] * 2},
    envelope({"authorized": 1}),
    envelope({"authorized": True, "response": {"status": 403, "body": {}}}),
])
def test_rejects_without_retry(offline, result):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    client = OfflineMcpClient(result)
    with pytest.raises(offline[1], match="^Latch MCP request failed closed$"):
        LatchMcpTransport(client).send(request())
    assert len(client.calls) == 1


def test_unavailable(offline):
    from latch_mcp_transport import LatchMcpTransport
    with pytest.raises(offline[1], match="failed closed"):
        LatchMcpTransport().send(request())


def test_client_error_sanitized(offline):
    from latch_mcp_transport import LatchMcpTransport
    class Client:
        calls = 0
        def call_tool(self, *args, **kwargs):
            self.calls += 1
            raise ConnectionError("private synthetic diagnostic")
    client = Client()
    with pytest.raises(offline[1]) as caught:
        LatchMcpTransport(client).send(request())
    assert str(caught.value) == "Latch MCP request failed closed"
    assert caught.value.__suppress_context__
    assert client.calls == 1


def test_deadline_and_late_response_never_retry(offline):
    from latch_mcp_transport import LatchMcpTransport
    release, completed = threading.Event(), threading.Event()
    class Client:
        calls = 0
        def call_tool(self, *args, **kwargs):
            self.calls += 1
            release.wait(2)
            completed.set()
            return success()
    client = Client()
    transport = LatchMcpTransport(client, timeout_seconds=0.02)
    before = time.monotonic()
    try:
        with pytest.raises(offline[1]):
            transport.send(request())
        assert time.monotonic() - before < 0.5
    finally:
        release.set()
        assert completed.wait(1)
    with pytest.raises(offline[1]):
        transport.send(request())
    assert client.calls == 1


def test_synthetic_success_mapping_and_solver_entry(offline):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    from latch_proxy_solver import LatchProxySolver, main
    client = OfflineMcpClient(success())
    solver = LatchProxySolver(model="synthetic-model", transport=LatchMcpTransport(client))
    output = io.StringIO()
    assert main(solver=solver, stdin=io.StringIO('{"state":{},"history":[]}'), stdout=output) == 0
    assert json.loads(output.getvalue()) == {"guess": "synthetic-answer"}
    assert len(client.calls) == 1
    name, arguments, timeout = client.calls[0]
    assert name == "latch_authorize"
    assert set(arguments) == {"method", "path", "body"}
    assert arguments["method"] == "POST" and arguments["path"] == "/v1/chat/completions"
    assert arguments["body"]["model"] == "synthetic-model" and timeout == 5.0


def test_unverified_live_success_stays_blocked(offline):
    from latch_mcp_transport import LatchMcpTransport
    class Client:
        def call_tool(self, *args, **kwargs):
            return success()
    with pytest.raises(offline[1]):
        LatchMcpTransport(Client()).send(request())


def test_entry_failure_emits_nothing(offline):
    from latch_proxy_solver import main, LatchProxySolver
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    client = OfflineMcpClient(envelope({"authorized": False}))
    solver = LatchProxySolver(model="synthetic", transport=LatchMcpTransport(client))
    for supplied in (None, solver):
        output = io.StringIO()
        assert main(solver=supplied, stdin=io.StringIO('{"state":{},"history":[]}'), stdout=output) == 2
        assert output.getvalue() == ""
    assert len(client.calls) == 1


@pytest.mark.parametrize("timeout", [0, -1, True, float('nan'), float('inf')])
def test_invalid_timeout(offline, timeout):
    from latch_mcp_transport import LatchMcpTransport
    with pytest.raises(ValueError):
        LatchMcpTransport(timeout_seconds=timeout)


def test_probe_preserves_sanitized_deny_metadata(offline):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient, LatchDenied
    client = OfflineMcpClient(envelope({"authorized": False, "deniedBy": "endpoint_0", "reason": "private diagnostic"}))
    with pytest.raises(LatchDenied) as caught:
        LatchMcpTransport(client).probe_deny()
    assert caught.value.decision == 'DENY'
    assert caught.value.deciding_filter == 'endpoint_0'
    assert 'private' not in str(caught.value)
    assert client.calls[0][1] == {'method': 'GET', 'path': '/__latch_policy_denial_probe__'}
    assert len(client.calls) == 1


@pytest.mark.parametrize('name,args', [
    ('latch_proxy', {}), ('latch_run_action', {}),
    ('latch_authorize', {'method': 'POST', 'path': '/v1/chat/completions'}),
    ('latch_authorize', {'method': 'GET', 'path': '/__latch_policy_denial_probe__', 'headers': {}}),
])
def test_registered_client_blocks_other_requests_before_launch(offline, name, args):
    from latch_registered_client import RegisteredLatchClient
    client = RegisteredLatchClient()
    with pytest.raises(offline[1], match='Registered Latch MCP failed closed'):
        client.call_tool(name, args, timeout_seconds=1)
    assert client.dispatch_count == 0 and client.retries == 0 and not client.connected


def test_registered_client_never_reuses_dispatched_instance(offline):
    from latch_registered_client import RegisteredLatchClient, PROBE
    client = RegisteredLatchClient()
    client._used = True
    with pytest.raises(offline[1]):
        client.call_tool('latch_authorize', PROBE, timeout_seconds=1)
    assert client.dispatch_count == 0


def test_fetch_guard_one_request_and_no_proxy():
    import subprocess
    from pathlib import Path
    script = r'''
const assert = require('node:assert/strict');
let calls = 0;
globalThis.fetch = async (url, init) => {
  calls++;
  assert.equal(init.redirect, 'error');
  assert.ok(init.signal);
  return {ok: true};
};
require('./latch_deny_fetch_guard.cjs');
(async () => {
  const url = process.env.LATCH_URL + '/proxy/.well-known/latch-self/authorize';
  await assert.rejects(fetch(process.env.LATCH_URL + '/proxy/v1/chat/completions', {method:'POST', body:'{}'}));
  const init = {method:'POST', body:JSON.stringify({method:'GET',path:'/__latch_policy_denial_probe__',headers:{}})};
  await fetch(url, init);
  await assert.rejects(fetch(url, init));
  assert.equal(calls, 1);
})().catch(() => process.exitCode = 1);
'''
    import os
    result = subprocess.run(['node', '-e', script], cwd=Path(__file__).parent,
                            env={'PATH': os.environ['PATH'], 'LATCH_URL': 'https://offline.invalid',
                                 'LATCH_SMOKE_TIMEOUT_MS': '1000'},
                            capture_output=True, timeout=3)
    assert result.returncode == 0
    assert result.stdout == b'' and result.stderr == b''


def success_payload():
    return json.loads(success()["content"][0]["text"])


@pytest.mark.parametrize("updates,remove", [
    ({}, "status"), ({"status": True}, None), ({"status": "200"}, None),
    ({"status": 200.0}, None), ({"status": None}, None),
    ({"status": 204}, None), ({"status": 302}, None), ({"status": 403}, None),
    ({}, "headers"), ({"headers": None}, None), ({"headers": []}, None),
    ({"headers": {"content-type": 1}}, None),
    ({"headers": {"bad name": "value"}}, None),
    ({"headers": {"x-test": "value\r\nother:value"}}, None),
    ({"headers": {"X-Test": "a", "x-test": "b"}}, None),
    ({}, "data"), ({"data": None}, None), ({"data": "{}"}, None),
    ({"data": []}, None), ({"data": {}}, None), ({"data": True}, None),
    ({"authorized": False, "deniedBy": "endpoint_0", "reason": "synthetic"}, None),
    ({"authorized": True}, None), ({"authorized": "ALLOW"}, None),
    ({"decision": "ALLOW"}, None), ({"decision": "DENY"}, None),
    ({"deniedBy": "endpoint_0"}, None), ({"receipt": "synthetic"}, None),
])
def test_success_envelope_rejects_invalid_fields(offline, updates, remove, capsys):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    payload = success_payload()
    payload.update(updates)
    if remove:
        del payload[remove]
    client = OfflineMcpClient(envelope(payload))
    with pytest.raises(offline[1], match="^Latch MCP request failed closed$"):
        LatchMcpTransport(client).send(request())
    assert len(client.calls) == 1
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("headers", [{}, {"content-type": "application/json", "x-latch-test": "synthetic"}])
def test_expected_success_data_only_and_matching_structured_content(offline, headers, capsys):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    payload = success_payload()
    payload["headers"] = headers
    result = envelope(payload)
    result.update(isError=False, structuredContent=payload)
    client = OfflineMcpClient(result)
    response = LatchMcpTransport(client).send(request())
    assert response.status == 200
    assert json.loads(response.body) == payload["data"]
    assert len(client.calls) == 1
    assert capsys.readouterr() == ("", "")


def test_invalid_model_data_still_rejected_by_solver(offline):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    from latch_proxy_solver import LatchProxySolver
    payload = success_payload()
    payload["data"] = {"error": {"message": "synthetic diagnostic"}}
    client = OfflineMcpClient(envelope(payload))
    solver = LatchProxySolver(model="synthetic", transport=LatchMcpTransport(client))
    with pytest.raises(offline[1], match="^Proxy Solver failed closed$"):
        solver.decide({}, [])
    assert len(client.calls) == 1


def test_probe_cannot_return_success(offline):
    from latch_mcp_transport import LatchMcpTransport, OfflineMcpClient
    client = OfflineMcpClient(success())
    with pytest.raises(offline[1]):
        LatchMcpTransport(client).probe_deny()
    assert len(client.calls) == 1
