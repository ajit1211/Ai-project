"""The JWT signing key and the public JWKS document.

Key sources, in order: the `JWT_PRIVATE_KEY` environment variable (deployed environments
inject it from AWS Secrets Manager), then the key file. In development and test a key file is
created on first use, so a fresh checkout works without setup. Staging and production refuse
to start signing without a provisioned key.
"""

import os
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from nexa_common.jwt import load_private_key, private_key_pem, public_jwk
from nexa_common.logs import get_logger

log = get_logger("nexa.core.keys")


def _create_key_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL: if two processes start at once, exactly one creates the key.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as handle:
        handle.write(private_key_pem(Ed25519PrivateKey.generate()))
    log.warning("jwt.signing_key.created", path=str(path))


@lru_cache
def signing_key() -> Ed25519PrivateKey:
    if settings.NEXA_JWT_PRIVATE_KEY:
        return load_private_key(settings.NEXA_JWT_PRIVATE_KEY)
    path = Path(settings.NEXA_JWT_PRIVATE_KEY_FILE)
    if not path.exists():
        if settings.NEXA_ENV not in ("development", "test"):
            raise ImproperlyConfigured("No JWT signing key: set JWT_PRIVATE_KEY")
        _create_key_file(path)
    return load_private_key(path.read_text())


def verification_keys() -> dict[str, Ed25519PublicKey]:
    """Public keys accepted for verification. During rotation this also holds the previous key."""
    public = signing_key().public_key()
    return {public_jwk(public)["kid"]: public}


def jwks() -> dict[str, list[dict[str, str]]]:
    return {"keys": [public_jwk(key) for key in verification_keys().values()]}
