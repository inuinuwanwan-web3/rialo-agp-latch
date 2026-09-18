from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


CODEX_TIMEOUT_SECONDS = 90
MAX_INPUT_BYTES = 1_000_000
MAX_STDERR_TAIL = 2_000


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET = re.compile(
    r"(?i)(sk-[a-z0-9_-]+|authorization\s*:\s*(?:bearer\s+)?[^\s,;]+|"
    r"bearer\s+[^\s,;]+|(?:api[_ -]?key|token)\s*(?::|=)\s*[^\s,;]+)"
)


def _chatgpt_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Never let this solver silently switch from the cached ChatGPT login to API billing.
    environment.pop("OPENAI_API_KEY", None)
    return environment


def _prompt(payload: dict[str, Any]) -> str:
    material = json.dumps(
        {"state": payload["state"], "history": payload["history"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""You are a reasoning-only solver for one AGP Track Race point.
Use only the STATE and HISTORY supplied below as evidence. Do not inspect files, use shell commands,
use web search, call MCP servers, invoke tools, or modify anything.

Return exactly one JSON object and nothing else. Both keys are required by the schema; exactly one
value must be a non-empty string and the other must be null:
- {{"question":"...","guess":null}} if one concise, non-repeated question would materially reduce uncertainty.
- {{"question":null,"guess":"..."}} only when the evidence supports that exact answer.

Never repeat a question or a guess in HISTORY. In particular, never repeat an incorrect guess.
STATE_AND_HISTORY_JSON:
{material}
"""


def _safe_stderr_tail(stderr: str) -> str:
    # Unknown credential formats and truncated prefixes cannot be masked safely.
    return '<redacted>' if stderr else ''


def decide(payload: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != {"state", "history"}:
        raise ValueError("input must contain state and history only")
    if not isinstance(payload["state"], dict) or not isinstance(payload["history"], list):
        raise ValueError("state/history have invalid types")
    prompt = _prompt(payload)
    if len(prompt.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("solver input is too large")

    environment = _chatgpt_environment()
    status = subprocess.run(
        ["codex", "login", "status"], text=True, capture_output=True,
        timeout=10, env=environment,
    )
    status_output = status.stdout + status.stderr
    if status.returncode != 0 or "Logged in using ChatGPT" not in status_output:
        raise RuntimeError("Codex is not logged in using ChatGPT")

    schema = Path(__file__).with_name("codex_solver_output.schema.json")
    with tempfile.TemporaryDirectory(prefix="agp-codex-solver-") as isolated_cwd:
        result = subprocess.run(
            [
                "codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--sandbox", "read-only", "--skip-git-repo-check", "--color", "never",
                "--output-schema", str(schema), "--cd", isolated_cwd, "-",
            ],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=CODEX_TIMEOUT_SECONDS,
            env=environment,
        )
    if result.returncode != 0:
        diagnostic = _safe_stderr_tail(result.stderr)
        raise RuntimeError(
            f"codex exec failed (returncode={result.returncode}, stderr_tail={diagnostic!r})"
        )
    decision = json.loads(result.stdout)
    if not isinstance(decision, dict) or set(decision) != {"question", "guess"}:
        raise ValueError("Codex returned an invalid decision")
    non_null = [(key, value) for key, value in decision.items() if value is not None]
    if len(non_null) != 1 or not isinstance(non_null[0][1], str) or not non_null[0][1].strip():
        raise ValueError("Codex returned an empty decision")
    key, value = non_null[0]
    return {key: value.strip()}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("solver input is too large")
        payload = json.loads(raw)
        print(json.dumps(decide(payload), ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"Codex Solver error: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
