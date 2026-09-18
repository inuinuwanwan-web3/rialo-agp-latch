# Rialo AGP + Latch integration work

Development snapshot of a Rialo AGP Track Race Agent and an isolated Latch MCP
transport integration. This initial public commit preserves reviewed development
results; it does not reconstruct or invent earlier Git history.

## Verified milestones

The development-session checks established:

- Rialo AGP Track Race Agent development.
- Codex CLI + AGP MCP + Latch MCP coexistence verified.
- Real Latch MCP connection verified; `latch_capabilities` successful.
- Real Latch endpoint-policy DENY verified.
- Real MCP transport connected; real DENY through the isolated `latch-work`
  transport verified.
- One-dispatch / retries 0 / no fallback safety model for the Latch smoke path.
- Guarded live-ALLOW entry point implemented.
- Latest focused Latch tests: **113 passed / 0 failed**.
- AGP production source remained unchanged during Latch isolation work.

These are recorded development milestones, not claims of production readiness.
Real ALLOW remains runtime-unverified. `start_track` occurs outside the current
solver gate and is not yet protected by this Latch boundary. No points, rewards,
airdrop eligibility, or completed live race are claimed.

## Layout

- `rialo-first/`: small Rust Rialo DevNet RPC example. Running it contacts DevNet;
  it was not executed as part of publication preparation.
- `agp-track-race-agent/`: agent source, synthetic tests, safe example configuration
  and optional Telegram tooling. No private configuration or runtime data.
- `latch-work/`: transport adapters, request guards, smoke entry points and
  synthetic offline contracts.

## Safe local validation

Python 3.11+, Node.js, and pytest are needed for the focused contracts. From this
repository root, using a separately prepared Python environment:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/agp-track-race-agent:$PWD/latch-work" python -B -m pytest --disable-plugin-autoload -p no:cacheprovider --confcutdir=latch-work --rootdir=latch-work -q latch-work/test_latch_allow_smoke_contract.py latch-work/test_latch_mcp_transport_contract.py latch-work/test_latch_proxy_contract.py
```

These tests use synthetic fixtures. They do not launch registered live MCP or
send real model requests. The broader historical suite was not rerun for this
publication. AGP test files are included as development artifacts, not a claim
that the entire suite was independently rerun in this public snapshot.

## Live operations are opt-in

`latch_allow_smoke.py` without arguments prints a safe preflight and dispatches
nothing. Its `--execute-live-allow` flag is a real, potentially billable action,
requiring separate explicit authorization. Do not run it merely to install or
inspect this repository. The DENY smoke also contacts a real service when run.

The published registered client defaults to a local Codex server named `latch`;
set `LATCH_MCP_SERVER` locally to select your own registration. This replaces a
private machine-specific server selector in the development copy. Credentials
are read only from the user's existing local registration and are never bundled.
The client also requires the audited cached-server hash; it fails closed if the
matching package is unavailable. No automatic package download is implemented.

See [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md), [SECURITY.md](SECURITY.md), and
[latch-work/LATCH_ADAPTER_SPEC.md](latch-work/LATCH_ADAPTER_SPEC.md).
