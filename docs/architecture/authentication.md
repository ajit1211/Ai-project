# Authentication

NEXA signs people in with an email address and password, issues a short-lived access token
plus a rotating refresh token, and checks the session in the database on every request.
Both services verify tokens the same way; only Core can issue them.

## Credentials at a glance

| Credential | Form | Lifetime | Where the client keeps it | What the server stores |
| --- | --- | --- | --- | --- |
| Access token | JWT, EdDSA (Ed25519) signed by Core | 10 minutes | JavaScript memory only | Nothing (stateless), but the session it names is checked on every request |
| Refresh token | 256-bit random string | Rotated on every use; 14 days of inactivity; 30 days absolute (the session) | Web: `HttpOnly; Secure; SameSite=Strict` cookie scoped to `/api/v1/auth`. Mobile and API clients: response body | SHA-256 hash, with the chain of rotations |
| Email verification link | 256-bit random token in the URL fragment | 24 hours, single use | Email | SHA-256 hash |
| Password reset link | 256-bit random token in the URL fragment | 30 minutes, single use; all open links die when one is used | Email | SHA-256 hash |
| Invitation link | 256-bit random token in the URL fragment | 7 days by default (organization setting, 1–30) | Email | SHA-256 hash |
| Password | Argon2id hash | — | — | Hash only |

Secrets that are random (not chosen by a person) are hashed with plain SHA-256: there is
nothing to brute-force, and a lookup by hash stays one indexed query.

## Where the browser keeps tokens

| Option | Verdict | Why |
| --- | --- | --- |
| `localStorage` / `sessionStorage` | Rejected | Any script running on the page (an XSS bug, a compromised dependency) can read it and send it elsewhere. A long-lived refresh token there is a standing invitation. |
| Readable cookie | Rejected | Same exposure as storage, plus CSRF. |
| Access token in memory, refresh token in an `HttpOnly` cookie | **Chosen** | Scripts cannot read either credential: the access token lives in a module variable that dies with the tab, and the refresh cookie is invisible to JavaScript. A stolen access token is useful for at most ten minutes. |

The refresh cookie's scope is deliberately narrow: `SameSite=Strict` means other sites never
trigger it, and `Path=/api/v1/auth` means it travels only to the authentication endpoints,
never with ordinary API calls. Because the cookie is sent automatically, the refresh endpoint
also requires a custom header (`X-NEXA-CSRF: 1`) that a cross-site form cannot set, and
checks the `Origin` header when a browser sends one.

Mobile apps and API clients cannot use cookies the same way. They pass `"client": "mobile"`
at login and receive the refresh token in the body, to store in the platform keychain. The
same endpoints serve both kinds of client; nothing else changes.

## Login

```
POST /api/v1/auth/login/ {email, password, client}
  │
  ├─ rate limits: 30 attempts per IP per 5 min; after 10 failures for one account,
  │  that account waits 15 minutes (never permanently)
  ├─ password check with Argon2id; unknown emails take the same time as wrong passwords
  ├─ email must be verified (revealed only to someone who knows the password)
  ├─ a new AuthSession (device label from the User-Agent, IP, active organization)
  ├─ refresh token #1 for that session; access token for its active organization
  └─ audit: USER_LOGIN, SESSION_CREATED, NEW_DEVICE_LOGIN when the device is unfamiliar
```

The session starts in the user's personal workspace, or the organization they joined first.

## Access tokens

Claims: `iss nexa-core`, `aud nexa-api`, `typ access`, `sub` (user ID), `sid` (session ID),
`org` (active organization ID, may be null), `jti`, `iat`, `exp`. The header carries the
`kid` of the signing key.

Deliberately **not** in the token: role or permissions. Both services load them from the
database for every request, so a demotion or a revoked session takes effect on the next
call rather than up to ten minutes later. The cost is one indexed query per request.

Verification is the same code in both services (`nexa_common.jwt.decode_access_token`):
algorithm pinned to EdDSA (no `none`, no HMAC), issuer and audience checked, 30 seconds of
clock leeway, required claims enforced.

## Refresh and rotation

Every refresh spends the presented token and issues a new one linked to it:

```
token #1 ──used──► token #2 ──used──► token #3 ──► ...
```

Presenting a token that was already spent means two parties hold it. Inside a 10-second
grace window this is treated as a race between browser tabs and answered with a retryable
401. Outside it, the whole session is revoked (`TOKEN_REUSE_DETECTED`, a critical security
event), which cuts off the attacker and the legitimate holder alike; the legitimate user
signs in again.

A refresh never changes the active organization; `POST /auth/switch-organization/` does,
and only to an organization the user is an active member of.

## Sessions and devices

`AuthSession` is one sign-in on one device: label ("Chrome on Windows"), IP, first and last
seen, absolute expiry, and the organization currently active in it. Users list their
sessions, revoke one, or sign out everywhere. A revoked session fails immediately on the
next request, for both the refresh and the access token.

Sessions are also revoked automatically: all of them on password reset or account
deactivation, all but the current one on password change.

## Signing keys

Core signs with an Ed25519 private key. In development and test a key is generated into
`.secrets/jwt_signing_key.pem` (git-ignored) on first use; staging and production must
provide `JWT_PRIVATE_KEY` (PEM) from the secret store and refuse to start without it.

Public keys are published at `GET /.well-known/jwks.json` (cached 10 minutes). Rotation:
publish the new key alongside the old, wait longer than the cache, start signing with the
new key, retire the old one after the longest access-token lifetime. Runtime refetches the
document when it sees an unknown `kid`, rate-limited to once per 30 seconds.

## Email verification and password reset

Links carry their token in the URL fragment (`/verify-email#token=…`). Browsers never send
the fragment to a server, so tokens stay out of access logs, proxies and `Referer` headers;
the SPA reads it and posts it to the API.

Registration always answers "check your email", whether or not the address was already
registered; an existing verified account gets a "you already have an account" email instead
of a second account. The same shape of answer covers "resend verification" and "forgot
password", so the API never confirms that an email is registered.

Issuing a new link invalidates any open link of the same kind. Following a reset link
proves control of the mailbox, so it also verifies the email if that was still pending.

## Passwords

Argon2id hashing (Django's hasher), minimum 10 characters, common-password and
user-attribute similarity checks, no composition rules (NIST SP 800-63B). Passwords are
never logged, never returned, and never included in audit metadata (the audit writer strips
any key that looks like a credential).

## How Runtime authenticates

Runtime never calls Core at request time. It verifies the token with Core's public keys,
then reads `auth_sessions`, `users`, `organization_memberships`, `roles`, `role_permissions`
and `team_memberships` over its own read-only database role to build the same `Principal`
Core would. A client cannot send Runtime a user ID, organization ID or role: everything
comes from the verified token and the database.

## Service-to-service authentication

| Option | Verdict |
| --- | --- |
| Synchronous calls between Core and Runtime | **Avoided entirely.** Runtime fetches only Core's public JWKS document; all other data flows through the shared database and queues. There is no internal API to secure yet. |
| Database roles | **In place.** `nexa_core` owns the schema; `nexa_runtime` can read Core's tables but write only its own (table grants in `scripts/db/init.sql` and future migrations). |
| Network restrictions | **Production design.** Core and Runtime run in private subnets; the JWKS fetch uses Core's internal address; only the edge is public. |
| Internal JWT / mTLS | **When an internal API appears** (first candidate: Phase 05, if Runtime must notify Core). mTLS via the service mesh or ALB, or Core-signed service tokens verified with the same JWKS, whichever the deployment platform makes cheaper. |

## Related

- [authorization.md](authorization.md): what a signed-in user may do
- [multi-tenancy.md](multi-tenancy.md): how the active organization is chosen and enforced
- [security.md](security.md): threats and controls, rate limits
- [../api/authentication.md](../api/authentication.md): endpoints
- ADRs 019–025 in [../adr/](../adr/)
