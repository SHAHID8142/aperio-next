"""Cross-platform, standard-library-only advisory file locking.

Used to coordinate aperio's read-modify-write operations on shared memory
artifacts (decision-log appends, handoff replacements). The mechanism is
atomic exclusive creation (``O_CREAT | O_EXCL``) of a small lock file plus
a bounded retry loop — no fcntl (POSIX-only), no third-party dependencies.

Guarantees and limitations (accurate):

- **Bounded wait:** acquisition gives up after ``timeout`` seconds with a
  clear conflict error; writes never hang indefinitely.
- **Stale locks:** a lock whose modification time is older than
  ``stale_after`` seconds is treated as abandoned (the owning process
  crashed or stalled far longer than any legitimate aperio write takes)
  and is broken by the next acquirer. Lock content records the owner
  (``pid:timestamp``) for diagnostics.
- **Ownership-checked release:** the lock file's content must match the
  acquirer's token before it is removed, so a lock that was broken and
  re-created by someone else is never deleted by the previous owner.
- **Symlinks refused:** a symlink at the lock path is never followed or
  removed (a planted link must not turn the break path into a deletion
  of arbitrary content).
- **Advisory only:** tools that do not use aperio ignore the lock entirely;
  it coordinates aperio writers with each other, nothing more.
- Lock files are reserved aperio names inside ``.aperio/`` and are
  git-ignored in generated projects; a leftover lock is either broken by
  the staleness rule or resolved by explicit user action.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from aperio.errors import PathError, StateError

LOCK_STALE_SECONDS = 10.0
LOCK_TIMEOUT_SECONDS = 10.0
_RETRY_SLEEP_SECONDS = 0.02


def acquire_lock(
    path: Path,
    *,
    timeout: float = LOCK_TIMEOUT_SECONDS,
    stale_after: float = LOCK_STALE_SECONDS,
    artifact: str = "operation",
) -> str:
    """Acquire an exclusive lock file, returning the ownership token.

    :raises PathError: when the lock path is a symlink or a directory.
    :raises StateError: when the lock cannot be acquired within the
        bounded wait, or a stale lock cannot be broken.
    """
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path}; "
            "remove it and re-run 'aperio init' to repair"
        )
    if path.exists() and not path.is_file():
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
                    f"{timeout:g}s (another writer appears active); "
                    "no content was modified — retry shortly"
                )
            now = time.time()
            try:
                age = now - path.stat().st_mtime
            except FileNotFoundError:
                continue  # released between exists and stat: retry at once
            if age > stale_after:
                # Stale: abandoned by a crashed writer. Break it — unlink
                # never follows symlinks, and symlinked locks were refused
                # above (and are re-checked by the OSError path).
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass  # someone else broke it first; retry at once
                except IsADirectoryError as exc:
                    raise PathError(
                        f"cannot break stale lock: {path} is a directory; "
                        "remove it and re-run 'aperio init' to repair"
                    ) from None
                except OSError as exc:
                    raise StateError(
                        f"cannot break stale lock at {path}: "
                        f"{exc.strerror or exc}"
                    ) from None
                continue
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
        except FileNotFoundError:
            continue  # removed instantly after our write: retry
        except OSError:
            continue
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
        return  # nothing to release (already broken/removed)
    if current != token:
        return  # ownership was lost (stale-broken and re-created): leave it
    try:
        os.unlink(path)
    except OSError:
        pass  # a leftover is handled by the staleness rule later


def _best_effort_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
