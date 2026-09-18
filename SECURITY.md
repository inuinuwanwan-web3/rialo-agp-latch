# Security

Never commit credentials. Latch access tokens, API credentials, Telegram bot
credentials, wallet private keys and seed phrases must stay local. Do not attach
real configuration, authentication caches, logs, session data or databases to
issues or pull requests. Example configurations contain no credential values.

The Latch boundary fails closed on DENY, unknown/conflicting decisions, malformed
responses, unavailable MCP and timeouts. It does not retry after dispatch or fall
back to a direct model. A timeout cannot undo a request already executed upstream.
The one-dispatch rule applies to each smoke invocation, not a permanent global
limit across independent invocations. Registration/start_track is outside this
solver boundary and is not protected by this Latch gate.

Synthetic test strings containing words such as token or secret are not live
credentials. Runtime values and machine-specific server identifiers are excluded.
The ignore rules are a safeguard, not a substitute for reviewing every commit.

Responsible disclosure contact: not yet specified. Do not disclose secrets or
sensitive operational details publicly.
