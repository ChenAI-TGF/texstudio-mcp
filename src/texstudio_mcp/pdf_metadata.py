"""Read PDF metadata via Poppler ``pdfinfo`` for workspace-scoped files."""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace

DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_FIELD_CHARS = 4096

_SAFE_META_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9 _\-]*$")


def _workspace_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _pdf_magic_ok(head: bytes) -> bool:
    return head.startswith(b"%PDF")


def _parse_pdfinfo_stdout(stdout: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip("\r\n")
        if ":" not in line:
            continue
        key_part, _, rest = line.partition(":")
        key_raw = key_part.strip()
        if not key_raw or not _SAFE_META_KEY.match(key_raw):
            continue
        val = rest.strip()
        if len(val) > _MAX_FIELD_CHARS:
            val = val[: _MAX_FIELD_CHARS - 3] + "..."
        norm_key = key_raw.lower().replace(" ", "_")
        if norm_key not in metadata:
            metadata[norm_key] = val
    return metadata


def read_pdf_metadata(
    workspace_root: str,
    relative_pdf_path: str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``pdfinfo`` on a PDF inside workspace_root and return parsed tag/value pairs."""
    warnings: list[str] = []

    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        pdf_path = resolve_under_workspace(root, relative_pdf_path)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if not pdf_path.exists():
        return {"ok": False, "error": "path does not exist"}
    if not pdf_path.is_file():
        return {"ok": False, "error": "not a regular file"}

    source_rel = _workspace_relative(pdf_path, root).replace("\\", "/")
    low = source_rel.lower()
    if not low.endswith(".pdf"):
        warnings.append("relative_pdf_path does not end with .pdf; pdfinfo is invoked anyway")

    try:
        st = pdf_path.stat()
        pdf_bytes = st.st_size
        mtime_iso = _dt.datetime.fromtimestamp(st.st_mtime, tz=_dt.timezone.utc).isoformat()
    except OSError as exc:
        return {"ok": False, "error": f"cannot stat file: {exc}"}

    if pdf_bytes > max_file_bytes:
        return {
            "ok": False,
            "error": f"file exceeds max_file_bytes ({pdf_bytes} > {max_file_bytes})",
            "pdf_bytes": pdf_bytes,
        }

    try:
        header = pdf_path.read_bytes()[:16]
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file header: {exc}"}

    if not _pdf_magic_ok(header):
        warnings.append("file header does not start with %PDF (may still be readable by pdfinfo)")

    pdfinfo_exe = shutil.which("pdfinfo")
    if not pdfinfo_exe:
        return {
            "ok": False,
            "error": "pdfinfo not found on PATH",
            "hint": "Install Poppler utilities and ensure pdfinfo is on PATH (e.g. TeX Live/MiKTeX often ship Poppler pdfinfo separately).",
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }

    abs_pdf = os.path.realpath(str(pdf_path))
    cmd = [pdfinfo_exe, abs_pdf]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=float(timeout_seconds),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"pdfinfo timed out after {timeout_seconds}s",
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": f"cannot execute pdfinfo: {exc}",
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }

    stderr_txt = (proc.stderr or "").strip()
    stdout_txt = proc.stdout or ""

    if proc.returncode != 0:
        tail = stderr_txt[-4000:] if len(stderr_txt) > 4000 else stderr_txt
        return {
            "ok": False,
            "error": f"pdfinfo exited with code {proc.returncode}",
            "stderr_tail": tail,
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }

    metadata = _parse_pdfinfo_stdout(stdout_txt)

    out: dict[str, Any] = {
        "ok": True,
        "relative_pdf_path": source_rel,
        "resolved_absolute_path": abs_pdf,
        "pdf_bytes": pdf_bytes,
        "pdf_mtime_utc": mtime_iso,
        "pdfinfo_path": pdfinfo_exe,
        "metadata": metadata,
        "pages": None,
        "pdf_version": None,
    }

    pages_raw = metadata.get("pages")
    if pages_raw is not None:
        try:
            out["pages"] = int(str(pages_raw).strip())
        except ValueError:
            out["pages"] = None

    ver_raw = metadata.get("pdf_version")
    if ver_raw:
        m = re.search(r"[\d.]+", ver_raw)
        if m:
            try:
                out["pdf_version"] = float(m.group(0))
            except ValueError:
                out["pdf_version"] = ver_raw.strip()

    if warnings:
        out["warnings"] = warnings

    return out
