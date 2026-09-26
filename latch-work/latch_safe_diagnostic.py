"""Closed-vocabulary diagnostics: never serialize exception text or payloads."""
import json
import subprocess
import threading

FETCH_CATEGORIES = frozenset({'DNS_FAILED', 'CONNECT_FAILED', 'TLS_FAILED',
                              'FETCH_TIMEOUT', 'UNKNOWN_FETCH_FAILED'})


class SafeDiagnostic:
    def __init__(self):
        self._lock = threading.Lock()
        self._frozen = False
        self._data = dict(failure_stage='LOCAL_PRECHECK', exception_class='UNKNOWN',
                          error_category='LOCAL_GUARD', dispatch_started=False,
                          mcp_call_started=False, mcp_response_received=False,
                          timeout=False, exit_code=2, retries=0, fallback='NONE')

    def stage(self, stage, category):
        assert stage in {'LOCAL_PRECHECK', 'CLIENT_INIT', 'MCP_START', 'MCP_DISPATCH',
                         'MCP_RESPONSE', 'RESPONSE_VALIDATE', 'FETCH_GUARD', 'UNKNOWN'}
        assert category in {'LOCAL_GUARD', 'IMPORT', 'CONFIG', 'CACHE', 'ENVIRONMENT',
                            'PROCESS_START', 'HANDSHAKE', 'MCP_WRITE', 'MCP_READ',
                            'SCHEMA_MISMATCH', 'COMPLETION_SCHEMA', 'MCP_ERROR',
                            'FETCH_BLOCKED', 'FETCH_FAILED', 'POLICY_DENY', 'UNKNOWN'}
        with self._lock:
            if not self._frozen:
                self._data.update(failure_stage=stage, error_category=category)

    def mark(self, field):
        assert field in {'dispatch_started', 'mcp_call_started', 'mcp_response_received'}
        with self._lock:
            if not self._frozen:
                self._data[field] = True

    def fetch_failure(self, phase, category):
        assert phase in {'AUTHORIZE', 'PROXY'} and category in FETCH_CATEGORIES
        with self._lock:
            if not self._frozen:
                self._data.update(failure_stage='MCP_RESPONSE', error_category=category,
                                  fetch_phase=phase, timeout=category == 'FETCH_TIMEOUT')

    def fail(self, error, *, timeout=False):
        # Exact trusted classes only: even a custom class name may contain a secret.
        classes = (ValueError, TypeError, KeyError, ImportError, ModuleNotFoundError,
                   OSError, PermissionError, FileNotFoundError, BrokenPipeError,
                   ConnectionError, TimeoutError, RuntimeError, json.JSONDecodeError,
                   subprocess.TimeoutExpired)
        name = next((c.__name__ for c in classes if type(error) is c), 'UNKNOWN')
        with self._lock:
            if not self._frozen:
                self._data['exception_class'] = name
                self._data['timeout'] = self._data['timeout'] or timeout or isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
                if self._data['timeout'] and self._data['error_category'] != 'FETCH_TIMEOUT':
                    self._data['error_category'] = 'TIMEOUT'
                self._frozen = True

    def snapshot(self):
        with self._lock:
            return dict(self._data)
