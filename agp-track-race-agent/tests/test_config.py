from pathlib import Path
import tempfile
import textwrap
import unittest
from io import StringIO
from unittest.mock import patch

from agp_race_agent.config import Settings, _load_codex_agp, preflight, resolve_launch
from agp_race_agent.agent import RaceAgent
from agp_race_agent.audit import AuditLog
from agp_race_agent.gateway import McpGateway


class ConfigTests(unittest.TestCase):
    def test_dollar_credit_cap_is_enforced(self) -> None:
        settings = Settings("cmd", [], [], {}, "direct", None, "2025-03-26", 1.01, 1, 1, "solver", Path("logs"))
        self.assertTrue(any("$1.00" in item for item in preflight(settings)))

    def test_reads_only_named_codex_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(textwrap.dedent("""
                [mcp_servers.unrelated]
                command = "do-not-use"
                env = { SECRET = "unrelated-secret" }

                [mcp_servers.agp-track-race]
                command = "agp-command"
                args = ["serve"]
                env = { AGP_TOKEN = "agp-secret" }
            """), encoding="utf-8")
            resolved = _load_codex_agp(path)
        self.assertEqual(resolved["command"], "agp-command")
        self.assertEqual(resolved["args"], ["serve"])
        self.assertEqual(resolved["env"], {"AGP_TOKEN": "agp-secret"})
        self.assertNotIn("unrelated", str(resolved))

    def test_operation_log_does_not_persist_tool_response_values(self) -> None:
        class Gateway:
            def call(self, _tool: str, _arguments: object) -> dict[str, str]:
                return {"token": "never-write-this-value", "status": "ok"}

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings("cmd", [], [], {}, "direct", None, "2025-03-26", 1.0, 1, 1, "solver", Path(tmp))
            audit = AuditLog(Path(tmp))
            RaceAgent(settings, Gateway(), audit).call("my_race")
            contents = audit.path.read_text(encoding="utf-8")
        self.assertNotIn("never-write-this-value", contents)

    def test_expands_codex_and_parent_environment_without_a_shell(self) -> None:
        settings = Settings("$PARENT_CMD", ["--value=${MCP_ARG}", "$PARENT_ARG"], [], {"MCP_ARG": "private-value"}, "codex", None, "2025-03-26", 1.0, 1, 1, "solver", Path("logs"))
        import os
        old_command, old_arg = os.environ.get("PARENT_CMD"), os.environ.get("PARENT_ARG")
        os.environ["PARENT_CMD"], os.environ["PARENT_ARG"] = "safe-command", "safe-argument"
        try:
            command, args, environment = resolve_launch(settings)
        finally:
            if old_command is None: os.environ.pop("PARENT_CMD")
            else: os.environ["PARENT_CMD"] = old_command
            if old_arg is None: os.environ.pop("PARENT_ARG")
            else: os.environ["PARENT_ARG"] = old_arg
        self.assertEqual((command, args), ("safe-command", ["--value=private-value", "safe-argument"]))
        self.assertEqual(environment["MCP_ARG"], "private-value")

    def test_unresolved_variable_blocks_launch_before_process_start(self) -> None:
        settings = Settings("cmd", ["${MISSING_MCP_VARIABLE}"], [], {}, "codex", None, "2025-03-26", 1.0, 1, 1, "solver", Path("logs"))
        self.assertTrue(any("未解決" in item for item in preflight(settings)))

    def test_initialization_restarts_then_falls_back_without_notification_params(self) -> None:
        class Process:
            def __init__(self, response: str) -> None:
                self.stdin, self.stdout, self.stderr = StringIO(), StringIO(response), StringIO()
                self.terminated = False

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

        failed = Process("")
        succeeded = Process('{"jsonrpc":"2.0","id":1,"result":{}}\n')
        settings = Settings("cmd", [], [], {}, "direct", None, "2025-03-26", 1.0, 1, 1, "solver", Path("logs"))
        with patch("agp_race_agent.gateway.subprocess.Popen", side_effect=[failed, succeeded]) as spawn:
            gateway = McpGateway(settings)
        self.assertEqual(spawn.call_count, 2)
        self.assertTrue(failed.terminated)
        self.assertEqual(gateway.protocol_version, "2024-11-05")
        messages = [__import__("json").loads(line) for line in succeeded.stdin.getvalue().splitlines()]
        self.assertEqual(messages[0]["params"]["protocolVersion"], "2024-11-05")
        self.assertEqual(messages[1], {"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_remote_header_secret_never_reaches_argv():
    settings = Settings('npx', ['-y','mcp-remote','https://example.test/mcp','--header','Authorization: ${SECRET}'], [], {'SECRET':'Bearer private-secret'}, 'codex', None, '2025-03-26', 1, 1, 1, '', Path('logs'))
    command, args, environment = resolve_launch(settings)
    assert 'private-secret' not in str(args)
    assert args[-1] == 'Authorization:${AGP_MCP_PRIVATE_HEADER_4}'
    assert environment['AGP_MCP_PRIVATE_HEADER_4'] == ' Bearer private-secret'
