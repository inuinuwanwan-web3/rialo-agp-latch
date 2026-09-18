import errno
import json
from io import StringIO
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.agent import RaceAgent
from agp_race_agent.audit import AuditLog
from agp_race_agent.gateway import McpGateway, McpTransportError, READ_ONLY_TOOLS
from tests.test_agent import local_settings


@pytest.fixture(autouse=True)
def no_real_wait():
    with patch("agp_race_agent.gateway.time.sleep"):
        yield


def gateway_with_mock_transport():
    gateway = McpGateway.__new__(McpGateway)
    gateway.request = Mock()
    gateway.close = Mock()
    gateway._connect = Mock()
    gateway.call = gateway._call  # Transport-unit fixture only; public gate tested separately.
    return gateway


@pytest.mark.parametrize("tool", sorted(READ_ONLY_TOOLS))
@pytest.mark.parametrize("error", [
    McpTransportError("lost"), BrokenPipeError(), ConnectionResetError(),
    TimeoutError(), OSError(errno.EAGAIN, "temporary"),
])
def test_read_reconnects_then_retries(tool, error):
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = [error, {"content": [{"type": "text", "text": '{"ok":true}'}]}]
    assert gateway.call(tool) == {"ok": True}
    assert gateway.request.call_count == 2
    gateway.close.assert_called_once()
    gateway._connect.assert_called_once()
    assert gateway.request.call_args_list[0] == gateway.request.call_args_list[1]


def test_read_retries_are_bounded():
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = McpTransportError("lost")
    with pytest.raises(McpTransportError):
        gateway.call("track_state")
    assert gateway.request.call_count == 3
    assert gateway._connect.call_count == 2


def test_failed_reinitialization_stops_without_resending_tool():
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = McpTransportError("lost")
    gateway._connect.side_effect = RuntimeError("initialization failed")
    with pytest.raises(RuntimeError):
        gateway.call("track_state")
    gateway.request.assert_called_once()
    gateway._connect.assert_called_once()


@pytest.mark.parametrize("tool", ["start_track", "ask", "guess", "unknown_tool"])
def test_mutating_and_unknown_tools_are_never_retried(tool):
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = McpTransportError("lost")
    with pytest.raises(McpTransportError):
        gateway.call(tool)
    gateway.request.assert_called_once()
    gateway._connect.assert_not_called()


@pytest.mark.parametrize("error", [
    RuntimeError("server error"), json.JSONDecodeError("bad", "", 0),
    PermissionError(errno.EACCES, "denied"),
])
def test_non_transport_errors_are_not_retried(error):
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = error
    with pytest.raises(type(error)):
        gateway.call("track_state")
    gateway.request.assert_called_once()
    gateway._connect.assert_not_called()


def test_invalid_tool_json_is_not_retried():
    gateway = gateway_with_mock_transport()
    gateway.request.return_value = {"content": [{"type": "text", "text": "invalid"}]}
    with pytest.raises(json.JSONDecodeError):
        gateway.call("track_state")
    gateway.request.assert_called_once()
    gateway._connect.assert_not_called()


class FakeProcess:
    def __init__(self, response):
        self.stdin = StringIO()
        self.stdout = StringIO(response)
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


def test_eof_reinitializes_new_session_before_read_retry(tmp_path):
    initialized = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
    first = FakeProcess(initialized)
    second = FakeProcess(initialized + '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\\"finished\\":true}"}]}}\n')
    with patch("agp_race_agent.gateway.subprocess.Popen", side_effect=[first, second]) as spawn:
        gateway = McpGateway(local_settings(tmp_path))
        assert gateway.call("track_state") == {"finished": True}
        gateway.close()
    assert spawn.call_count == 2
    assert first.terminated
    for process in (first, second):
        messages = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        assert [message["method"] for message in messages] == [
            "initialize", "notifications/initialized", "tools/call",
        ]
        assert messages[-1]["params"]["name"] == "track_state"


@pytest.mark.parametrize("tool", ["ask", "guess"])
def test_agent_stops_after_uncertain_paid_operation(tmp_path, tool):
    gateway = gateway_with_mock_transport()
    state = {"finished": False, "remainingUsd": 1, "spentUsd": 0,
             "questionCostUsd": 0.01, "guessCostUsd": 0.01}
    gateway.request.side_effect = [
        {"content": [{"type": "text", "text": json.dumps(state)}]},
        McpTransportError("private-test-diagnostic"),
    ]
    audit = AuditLog(tmp_path)
    agent = RaceAgent(local_settings(tmp_path), gateway, audit)
    agent.solver = Mock()
    agent.solver.decide.return_value = {"question" if tool == "ask" else "guess": "test"}
    result = agent.solve()
    assert "MCP操作の応答を確認できない" in result
    assert [call.args[1]["name"] for call in gateway.request.call_args_list] == ["track_state", tool]
    agent.solver.decide.assert_called_once()
    gateway._connect.assert_not_called()
    assert "private-test-diagnostic" not in result + audit.path.read_text()


@pytest.mark.parametrize("entry,tool", [("run", "my_race"), ("solve", "track_state")])
def test_agent_stops_after_read_retry_exhaustion(tmp_path, entry, tool):
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = McpTransportError("private-test-diagnostic")
    audit = AuditLog(tmp_path)
    agent = RaceAgent(local_settings(tmp_path), gateway, audit)
    agent.solver = Mock()
    result = getattr(agent, entry)()
    assert "MCP操作の応答を確認できない" in result
    assert [call.args[1]["name"] for call in gateway.request.call_args_list] == [tool] * 3
    agent.solver.decide.assert_not_called()
    assert "private-test-diagnostic" not in result + audit.path.read_text()


@pytest.mark.parametrize('message', ['fetch failed', 'UND_ERR_CONNECT_TIMEOUT', 'getaddrinfo EAI_AGAIN', 'ECONNRESET'])
def test_remote_network_error_retries_read_without_logging_secret(message):
    from agp_race_agent.gateway import McpResponseError
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = [McpResponseError({'code':-32603,'message':message + ' Bearer private-secret'}), {'structuredContent':{'tracks':[]}}]
    assert gateway.call('list_tracks') == {'tracks':[]}
    assert gateway.request.call_count == 2
    assert 'private-secret' not in str(gateway.last_failure)
    assert gateway.last_failure['rpc_code'] == -32603


def test_auth_rejection_overrides_transient_message():
    from agp_race_agent.gateway import McpResponseError
    gateway = gateway_with_mock_transport()
    gateway.request.side_effect = McpResponseError({'message':'401 unauthorized fetch failed'})
    with pytest.raises(McpResponseError):
        gateway.call('sigil_balance')
    gateway.request.assert_called_once()
    gateway._connect.assert_not_called()


def test_initialization_captures_stderr_and_exit_without_secret(tmp_path):
    import sys
    from dataclasses import replace
    from agp_race_agent.gateway import McpInitializationError
    script = tmp_path / 'stderr_child.py'
    script.write_text("import sys; sys.stderr.write('fetch failed UND_ERR_CONNECT_TIMEOUT Bearer private-secret\\n'); sys.exit(7)")
    config = replace(local_settings(tmp_path), mcp_command=sys.executable, mcp_args=[str(script)])
    with pytest.raises(McpInitializationError) as caught:
        McpGateway(config)
    assert 'private-secret' not in str(caught.value.details)
    assert 'connect_timeout' in caught.value.details['categories']
    assert caught.value.details['child_exit_code'] == 7
