# Security (identity and access)

This page lists the threats Phase 03 defends against and how. The platform-wide threat
model, including AI-specific threats for later phases, is section 20 of the architecture
document.

## Threats and controls

| Threat | Control |
| --- | --- |
| Credential stuffing, password brute force | Argon2id; per-IP and per-account rate limits with temporary account lock; constant-time behaviour for unknown emails; failed logins audited as security events |
| Account enumeration | Register, resend-verification and forgot-password answer identically for known and unknown addresses; registration of an existing address emails the owner instead of erroring; login reveals "unverified" only to someone who knows the password |
| Token theft through XSS | Access token in memory only (10-minute life); refresh token in an `HttpOnly` cookie; React escapes output; no raw HTML rendering |
| CSRF on the refresh endpoint | `SameSite=Strict`, cookie path limited to `/api/v1/auth`, required `X-NEXA-CSRF` header, `Origin` check |
| CSRF elsewhere | Every other endpoint authenticates with the `Authorization` header, which browsers never attach automatically |
| Token forgery | EdDSA signatures; algorithm pinned; `none` and HMAC rejected; issuer and audience checked; only Core holds the private key |
| Stolen refresh token | Rotation with reuse detection: a replayed token revokes the whole session |
| Session persistence after logout, password change or deactivation | Session row checked on every request; revocation is immediate |
| Insecure direct object reference | Every lookup is scoped to the caller's organization or user; foreign IDs are 404 |
| Privilege escalation | Role ceiling plus escalation rules (cannot grant what you lack, Owner rules, no self-management, last Owner protected); system roles immutable |
| Cross-tenant access | Membership verified per request; composite foreign keys in the database |
| Leaked email links | Tokens in URL fragments (not sent to servers), hashed at rest, single use, short lived |
| Automated registration and reset abuse | Per-IP limits on registration, reset and resend; per-account limits on reset and resend |
| Audit tampering | Append-only model (`save` on existing rows and `delete` raise); no foreign keys that cascade; retention by partition drop later |
| Secrets in logs or audit metadata | Audit writer strips keys containing password/token/secret/authorization/cookie/key; structured logs never carry credentials |
| Open redirects after login | The SPA only navigates to in-app paths stored in router state |

## Rate limits

Fixed windows in Redis, keyed by a hash of the subject (never the raw email or IP).

| Endpoint | Per IP | Per account |
| --- | --- | --- |
| Login | 30 attempts / 5 min | 10 failures / 15 min, cleared by a successful login or a password reset |
| Register | 5 / hour | — |
| Forgot password | 10 / hour | 3 / hour (silently ignored beyond that, to avoid enumeration) |
| Resend verification | 10 / hour | 3 / hour (silently ignored) |
| Redeem a link (verify, reset, invitation) | 30 / hour | — |
| Invitations | — | 50 / hour per organization |

Nothing locks an account permanently: every counter expires with its window. The first
breach of a limit in a window is audited (`RATE_LIMIT_EXCEEDED`); later ones are not, so an
attack cannot flood the audit log.

If Redis is unreachable the limits fail open and log a warning, and a five-second breaker
stops each request from paying a connection timeout. Refusing every login because a cache is
down would turn a Redis outage into a full outage; the trade-off is documented in ADR-025.

## CORS and CSRF configuration

- The SPA is served from the same origin as the API (Vite proxy locally, the edge in
  production), so **no CORS is needed** and `CORS_ALLOWED_ORIGINS` is empty by default. Extra
  origins must be listed explicitly; there is no wildcard, and credentials are allowed only
  when a list is set.
- `CSRF_TRUSTED_ORIGINS` is the frontend URL plus any listed origins; the refresh endpoint
  compares the `Origin` header against it.
- Django's own CSRF middleware stays active for the admin site, which uses session cookies.

## Cookies

```
Set-Cookie: nexa_refresh=<token>; Path=/api/v1/auth; Expires=<14 days>; Secure; HttpOnly; SameSite=Strict
```

Browsers treat `http://localhost` as a secure context, so the `Secure` flag works in
development without TLS.

## Security events

Security-relevant actions are audit entries with `category=security` and a severity. They
appear in the organization's audit log (filter `?category=security`) and are also written to
the log stream at WARNING level for monitoring:

| Action | Severity |
| --- | --- |
| USER_LOGIN_FAILED, RATE_LIMIT_EXCEEDED, USER_DEACTIVATED, PERMISSION_CHANGED | warning |
| TOKEN_REUSE_DETECTED | critical |
| NEW_DEVICE_LOGIN, PASSWORD_CHANGED, PASSWORD_RESET_REQUESTED, PASSWORD_RESET, SESSION_REVOKED, ALL_SESSIONS_REVOKED, MEMBER_ROLE_CHANGED, ROLE_* | info |

Audit entries record the actor, organization, action, resource, IP, user agent, request ID
and redacted metadata. `X-Forwarded-For` is trusted only behind the configured number of
proxies (`TRUSTED_PROXY_COUNT`), because anyone can send that header.

## Known gaps, by design, for now

| Gap | Plan |
| --- | --- |
| No multi-factor authentication | The login flow is built as a state machine (`password_ok → mfa_required → authenticated`) so TOTP and passkeys slot in without changing clients. Phase 17. |
| No CAPTCHA on registration | Rate limits only. Add when abuse appears. |
| Rate limits fail open without Redis | Accepted (see above); alert on the warning. |
| No row-level security yet | Phase 09, with the first data tables. |
| Signing key rotation is a manual procedure | Automate when a secret manager with rotation hooks is in place (Phase 19). |
| Email delivery is best-effort | Sent after commit via Celery, inline if the queue is down; a failed send is logged, not retried indefinitely. Delivery tracking arrives with notifications. |
