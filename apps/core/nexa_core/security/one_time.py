"""One-time secrets for email links and refresh tokens.

A secret is 256 bits from the OS's CSPRNG; only its SHA-256 hash is stored. A fast hash is
right here: these secrets are random, not human-chosen, so there is nothing to brute-force,
and lookups by hash stay a single indexed query.
"""

import hashlib
import hmac
import secrets


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def same_secret(raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(raw), stored_hash)
