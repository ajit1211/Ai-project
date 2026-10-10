# ADR-025: Fixed-window rate limits in Redis that fail open

Status: accepted

## Context

Login, registration, password reset and verification resend must resist brute force and
abuse. The limits must work across several API processes, must never lock a legitimate
user out for good, and must not take the platform down with them.

## Decision

- Counters live in Redis, keyed by limit name, a hash of the subject (IP or account, never
  the raw value) and the current window. `INCR` + `EXPIRE` per hit; windows are fixed.
- Limits are per IP for everything and per account for failures and reset-style requests.
  Account counters are cleared by a successful login or password reset. Every limit expires
  with its window, so nothing is permanent.
- If Redis is unreachable, limits fail open with a warning, and a five-second breaker avoids
  paying a connection timeout per request. The first breach in a window is audited.
- Per-account limits on "forgot password" and "resend verification" are enforced silently,
  because an error there would reveal that the account exists.

## Alternatives

- Fail closed: a Redis outage would stop every login. Rejected; rate limiting is a layer, not
  the only defence (Argon2id and audit remain).
- Sliding windows or token buckets: smoother, but fixed windows are simpler to reason about
  and the burst they allow at a boundary is small at these limits.
- Database counters: contention on hot rows and no automatic expiry.
- Account lockout until an admin unlocks: a denial-of-service vector against any known email.

## Consequences

- Core's tests run against an in-memory fake Redis; production behaviour is the same code.
- The limits are starting values, listed in `security/ratelimit.py` and `security.md`, to
  be tuned from the `RATE_LIMIT_EXCEEDED` audit events.
