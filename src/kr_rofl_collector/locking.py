from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from .errors import ConfigurationError


class CollectorLock:
    def __init__(self, path: Path):
        self.path = path
        self._stream = None

    def __enter__(self) -> CollectorLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._stream = self.path.open("a+b")
            self._stream.seek(0)
            if self._stream.read(1) == b"":
                self._stream.seek(0)
                self._stream.write(b"0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._stream is not None:
                with suppress(OSError):
                    self._stream.close()
            self._stream = None
            raise ConfigurationError(
                "COLLECTOR_ALREADY_RUNNING",
                "Another collector process is already running",
            ) from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._stream is None:
            return
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None
