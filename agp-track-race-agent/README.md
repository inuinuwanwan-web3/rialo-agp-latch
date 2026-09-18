# AGP Track Race Agent

Development agent with explicit execution controls, conservative participation
checks, bounded solver execution, write-safety tracking and synthetic tests.
See the repository-level development status for verified and unverified results.
No full live race completion or production readiness is claimed.

`agp_race_agent/` contains source; `tests/` contains synthetic contracts.
`config.example.toml` is a credential-free template. Keep any local `config.toml`,
logs and state databases outside version control. Authentication references an
existing local Codex MCP registration; no credentials are bundled here.

`solver_stub.py` is the simple solver example; `codex_solver.py` runs an explicitly
configured Codex CLI solver. The separate Latch integration lives alongside this
project in `../latch-work/`. Its gate does not protect start_track.

The optional scripts under `scripts/` support local Telegram configuration and
notifications. They can make real Telegram requests when explicitly executed.
Do not run them as part of a test or installation and never publish their local
configuration. Deployment-specific notes, pairing state, and service settings
were excluded from this public snapshot.
