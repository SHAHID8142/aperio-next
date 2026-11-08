"""Safe filesystem helpers for run artifacts (Feature 04).

Run artifacts are the most attacker-influenced outputs aperio writes: a
record's ``handoff_ref``/``local_log_ref`` is data read back from disk, and
``run-report`` carries free-form text from an external agent. This module
implements one policy for all of them:

1. **Whole parent chain.** Every component from ``.aperio/`` down to the
   leaf directory is checked: a symlink at any level (live *or* dangling)
   is refused, and a non-directory occupant is refused, with an actionable
   error. Creating a directory never walks through a link.
2. **Exclusive leaf creation.** Leaf artifacts are created with
   ``O_CREAT|O_EXCL`` (plus ``O_NOFOLLOW`` where the platform has it):
   aperio never truncates or appends to a file it did not just create. An
   occupied path is reported, preserved byte-for-byte, and left to
   explicit user action.
3. **Confined reference reads.** A path stored in a record is resolved
   only inside the permitted artifact directory, must be relative, must
   have exactly the expected ``<dir>/<name><suffix>`` shape, and the
   resolved parent must be that directory — traversal, absolute paths,
   malformed references, and links to outside files are rejected.

The same exclusive-creation policy is reused for operator-visible output
files (``publish_text_atomic``), so a command that writes a shareable
artifact cannot follow a planted temporary path, truncate an existing file
without an explicit overwrite, or lose a race with a concurrent writer.

Residual hazard (documented, not hidden): without ``openat``-style
traversal, check-then-create is not atomic against a hostile process
running as the same user. The exclusive-create flag closes the practical
race on the leaf and the post-creation re-check closes the ordinary
symlink-swap window; a process that can rewrite ``.aperio/`` mid-write can
still win a race, exactly as it can with any tool that trusts its own
workspace.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path, PurePosixPath

from aperio.errors import PathError, StateError

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW


def _errno_set(*names: str) -> frozenset[int]:
    """Codes present on this platform only (errno constants are not portable)."""
    return frozenset(
        getattr(errno, name) for name in names if hasattr(errno, name)
    )


#: "This filesystem/platform cannot hard-link" — atomic no-clobber is simply
#: unavailable, so publication must fail closed rather than degrade.
_LINK_UNSUPPORTED_ERRNOS = _errno_set(
    "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EXDEV", "EMLINK"
)
#: The link was refused by permissions/ownership rules.
_LINK_PERMISSION_ERRNOS = _errno_set("EPERM", "EACCES", "EROFS")


def check_artifact_dir(
    aperio_dir: Path, *parts: str, create: bool = True
) -> Path:
    """Return ``aperio_dir/parts...`` after validating the whole chain.

    Every component from ``aperio_dir`` down is proven to be a real
    directory (not a symlink, not a non-directory occupant). Missing
    components are created one level at a time with ``mkdir`` (never
    ``parents=True``, which would follow links) and re-checked afterwards.

    :raises PathError: a symlink or non-directory occupant anywhere in the
        chain, or an unsafe post-creation state.
    :raises StateError: the directory cannot be created.
    """
    if os.path.islink(aperio_dir):
        raise PathError(
            f"refusing to follow symlink at {aperio_dir}; the project "
            "context directory itself is a symlink, so run artifacts could "
            "live outside the project boundary; remove it and re-run "
            "'aperio init' to repair"
        )
    current = aperio_dir
    for part in parts:
        current = current / part
        if os.path.islink(current):
            raise PathError(
                f"refusing to follow symlink at {current}: run artifacts "
                "must stay inside the project (a link could redirect "
                "writes outside it); remove it and re-run the command to "
                "repair"
            )
        if current.exists():
            if not current.is_dir():
                raise PathError(
                    f"cannot use {current}: a non-directory occupant exists "
                    "there; remove it and re-run the command to repair"
                )
            continue
        if not create:
            raise StateError(
                f"no artifact directory at {current}; the referenced run "
                "artifact does not exist"
            )
        try:
            current.mkdir()
        except FileExistsError:
            pass  # a concurrent creator won; the post-check decides
        except OSError as exc:
            raise StateError(
                f"cannot create artifact directory {current}: "
                f"{exc.strerror or exc}; nothing was written"
            ) from None
        if os.path.islink(current) or not current.is_dir():
            raise PathError(
                f"{current} is not a safe directory after creation (a "
                "symlink or non-directory occupant appeared); nothing was "
                "written"
            )
    return current


def _reject_unsafe_leaf(path: Path, what: str) -> None:
    if os.path.islink(path):
        raise PathError(
            f"refusing to follow symlink at {path} while creating {what}; "
            "remove it and re-run the command to repair"
        )
    if path.exists() and not path.is_file():
        kind = "a directory" if path.is_dir() else "a special file"
        raise PathError(
            f"cannot create {what} at {path}: {kind} exists there; remove "
            "it and re-run the command to repair"
        )


def _open_exclusive(path: Path, what: str):
    _reject_unsafe_leaf(path, what)
    try:
        fd = os.open(path, _CREATE_FLAGS, 0o666)
    except FileExistsError:
        raise StateError(
            f"cannot create {what} at {path}: the path is already occupied "
            "(a concurrent writer, or a leftover from an earlier run). The "
            "existing file was left untouched; remove it manually if it is "
            "not needed."
        ) from None
    except OSError as exc:
        raise StateError(
            f"cannot create {what} at {path}: {exc.strerror or exc}; "
            "nothing was written"
        ) from None
    return fd


def open_exclusive_binary(path: Path, *, what: str = "artifact"):
    """Open a brand-new binary file for writing; never truncates."""
    fd = _open_exclusive(path, what)
    try:
        return os.fdopen(fd, "wb")
    except OSError as exc:  # pragma: no cover - fdopen rarely fails
        _unlink_if_created(path, fd)
        try:
            os.close(fd)
        except OSError:
            pass
        raise StateError(
            f"cannot open {what} at {path}: {exc.strerror or exc}"
        ) from None


def create_exclusive_text(path: Path, text: str, *, what: str = "artifact") -> None:
    """Create ``path`` exclusively with ``text``; never truncate or append."""
    fd = _open_exclusive(path, what)
    created = None
    try:
        created = os.fstat(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        if created is None:
            try:
                os.close(fd)
            except OSError:
                pass
        else:
            _unlink_if_same(path, created)
        raise StateError(
            f"cannot write {what} at {path}: {exc.strerror or exc}; the "
            "partial file created by this command was removed"
        ) from None


def publish_text_atomic(
    path: Path, text: str, *, force: bool = False, what: str = "output file"
) -> None:
    """Publish ``text`` at ``path`` without unsafe overwrites.

    Policy (POSIX-first, with a documented fallback):

    1. The destination leaf is rejected if it is a symlink or a non-regular
       file, *before* anything is created (same rule as run artifacts).
    2. The content is written to a uniquely named temporary file in the
       destination directory, created with :func:`tempfile.mkstemp`
       (``O_CREAT|O_EXCL``): a planted symlink or an existing leftover at any
       guessed temporary name is never opened, followed, or truncated, and
       concurrent commands cannot collide on the same name.
    3. Without ``force`` the file is published with ``os.link``, which fails
       atomically if the destination exists — so a destination created
       *after* any earlier check is still never overwritten. Where hard links
       are unavailable, the fallback re-checks existence immediately before
       ``os.replace`` and refuses; that window is not atomic and is
       documented rather than claimed.
    4. With ``force`` the file is published with ``os.replace`` (atomic
       replacement on POSIX; on Windows it fails cleanly if the destination
       is open in another process).
    5. Cleanup removes only the temporary file this call created (inode
       re-check), so a file another writer swapped in is never deleted.

    The published file is owner-readable/writable only (``0600``, as created
    by ``mkstemp``): a pack can contain operator material, so it is not
    world-readable by default.
    """
    _reject_unsafe_leaf(path, what)
    if path.exists() and not force:
        raise StateError(
            f"cannot write {what} at {path}: the destination already exists; "
            "pass --force to overwrite it"
        )
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".aperio-tmp", dir=str(path.parent)
        )
    except OSError as exc:
        raise StateError(
            f"cannot write {what} at {path}: {exc.strerror or exc}; nothing "
            "was written"
        ) from None
    temp = Path(temp_name)
    created = None
    published = False
    try:
        created = os.fstat(fd)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _publish_temp(temp, path, force=force, what=what)
        published = True
    except OSError as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if created is not None:
            _unlink_if_same(temp, created)
        raise StateError(
            f"cannot write {what} at {path}: {exc.strerror or exc}"
            + ("" if published else "; the destination was not written")
        ) from None
    finally:
        if not published and created is not None:
            _unlink_if_same(temp, created)


def _publish_temp(temp: Path, path: Path, *, force: bool, what: str) -> None:
    """Publish ``temp`` at ``path``.

    ``force`` uses ``os.replace`` (an explicit, atomic overwrite). Without
    ``force`` the only accepted mechanism is ``os.link``, which fails
    atomically when the destination exists. If hard links are unavailable or
    refused, publication **fails closed**: there is deliberately no
    check-then-replace fallback, because that would let a concurrent creator
    lose its file to us. ``--force`` remains the explicit way to overwrite.
    """
    if force:
        os.replace(temp, path)
        return
    try:
        os.link(temp, path)
    except FileExistsError:
        raise StateError(
            f"cannot write {what} at {path}: the destination already exists "
            "(it was created after this command started); pass --force to "
            "overwrite it"
        ) from None
    except NotImplementedError:
        raise StateError(_no_overwrite_unavailable(what, path)) from None
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise StateError(
                f"cannot write {what} at {path}: the destination already "
                "exists (it was created after this command started); pass "
                "--force to overwrite it"
            ) from None
        if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
            raise StateError(_no_overwrite_unavailable(what, path)) from None
        if exc.errno in _LINK_PERMISSION_ERRNOS:
            raise StateError(
                f"cannot write {what} at {path}: no-overwrite publication is "
                "not permitted here "
                f"({exc.strerror or exc}); nothing was written. Choose a "
                "writable directory, or pass --force to overwrite explicitly"
            ) from None
        raise StateError(
            f"cannot write {what} at {path}: {exc.strerror or exc}; nothing "
            "was written. Aperio does not fall back to a non-atomic "
            "publication, so an existing file is never replaced here; "
            "pass --force to overwrite explicitly"
        ) from None
    try:
        temp.unlink()
    except OSError:  # pragma: no cover - published copy already exists
        pass


def _no_overwrite_unavailable(what: str, path: Path) -> str:
    return (
        f"cannot write {what} at {path}: this filesystem does not support "
        "no-overwrite publication (hard links are unavailable or refused "
        f"with {os.name!r} semantics), and Aperio will not risk overwriting a "
        "concurrently created file. Nothing was written. Choose a directory "
        "on a filesystem with hard-link support, or pass --force to overwrite "
        "explicitly"
    )


def discard_created_file(path: Path, handle) -> None:
    """Close ``handle`` and remove the file only if it is our creation.

    Used to clean up an artifact when the operation that needed it never
    happened (e.g. the child process could not be started at all). The
    inode check guarantees we never delete a file another writer swapped in.
    """
    try:
        created = os.fstat(handle.fileno())
    except OSError:
        created = None
    try:
        handle.close()
    except OSError:
        pass
    if created is not None:
        _unlink_if_same(path, created)


def _unlink_if_created(path: Path, fd: int) -> None:
    try:
        created = os.fstat(fd)
    except OSError:
        return
    _unlink_if_same(path, created)


def _unlink_if_same(path: Path, created) -> None:
    try:
        current = os.lstat(path)
    except OSError:
        return
    if (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
        return  # replaced by another writer: never touch it
    try:
        os.unlink(path)
    except OSError:
        pass  # a leftover is reported as a conflict next time


def read_confined_artifact(
    artifact_dir: Path,
    ref: object,
    *,
    dir_name: str,
    suffix: str,
    what: str = "artifact",
) -> str:
    """Read a record-stored reference confined to one artifact directory.

    :param artifact_dir: the permitted directory (e.g. ``.aperio/runs``).
    :param ref: the reference value read from the record (untrusted).
    :param dir_name: the exact first path component the reference must use.
    :param suffix: the exact filename suffix the leaf must have.
    :raises PathError: traversal, absolute paths, malformed references,
        symlinked directories/leaf, or a non-regular-file occupant.
    :raises StateError: the referenced artifact does not exist.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise StateError(
            f"the run records an empty {what} reference; expected "
            f"'{dir_name}/<name>{suffix}'"
        )
    if "\x00" in ref:
        raise PathError(
            f"refusing malformed {what} reference (NUL byte); expected "
            f"'{dir_name}/<name>{suffix}'"
        )
    if "\\" in ref:
        raise PathError(
            f"refusing malformed {what} reference {ref!r}: backslashes are "
            f"not accepted; expected '{dir_name}/<name>{suffix}'"
        )
    relative = PurePosixPath(ref)
    if relative.is_absolute():
        raise PathError(
            f"refusing absolute {what} reference {ref!r}: references must "
            f"be project-relative ('{dir_name}/<name>{suffix}')"
        )
    parts = relative.parts
    if (
        len(parts) != 2
        or parts[0] != dir_name
        or parts[1] in ("", ".", "..")
        or not parts[1].endswith(suffix)
        or parts[1] == suffix
    ):
        raise PathError(
            f"refusing malformed {what} reference {ref!r}: expected exactly "
            f"'{dir_name}/<name>{suffix}' inside the project"
        )
    if ".." in parts or "." in parts:
        raise PathError(
            f"refusing traversal in {what} reference {ref!r}; references "
            f"must resolve inside '{dir_name}/'"
        )
    if str(relative) != ref:
        raise PathError(
            f"refusing non-canonical {what} reference {ref!r}; use the "
            f"exact form '{dir_name}/<name>{suffix}' with a single '/'"
        )

    check_artifact_dir(artifact_dir.parent, create=False)
    if os.path.islink(artifact_dir):
        raise PathError(
            f"refusing to follow symlink at {artifact_dir} while reading "
            f"{what}; remove it and re-run the command to repair"
        )
    if not artifact_dir.is_dir():
        raise StateError(
            f"no artifact directory at {artifact_dir}; the referenced "
            f"{what} does not exist"
        )
    leaf = artifact_dir / parts[1]
    if os.path.islink(leaf):
        raise PathError(
            f"refusing to follow symlink at {leaf} while reading {what}; "
            "remove it and re-run the command to repair"
        )
    if not leaf.exists():
        raise StateError(
            f"the referenced {what} does not exist at {leaf}; the run "
            "record is stale — re-prepare the run before reading it"
        )
    if not leaf.is_file():
        raise PathError(
            f"cannot read {what} at {leaf}: not a regular file; remove it "
            "and re-run the command to repair"
        )
    root = artifact_dir.resolve()
    if leaf.resolve().parent != root:
        raise PathError(
            f"refusing {what} reference {ref!r}: it resolves outside "
            f"{artifact_dir}"
        )
    try:
        fd = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW)
    except FileNotFoundError:
        raise StateError(
            f"the referenced {what} disappeared from {leaf} while being "
            "read; re-prepare the run and retry"
        ) from None
    except OSError as exc:
        raise StateError(
            f"cannot read {what} at {leaf}: {exc.strerror or exc}"
        ) from None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError:
        raise StateError(
            f"the referenced {what} at {leaf} is not valid UTF-8; aperio "
            "will not guess at its content"
        ) from None
    except OSError as exc:
        raise StateError(
            f"cannot read {what} at {leaf}: {exc.strerror or exc}"
        ) from None
