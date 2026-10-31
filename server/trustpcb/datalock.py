"""One backend per data folder.

Two apps on the same folder would share one database and one set of solver containers (the container
label names the folder, not the process), so a second app's start-up sweep could stop the first app's
running solver, and its crash recovery would mark the first app's live reviews as interrupted.

`DataLock` stops that. The lock file `trustpcb.lock` in the data folder holds the owner's process id and
start time as JSON, and the owner keeps an operating-system lock on the file for as long as it lives.
The operating system drops that lock when the process ends, however it ends, so a lock left behind by a
crash is stale by construction: the next start takes it over and says so. A second live backend is refused
with a message that names the first one.

Everything that acts on state it did not create in this process (crash recovery, the container sweep)
must check `holds(data_dir)` first.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

try:                                    # POSIX
    import fcntl
except ImportError:                     # Windows
    fcntl = None
    import msvcrt

logger = logging.getLogger('trustpcb')

LOCK_NAME = 'trustpcb.lock'
_WIN_OFFSET = 1_000_000                 # Windows locks a byte past the end of the file, so the owner's details stay readable by others

_HELD: dict[str, 'DataLock'] = {}


class DataFolderInUse(RuntimeError):
    """Another running TrustPCB backend owns this data folder."""

    def __init__(self, folder: Path, holder: dict | None):
        self.folder, self.holder = folder, holder or {}
        who = f'process {self.holder["pid"]}, started {self.holder.get("started", "at an unknown time")}' if self.holder.get('pid') else 'another process'
        port = f' on port {self.holder["port"]}' if self.holder.get('port') else ''
        super().__init__(f'Another TrustPCB ({who}{port}) is already using the data folder {folder}. '
                         'Stop it first, or start this one with TRUSTPCB_DATA_DIR set to a different folder.')


def _key(data_dir: str | Path) -> str:
    return str(Path(data_dir).expanduser().resolve())


def holds(data_dir: str | Path | None) -> bool:
    """True when this process owns the lock of `data_dir`."""
    if data_dir is None:
        return False
    lock = _HELD.get(_key(data_dir))
    return bool(lock and lock.owned)


def read_holder(data_dir: str | Path) -> dict | None:
    """The details the owner wrote, or None when there is no readable lock file."""
    try:
        return json.loads((Path(data_dir).expanduser() / LOCK_NAME).read_text(encoding='utf-8') or 'null')
    except (OSError, ValueError):
        return None


class DataLock:
    def __init__(self, data_dir: str | Path, *, port: int | None = None):
        self.folder = Path(data_dir).expanduser().resolve()
        self.path = self.folder / LOCK_NAME
        self.port = port
        self.owned = False
        self.took_over: dict | None = None      # the stale owner's details when this start replaced a lock a crash left behind
        self._fd: int | None = None

    def acquire(self) -> 'DataLock':
        """Take the lock or raise `DataFolderInUse`. Taking it twice in one process is a no-op."""
        if self.owned:
            return self
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._lock(fd)
        except OSError:
            holder = read_holder(self.folder)
            os.close(fd)
            raise DataFolderInUse(self.folder, holder) from None
        previous = read_holder(self.folder)
        if previous and previous.get('pid') not in (None, os.getpid()):
            self.took_over = previous
            logger.warning('The previous TrustPCB on %s (process %s, started %s) is gone without releasing its lock. Taking over; its unfinished work will be marked interrupted.',
                           self.folder, previous.get('pid'), previous.get('started'))
        self._fd, self.owned = fd, True
        info = {'pid': os.getpid(), 'started': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'started_epoch': time.time(),
                'host': socket.gethostname(), 'port': self.port}
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info).encode('utf-8'))
        _HELD[_key(self.folder)] = self
        return self

    def release(self) -> None:
        if not self.owned or self._fd is None:
            return
        _HELD.pop(_key(self.folder), None)
        self.owned = False
        try:
            os.ftruncate(self._fd, 0)       # an empty file says "free"; the lock itself goes with the descriptor
            self._unlock(self._fd)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    # ---- the operating-system lock -------------------------------------------------------------------

    @staticmethod
    def _lock(fd: int) -> None:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:                               # pragma: no cover - Windows
            os.lseek(fd, _WIN_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    @staticmethod
    def _unlock(fd: int) -> None:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:                               # pragma: no cover - Windows
            os.lseek(fd, _WIN_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def __enter__(self) -> 'DataLock':
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
