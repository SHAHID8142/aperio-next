"""Cross-platform, standard-library-only advisory file locking.

Used to coordinate aperio's read-modify-write operations on shared memory
artifacts (state updates, decision-log appends, handoff replacements). The
mechanism is atomic exclusive creation (``O_CREAT | O_EXCL``) of a small
lock file plus a bounded retry loop — no fcntl (POSIX-only), no
third-party dependencies.

Guarantees and limitations (accurate):

- **Bounded wait:** acquisition gives up after ``timeout`` seconds with an
  actionable conflict error; writes never hang indefinitely.
- **A lock is never stolen.** There is no age-based automatic recovery: a
  lock held by an active (or merely slow) process is never removed by
  another aperio process, so mutual exclusion cannot be destroyed behind a
  writer's back. If acquisition times out, the error points at the lock
  file (which records its owner as ``pid:timestamp``) and leaves the
  decision to remove a possibly-abandoned lock to the user — a conservative
  policy that prefers a clear conflict over unsafe automatic recovery.
- **Ownership-checked release:** the lock file's content must match the
  acquirer's token before it is removed, so one process can never release
  a different process's lock.
- **Symlinks refused:** a symlink at the lock path is never followed or
  removed.
- **Advisory only:** tools that do not use aperio ignore the lock entirely;
  it coordinates aperio writers with each other, nothing more.
- Lock files are reserved aperio names inside ``.aperio/`` and are
  git-ignored in generated projects; an abandoned lock is resolved by
  explicit user action (the conflict error says exactly that).
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from aperio.errors import PathError, StateError

LOCK_TIMEOUT_SECONDS = 30.0
_RETRY_SLEEP_SECONDS = 0.02


def acquire_lock(
    path: Path,
    *,
    timeout: float = LOCK_TIMEOUT_SECONDS,
    artifact: str = "operation",
) -> str:
    """Acquire an exclusive lock file, returning the ownership token.

    Never steals an existing lock: if the path is occupied for the whole
    bounded wait, acquisition fails with an actionable error that names
    the lock file and how to resolve an abandoned lock manually.

    :raises PathError: when the lock path is a symlink or a directory.
    :raises StateError: when the lock cannot be acquired within the
        bounded wait, or the lock file cannot be created/written.
    """
    # Classify the lock path with a SINGLE lstat. Two separate checks
    # (exists() then is_file()) race against a concurrent release: another
    # writer can unlink the lock in between, making is_file() return False
    # for a path that no longer exists and turning normal contention into a
    # spurious "non-file occupant" failure. A vanished path is simply
    # unoccupied.
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise StateError(
            f"cannot inspect the {artifact} lock at {path}: "
            f"{exc.strerror or exc}"
        ) from None
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise PathError(
                f"refusing to follow symlink at {path}; "
                "remove it and re-run 'aperio init' to repair"
            )
        if not stat.S_ISREG(info.st_mode):
            raise PathError(
                f"cannot lock {artifact}: a non-file occupant exists at {path}; "
                "remove it and re-run 'aperio init' to repair"
            )

    token = f"{os.getpid()}:{time.time_ns()}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            # Deadline is checked on every occupied-path iteration so no
            # retry path can spin past the bounded wait.
            if time.monotonic() >= deadline:
                raise StateError(
                    f"could not acquire the {artifact} lock at {path} within "
                    f"{timeout:g}s. The lock is held by another writer (or "
                    "was abandoned by a crashed one): the file records its "
                    "owner as 'pid:timestamp' — inspect it, and remove it "
                    "manually only if that process no longer exists. "
                    "No content was modified."
                )
            time.sleep(_RETRY_SLEEP_SECONDS)
            continue
        except OSError as exc:
            raise StateError(
                f"cannot create lock file {path}: {exc.strerror or exc}"
            ) from None

        # Acquired: record ownership for diagnostics and release checks.
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
        except OSError:
            _best_effort_unlink(path)
            raise StateError(
                f"cannot write lock file {path}; no content was modified"
            ) from None
        try:
            current = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue  # removed instantly after our write: retry
        if current != token:
            # Our brand-new lock was replaced before we could claim it;
            # do not fight — retry within the bounded wait.
            continue
        return token


def release_lock(path: Path, token: str) -> None:
    """Release the lock only if we still own it (content == token)."""
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return  # nothing to release (already removed)
    if current != token:
        return  # ownership was lost (removed and re-created): leave it
    try:
        os.unlink(path)
    except OSError:
        pass  # leftover: resolved by explicit user action


def _best_effort_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
