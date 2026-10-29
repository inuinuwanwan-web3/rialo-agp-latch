# Phase 8P: Telegram reliability and real delivery verified

**PASS for the Phase 8P scope.** The improved notification relay was activated,
one dedicated message was accepted by Telegram, and the user confirmed receipt
on a smartphone. This is not a claim that automatic real-Track delivery or a
live race has been verified.

## Verified operational result

| Item | Evidence-backed result |
| --- | --- |
| Improved Telegram relay | Activated on 2026-10-29 at 20:49:57 JST |
| Telegram relay restarts | One separately authorized manual restart |
| AGP watcher | Continued running; no restart |
| Telegram API | HTTP 200, `ok=true` |
| `message_id` | 8 |
| Telegram message timestamp | 2026-10-29 21:35:19 JST / 12:35:19 UTC |
| Actual HTTP send requests | 1; automatic retries 0 |
| Destination and message | Matched the configured destination and authorized text |
| Smartphone receipt | Confirmed by the user in the publication request; no exact receipt timestamp supplied |
| Notification cursor | 39 before and after |
| Pending new-Track events | 0 before and after |
| Existing observation snapshots | All 979 records preserved across activation |
| AGP automatic participation | Remains BLOCKED |

The activated code matches the previously tested notifier. The improved private
delivery database was created with mode 0600, its integrity check passed, and
scan timestamps advanced. The watcher completed a successful poll after relay
activation. Notification and watcher processes retained their identities during
the later dedicated delivery test. Production cursors, histories, credentials,
and service configuration were not manually changed by that test.

The first dedicated test harness stopped with TypeError before any HTTP
request. Its instrumentation had changed a standard-library class binding used
by `super`. The harness was corrected to isolate the notifier's binding,
verified offline, and only then rerun under a new explicit authorization and
independent one-shot execution ID. The first attempt made zero API requests;
the authorized second attempt made one. Earlier consumed legacy test markers
were retained. This temporary harness was not the production relay.

## Implementation and tests

- Reuse the existing read-only watcher and observation archive.
- Persist discovered events and cursor together in an independent delivery DB.
- Record event state and every dispatch attempt before and after sending.
- Retry only pre-dispatch connection failures and explicit 429 rejections,
  with backoff and `retry_after` support.
- Retain uncertain/permanently rejected deliveries for operator review rather
  than automatically duplicating an uncertain delivery.
- Preserve legacy cursor import, one-time test semantics, and duplicate-process
  locking. Add local read-only status inspection.

The development validation passed all 475 pre-existing cases and 24 added cases
(499 unique cases). A fresh full run of the publication snapshot is recorded in
[the publication test result](phase-8p-offline-tests-2026-10-29.json).

Reproduce using an environment where pytest is already installed:

```sh
python -B scripts/verify_phase8p_offline.py --output offline-results.json
```

The runner copies code/tests and a credential-free example configuration into a
temporary directory, uses a synthetic home and MCP registration, blocks network
and production-home access, and restricts child processes to Python tests.
It does not install packages, invoke services, send Telegram messages, or
contact production AGP. The suite includes a virtual-clock 24-hour audit test,
which is not an actual 24-hour live run.

## Scope and remaining unverified items

- The real dedicated test used the same deployed `send`/API functions as the
  improved relay. It did not fabricate a Track or run the complete real
  new-Track-to-notification loop. That automatic path is verified offline only.
- API acceptance and user-confirmed receipt are separate evidence. The original
  machine-generated send record reported receipt as unconfirmed at execution;
  the user's later confirmation updates that status without rewriting history.
- Real outage/429 retry recovery and delivery under an interrupted in-flight
  request remain untested. Unknown deliveries can still require human review.
- Existing Windows boot task, WSL/systemd configuration, and a prior boot's
  successful service startup were inspected read-only. A PC reboot after this
  new relay activation has not been tested.
- Power-off/sleep, hung processes, polling gaps, and announcements absent from
  the watched MCP responses are not eliminated by this change. Independent
  Season/Tournament/rule announcements are outside this verification.
- AGP participation stays BLOCKED while official race participation rules are
  awaited. No AGP participation, ask/guess, signing, transfer, or production AGP
  write was performed as part of Phase 8P.

## Publication boundary

[The results summary](phase-8p-results-2026-10-29.json) is a strict field-selected
export of verified counts, timestamps, and outcomes plus the user's receipt
confirmation. Raw logs/HTTP bodies, bot tokens, chat IDs, usernames, host names,
absolute workstation paths, process/invocation IDs, credentials, notification
DBs/cursors, runtime claims, and machine-specific service files are excluded.
No extra Telegram send, service operation, Windows change, or production RPC
was performed to prepare this publication.
