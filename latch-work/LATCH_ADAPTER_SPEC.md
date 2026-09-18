# Latch MCP transport boundary: isolated implementation

## Current MCP semantics and limits

`latch_authorize` is an execution gate: when policy ALLOWs, Latch executes the
upstream request itself. There is no authorization-only dry-run for this OpenAI
latch. Never invoke CodexSolver after an authorized Latch model request.
The verified denial payload contains `authorized: false`, `deniedBy`, and
`reason`, inside an MCP text content block. The expected success format comes from inspected cached-server code; a real
ALLOW response shape is NOT yet runtime-verified. This parser update uses no real
MCP or upstream requests. Earlier connection and DENY smoke checks succeeded.

`latch_mcp_transport.py` accepts an injected synchronous client implementing
`call_tool(name, arguments, *, timeout_seconds)`. The client must disable SDK
retries. The separately implemented registered client remains restricted to the
exact DENY probe; no real model dispatch is enabled.
It maps the existing proposed Chat Completions request to `latch_authorize` with
POST `/v1/chat/completions` and the complete model body. This path conforms to
the reported `/v1/*` policy; allowed model/body and live success schema still
require verification before dispatching any real model request.

The transport enforces the smaller of its configured deadline and the request
budget. It dispatches once, has no fallback, rejects concurrent sends, and
permanently blocks reuse after a response timeout. A daemon worker bounds caller
waiting even when an injected client ignores its timeout. It cannot undo a
server-side request: a late operation may finish upstream, but its response is
ignored and it is not retried. A future concrete client needs bounded network
operations and cancellation support; cancellation does not prove nonexecution.

Unavailable MCP, timeout, MCP error, malformed JSON/envelopes, conflicting
content, DENY, and invalid success envelopes raise sanitized AGP `SolverError`.
Raw diagnostics and header values are not logged. Duplicate keys and nonfinite
JSON are rejected. The parser accepts exactly two disjoint payload shapes:

- DENY: exactly `authorized: false`, nonempty string `deniedBy`, and nonempty
  string `reason`. Any explicit ALLOW/unknown authorization value is rejected.
- Success: exactly `{status, headers, data}`, with integer (not boolean or float)
  status 200, a header object mapping valid HTTP token names to strings without
  prohibited control characters, and a nonempty JSON object for data. Header names
  must be unique ignoring case. Empty headers are valid. Missing fields and extra
  fields, including authorization/decision/denial/receipt indicators, fail closed.

Headers are validated and discarded. Only serialized `data` is passed to the
existing proxy solver, which separately validates model choices, messages and
question/guess output. Other HTTP statuses fail closed for this nonstreaming Chat
Completions boundary. MCP structuredContent, if supplied, must match the text
payload exactly under canonical JSON comparison.

This is the **currently observed/documented contract from inspected server code**,
not a runtime-verified real ALLOW envelope. Synthetic offline fixtures exercise
it; the former `{authorized: true, response: ...}` fixture shape is rejected.
Only the in-memory `OfflineMcpClient` may currently return model success through
the transport. The DENY probe never returns success, even with an offline fixture.
The concrete registered client and its fetch guard remain DENY-only.

The first real ALLOW requires **separate explicit authorization** for a concrete
model, request body and spending limit, plus a deliberate, reviewed change to the
DENY-only client/egress restriction and offline-only success gate. It must then
verify this envelope at runtime. No such change or request is part of this step.

`LatchProxySolver` accepts `LatchMcpTransport` as well as its historical fake and
blocked transports. Its existing model-output validation remains active. Its
`main(solver=..., stdin=..., stdout=...)` boundary reads state/history and emits
only one JSON question/guess on success; failures exit nonzero with no stdout.
Direct CLI execution has no configured solver and fails closed. A future launcher
must inject the configured solver explicitly. No AGP or Codex configuration was
changed. Existing generic AGP Solver maps nonzero exit to SolverError.

`start_track` occurs before solver execution and is outside this boundary: it is
**NOT protected by this gate**. Existing AGP registration and safety checks remain
separate. A solver failure stops subsequent solver-driven ask/guess operations;
it does not retroactively protect registration.

Focused validation covers the new MCP boundary and affected proxy contracts only.
The existing 526-test suite is not rerun.

## Historical offline design (superseded where noted above)

# Latch integration: Proxy Solver, offline only

## Decision B: separate Solver adapter

The current CodexSolver cannot be put behind Latch simply by changing its base
URL. config.toml:28–30 selects the agp_race_agent.codex_solver module;
config.py:126 loads that command and agent.py:71 constructs the generic Solver.

| Property | Current implementation evidence |
|---|---|
| Outer Solver | solver.py:20–29 sends JSON state/history on stdin to a command; timeout 120s |
| Model call | codex_solver.py:67–88 runs codex login status and codex exec; no SDK/HTTP implementation |
| Authentication | codex_solver.py:73–74 requires Logged in using ChatGPT; actual credential resolution delegated to CLI |
| API key | codex_solver.py:25–29 copies environment and removes OPENAI_API_KEY |
| Environment | All other inherited variables remain; no environment allowlist |
| Base URL/path | No base_url setting or HTTP endpoint/path in this wrapper |
| Model | No model flag passed to codex exec; CLI-selected model unverified |
| Timeouts | login status 10s; codex exec 90s; outer subprocess 120s |
| Retry | No wrapper retry loop; internal Codex network retries unverified |
| Isolation | ephemeral, ignore-user-config, ignore-rules, read-only sandbox, temporary cwd |
| Output | Two-field Codex schema normalized to one nonempty question/guess, lines 95–102 |

No Codex command or authentication request was executed during this audit. No
credential cache/keychain was read. Actual CLI credential storage, model and
network endpoint remain unverified. This conclusion is about the current wrapper,
not a claim that every possible Codex CLI configuration is incapable of proxying.

## Official behavior supplied by the user

The user confirmed Subzero Labs Latch official documentation. We did not fetch
remote documentation in this offline task:

- Latch proxies the upstream request, rather than requiring a preliminary policy
  ALLOW/DENY call. The client uses a Latch access token instead of an upstream key.
- OpenAI-compatible base_url: https://onlatch.com/proxy. Paths below /proxy/ are
  forwarded upstream after policy passes and Latch injects the real credential.
- DENY returns 403 without forwarding upstream. Rate, body, time and daily spend
  policies are evaluated in the proxy pipeline.
- /proxy/rpc is the separate Rialo signing route; it is not used by this adapter.

The old latch_adapter.py preflight design is superseded. Its tests remain only
as historical regression evidence, not certification of a production integration.

## Selected integration boundary

    RaceAgent -> existing generic Solver -> separate LatchProxySolver
              -> Latch Proxy -> model upstream

A future separate executable can emit the one-key JSON decision for the generic
Solver to validate. Existing CodexSolver and production settings stay unchanged.
There is no second/direct upstream call after a preliminary ALLOW result.

## Offline implementation

- LatchProxySolver accepts state/history, an explicit model and only a concrete
  FakeProxyTransport or default BlockedProxyTransport. No network client, token
  parameter, environment lookup, authentication header or executable CLI exists.
- Proposed variant: nonstreaming Chat Completions, POST to base_url plus
  /chat/completions, using model/messages/stream=false. The exact path/schema must
  be confirmed for the chosen upstream before live testing. No SDK is installed.
- Expected successful model response: one assistant message, finish_reason=stop,
  JSON content containing question OR guess. Match existing solver.py:30–41,
  including rejection of repeated incorrect guesses.
- 403,401,429,5xx,redirect,timeout,connection failures and malformed, truncated,
  tool-call or refusal output raise SolverError. No response body is logged.
- Retries=0, redirect following=0, direct upstream fallback=0. Local proposed
  timeout budget=60s; Fake transport has no real timer. Outer Solver timeout=120s.
- AGP Safety/Budget are unchanged. Model spend and AGP ask/guess costs remain
  distinct; model costs are not silently added to or substituted for AGP costs.
- No real Latch token or upstream credential is accepted or used in this phase.

## Before first live request

Confirm allowed upstream model, concrete Chat Completions path/schema, token
scope and authentication header, policy/allowlist and egress IP, timeout and
quota/spend accounting details. Disable SDK retries if an SDK is later chosen.
The user-provided window is America/Los_Angeles days 1–5,09:00–18:00. Simulation
success does not certify live routing, rate or spend accounting.

Current readiness is for a separately authorized proxy test, not production
rollout. No live request is performed or authorized by this offline prototype.

From /path/to/rialo-agp-latch/agp-track-race-agent:

    .venv/bin/python -B /path/to/rialo-agp-latch/latch-work/offline_regression.py proxy
    .venv/bin/python -B /path/to/rialo-agp-latch/latch-work/offline_regression.py all

## Registered stdio DENY client

`latch_registered_client.py` reads the existing Codex registration in memory,
resolves its already cached npm package by matching the lockfile locator, and
verifies the audited server SHA-256. It launches the cached server directly with
Node; no npm download or configuration change is needed. Credentials are passed
only in the child's environment; stderr is discarded. No credential is placed
in argv, reports, source, or fixtures. MCP initialize/initialized are local stdio
handshakes; only one tools/call is sent. Startup and response share a deadline;
the child is killed and reaped on completion/failure. No retries or fallback.

The concrete client deliberately accepts only the exact GET denial probe and
is single-use. `LatchMcpTransport.probe_deny()` feeds that request through the same
deadline and response parser as model requests. A validated denial raises
`LatchDenied`, a SolverError subtype with only an allowlisted filter identifier.
Raw denial reasons are not emitted. The Node fetch guard permits a single request
to Latch's authorization endpoint and rejects redirects, additional fetches,
proxy requests and altered probe arguments. Thus even an unexpected policy ALLOW
cannot cause the server's subsequent proxy fetch to proceed in this smoke client.

Inspection of the cached server clarifies success semantics: after authorization
it calls its internal proxy handler and returns `{status, headers, data}`, without
an explicit `authorized` field for OpenAI success. The previous synthetic envelope
is not the real wire format. Its replacement parser is now covered by synthetic
offline tests. Live model success remains disabled pending separately authorized
client changes and the first runtime verification.

## Explicit ALLOW smoke entry point (prepared, NOT executed live)

`latch_allow_smoke.py` now provides an opt-in exception to the historical
DENY-only client. Without arguments it prints only a credential-free preflight
and never constructs a registered client. The exact fixed request is:

```json
{"method":"POST","path":"/v1/chat/completions","body":{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}],"max_completion_tokens":1}}
```

The documented Chat Completions output-bound field is `max_completion_tokens`.
No live request was used to validate it. No alternative model or payload is
accepted. `latch_smoke_request.py` compares canonical JSON, including exact scalar
types. The explicit `--execute-live-allow` flag is required. Unknown flags fail
closed. The CLI checks weekdays 09:00 <= local time < 18:00 America/Los_Angeles;
Latch still decides policy, including live rate and remaining daily spend.

`RegisteredAllowLatchClient` reuses the existing credential loader and stdio
connection, disables reuse, and exposes retries as an immutable zero property.
The transport provides a separate single-attempt `smoke_allow` operation. General
model execution remains offline-only. No CodexSolver or AGP execution is involved.
The smoke validates transport success and completion usage directly; `length`
finish reason is acceptable at one token. Model text, headers and raw errors are
never printed. Output is a fixed sanitized summary.

`latch_allow_fetch_guard.cjs` permits one Latch authorization HTTP exchange and,
only after explicit authorization, at most one governed Latch proxy HTTP exchange
for the identical model body. These are internal to ONE MCP tools/call dispatch;
there is at most ONE model request. No direct OpenAI egress, redirect, retry or
fallback is permitted. DENY, malformed result, timeout or any error terminates the
attempt. A timeout cannot undo an upstream request already dispatched.

Prepared command (DO NOT RUN without separate explicit live authorization):

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/path/to/rialo-agp-latch/agp-track-race-agent:/path/to/rialo-agp-latch/latch-work /path/to/rialo-agp-latch/agp-track-race-agent/.venv/bin/python -B /path/to/rialo-agp-latch/latch-work/latch_allow_smoke.py --execute-live-allow
```

Omit the flag for safe preflight only. There is no scheduling, automatic retry or
policy modification. Focused offline tests use in-memory clients or stubbed fetch;
no registered live client is launched. Real ALLOW is still NOT executed and its
success envelope remains NOT runtime-verified.
