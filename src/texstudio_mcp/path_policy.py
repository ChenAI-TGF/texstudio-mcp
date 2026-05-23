"""Workspace root normalization and sandbox checks for relative paths."""

from __future__ import annotations

import os
from pathlib import Path


class PathPolicyError(ValueError):
    """Raised when a path violates workspace sandbox rules."""


def normalize_workspace_root(raw: str) -> Path:
    """Return a canonical directory Path for ``raw``.

    ``raw`` must be non-empty after stripping, refer to an existing directory.
    Uses :func:`os.path.realpath` so symlinks/junctions are honored consistently.
    """
    text = raw.strip()
    if not text:
        raise PathPolicyError("workspace_root must not be empty")
    p = Path(text).expanduser()
    try:
        resolved = Path(os.path.realpath(p))
    except OSError as exc:
        raise PathPolicyError(f"cannot resolve workspace_root: {exc}") from exc
    if not resolved.exists():
        raise PathPolicyError("workspace_root does not exist")
    if not resolved.is_dir():
        raise PathPolicyError("workspace_root is not a directory")
    return resolved


def resolve_under_workspace(workspace_root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` inside ``workspace_root``.

    ``workspace_root`` should already come from :func:`normalize_workspace_root`.
    ``relative_path`` must be relative (no drive letter / UNC root). Raises
    :class:`PathPolicyError` if the resolved real path escapes ``workspace_root``.
    """
    rel_text = relative_path.strip()
    if not rel_text:
        rel_text = "."

    rel = Path(rel_text)
    if rel.is_absolute():
        raise PathPolicyError("relative_path must not be absolute")

    root_real = Path(os.path.realpath(workspace_root))
    joined = root_real / rel
    try:
        candidate_real = Path(os.path.realpath(joined))
    except OSError as exc:
        raise PathPolicyError(f"cannot resolve path: {exc}") from exc

    try:
        candidate_real.relative_to(root_real)
    except ValueError as exc:
        raise PathPolicyError("path escapes workspace_root") from exc

    return candidate_real
