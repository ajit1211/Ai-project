# API: authentication, accounts and organizations

All endpoints live under `/api/v1`. Responses use one envelope:

```json
{ "success": true, "data": { }, "message": "optional", "meta": { } }
{ "success": false, "error": { "code": "PERMISSION_DENIED", "message": "…", "details": [ ] }, "request_id": "req_…" }
```

`details` appears on validation errors: `[{"field": "email", "code": "invalid", "message": "…"}]`.
Every response carries an `X-Request-ID` header; quote it when reporting a problem.

**Auth column:** *Public* needs no credential. *Token* needs `Authorization: Bearer <access token>`.
*Cookie* uses the refresh cookie (web) or `refresh_token` in the body (mobile) plus the
`X-NEXA-CSRF: 1` header. Organization endpoints additionally require active membership of
`{org}` (404 otherwise) and the listed permission (403 otherwise).

## Error codes

| Code | Status | Meaning |
| --- | --- | --- |
| VALIDATION_FAILED | 422 | A field is invalid; see `details` |
| AUTH_REQUIRED | 401 | No credential |
| AUTH_INVALID_CREDENTIALS | 401 | Wrong email or password |
| AUTH_TOKEN_INVALID / AUTH_TOKEN_EXPIRED | 401 | Bad or expired access token: refresh and retry |
| AUTH_SESSION_REVOKED | 401 | The session has ended: sign in again |
| EMAIL_NOT_VERIFIED | 403 | Verify the email first |
| PERMISSION_DENIED | 403 | Signed in, not allowed |
| RESOURCE_NOT_FOUND | 404 | Does not exist, or belongs to another organization |
| CONFLICT | 409 | The request conflicts with current state (duplicate, last owner, …) |
| TOKEN_INVALID / TOKEN_EXPIRED | 400 | An email link that is used, unknown or expired |
| RATE_LIMITED | 429 | Too many requests; `Retry-After` header in seconds |

## Authentication

| Method and path | Auth | Body | Response |
| --- | --- | --- | --- |
| POST `/auth/register/` | Public | `{email, password, full_name?}` | 202, always the same message |
| POST `/auth/verify-email/` | Public | `{token}` | 200 |
| POST `/auth/resend-verification/` | Public | `{email}` | 202, always the same message |
| POST `/auth/login/` | Public | `{email, password, client: "web" \| "mobile"}` | 200 `{access_token, token_type, expires_in, session_id, active_organization_id, user}`; web gets the refresh cookie, mobile gets `refresh_token` and `refresh_expires_at` in the body |
| POST `/auth/refresh/` | Cookie | `{refresh_token?}` (mobile) | 200, same shape as login; a dead cookie is cleared |
| POST `/auth/logout/` | Token | — | 200, session revoked, cookie cleared |
| POST `/auth/forgot-password/` | Public | `{email}` | 202, always the same message |
| POST `/auth/reset-password/` | Public | `{token, password}` | 200; every session is signed out |
| POST `/auth/change-password/` | Token | `{current_password, new_password}` | 200; other sessions are signed out |
| POST `/auth/switch-organization/` | Token | `{organization_id}` | 200 `{access_token, …, active_organization_id}`; 404 if not a member |
| GET `/.well-known/jwks.json` | Public | — | Public signing keys (no envelope; RFC 7517) |

## Account and sessions

| Method and path | Auth | Body | Response |
| --- | --- | --- | --- |
| GET `/users/me/` | Token | — | `{id, email, full_name, is_verified, created_at, profile: {avatar_url, timezone, locale, preferences}}` |
| PATCH `/users/me/` | Token | any of `full_name, avatar_url (https), timezone (IANA), locale, preferences` | Updated user |
| POST `/users/me/deactivate/` | Token | `{password}` | 200; 409 if sole owner of a shared organization |
| GET `/sessions/` | Token | — | `[{id, device, client_type, ip_address, created_at, last_seen_at, expires_at, is_current}]` |
| DELETE `/sessions/{id}/` | Token | — | 200; only the caller's own sessions are visible |
| POST `/sessions/logout-all/` | Token | — | 200 `{sessions_revoked}` |

## Organizations

| Method and path | Permission | Body | Response |
| --- | --- | --- | --- |
| GET `/organizations/` | Token | — | Organizations the caller belongs to, with their role |
| POST `/organizations/` | Token | `{name}` | 201; caller becomes Owner |
| GET `/organizations/{org}/` | organization.view | — | Organization with `settings` and the caller's effective `permissions` |
| PATCH `/organizations/{org}/` | organization.update | `{name?, settings?}` | Updated organization |
| DELETE `/organizations/{org}/` | organization.delete | — | 200 (soft delete); 409 for personal workspaces |
| POST `/organizations/{org}/leave/` | member | — | 200; 409 for the last owner or a personal workspace |

## Members and invitations

| Method and path | Permission | Body | Response |
| --- | --- | --- | --- |
| GET `/organizations/{org}/members/` | members.view | — | `[{id, user, role, status, joined_at}]` |
| PATCH `/organizations/{org}/members/{membership}/` | members.update | `{role_id}` or `{status: active \| suspended}` | Updated member; 403 on escalation; 409 for the last owner |
| DELETE `/organizations/{org}/members/{membership}/` | members.remove | — | 200 |
| GET `/organizations/{org}/invitations/` | members.invite | — | Pending invitations |
| POST `/organizations/{org}/invitations/` | members.invite | `{email, role_id}` | 201; 409 if already a member or already invited; 403 if the role exceeds the caller's |
| POST `/organizations/{org}/invitations/{id}/` | members.invite | — | Resend (new link, old one dies); 409 after 5 sends |
| DELETE `/organizations/{org}/invitations/{id}/` | members.invite | — | Revoke |
| POST `/invitations/preview/` | Public | `{token}` | `{organization_name, inviter_name, role_name, email, expires_at}` |
| POST `/invitations/accept/` | Token | `{token}` | 200 with the organization; 403 unless signed in as the invited, verified address |
| POST `/invitations/decline/` | Public | `{token}` | 200 |

## Teams

| Method and path | Permission | Body | Response |
| --- | --- | --- | --- |
| GET `/organizations/{org}/teams/` | team.view | — | `[{id, name, slug, description, member_count}]` |
| POST `/organizations/{org}/teams/` | team.create | `{name, description?}` | 201; the creator leads the team |
| GET `/organizations/{org}/teams/{team}/` | team.view | — | Team with `members` |
| PATCH `/organizations/{org}/teams/{team}/` | team.update + resource access | `{name?, description?}` | Updated team |
| DELETE `/organizations/{org}/teams/{team}/` | team.delete + resource access | — | 200 |
| POST `/organizations/{org}/teams/{team}/members/` | team.manage_members + MANAGE on the team | `{membership_id, role: lead \| member}` | 201 |
| DELETE `/organizations/{org}/teams/{team}/members/{membership}/` | team.manage_members + MANAGE on the team | — | 200 |

## Roles

| Method and path | Permission | Body | Response |
| --- | --- | --- | --- |
| GET `/organizations/{org}/roles/` | roles.view | — | `[{id, key, name, description, is_system, permissions}]` |
| POST `/organizations/{org}/roles/` | roles.manage | `{name, description?, permissions: [codename]}` | 201; 403 if the permissions exceed the caller's; 422 for unknown codenames |
| PATCH `/organizations/{org}/roles/{role}/` | roles.manage | `{name?, description?, permissions?}` | Updated role; 403 for system roles |
| DELETE `/organizations/{org}/roles/{role}/` | roles.manage | — | 200; 409 while members hold the role |

## Audit log

| Method and path | Permission | Query | Response |
| --- | --- | --- | --- |
| GET `/organizations/{org}/audit-logs/` | audit.view | `category=audit\|security`, `action`, `actor_id`, `limit` (≤200), `cursor` | `data: [{id, action, category, severity, actor_type, actor_id, resource_type, resource_id, ip_address, metadata, created_at}]`, `meta: {next_cursor, has_more}` |

## Runtime

| Method and path | Auth | Response |
| --- | --- | --- |
| GET `/runtime/whoami` | Token (verified by Runtime with Core's JWKS) | `{user_id, email, session_id, organization_id, role, permissions}` as Runtime sees them |
