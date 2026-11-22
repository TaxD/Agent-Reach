"""Cross-process file locks under ~/.agent-reach/locks."""

import hashlib
import sys
from contextlib import contextmanager

from .result import ReachError


@contextmanager
def execution_lock(name: str):
    """Nonblocking cross-process lock on `name` (one OS file lock); BUSY when another process holds it."""
    from agent_reach.utils.paths import ensure_no_symlink_path, make_private_dir

    from . import state

    folder = state.root() / "locks"
    make_private_dir(folder)
    path = folder / (hashlib.sha256(name.encode()).hexdigest() + ".lock")
    ensure_no_symlink_path(path, "lock file")
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ReachError("BUSY", "Another Agent Reach command holds this lock") from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
