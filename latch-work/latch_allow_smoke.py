"""Explicit single-request ALLOW smoke. No-flag invocation never creates a client."""
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from latch_smoke_request import allow_request
from latch_safe_diagnostic import SafeDiagnostic


def inside_window():
    now = datetime.now(ZoneInfo('America/Los_Angeles'))
    return now.weekday() < 5 and 9 <= now.hour < 18


def validate_completion(response):
    data = json.loads(response.body)
    if data.get('object') != 'chat.completion' or 'error' in data:
        raise ValueError()
    choices = data.get('choices')
    if type(choices) is not list or len(choices) != 1:
        raise ValueError()
    choice = choices[0]
    if type(choice) is not dict or choice.get('finish_reason') not in {'stop', 'length'}:
        raise ValueError()
    message = choice.get('message')
    if (type(message) is not dict or message.get('role') != 'assistant'
            or type(message.get('content')) is not str or message.get('tool_calls') or message.get('refusal')):
        raise ValueError()
    usage = data.get('usage')
    if type(usage) is not dict or any(type(usage.get(k)) is not int or usage[k] < 0
            for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
        raise ValueError()
    if usage['completion_tokens'] > 1 or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
        raise ValueError()


def main(argv=None, *, client_factory=None, window_check=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(json.dumps({'mode': 'preflight', 'request': allow_request(),
                          'dispatch_count': 0, 'retries': 0, 'fallback': 'NONE',
                          'live_execution': 'disabled'}))
        return 0
    if argv != ['--execute-live-allow']:
        print('{"result":"REFUSED","dispatch_count":0}')
        return 2
    # Only offline tests inject a factory/clock. CLI has no alternate client option.
    diagnostic = SafeDiagnostic()
    try:
        if not (window_check or inside_window)():
            print('{"result":"OUTSIDE_TIME_WINDOW","dispatch_count":0}')
            return 2
        diagnostic.stage('CLIENT_INIT', 'IMPORT')
        from latch_registered_client import RegisteredAllowLatchClient
        from latch_mcp_transport import LatchMcpTransport
        diagnostic.stage('CLIENT_INIT', 'LOCAL_GUARD')
        client = (client_factory or RegisteredAllowLatchClient)(execute_live_allow=True)
        client.diagnostic = diagnostic
        response = LatchMcpTransport(client, timeout_seconds=50, diagnostic=diagnostic).smoke_allow(
            allow_request(), execute_live_allow=True)
        diagnostic.stage('RESPONSE_VALIDATE', 'COMPLETION_SCHEMA')
        validate_completion(response)
        print('{"result":"PASS","dispatch_count":1,"retries":0}')
        return 0
    except Exception as error:
        # Never emit raw diagnostics, headers, model text, config, or environment.
        diagnostic.fail(error)
        print(json.dumps(diagnostic.snapshot()), file=sys.stderr)
        print('{"result":"FAILED_CLOSED","retries":0,"fallback":"NONE"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
