"""User-facing error types for Aperio.

Every error raised intentionally by Aperio derives from :class:`AperioError`.
The CLI catches that base class, prints a single clean message to stderr,
and exits with the error's exit code.
"""

from __future__ import annotations


class AperioError(Exception):
    """Base class for all user-facing Aperio errors."""

    exit_code: int = 1


class PathError(AperioError):
    """A target path is unsafe or unusable (symlink, file, root, ...)."""


class NotInitializedError(AperioError):
    """The target directory is not an initialized Aperio project."""


class StateError(AperioError):
    """Project state is unreadable, corrupt, or from an unsupported schema."""
