"""Offline proxy Solver: model request through proxy, never policy preflight.

No bundled live client, token handling, or direct upstream fallback.
"""
from dataclasses import dataclass
import json
import math
from agp_race_agent.solver import SolverError

BASE_URL = "https://onlatch.com/proxy"
CHAT_PATH = "/chat/completions"  # Proposed client variant; live mapping untested.


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    body: str


class BlockedProxyTransport:
    def send(self, request):
        raise ConnectionError("Live proxy transport is disabled")


class FakeProxyTransport:
    def __init__(self, response=None, *, fault=None):
        if fault not in {None, "timeout", "connection"}:
            raise ValueError("Unsupported fake fault")
        self.response, self.fault = response, fault
        self.requests = []

    def send(self, request):
        self.requests.append(json.loads(json.dumps(request)))
        if self.fault == "timeout":
            raise TimeoutError("Synthetic timeout")
        if self.fault == "connection":
            raise ConnectionError("Synthetic connection failure")
        return self.response


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


class LatchProxySolver:
    """state/history -> one fake proxy model request -> question or guess.

    Default transport blocks; no credentials accepted/read and no retries.
    A future separate executable can emit the result to the existing AGP Solver.
    """
    def __init__(self, *, model, transport=None, timeout_seconds=60.0):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Explicit model is required")
        if transport is None:
            transport = BlockedProxyTransport()
        from latch_mcp_transport import LatchMcpTransport
        if type(transport) not in {BlockedProxyTransport, FakeProxyTransport, LatchMcpTransport}:
            raise TypeError("Unsupported proxy transport")
        if (type(timeout_seconds) not in {int, float}
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("Invalid local timeout")
        self.model, self.transport = model, transport
        self.timeout_seconds = timeout_seconds

    def decide(self, state, history):
        try:
            if not isinstance(state, dict) or not isinstance(history, list) or any(
                not isinstance(item, dict) for item in history
            ):
                raise ValueError("Invalid Solver input")
            material = json.dumps({"state": state, "history": history}, allow_nan=False)
            request = {
                "method": "POST", "url": BASE_URL + CHAT_PATH,
                "timeout_seconds": self.timeout_seconds,
                "json": {
                    "model": self.model, "stream": False,
                    "messages": [
                        {"role": "system", "content":
                         'Use only supplied state/history. Return one JSON object with exactly '
                         'one nonempty string field: "question" or "guess". '
                         'Never repeat a guess already marked incorrect. Do not invoke tools.'},
                        {"role": "user", "content": material},
                    ],
                },
            }
            response = self.transport.send(request)
            if (type(response) is not ProxyResponse or type(response.status) is not int
                    or response.status != 200 or type(response.body) is not str):
                raise ValueError("Proxy request rejected or failed")
            body = json.loads(response.body, object_pairs_hook=_unique_object)
            if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or len(body["choices"]) != 1:
                raise ValueError("Invalid model envelope")
            choice = body["choices"][0]
            if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete model output")
            message = choice.get("message")
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or message.get("tool_calls") or message.get("refusal")
                    or not isinstance(message.get("content"), str)):
                raise ValueError("Invalid model message")
            decision = json.loads(message["content"], object_pairs_hook=_unique_object)
            # Preserve solver.py:30-41. Outer Solver validates again in a future CLI.
            if not isinstance(decision, dict) or set(decision) not in ({"question"}, {"guess"}):
                raise ValueError("Invalid decision keys")
            value = next(iter(decision.values()))
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Invalid decision value")
            if "guess" in decision and any(
                item.get("guess") == decision["guess"] and isinstance(item.get("result"), dict)
                and item["result"].get("correct") is False for item in history
            ):
                raise ValueError("Repeated incorrect guess")
            return decision
        except Exception:
            raise SolverError("Proxy Solver failed closed") from None


def main(*, solver=None, stdin=None, stdout=None):
    """JSON stdin/stdout boundary; no configured solver means no dispatch."""
    import sys
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        raw = stdin.read(1_000_001)
        if len(raw.encode("utf-8")) > 1_000_000:
            raise ValueError("Input too large")
        payload = json.loads(raw, object_pairs_hook=_unique_object)
        if type(payload) is not dict or set(payload) != {"state", "history"} or solver is None:
            raise ValueError("Unconfigured solver or invalid input")
        decision = solver.decide(payload["state"], payload["history"])
        stdout.write(json.dumps(decision, allow_nan=False) + "\n")
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
