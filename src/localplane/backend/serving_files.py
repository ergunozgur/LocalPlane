"""Descriptor-pinned, no-symlink startup inputs. Never reads private-key contents."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ServingConfigurationError(ValueError):
    """Only fixed actionable messages, never underlying OS/SSL errors or paths."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _trusted_directory(fd: int) -> None:
    info = os.fstat(fd)
    # Root-owned sticky temporary ancestors cannot let another user replace a
    # service-owned child. Ordinary group/other-writable ancestors are refused.
    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    if info.st_uid not in {0, os.geteuid()} or (info.st_mode & 0o022 and not sticky_root):
        raise OSError("unsafe directory")


@contextmanager
def pinned_directory(path: Path) -> Iterator[int]:
    """Walk all components with openat/O_NOFOLLOW, retaining the final directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    # Do not resolve symlinks or collapse '..' across a symlink.
    absolute = path if path.is_absolute() else Path.cwd() / path
    if ".." in absolute.parts:
        raise OSError("parent component not supported")
    fd = os.open("/", flags)
    try:
        _trusted_directory(fd)
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            _trusted_directory(fd)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def pinned_file(directory: int, name: str, *, private: bool = False) -> Iterator[int]:
    if not name or name in {".", ".."} or "/" in name:
        raise OSError("invalid file component")
    # NONBLOCK makes a replaced FIFO fail validation rather than hang startup.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("not a single regular file")
        if private:
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
                raise OSError("private key ownership or permissions")
        elif info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise OSError("unsafe public input")
        if not info.st_mode & 0o444:
            raise OSError("unreadable input")
        yield fd
    finally:
        os.close(fd)
