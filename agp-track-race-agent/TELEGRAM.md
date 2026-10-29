# Telegram notifications

The read-only Track watcher saves observations independently of the Telegram
relay. The relay reads `NEW_TRACK_DETECTED` snapshots and never calls the AGP
gateway. Notification handling does not enable race participation.

## Durable delivery state

The relay uses `~/.config/agp-track-telegram/deliveries.sqlite3` with mode 0600
and SQLite FULL synchronization. It atomically records discovered events and
the discovery cursor. Each dispatch is claimed before sending and each attempt
has a start time, completion time, outcome, and fixed reason. Credentials,
destination identifiers, response bodies, and exception contents are excluded.

The existing `cursor.json` is imported once and left unchanged. On a fresh
installation without a cursor, the initial maximum snapshot ID is the baseline;
older events are not sent. Existing `setup`, `test`, `test-once`, credential
format, notification text, and the process-wide `notifier.lock` are retained.

| State | Handling |
| --- | --- |
| pending | Persisted, not yet claimed for dispatch |
| sending | Persisted before dispatch; recovered as unconfirmed after interruption |
| sent | HTTP 200 and API `ok=true`; not dispatched again |
| retry | Connection failure before HTTP dispatch, or explicit 429 rejection |
| failed | Explicit 400/401/403/404 rejection; requires review |
| unconfirmed | Timeout, interrupted request, malformed response, 5xx, or interrupted sending state; requires review |

Safe retries wait 5, 10, 20, 40, 80, 160, then 300 seconds, and continue at
300 seconds. A longer API `retry_after` takes precedence. There is no attempt
count that silently discards a safely retryable event. A slow backlog cannot
retry the same event repeatedly within one scan. Failed or unconfirmed events
do not block later events.

Uncertain deliveries are not automatically retried because Telegram may already
have accepted the request. Exactly-once receipt and zero missed notifications
are not guaranteed. Operators must review failed/unconfirmed events. API
acceptance is distinct from a person's confirmation of device receipt.

## Local inspection

```sh
python -B scripts/telegram_notify.py status
```

This action does not read credentials, contact Telegram, or create a missing
state database. It reports status counts, last scan, last API acceptance, and
next retry timestamps. A never-started improved relay reports `not_initialized`.
Timestamps are Unix seconds. This command alone does not prove process liveness.

Do not delete the delivery database or reset consumed test markers. Legacy
`test` and `test-once` each retain their existing no-retry behavior. A new test
requires separate explicit authorization and an independent durable one-shot
claim; it must not fabricate a new Track or alter production cursors/history.

## Activation and rollback

Replacing the Python file does not update an already running relay. Activation
requires a separately authorized restart of the Telegram relay only; keep the
AGP watcher running. Starting the relay can dispatch normal pending events.

After migration, the delivery database is authoritative. Rolling back to an old
relay against its stale legacy cursor can duplicate notifications. Preserve
state and reconcile sent, uncertain, and pending events before any separately
approved rollback. No automatic rollback is implemented.

Machine-specific service files, credentials, databases, and runtime logs are
deliberately excluded from this repository.

See [Phase 8P completion](docs/phase-8p-completion-2026-10-29.md) for verified
results and remaining limitations.
