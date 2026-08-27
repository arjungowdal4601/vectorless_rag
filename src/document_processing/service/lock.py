"""Exclusive process ownership for one local artifact root."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from types import TracebackType
from typing import TextIO

from document_processing.errors import ArtifactRootLockedError


class ArtifactRootLock:
    """Hold a non-blocking advisory lock for the service lifetime."""

    def __init__(self, artifact_root: Path) -> None:
        self._artifact_root = artifact_root
        self._path = artifact_root / ".service.lock"
        self._file: TextIO | None = None

    @property
    def is_held(self) -> bool:
        return self._file is not None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        if self.is_held:
            return
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        if self._artifact_root.is_symlink() or not self._artifact_root.is_dir():
            raise ArtifactRootLockedError("The configured artifact root is not a safe directory.")

        try:
            before = self._path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
            raise ArtifactRootLockedError("The service lock path is not a safe regular file.")

        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise ArtifactRootLockedError("The service lock path is unsafe.") from exc
        lock_file = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            opened = os.fstat(lock_file.fileno())
            current = self._path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (
                    before is not None
                    and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                )
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ArtifactRootLockedError(
                    "The service lock path changed or is not a safe regular file."
                )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise ArtifactRootLockedError(
                "Another document-processing service owns the artifact root."
            ) from exc
        except BaseException:
            lock_file.close()
            raise

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        lock_file = self._file
        self._file = None
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def __enter__(self) -> ArtifactRootLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
