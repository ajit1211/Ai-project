"""Access tokens: EdDSA-signed JWTs issued by Core and verified by every service (doc 19).

Only Core holds the private key. Every other service verifies with public keys published at
`/.well-known/jwks.json`, so a compromised Runtime cannot mint tokens.

An access token carries identity only (user, session, active organization). Permissions are
never read from the token: each service loads them from the database for every request, so a
role change or a revoked session takes effect immediately rather than when the token expires.
"""

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from nexa_common.errors import ErrorCode, NexaError

ALGORITHM = "EdDSA"
ISSUER = "nexa-core"
AUDIENCE = "nexa-api"
ACCESS_TOKEN_TTL = timedelta(minutes=10)
CLOCK_LEEWAY_SECONDS = 30


class TokenInvalid(NexaError):
    status = 401
    code = ErrorCode.AUTH_TOKEN_INVALID


class TokenExpired(NexaError):
    status = 401
    code = ErrorCode.AUTH_TOKEN_EXPIRED


class UnknownSigningKey(TokenInvalid):
    """The token names a key this service has not seen; refetching the JWKS may help."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: UUID
    session_id: UUID
    organization_id: UUID | None
    token_id: str
    issued_at: datetime
    expires_at: datetime


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _raw_public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(public_key: Ed25519PublicKey) -> str:
    """RFC 7638 JWK thumbprint: a stable ID derived from the key itself."""
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": _b64url(_raw_public_bytes(public_key))},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def public_jwk(public_key: Ed25519PublicKey) -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64url(_raw_public_bytes(public_key)),
        "kid": key_id(public_key),
        "alg": ALGORITHM,
        "use": "sig",
    }


def keys_from_jwks(document: Mapping[str, Any]) -> dict[str, Ed25519PublicKey]:
    keys: dict[str, Ed25519PublicKey] = {}
    for entry in document.get("keys", []):
        if entry.get("kty") != "OKP" or entry.get("crv") != "Ed25519" or "kid" not in entry:
            continue
        key = jwt.PyJWK(entry, algorithm=ALGORITHM).key
        if isinstance(key, Ed25519PublicKey):
            keys[str(entry["kid"])] = key
    return keys


def load_private_key(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The JWT signing key must be an Ed25519 private key")
    return key


def private_key_pem(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def encode_access_token(
    private_key: Ed25519PrivateKey,
    *,
    user_id: UUID,
    session_id: UUID,
    organization_id: UUID | None,
    token_id: str,
    now: datetime | None = None,
    ttl: timedelta = ACCESS_TOKEN_TTL,
) -> str:
    issued = now or datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "typ": "access",
        "sub": str(user_id),
        "sid": str(session_id),
        "org": str(organization_id) if organization_id else None,
        "jti": token_id,
        "iat": issued,
        "exp": issued + ttl,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm=ALGORITHM,
        headers={"kid": key_id(private_key.public_key())},
    )


def decode_access_token(token: str, keys: Mapping[str, Ed25519PublicKey]) -> AccessClaims:
    """Verify signature, issuer, audience and expiry. Raises TokenInvalid / TokenExpired."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise TokenInvalid from exc
    key = keys.get(kid) if isinstance(kid, str) else None
    if key is None:
        raise UnknownSigningKey

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],  # pinned: never accept "none" or HMAC algorithms
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "sid", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired from exc
    except jwt.PyJWTError as exc:
        raise TokenInvalid from exc

    if payload.get("typ") != "access":
        raise TokenInvalid
    try:
        org = payload.get("org")
        return AccessClaims(
            user_id=UUID(payload["sub"]),
            session_id=UUID(payload["sid"]),
            organization_id=UUID(org) if org else None,
            token_id=str(payload["jti"]),
            issued_at=datetime.fromtimestamp(payload["iat"], UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenInvalid from exc
