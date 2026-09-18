from __future__ import annotations

import json
import os
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex}.jsonl"

    def write(self, event: str, **data: object) -> None:
        from .write_safety import READ_TOOLS, WRITE_TOOLS
        allowed = {
            'tool': READ_TOOLS | WRITE_TOOLS,
            'reason': {'mcp_call_failed', 'no_participable_track', 'registration_error',
                       'registration_unconfirmed', 'budget_limit', 'solver_failure',
                       'missing_question_cost', 'point_limits'},
            'result_type': {'dict', 'list', 'str', 'bool', 'int', 'float', 'NoneType'},
        }
        safe = {key: value for key, value in data.items()
                if key in allowed and isinstance(value, str) and value in allowed[key]}
        if event not in {'tool_call', 'tool_result', 'safe_exit'}:
            event = 'unknown_event'
        record = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **safe}
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
