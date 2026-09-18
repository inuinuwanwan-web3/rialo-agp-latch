"""Latch MCP boundary. No connection discovery, credentials, or network client.

Injected clients implement call_tool(name, arguments, *, timeout_seconds), with
SDK retries disabled. ALLOW executes upstream; this is not a preflight API.
General live success stays disabled; explicit fixed ALLOW smoke is a separate opt-in.
"""
import json
import math
import queue
import re
import threading

from agp_race_agent.solver import SolverError

_FAILURE = "Latch MCP request failed closed"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON")


class LatchDenied(SolverError):
    """Sanitized denial with allowlisted filter metadata only."""
    def __init__(self, filter_name):
        super().__init__(_FAILURE)
        self.decision = "DENY"
        self.deciding_filter = "endpoint_0" if filter_name == "endpoint_0" else "unreported"


def _decode(result):
    if type(result) is not dict or set(result) - {"content", "isError", "structuredContent", "_meta"}:
        raise ValueError("Invalid MCP envelope")
    if "isError" in result and result["isError"] is not False:
        raise ValueError("MCP error")
    content = result.get("content")
    if type(content) is not list or len(content) != 1:
        raise ValueError("Ambiguous MCP content")
    block = content[0]
    if type(block) is not dict or set(block) != {"type", "text"} or block["type"] != "text" or type(block["text"]) is not str:
        raise ValueError("Invalid MCP text")
    payload = json.loads(block["text"], object_pairs_hook=_unique, parse_constant=_invalid_constant)
    if type(payload) is not dict:
        raise ValueError("Invalid payload")
    if "structuredContent" in result:
        # Compare canonical JSON, avoiding Python's True == 1 equivalence.
        canonical = lambda value: json.dumps(value, sort_keys=True, allow_nan=False)
        if canonical(result["structuredContent"]) != canonical(payload):
            raise ValueError("Conflicting MCP content")
    if "authorized" in payload:
        # DENY is the only explicit authorization envelope supported by this
        # OpenAI link. Mixing authorization fields with success is ambiguous.
        if (payload["authorized"] is not False
                or set(payload) != {"authorized", "deniedBy", "reason"}
                or type(payload["deniedBy"]) is not str or not payload["deniedBy"]
                or type(payload["reason"]) is not str or not payload["reason"]):
            raise ValueError("Malformed or ambiguous authorization")
        raise LatchDenied(payload["deniedBy"])
    # Observed cached-server contract; real ALLOW is not runtime-verified.
    # Exactly 200 is required for this nonstreaming Chat Completions adapter.
    if (set(payload) != {"status", "headers", "data"}
            or type(payload["status"]) is not int or payload["status"] != 200):
        raise ValueError("Invalid success status or fields")
    headers = payload["headers"]
    if type(headers) is not dict:
        raise ValueError("Invalid headers")
    names = set()
    for name, value in headers.items():
        if (type(name) is not str or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or name.lower() in names or type(value) is not str
                or any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value)):
            raise ValueError("Invalid or ambiguous header")
        names.add(name.lower())
    data = payload["data"]
    if type(data) is not dict or not data:
        raise ValueError("Missing or malformed model data")
    # Headers are validated then discarded, never logged or forwarded to model
    # output. LatchProxySolver validates choices/message/decision semantics.
    from latch_proxy_solver import ProxyResponse
    return ProxyResponse(200, json.dumps(data, allow_nan=False))


class OfflineMcpClient:
    """In-memory JSON fixture only; accepts no callback or network configuration."""
    def __init__(self, result):
        self._encoded = json.dumps(result, allow_nan=False)
        self.calls = []

    def call_tool(self, name, arguments, *, timeout_seconds):
        self.calls.append((name, arguments, timeout_seconds))
        return json.loads(self._encoded)


class LatchMcpTransport:
    def __init__(self, client=None, *, timeout_seconds=5.0):
        if (type(timeout_seconds) not in {int, float}
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("Invalid MCP timeout")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._timed_out = False
        self._allow_attempted = False

    def probe_deny(self):
        """One exact safe probe; never accepts arbitrary live request arguments."""
        return self._send(None, probe=True)

    def send(self, request):
        return self._send(request, probe=False)

    def smoke_allow(self, request, *, execute_live_allow=False):
        if execute_live_allow is not True:
            raise SolverError(_FAILURE) from None
        return self._send(request, probe=False, allow_smoke=True)

    def _send(self, request, *, probe, allow_smoke=False):
        if not self._lock.acquire(blocking=False):
            raise SolverError(_FAILURE) from None
        try:
            if self.client is None or self._timed_out:
                raise ValueError("MCP unavailable")
            if allow_smoke:
                from latch_smoke_request import validate_allow_request, allow_request
                from latch_registered_client import RegisteredAllowLatchClient
                validate_allow_request(request)
                if self._allow_attempted or type(self.client) not in {OfflineMcpClient, RegisteredAllowLatchClient}:
                    raise ValueError("Smoke unavailable")
                self._allow_attempted = True
                timeout = self.timeout_seconds
                arguments = allow_request()
            elif probe:
                timeout = self.timeout_seconds
                arguments = {"method": "GET", "path": "/__latch_policy_denial_probe__"}
            else:
                from latch_proxy_solver import BASE_URL, CHAT_PATH
                if (type(request) is not dict
                        or set(request) != {"method", "url", "json", "timeout_seconds"}
                        or request["method"] != "POST" or request["url"] != BASE_URL + CHAT_PATH
                        or type(request["json"]) is not dict):
                    raise ValueError("Invalid model request")
                budget = request["timeout_seconds"]
                if type(budget) not in {int, float} or not math.isfinite(budget) or budget <= 0:
                    raise ValueError("Invalid request timeout")
                timeout = min(budget, self.timeout_seconds)
                arguments = {"method": "POST", "path": "/v1/chat/completions",
                             "body": json.loads(json.dumps(request["json"], allow_nan=False))}
            output = queue.Queue(maxsize=1)
            client = self.client

            def dispatch():
                try:
                    result = client.call_tool("latch_authorize", arguments, timeout_seconds=timeout)
                    output.put((True, result))
                except BaseException:
                    # Never retain or forward client diagnostics/credentials.
                    output.put((False, None))

            # A daemon worker bounds caller wait even for a noncooperative client.
            # Timeout cannot undo upstream execution. Never retry this transport
            # after timeout, and never consume its eventual late response.
            threading.Thread(target=dispatch, daemon=True).start()
            try:
                ok, result = output.get(timeout=timeout)
            except queue.Empty:
                self._timed_out = True
                raise ValueError("MCP deadline exceeded") from None
            if not ok:
                raise ValueError("MCP failed")
            response = _decode(result)
            if probe or (not allow_smoke and type(client) is not OfflineMcpClient):
                raise ValueError("Live success schema is unverified")
            return response
        except LatchDenied as denial:
            raise denial from None
        except Exception:
            raise SolverError(_FAILURE) from None
        finally:
            self._lock.release()
