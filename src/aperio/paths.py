"""Safe path handling for Aperio.

Policy (implemented, not aspirational):

1. Paths must be non-empty and must not contain NUL bytes.
2. ``~`` is expanded to the user's home directory.
3. The filesystem root (``/`` on POSIX, drive roots on Windows) is rejected.
4. For writing operations: if the target path exists it must be a directory;
   a regular file is rejected so unrelated content can never be clobbered,
   and a symlink at the target is rejected so writes can never be silently
   redirected. The same rule is enforced per write target *inside* the
   project (see ``project._ensure_file`` / ``project._ensure_dir``), not
   only at the top-level project path.
5. Ancestor components are resolved transparently. This is a documented
   trade-off: on macOS ``/tmp`` is itself a symlink, so rejecting symlinked
   ancestors would break ordinary usage. The returned path is passed through
   ``os.path.realpath`` so the location echoed back to the user is where the
   files will actually live — nothing is hidden.

``status`` is read-only, so it applies rules 1-2 only (and may read through
symlinks; it reports the resolved location).
"""

from __future__ import annotations

import os
from pathlib import Path

from aperio.errors import PathError


def resolve_target(raw: str, *, for_writing: bool) -> Path:
    """Resolve a user-supplied path against the safety policy.

    :param raw: the path exactly as typed by the user.
    :param for_writing: apply the full write-safety policy (rules 3-4);
        read-only callers pass ``False``.
    :raises PathError: when the path violates the policy.
    """
    if not isinstance(raw, str) or raw.strip() == "":
        raise PathError("project directory path must not be empty")
    if "\x00" in raw:
        raise PathError("project directory path must not contain NUL bytes")

    absolute = Path(os.path.abspath(os.path.expanduser(raw)))

    if for_writing and absolute == Path(absolute.anchor):
        raise PathError(
            f"refusing to initialize a project at the filesystem root ({absolute})"
        )

    if for_writing and absolute.exists() and os.path.islink(absolute):
        raise PathError(
            f"refusing to follow symlink at target path ({absolute} -> "
            f"{os.path.realpath(absolute)})"
        )

    if for_writing and absolute.exists() and not absolute.is_dir():
        raise PathError(f"target path exists and is not a directory: {absolute}")

    # Report the kernel-resolved location so the echoed path is where files
    # will actually be (e.g. /tmp/... -> /private/tmp/... on macOS).
    return Path(os.path.realpath(absolute))
