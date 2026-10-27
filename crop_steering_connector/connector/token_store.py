"""The connector token in `/data/connector_token`: mode 0600, written atomically, out of HA backups."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from .settings import is_connector_token

TOKEN_FILE_NAME = "connector_token"


class TokenStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._path = data_dir / TOKEN_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> str | None:
        """The stored token, or None when there is none or it is not a token."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        if stat.S_IMODE(info.st_mode) != 0o600:
            with contextlib.suppress(OSError):
                os.chmod(self._path, 0o600)
        try:
            with self._path.open("r", encoding="ascii") as handle:
                value = handle.read(512).strip()
        except (OSError, UnicodeDecodeError):
            return None
        return value if is_connector_token(value) else None

    def write(self, token: str) -> None:
        """Write through a private temporary file in the same folder, then rename it over the old one."""
        if not is_connector_token(token):
            raise ValueError("not a connector token")
        descriptor, temporary = tempfile.mkstemp(prefix=f"{TOKEN_FILE_NAME}.", suffix=".tmp", dir=self._dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        with contextlib.suppress(OSError):
            directory = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def delete(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
