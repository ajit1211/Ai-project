# ADR-019: Custom user model, email identity, UUIDv7 keys

Status: accepted (Phase 02 created the model; Phase 03 finished it)

## Context

Django's default user is keyed by a username with a sequential integer ID and splits names
into first and last. NEXA identifies people by email, exposes IDs in URLs and APIs, and
serves users whose names do not split into a given and a family name. Django only allows
the user model to be swapped before the first migration.

## Decision

- `identity.User` extends `AbstractBaseUser` + `PermissionsMixin` with `email` as the
  username field, stored lowercase and unique (a check constraint enforces lowercase).
- Primary key is a UUIDv7: time-ordered, so inserts stay local in the index, and safe to
  expose, so no second "public ID" column is needed. Every table in NEXA follows this.
- One `full_name` field instead of first/last.
- Account state is explicit: `is_active` (deactivated accounts cannot sign in),
  `email_verified_at`, `password_changed_at`, `deactivated_at`.
- Presentation and preferences (avatar, time zone, language, notification settings) live on
  `UserProfile`, a one-to-one table created automatically for every user. The
  authentication table stays small and changes to preferences never touch security rows;
  `preferences` is JSON validated against a Pydantic schema so unknown keys are refused.

## Alternatives

- Django's default user with a profile table: username would still be required and the
  integer IDs would leak row counts.
- Everything on the user model: simpler joins, but every preference change becomes a write
  to the table that logins and sessions depend on, and the model grows without bound.

## Consequences

- Sequential IDs never appear in the API.
- Email changes (not yet supported) will need re-verification; the one-time token stores the
  address it was sent to for that reason.
