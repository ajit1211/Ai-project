# ADR-020: Access tokens in memory, refresh tokens in an HttpOnly cookie, sessions checked per request

Status: accepted

## Context

Two services must authenticate the same users without calling each other, browsers are the
main client but mobile apps and API clients will follow, and a sign-out, password change or
suspension must take effect immediately. Plain JWTs with a long life cannot be revoked;
server sessions alone cannot be verified by a second service without a shared store.

## Decision

- Core issues EdDSA-signed access tokens that live 10 minutes and carry identity only
  (user, session, active organization), never permissions.
- Every request looks up the session row named by the token. Revocation, expiry, account
  deactivation and unverified email are all enforced there, so a token is only as alive as
  its session.
- Refresh tokens are opaque 256-bit secrets stored as hashes, rotated on every use, with
  reuse detection (10-second grace for racing tabs, otherwise the session is revoked).
- Web clients receive the refresh token as an `HttpOnly; Secure; SameSite=Strict` cookie
  scoped to `/api/v1/auth`, and keep the access token in JavaScript memory. Mobile and API
  clients receive it in the body.
- Lifetimes: 10 minutes access (a leaked token is briefly useful; a busy client refreshes
  six times an hour), 14 days of refresh inactivity, 30 days absolute per session.
- Runtime verifies tokens with Core's public keys from `/.well-known/jwks.json`.

## Alternatives

- Tokens in `localStorage`: readable by any script on the page; rejected.
- Long-lived JWTs with permissions inside: cannot revoke, stale after role changes.
- Server-side session cookies only: simplest for one service, but Runtime would need Core's
  session store or an internal API on every request, and mobile clients would need a
  separate scheme.
- HMAC-signed JWTs with a shared secret: every service holding the secret can mint tokens.

## Consequences

- One indexed query per authenticated request (the session check). Acceptable; it is what
  makes revocation immediate.
- Clients must handle 401 by refreshing once and replaying (the SPA client does this with a
  single refresh shared between concurrent failures).
- Key rotation is a documented manual procedure until Phase 19 automates it.
