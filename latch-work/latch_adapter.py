"""Historical preflight experiment; NOT the production integration design.

Superseded by latch_proxy_solver.py. Retained only for regression evidence.

No endpoint, credentials, HTTP client, MCP client, login or retry is implemented.
The response envelope below is a LOCAL fake protocol, not a verified wire schema.
Only explicit fake responses can pass this boundary; default transport blocks.
An injected AGP Solver must itself use a fake model in this offline phase.
"""
from dataclasses import dataclass
import json
import math

from agp_race_agent.solver import Solver, SolverError


@dataclass(frozen=True)
class LocalResponse:
    status: int
    body: str


class BlockedTransport:
    def exchange(self, payload, *, timeout_seconds):
        raise ConnectionError("Live Latch transport is disabled")


class FakeTransport:
    """In-memory response/failure only. Cannot accept a network callback."""

    def __init__(self, response=None, *, fault=None):
        if fault not in {None, "connection", "timeout"}:
            raise ValueError("Unsupported fake fault")
        self.response, self.fault = response, fault
        self.requests = []

    def exchange(self, payload, *, timeout_seconds):
        self.requests.append((json.loads(json.dumps(payload)), timeout_seconds))
        if self.fault == "timeout":
            raise TimeoutError("Synthetic timeout")
        if self.fault == "connection":
            raise ConnectionError("Synthetic connection failure")
        return self.response


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response key")
        result[key] = value
    return result


class LatchGuardedSolver:
    """One authorization attempt per decide; only ALLOW invokes the AGP Solver.

    timeout_seconds is a local proposed budget passed to the fake transport.
    It is not a verified Latch timeout and does not implement a network timer.
    """

    def __init__(self, solver, transport=None, *, timeout_seconds=5.0):
        if not isinstance(solver, Solver):
            raise TypeError("An existing AGP Solver is required")
        if transport is None:
            transport = BlockedTransport()
        if type(transport) not in {BlockedTransport, FakeTransport}:
            raise TypeError("Only blocked or fake Latch transport is permitted")
        if (type(timeout_seconds) not in {int, float}
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("Invalid local timeout budget")
        self.solver, self.transport = solver, transport
        self.timeout_seconds = timeout_seconds

    def decide(self, state, history):
        try:
            if (not isinstance(state, dict) or not isinstance(history, list)
                    or any(not isinstance(item, dict) for item in history)):
                raise ValueError("Invalid input")
            # Snapshot input: fake transport cannot mutate what the Solver sees.
            payload = json.loads(json.dumps({"state": state, "history": history}, allow_nan=False))
            response = self.transport.exchange(
                json.loads(json.dumps(payload)), timeout_seconds=self.timeout_seconds,
            )
            if (type(response) is not LocalResponse or type(response.status) is not int
                    or response.status != 200 or type(response.body) is not str):
                raise ValueError("Invalid local envelope")
            parsed = json.loads(response.body, object_pairs_hook=_unique_object)
            if (type(parsed) is not dict or set(parsed) != {"decision"}
                    or type(parsed["decision"]) is not str
                    or parsed["decision"] not in {"allow", "deny"}):
                raise ValueError("Invalid local decision schema")
            if parsed["decision"] != "allow":
                raise ValueError("Authorization denied")
        except Exception:
            # No diagnostics, response body, credential or fallback escapes.
            raise SolverError("Latch authorization unavailable or denied") from None
        # Preserve the real Solver's JSON/output/history validation, with no retry.
        return self.solver.decide(payload["state"], payload["history"])
