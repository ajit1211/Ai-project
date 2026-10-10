# Phase 03 verification report

Date: 2026-10-08. Scope: identity, authentication, authorization, organizations, teams,
roles, sessions, audit, rate limiting, in Django (Core), FastAPI (Runtime) and React.

## Checklist

| Item | Status | Evidence |
| --- | --- | --- |
| Registration | ✓ | `test_auth_api.py::TestRegistrationAndVerification`; e2e step 1 |
| Login | ✓ | `TestLoginAndTokens`; identical answers for wrong password and unknown email |
| JWT | ✓ | EdDSA tokens, `test_security_services.py::TestAccessTokens` (expiry, tampering, `alg=none`, unknown key) |
| Refresh | ✓ | Rotation, reuse detection with grace window, cookie + CSRF header, mobile body mode |
| Logout | ✓ | Session revoked immediately; cookie cleared; `AUTH_SESSION_REVOKED` on the next call |
| Email verification | ✓ | Single-use, expiring, resend replaces the link; links read from `.local/mail` in e2e |
| Password reset | ✓ | Request/reset/expired/reused; every session ends; `TestPasswords` |
| Session management | ✓ | List, revoke one (not another user's), sign out everywhere; device labels |
| Organizations | ✓ | Create (creator is Owner, five system roles), detail with effective permissions, update with validated settings, soft delete, leave |
| Organization switching | ✓ | `POST /auth/switch-organization/` re-verifies membership; 404 for others; SPA switcher |
| Teams | ✓ | CRUD; leads manage their own team only; `team.manage_all` for managers |
| Roles | ✓ | System roles synced from the catalog; custom roles bounded by the creator's permissions; immutable system roles |
| Permissions | ✓ | 43 catalog permissions incl. future AI ones; loaded per request, never from the token |
| RBAC | ✓ | `required_permissions` on every org view; `test_organizations_api.py` role matrix cases |
| Resource authorization | ✓ | `nexa_common.access` policy tests (owner, grants, visibility, ceiling); teams as the first live resource |
| Multi-tenancy | ✓ | `TestTenantIsolation`: 10 endpoints × foreign org → 404; foreign IDs inside own org → 404/422; database composite FKs (`test_data_model.py`) |
| Audit logs | ✓ | Append-only model; API with cursor pagination and security filter; events on every change |
| Rate limiting | ✓ | Per-IP and per-account limits, temporary lock, fail-open breaker, first breach audited |
| Django authentication | ✓ | `JWTAuthentication` + per-request session check; 401 for missing credentials |
| FastAPI authentication | ✓ | JWKS fetch with refetch on unknown key; `/runtime/whoami`; `test_runtime_auth.py` (7 integration tests against the real database) |
| React protected routes | ✓ | `ProtectedRoute`, `PublicOnlyRoute`, loading state; `routes.test.tsx` |
| Security tests | ✓ | IDOR, privilege escalation (admin→owner, manager→admin invite, self-edit, last owner, custom role ceiling), cross-organization access, token misuse, CSRF on refresh, brute-force lock |

## Test results

| Suite | Count | Result |
| --- | --- | --- |
| Python: `packages/nexa-common` (policy, envelope, JWT helpers) | 16 | pass |
| Python: `apps/core` (models, services, auth API, organizations API, emails) | 116 | pass |
| Python: `apps/runtime` (envelope, health, auth integration) | 7 | pass |
| Web: API client refresh logic, route guards, status summary | 13 | pass |
| End-to-end through the Vite proxy (`scripts/e2e_auth.py`) | 23 checks | pass |
| ruff, ruff format, mypy --strict (93 files), ESLint strict, tsc, `makemigrations --check` | — | clean |

The Python suite runs in about 55 seconds; the Runtime integration tests dominate because
they commit real rows and connect a second time.

## Live run

With `pnpm dev` and no Redis on the machine: register → verify → login → refresh →
create organization → invite → team → audit log → Runtime `whoami` → logout, all through
`http://localhost:5173`. Without Redis, the first signup paid a 4–5 s broker timeout before
the inline email fallback; the breaker added afterwards limits that to one attempt per
30 seconds.

## Decisions made during the phase

- API envelope (`{success, data, message}` / `{success, error, request_id}`) replaces the
  Phase 02 Problem Details format, as the Phase 03 brief asked; both services switched
  centrally.
- `/runtime/whoami` lives under a new Runtime-owned prefix because `/auth` belongs to Core
  in the edge routing table.
- Phase 03's requested ADR-001…007 are recorded as ADR-019…025 to continue the Phase 01
  numbering.
- Tailwind and TanStack Query from the Phase 01 standards are deferred to Phase 05, when the
  chat UI needs server-state caching; Phase 03's UI uses plain CSS and `AuthContext`.

## Known gaps

| Gap | Plan |
| --- | --- |
| No MFA | Login is a state machine ready for TOTP/passkeys; Phase 17 |
| No CAPTCHA | Add if registration abuse appears |
| Redis not installed on this development machine (Memurai's installer failed from the automation sandbox) | Install from an administrator PowerShell: `winget install Memurai.MemuraiDeveloper`; everything works without it, with rate limits failing open |
| Row-level security | Phase 09 with the first data tables |
| Signing-key rotation is manual | Automate in Phase 19 |
| Browser-level E2E (Playwright) | Phase 05, when the UI gains real journeys |
