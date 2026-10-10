# Testing authentication locally

## Prerequisites

- PostgreSQL. Either the project-local instance (`./scripts/dev-db.ps1 start`, port 5433,
  created in Phase 03 so no system password is needed) or any PostgreSQL with the roles from
  `scripts/db/init.sql`. `.env` points at it.
- Redis is optional for authentication: without it, rate limits fail open with a warning and
  emails are sent inline instead of through Celery. Install Memurai (Windows) or run
  `docker compose up -d` for the real behaviour.

```powershell
./scripts/dev-db.ps1 start   # after every reboot
pnpm migrate                 # applies migrations and syncs the permission catalog
pnpm dev                     # React :5173, Django :8000, FastAPI :8001, Celery worker
```

## Emails

In development the email backend writes each message to a file under `.local/mail/`
(git-ignored). Links point at the SPA, e.g.
`http://localhost:5173/verify-email#token=…`. Open the newest file, copy the link into the
browser, or copy the token for a curl request.

## The scripted walk-through

`uv run python scripts/e2e_auth.py` runs the whole flow against the running stack through
the Vite proxy: register → verification email → verify → login (cookie) → refresh → create
organization → invite → team → audit log → Runtime `whoami` → logout. Each step prints its
status and the exit code is non-zero if anything failed. Run it after any change to
authentication.

## By hand, with curl

```bash
B=http://localhost:5173/api/v1

# 1. Register (always 202) and find the token in .local/mail
curl -s -X POST $B/auth/register/ -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-passphrase","full_name":"Me"}'
curl -s -X POST $B/auth/verify-email/ -H 'Content-Type: application/json' -d '{"token":"<token>"}'

# 2. Sign in; the refresh token arrives as a cookie, the access token in the body
curl -s -c jar.txt -X POST $B/auth/login/ -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-passphrase"}'
export T=<access_token>

# 3. Authenticated calls
curl -s $B/users/me/ -H "Authorization: Bearer $T"
curl -s $B/organizations/ -H "Authorization: Bearer $T"
curl -s $B/runtime/whoami -H "Authorization: Bearer $T"      # answered by FastAPI

# 4. Refresh (cookie + CSRF header; without the header you get 403)
curl -s -b jar.txt -c jar.txt -X POST $B/auth/refresh/ -H 'X-NEXA-CSRF: 1'

# 5. Mobile-style client: refresh token in the body
curl -s -X POST $B/auth/login/ -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-passphrase","client":"mobile"}'
```

Note for curl: the refresh cookie is `Secure`; curl sends it to `http://localhost` only
when the jar line is edited to drop the secure flag, or when you test over https. Browsers
treat localhost as secure and need no workaround.

## In the browser

1. http://localhost:5173/register, then open the link from `.local/mail`.
2. Sign in. The organization switcher shows your personal workspace; create a team
   organization from it.
3. Settings → Sessions shows this device; open a second browser (or a private window) and
   sign in again to see two sessions, then revoke one.
4. Organizations → Members: invite an address, open the invitation link from `.local/mail`
   in a private window, register or sign in as that address, accept.
5. http://localhost:5173/status still shows the stack health.

## Tests

```powershell
uv run pytest                      # 138 tests: models, services, APIs, tenancy, escalation, Runtime auth
uv run pytest -k "tenant or escalation"   # just the security suites
pnpm --filter @nexa/web test       # API client refresh logic, route guards
```

Core's tests use a fake in-memory Redis, so they never need a server. Runtime's auth tests
commit real rows (`transaction=True`) because Runtime reads them over its own connection.

## Resetting

Drop and recreate the local database if it gets into a strange state:

```powershell
./scripts/dev-db.ps1 stop
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\nexa\postgres-18"
# then re-run the initdb command shown in scripts/dev-db.ps1 and ./scripts/dev-db.ps1 init
```

The JWT signing key for development is `.secrets/jwt_signing_key.pem`; deleting it signs
everyone out (every existing access token becomes unverifiable) and a new key is generated
on the next start.
