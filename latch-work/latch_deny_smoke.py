"""Explicitly invoked, one-shot real DENY smoke. Never used by pytest."""
import json
from latch_mcp_transport import LatchMcpTransport, LatchDenied
from latch_registered_client import RegisteredLatchClient


def main():
    client = RegisteredLatchClient()
    decision, filter_name = 'unconfirmed', 'unconfirmed'
    try:
        LatchMcpTransport(client, timeout_seconds=50).probe_deny()
    except LatchDenied as denial:
        decision, filter_name = denial.decision, denial.deciding_filter
    except Exception:
        pass
    passed = client.connected and decision == 'DENY' and filter_name == 'endpoint_0' and client.dispatch_count == 1
    print(json.dumps({'connected': client.connected, 'decision': decision,
                      'deciding_filter': filter_name, 'dispatch_count': client.dispatch_count,
                      'retries': client.retries, 'passed': passed}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
