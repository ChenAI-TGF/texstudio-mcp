"""Workspace-scoped file reads, scans, and controlled writes (phase C-1/C-2)."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from texstudio_mcp.path_policy import normalize_workspace_root, resolve_under_workspace

DEFAULT_SKIP_DIR_NAMES = frozenset({
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "node_modules",
    "build",
    "dist",
})

DEFAULT_LIST_SUFFIXES = frozenset({
    ".tex",
    ".bib",
    ".sty",
    ".cls",
    ".bst",
    ".clo",
    ".def",
    ".fd",
    ".map",
})

MAX_REGEX_PATTERN_LENGTH = 512
DEFAULT_READ_MAX_CHARS = 120_000
DEFAULT_GREP_MAX_MATCHES = 200
DEFAULT_GREP_MAX_FILE_BYTES = 2_000_000
DEFAULT_LIST_MAX_DEPTH = 12
DEFAULT_GREP_EXTENSIONS = ".tex,.bib,.sty,.cls,.bst"
DEFAULT_WRITE_MAX_BYTES = 5_000_000


def _workspace_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _joined_text_lines(lines: list[str]) -> str:
    """Join logical lines as POSIX-ish UTF-8 text (LF only)."""
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"{body}\n"


def _utf8_payload_size_ok(text: str, limit: int) -> bool:
    return len(text.encode("utf-8")) <= limit


def _atomic_replace_utf8(target: Path, text: str) -> None:
    """Write UTF-8 via temp file + os.replace."""
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(parent),
            prefix=".texstudio-mcp-",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(data)
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def parse_suffix_filter(spec: str) -> frozenset[str]:
    """Turn ``'.tex,.bib'`` into ``{'.tex', '.bib'}``."""
    parts = [p.strip().lower() for p in spec.split(",") if p.strip()]
    out: list[str] = []
    for p in parts:
        if not p.startswith("."):
            p = f".{p}"
        out.append(p)
    return frozenset(out)


def read_project_file_segment(
    workspace_root: str,
    relative_path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = DEFAULT_READ_MAX_CHARS,
) -> dict[str, Any]:
    """Read UTF-8 text from a single file inside workspace_root with optional line bounds."""
    root = normalize_workspace_root(workspace_root)
    target = resolve_under_workspace(root, relative_path)

    if not target.exists():
        return {"ok": False, "error": "path does not exist"}
    if not target.is_file():
        return {"ok": False, "error": "not a regular file"}

    try:
        raw = target.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}"}

    if b"\x00" in raw[:8192]:
        return {"ok": False, "error": "file appears binary (nul byte in header)"}

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)

    if start_line is None and end_line is None:
        body_lines = lines
        slice_start = 1
        slice_end = total_lines if total_lines else 1
    else:
        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = total_lines if total_lines else 1
        if start_line < 1 or end_line < 1:
            return {"ok": False, "error": "start_line and end_line must be >= 1"}
        if end_line < start_line:
            return {"ok": False, "error": "end_line must be >= start_line"}
        slice_start = start_line
        slice_end = end_line
        body_lines = lines[start_line - 1 : end_line]

    body = "\n".join(body_lines)
    truncated = False
    truncation_reason: str | None = None
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
        truncation_reason = f"content truncated to max_chars={max_chars}"

    return {
        "ok": True,
        "relative_path": _workspace_relative(target, root),
        "total_lines": total_lines,
        "start_line": slice_start,
        "end_line": slice_end,
        "lines_returned": len(body_lines),
        "content": body,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
    }


def grep_project_files(
    workspace_root: str,
    pattern: str,
    *,
    file_extensions: str = DEFAULT_GREP_EXTENSIONS,
    ignore_case: bool = False,
    max_matches: int = DEFAULT_GREP_MAX_MATCHES,
    max_file_bytes: int = DEFAULT_GREP_MAX_FILE_BYTES,
    max_depth: int = DEFAULT_LIST_MAX_DEPTH,
) -> dict[str, Any]:
    """Regex search across small-ish text files under workspace_root."""
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        return {"ok": False, "error": f"pattern exceeds max length {MAX_REGEX_PATTERN_LENGTH}"}

    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}

    root = normalize_workspace_root(workspace_root)
    suffixes = parse_suffix_filter(file_extensions)
    matches: list[dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        rp = Path(dirpath)

        dirnames[:] = [d for d in dirnames if d not in DEFAULT_SKIP_DIR_NAMES]

        try:
            rel_parts = rp.relative_to(root).parts
        except ValueError:
            continue
        if len(rel_parts) >= max_depth:
            dirnames.clear()

        for name in filenames:
            path = rp / name
            suf = path.suffix.lower()
            if suf not in suffixes:
                continue
            try:
                rel_file = _workspace_relative(path, root)
            except ValueError:
                continue

            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                continue

            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue

            text = raw.decode("utf-8", errors="replace")
            line_no = 0
            for line in text.splitlines():
                line_no += 1
                if rx.search(line):
                    matches.append(
                        {
                            "relative_path": rel_file,
                            "line": line_no,
                            "text": line[:2000],
                        }
                    )
                    if len(matches) >= max_matches:
                        return {
                            "ok": True,
                            "pattern": pattern,
                            "ignore_case": ignore_case,
                            "truncated": True,
                            "truncation_reason": f"stopped after max_matches={max_matches}",
                            "matches": matches,
                            "match_count": len(matches),
                        }

    return {
        "ok": True,
        "pattern": pattern,
        "ignore_case": ignore_case,
        "truncated": False,
        "truncation_reason": None,
        "matches": matches,
        "match_count": len(matches),
    }


def list_latex_related_files(
    workspace_root: str,
    *,
    max_depth: int = DEFAULT_LIST_MAX_DEPTH,
    extra_extensions: str = "",
) -> dict[str, Any]:
    """List LaTeX-related source files under workspace_root."""
    root = normalize_workspace_root(workspace_root)
    suffixes = set(DEFAULT_LIST_SUFFIXES)
    if extra_extensions.strip():
        suffixes |= set(parse_suffix_filter(extra_extensions))

    paths: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        rp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_SKIP_DIR_NAMES]

        try:
            rel_parts = rp.relative_to(root).parts
        except ValueError:
            continue
        if len(rel_parts) >= max_depth:
            dirnames.clear()

        for name in filenames:
            path = rp / name
            if path.suffix.lower() not in suffixes:
                continue
            try:
                paths.append(_workspace_relative(path, root))
            except ValueError:
                continue

    paths.sort()
    return {"ok": True, "files": paths, "count": len(paths)}


def replace_lines_in_project_file(
    workspace_root: str,
    relative_path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    *,
    max_file_bytes_before: int = DEFAULT_WRITE_MAX_BYTES,
    max_file_bytes_after: int = DEFAULT_WRITE_MAX_BYTES,
) -> dict[str, Any]:
    """Replace inclusive 1-based line range ``start_line..end_line`` with ``new_content`` lines."""
    if "\x00" in new_content:
        return {"ok": False, "error": "new_content must not contain NUL bytes"}

    if start_line < 1 or end_line < 1:
        return {"ok": False, "error": "start_line and end_line must be >= 1"}
    if end_line < start_line:
        return {"ok": False, "error": "end_line must be >= start_line"}

    root = normalize_workspace_root(workspace_root)
    target = resolve_under_workspace(root, relative_path)

    if not target.exists():
        return {"ok": False, "error": "path does not exist"}
    if not target.is_file():
        return {"ok": False, "error": "not a regular file"}

    try:
        raw = target.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}"}

    if len(raw) > max_file_bytes_before:
        return {"ok": False, "error": f"file exceeds max_file_bytes_before={max_file_bytes_before}"}

    if b"\x00" in raw[:8192]:
        return {"ok": False, "error": "file appears binary (nul byte in header)"}

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_before = len(lines)
    new_lines = new_content.splitlines()

    if total_before == 0:
        if start_line != 1 or end_line != 1:
            return {
                "ok": False,
                "error": "empty file: use write_project_file or replace lines (1, 1) only",
            }
        merged = new_lines
    else:
        if start_line > total_before:
            return {"ok": False, "error": "start_line is past end of file"}
        if end_line > total_before:
            return {"ok": False, "error": "end_line is past end of file"}

        idx_lo = start_line - 1
        idx_hi = end_line
        merged = lines[:idx_lo] + new_lines + lines[idx_hi:]

    new_text = _joined_text_lines(merged)

    if not _utf8_payload_size_ok(new_text, max_file_bytes_after):
        return {"ok": False, "error": f"result exceeds max_file_bytes_after={max_file_bytes_after}"}

    try:
        _atomic_replace_utf8(target, new_text)
    except OSError as exc:
        return {"ok": False, "error": f"cannot write file: {exc}"}

    return {
        "ok": True,
        "relative_path": _workspace_relative(target, root),
        "lines_before": total_before,
        "lines_after": len(merged),
        "replaced_line_span": {"start_line": start_line, "end_line": end_line},
        "replacement_lines": len(new_lines),
    }


def write_project_text_file(
    workspace_root: str,
    relative_path: str,
    content: str,
    *,
    overwrite: bool = False,
    max_bytes: int = DEFAULT_WRITE_MAX_BYTES,
) -> dict[str, Any]:
    """Create or overwrite a UTF-8 text file under workspace_root (parents created when missing)."""
    if "\x00" in content:
        return {"ok": False, "error": "content must not contain NUL bytes"}

    root = normalize_workspace_root(workspace_root)
    target = resolve_under_workspace(root, relative_path)

    if target.exists() and target.is_dir():
        return {"ok": False, "error": "path exists and is a directory"}

    existed_before = target.is_file()

    if target.exists() and not overwrite:
        return {"ok": False, "error": "file exists (set overwrite=true to replace)"}

    if existed_before and overwrite:
        try:
            with target.open("rb") as fh:
                raw_head = fh.read(8192)
        except OSError as exc:
            return {"ok": False, "error": f"cannot read existing file: {exc}"}
        if b"\x00" in raw_head:
            return {"ok": False, "error": "existing file appears binary"}

    text = _joined_text_lines(content.splitlines())
    if not _utf8_payload_size_ok(text, max_bytes):
        return {"ok": False, "error": f"content exceeds max_bytes={max_bytes}"}

    try:
        _atomic_replace_utf8(target, text)
    except OSError as exc:
        return {"ok": False, "error": f"cannot write file: {exc}"}

    mode = "updated" if existed_before else "created"
    return {
        "ok": True,
        "relative_path": _workspace_relative(target, root),
        "bytes_written": len(text.encode("utf-8")),
        "mode": mode,
    }
