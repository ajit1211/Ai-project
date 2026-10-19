"""Where document bytes live (docs/architecture/documents.md#storage).

The application sees one small interface. Development and tests use a private directory
(`.local/documents`, outside the web root, never served); production gets an object-store
backend behind the same interface. Keys are made by the server from ids, never from
anything the client sent, and a local key can never leave the storage root.
"""

import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Protocol
from uuid import UUID

from django.conf import settings

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*){1,3}$")
CHUNK_BYTES = 1024 * 1024


class StorageError(Exception):
    """The stored bytes could not be written, read or removed."""


class DocumentStorage(Protocol):
    def save(self, key: str, chunks: Iterator[bytes]) -> int:
        """Write a new object from `chunks`; return the size. Never overwrites."""
        ...

    def open(self, key: str) -> IO[bytes]: ...

    def delete(self, key: str) -> None:
        """Remove the object; a missing object is not an error."""
        ...

    def exists(self, key: str) -> bool: ...


def storage_key(organization_id: UUID, document_id: UUID, extension: str) -> str:
    """`<organization>/<document><ext>`: unique per document, grouped per tenant."""
    suffix = extension.lower() if re.fullmatch(r"\.[a-z0-9]{1,10}", extension.lower()) else ""
    return f"{organization_id}/{document_id}{suffix}"


class LocalDocumentStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        if not _KEY.match(key) or ".." in key.split("/"):
            raise StorageError("invalid storage key")
        root = self.root.resolve()
        path = (root / key).resolve()
        if root not in path.parents:
            raise StorageError("storage key escapes the root")
        return path

    def save(self, key: str, chunks: Iterator[bytes]) -> int:
        path = self._path(key)
        if path.exists():
            raise StorageError("object already exists")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            # Written beside its final place, then renamed: a reader never sees a partial file.
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".upload-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                for chunk in chunks:
                    handle.write(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                if "temporary" in locals():
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError("could not write the file") from exc
        return size

    def open(self, key: str) -> IO[bytes]:
        try:
            return self._path(key).open("rb")
        except OSError as exc:
            raise StorageError("could not read the file") from exc

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("could not delete the file") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


def get_storage() -> DocumentStorage:
    """The configured backend. Only the local one exists; an S3-compatible backend plugs in
    here (NEXA_DOCUMENTS_STORAGE_BACKEND) without touching the rest."""
    backend = getattr(settings, "NEXA_DOCUMENTS_STORAGE_BACKEND", "local")
    if backend != "local":
        raise StorageError(f"unknown document storage backend '{backend}'")
    return LocalDocumentStorage(Path(settings.NEXA_DOCUMENTS_STORAGE_ROOT))


def read_chunks(handle: IO[bytes], size: int = CHUNK_BYTES) -> Iterator[bytes]:
    while chunk := handle.read(size):
        yield chunk
