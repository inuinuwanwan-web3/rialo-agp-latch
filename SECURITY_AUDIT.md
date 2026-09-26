# Publication security audit

This clean snapshot was assembled from an explicit source/docs/tests allowlist.
The source workspaces were not initialized, restructured, or modified.

Checks passed before publication:

- No environment value files, actual config.toml, database, log, runtime state,
  credential cache, backup, virtual environment, or generated Python cache files.
- No detected credential-like values, private Latch runtime identifiers, wallet
  private keys, Telegram bot tokens, API keys, JWTs, or private-key blocks.
- Contents were compared in memory against known local credential values;
  those values were not printed, stored in this repository, or copied into tests.
- Matches for token/secret/API-key terminology were variable names, redaction
  patterns, documentation, dependency names, or explicitly synthetic fixtures.
- Private server registration identifiers and machine-specific home paths were
  removed from the publication copy. Credential-loading code remains local-only.
- Python sources parsed successfully.
- Focused offline tests in the publication workspace: **113 passed / 0 failed**.
- Source-file hashes confirmed that the copied live originals were unchanged.

No real Latch, model, AGP race, or Telegram call was performed for this audit.
The full historical AGP regression suite was not rerun.

These checks reduce publication risk; they do not certify production security.
Future commits need the same review, even when ignore rules are present.


## 2026-09-26 diagnostic publication audit

Scope: the ALLOW fetch guard, safe diagnostic helper, transport and CLI/client
integration, two synthetic diagnostic test modules, and progress documentation.
The public client's generic registration selector and home-relative paths are
preserved. No AGP source, policy, machine configuration or runtime state is added.

Publication checks cover credential patterns, private-key blocks, authentication
values, private identifiers and known local credential values (compared only in
memory, never printed). Authentication-related field names in tests refer only
to synthetic fixtures; no real header values or runtime request bodies are added.
Diagnostics pass only fixed categories and a fixed phase. Unrecognized causes
remain unknown, and error responses still fail closed without retry or fallback.

Focused diagnostic suite: **142 passed / 0 failed**, with synthetic fetch failures
and no live service calls. Real ALLOW success and the live fetch root cause remain
unverified. This audit does not certify production security.
