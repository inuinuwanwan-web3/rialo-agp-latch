"""Systemd smoke test: only list_tracks and sigil_balance can reach MCP."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .config import load, preflight
from .gateway import McpGateway
from .diagnostics import failure_details


class ProbeGateway(McpGateway):
    def request(self, method, params):
        if method != "initialize" and not (
            method == "tools/call" and params.get("name") in {"list_tracks", "sigil_balance"}
            and params.get("arguments", {}) == {}
        ):
            raise RuntimeError("Read-only probe blocked operation")
        return super().request(method, params)

    def notify(self, method, params=None):
        if method != "notifications/initialized" or params not in (None, {}):
            raise RuntimeError("Read-only probe blocked notification")
        return super().notify(method, params)


def auth_check():
    try:
        result = subprocess.run(["codex", "login", "status"], capture_output=True,
                                text=True, timeout=15)
        return result.returncode == 0 and "Logged in using ChatGPT" in result.stdout + result.stderr
    except (OSError, subprocess.SubprocessError):
        return False


def run_probe(settings, *, sleep=time.sleep, output=print, factory=ProbeGateway):
    # Fixed fields only. Do not serialize errors, tool payloads, names or IDs.
    output(json.dumps({"event": "probe_started", "pid": os.getpid(), "codex_auth_ok": auth_check()}), flush=True)
    gateway = None
    polls = 0
    try:
        gateway = factory(settings)
        balance = gateway.call("sigil_balance")
        output(json.dumps({"event": "agp_read_auth", "ok": isinstance(balance, dict) and isinstance(balance.get("sigilBalance"), dict)}), flush=True)
        while True:
            try:
                response = gateway.call("list_tracks")
                valid = isinstance(response, dict) and isinstance(response.get("tracks"), list)
                polls += 1
                output(json.dumps({"event": "poll", "number": polls, "ok": valid}), flush=True)
            except (OSError, RuntimeError, ValueError) as error:
                details = getattr(error, "details", None) or getattr(gateway, "last_failure", None)
                if not isinstance(details, dict):
                    details = failure_details(error)
                output(json.dumps({"event": "poll", "ok": False, **details}), flush=True)
            sleep(60)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        details = getattr(error, "details", None) or failure_details(error)
        output(json.dumps({"event": "probe_failed", **details}), flush=True)
        return 1
    finally:
        if gateway is not None:
            gateway.close()


def main():
    try:
        settings = load(Path("config.toml"))
        if preflight(settings, monitor=True):
            return 2
        return run_probe(settings)
    except (OSError, RuntimeError, ValueError):
        print('{"event":"configuration_failed"}', flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
