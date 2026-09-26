# Development status

## Verified

Recorded in the isolated development session:

- AGP MCP enabled.
- Latch MCP enabled.
- Real Latch MCP connection: PASS.
- Real endpoint DENY: PASS.
- Real transport DENY: PASS.
- Focused tests: 113 passed / 0 failed.
- Guarded ALLOW entry point: PASS (offline contracts).

## Not yet verified

- Real ALLOW runtime envelope.
- Real OpenAI upstream ALLOW through the guarded entry point.
- Latch-governed live AGP race.
- start_track protection by Latch.
- Full live AGP race completion.

## Publication scope

This is one clean initial snapshot of genuine local work, not fabricated commit
history. Credentials, local databases, logs, deployment snapshots, Telegram
pairing/session state and machine configuration are intentionally excluded.
The public copy replaces the private Latch registration selector with a local
environment setting, uses the user's home directory for configuration discovery,
and generalizes absolute paths in documentation. The original live workspaces
are not changed. There is no production-readiness or reward claim.


## 2026-09-26: safe fetch root-cause diagnostics

Real ALLOW has not succeeded. The most recent diagnostic attempt established:

- `failure_stage: MCP_RESPONSE`
- `exception_class: ValueError`
- `error_category: FETCH_FAILED`
- MCP result: `isError=true`; fixed error text: `Error: fetch failed`.
- Exact Python throw site: `latch-work/latch_mcp_transport.py:44`,
  `raise ValueError("MCP error")`. This rejects the MCP error result; it is not
  the underlying fetch exception.

The new ALLOW fetch guard maps failed fetch causes to a closed vocabulary:
`DNS_FAILED`, `CONNECT_FAILED`, `TLS_FAILED`, `FETCH_TIMEOUT`, and
`UNKNOWN_FETCH_FAILED`. `fetch_phase` distinguishes `AUTHORIZE` from `PROXY`;
neither indicates proof of upstream execution. Only fixed markers are passed
through the existing MCP error response. No raw error details or runtime payloads
are recorded. The latest patch has not been exercised against the live service.

Fail-closed behavior, success conditions and policy decisions are unchanged.
Retries remain **0**, fallback remains **NONE**, and AGP source was **not modified**.
Live calls during the diagnostic patch: **0**.
Focused offline tests: **142 passed / 0 failed**.

The fetch error's underlying category is still unresolved; a network restriction
is a hypothesis, not a confirmed root cause. Next step: in the next Latch
TIME_WINDOW, with explicit authorization, execute a single Real ALLOW smoke
invocation once to inspect its fetch failure category. No retry, fallback or
second smoke invocation. One MCP invocation may perform an authorization fetch
and, if allowed, one proxy fetch; it does not mean one HTTP transaction.

## 2026-09-26: end-of-day work record (JST)

The following user-confirmed results are the latest status and supersede the
earlier Real ALLOW pending/failed status and next-step notes above and in the
README. This entry records completed work; no development, fixes, tests, live
calls, or AGP writes were performed to prepare this record. Runtime changes
described here do not imply that their code or configuration was published in
this documentation-only update.

### Latch Real ALLOW single verification: SUCCESS

- Model: `gpt-4o-mini`.
- `dispatch_count`: **1**.
- Latch response received: **YES**.
- Output contract: **PASS**.
- Retry: **0**; fallback: **NONE**.
- AGP writes: **0**; secrets exposed: **0**.

### AGP solver routing and offline verification

AGP `solver.command` was switched to the verified Latch CLI path.
AGP source was not changed, and a rollback path exists.

Post-switch offline verification:

- Question path: **PASS**.
- Guess path: **PASS**.
- DENY fail-closed: **PASS**.
- FETCH_FAILED fail-closed: **PASS**.
- Codex fallback: **NONE**.
- Retry: **0**; fallback: **NONE**.

### AGP MCP read-only checks and production write gate

Read-only checks were completed for `list_tracks`, `my_race`, `sigil_balance`,
and `track_state`. AGP writes: **0**.

The production write gate remains closed because the new Track has not yet been
published. After publication, confirm the following actual values read-only:

- Join Window / registration.
- Phase / started.
- Deadline.
- Track/race/run correspondence.
- Credit / spend conditions.

Only afterward determine the authorization conditions for `start_track`, `ask`,
and `guess`. This record does not authorize those operations.

### AGP Watch status

- systemd service enabled: **YES**.
- Active/running: **NO**.
- MainPID: **0**.
- Polling functional: **NO**.
- `Restart=on-failure` is causing repeated restarts.
- Last confirmed successful poll: **2026-09-24 01:28:56 JST**.
- Current blocker: preflight stops because AUTH environment configuration is
  not loaded correctly. The exact loader root cause remains unconfirmed.
- Telegram routing configuration exists, but actual notification delivery is
  unverified while Watch is stopped.
- Current state is **not new-Track waiting READY**.

### Next session starting point

Resume with read-only AUTH loader root-cause identification at `config.py:750`:
compare the `config.toml` actually loaded by the systemd service with the
location storing AUTH under `[mcp_servers.agp-track-race.env]`, without displaying
secret values.

Subsequent planned sequence: minimum AUTH fix → restore Watch active/running →
confirm successful polling → confirm actual Telegram delivery → new-Track
waiting READY. These steps were not performed as part of this record.
