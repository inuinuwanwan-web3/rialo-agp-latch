"""Emit only fixed diagnostic labels; never log raw errors or stderr."""
import json
import re

_PATTERNS = {
    "auth_rejected": r"\b(?:401|403|unauthorized|forbidden)\b|invalid.token|expired.token",
    "rate_limit": r"\b429\b|rate[ _-]*limit|throttl",
    "session_invalid": r"no valid session|session.*(?:expired|invalid|not found)",
    "fetch_failed": r"fetch failed",
    "connect_timeout": r"UND_ERR_CONNECT_TIMEOUT|connect timeout",
    "dns_failure": r"\b(?:ENOTFOUND|EAI_AGAIN)\b",
    "connection_reset": r"\bECONNRESET\b",
    "connection_refused": r"\bECONNREFUSED\b",
    "tls_failure": r"CERT_|certificate verify|self.signed certificate",
    "timeout": r"timeout|timed out",
    "http_server_error": r"\b50[0-9]\b",
}


def categories(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return sorted(key for key, pattern in _PATTERNS.items() if re.search(pattern, text, re.I))


def failure_details(error, proc=None, stderr_categories=()):
    labels = set(categories(str(error))) | set(getattr(error, "categories", ()))
    labels.update(stderr_categories)
    allowed = {"McpResponseError", "McpTransportError", "McpInitializationError", "McpReadDeadline",
               "RuntimeError", "ValueError", "JSONDecodeError", "BrokenPipeError", "ConnectionResetError",
               "TimeoutError", "OSError", "PermissionError"}
    kind = type(error).__name__
    return {"exception": kind if kind in allowed else "OtherError",
            "categories": sorted(labels & _PATTERNS.keys()),
            "rpc_code": getattr(error, "rpc_code", None),
            "child_exit_code": proc.poll() if proc is not None else None}
