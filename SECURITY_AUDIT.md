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
