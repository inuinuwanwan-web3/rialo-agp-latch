from __future__ import annotations

import json
import io
import errno
import os
import re
import select
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import subprocess
import threading
from typing import Any

from .config import Settings, resolve_launch
from .diagnostics import categories, failure_details
from .write_safety import READ_TOOLS, WRITE_TOOLS, WriteSafety, WriteBlocked


class McpInitializationError(RuntimeError):
    def __init__(self, details):
        self.details = details
        super().__init__("MCP initialization failed")


class McpReadDeadline(RuntimeError):
    """The enclosing read-only workflow has reached its deadline."""


class McpTransportError(RuntimeError):
    """The connection ended without a matching response; outcome is unknown."""


class McpResponseError(RuntimeError):
    """Sanitized failure; diagnostics are used in memory only."""

    def __init__(self, payload):
        text = json.dumps(payload)
        self.categories = categories(text)
        self.rpc_code = payload.get("code") if isinstance(payload, dict) and type(payload.get("code")) is int else None
        self.retryable = bool(re.search(r"429|rate[ _-]*limit|throttl|retry[ _-]*after|no valid session", text, re.I))
        self.retryable |= bool(set(self.categories) & {"fetch_failed", "connect_timeout", "dns_failure", "connection_reset", "connection_refused"})
        self.rejected = bool(re.search(r"\b(401|403|unauthorized|forbidden|invalid token|authentication failed)\b", text, re.I))
        def rejection(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "code" and item in ("FORBIDDEN", "UNAUTHORIZED", "TRACK_FULL", "REGISTRATION_CLOSED", "NOT_VERIFIED", "INSUFFICIENT_CREDIT", "INVALID_TRACK"):
                        self.rejected = True
                    if isinstance(item, (dict, list)):
                        rejection(item)
            elif isinstance(value, list):
                for item in value:
                    rejection(item)
        rejection(payload)
        self.retry_after = 0.0
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower().replace("_", "-") in {"retry-after", "retryafter"}:
                        parse(item)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, str):
                match = re.search(r"retry[ _-]*after\s*[:=]\s*([^\r\n]+)", value, re.I)
                if match:
                    parse(match.group(1))
        def parse(value):
            try:
                delay = float(value)
            except (ValueError, TypeError):
                try:
                    delay = (parsedate_to_datetime(str(value)) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    return
            if 0 <= delay < float("inf"):
                self.retry_after = max(self.retry_after, delay)
        visit(payload)
        super().__init__("MCP response failed")


READ_ONLY_TOOLS = frozenset({"my_race", "list_tracks", "sigil_balance", "track_state"})
MAX_READ_ATTEMPTS = 3


def _transient_transport_error(error: Exception) -> bool:
    return isinstance(error, (McpTransportError, ConnectionError, TimeoutError)) or (
        isinstance(error, OSError)
        and error.errno in {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED,
                            errno.ETIMEDOUT, errno.EAGAIN, errno.EINTR}
    )


class McpGateway:
    """Minimal stdio JSON-RPC MCP client. It is created only with --execute."""

    def __init__(self, settings: Settings, *, allow_writes=False, contract=None) -> None:
        self.settings = settings
        self._allow_writes = allow_writes
        self._write_safety = WriteSafety(settings, contract)
        if allow_writes:
            self._write_safety.import_legacy()
        self._write_authorized = False
        self._launch = resolve_launch(settings)
        self._id = 0
        self._last_list_end = None
        self.sleep = time.sleep
        self.proc: subprocess.Popen[str] | None = None
        self._stderr_categories = set()
        self.protocol_version: str | None = None
        self._connect()

    def _connect(self) -> None:
        settings = self.settings
        command, args, environment = self._launch
        versions = [settings.mcp_protocol_version, *[v for v in ("2024-11-05", "2025-06-18") if v != settings.mcp_protocol_version]]
        last_error: Exception | None = None
        for version in versions:
            try:
                self._stderr_categories = set()
                # A restarted stdio process is a new JSON-RPC connection.
                self._id = 0
                self.proc = subprocess.Popen(
                    [command, *args], text=True,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment,
                    cwd=settings.mcp_cwd,
                )
                self._stderr_categories = set()
                if isinstance(getattr(self.proc, "stderr", None), io.TextIOWrapper):
                    stream, labels = self.proc.stderr, self._stderr_categories
                    def drain(stream=stream, labels=labels):
                        # Bounded reads also handle stderr without newlines.
                        tail = ""
                        try:
                            while chunk := os.read(stream.fileno(), 4096):
                                text = tail + chunk.decode("utf-8", errors="replace")
                                labels.update(categories(text))
                                tail = text[-128:]
                        except (OSError, ValueError):
                            pass
                    self._stderr_thread = threading.Thread(target=drain, daemon=True)
                    self._stderr_thread.start()
                self._buffer = b""
                self.request("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "agp-race-agent", "version": "0.1.0"}})
                self.notify("notifications/initialized")
                self.protocol_version = version
                return
            except McpReadDeadline:
                self.close()
                raise
            except (OSError, RuntimeError, json.JSONDecodeError, BrokenPipeError) as error:
                last_error = error
                self.last_failure = failure_details(error, self.proc, self._stderr_categories)
                self.close()
        raise McpInitializationError(self.last_failure) from None

    def request(self, method: str, params: dict[str, Any]) -> Any:
        if method != 'initialize':
            name = params.get('name')
            if method != 'tools/call' or name not in READ_TOOLS | WRITE_TOOLS:
                raise WriteBlocked()
            if name in WRITE_TOOLS:
                if getattr(self, '_write_authorized', None) != (name, params.get('arguments', {})):
                    raise WriteBlocked()
                self._write_authorized = None
        self._id += 1
        assert self.proc and self.proc.stdin and self.proc.stdout
        deadline = time.monotonic() + 90
        workflow_deadline = getattr(self, "read_deadline", None)
        if workflow_deadline is not None:
            remaining = workflow_deadline - time.time()
            if remaining <= 0:
                raise McpReadDeadline("Read deadline reached")
            deadline = min(deadline, time.monotonic() + remaining)
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, deadline)
        while line := self._readline(deadline):
            msg = json.loads(line)
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise McpResponseError(msg["error"])
                return msg["result"]
        raise McpTransportError("MCP server closed its output")

    def _readline(self, deadline):
        stream = self.proc.stdout
        # In-memory Fake transports used by tests have no file descriptor.
        if not isinstance(stream, io.TextIOWrapper):
            return stream.readline()
        if time.monotonic() >= deadline:
            raise McpTransportError("MCP response timed out")
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
                raise McpTransportError("MCP response timed out")
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                raise McpTransportError("MCP output ended")
            self._buffer += chunk
            if len(self._buffer) > 4 * 1024 * 1024:
                raise McpTransportError("MCP response too large")
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("utf-8")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if method != 'notifications/initialized' or params not in (None, {}):
            raise WriteBlocked()
        assert self.proc and self.proc.stdin
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message, time.monotonic() + 90)

    def _write(self, message, deadline):
        stream = self.proc.stdin
        text = json.dumps(message) + "\n"
        if not isinstance(stream, io.TextIOWrapper):
            stream.write(text)
            stream.flush()
            return
        fd = stream.fileno()
        os.set_blocking(fd, False)
        data = memoryview(text.encode("utf-8"))
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise McpTransportError("MCP send timed out")
            try:
                data = data[os.write(fd, data):]
            except BlockingIOError:
                continue

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in READ_TOOLS | WRITE_TOOLS:
            raise WriteBlocked()
        if not hasattr(self, '_write_safety'):
            self._write_safety = WriteSafety(getattr(self, 'settings', None))
        if name in WRITE_TOOLS:
            if not getattr(self, '_allow_writes', False):
                raise WriteBlocked()
            def send():
                self._write_authorized = (name, arguments or {})
                try:
                    return self._call(name, arguments)
                finally:
                    self._write_authorized = False
            return self._write_safety.perform(name, arguments or {}, send, lambda: self._call('my_race'))
        result = self._call(name, arguments)
        self._write_safety.observe(name, result)
        return result

    def _call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name in READ_ONLY_TOOLS and getattr(self, "_needs_reconnect", False):
            self._connect()
            self._needs_reconnect = False
        attempts = getattr(self, 'read_attempts', MAX_READ_ATTEMPTS) if name in READ_ONLY_TOOLS else 1
        def sleep(seconds):
            deadline = getattr(self, "read_deadline", None)
            if deadline is not None and time.time() + seconds >= deadline:
                raise McpReadDeadline("Read deadline reached")
            getattr(self, "sleep", time.sleep)(seconds)
        for attempt in range(attempts):
            if name == "list_tracks" and getattr(self, "_last_list_end", None) is not None:
                delay = max(0, getattr(self, 'list_interval', 60) - (time.monotonic() - self._last_list_end))
                if delay:
                    sleep(delay)
            try:
                result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
                if not isinstance(result, dict):
                    raise McpResponseError({})
                if result.get("isError") or "error" in result:
                    raise McpResponseError(result)
                if isinstance(result.get("structuredContent"), dict):
                    decoded = result["structuredContent"]
                else:
                    text = "".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
                    decoded = json.loads(text) if text else result
                if isinstance(decoded, dict) and (decoded.get("isError") or "error" in decoded):
                    raise McpResponseError(decoded)
                return decoded
            except (OSError, McpTransportError, McpResponseError) as error:
                self.last_failure = failure_details(error, getattr(self, "proc", None), getattr(self, "_stderr_categories", ()))
                if name not in READ_ONLY_TOOLS and isinstance(error, McpResponseError):
                    raise
                retryable = error.retryable and not error.rejected if isinstance(error, McpResponseError) else _transient_transport_error(error)
                if not retryable:
                    raise
                self.close()
                self._needs_reconnect = True
                if attempt + 1 == attempts:
                    if isinstance(error, McpResponseError):
                        raise
                    raise McpTransportError("MCP operation response unavailable") from None
                sleep(max(60 * 2 ** attempt, getattr(error, "retry_after", 0)))
                self._connect()
                self._needs_reconnect = False
            finally:
                if name == "list_tracks":
                    self._last_list_end = time.monotonic()

    def close(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
        if hasattr(self.proc, "wait"):
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            thread = getattr(self, "_stderr_thread", None)
            if thread:
                thread.join(timeout=1)
            for stream in (self.proc.stdin, self.proc.stdout, getattr(self.proc, "stderr", None)):
                if stream:
                    stream.close()
