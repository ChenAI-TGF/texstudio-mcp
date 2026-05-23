"""Extract plain-text previews from sandbox PDFs via Poppler ``pdftotext``."""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace

DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_CHARS = 32_000
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 45.0
_HARD_PAGE_CAP = 50
_HARD_CHAR_CAP = 600_000
_HARD_CHAR_MIN = 32

_SUGGESTION_TRUNCATED_EN = (
    "Increase max_chars (and/or max_pages if you only need earlier pages covered) "
    "to retrieve more pdftotext output. Expect punctuation/spacing/quotes to differ from the "
    ".tex source; layout_preserving=true approximates PDF column layout better but stays lossy."
)
_SUGGESTION_TRUNCATED_ZH = (
    "可适当调高 max_chars（若只关心更靠前的页面，可同时或单独调高 max_pages）"
    "以获取更多 pdftotext 输出。PDF 抽出文本与 .tex 源码在标点、空格、引号等方面不必一致；"
    "layout_preserving=true 能多保留一些分栏占位，但依然是有损近似。"
)


def _normalize_suggestion_locale(fragment: str) -> tuple[str | None, str | None]:
    """Return ``(effective, error)``. ``effective`` is ``en`` or ``zh``."""
    raw = fragment.strip().lower().replace("_", "-")
    if raw in ("", "en"):
        return "en", None
    if raw in ("zh", "zh-cn", "zh-hans"):
        return "zh", None
    return (
        None,
        f"invalid suggestion_locale {fragment!r}; use en (English) or zh (Simplified Chinese)",
    )


def _workspace_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _pdf_magic_ok(head: bytes) -> bool:
    return head.startswith(b"%PDF")


def _clamp_pages(raw: int) -> int:
    return max(1, min(int(raw), _HARD_PAGE_CAP))


def _clamp_chars(raw: int) -> int:
    return max(_HARD_CHAR_MIN, min(int(raw), _HARD_CHAR_CAP))


def extract_pdf_text_preview(
    workspace_root: str,
    relative_pdf_path: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    layout_preserving: bool = False,
    suggestion_locale: str = "en",
) -> dict[str, Any]:
    """Run ``pdftotext`` on the first ``max_pages`` pages of a sandboxed PDF (stdout capture)."""
    warnings: list[str] = []

    mp = _clamp_pages(max_pages)
    mc = _clamp_chars(max_chars)
    sug_loc, sug_err = _normalize_suggestion_locale(suggestion_locale)
    if sug_err:
        return {"ok": False, "error": sug_err}

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
        warnings.append("relative_pdf_path does not end with .pdf; pdftotext is invoked anyway")

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
        warnings.append("file header does not start with %PDF (may still be readable by pdftotext)")

    exe = shutil.which("pdftotext")
    if not exe:
        return {
            "ok": False,
            "error": "pdftotext not found on PATH",
            "hint": "Install Poppler utilities and ensure pdftotext is on PATH.",
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }

    abs_pdf = os.path.realpath(str(pdf_path))
    cmd: list[str] = [exe]
    if layout_preserving:
        cmd.append("-layout")
    cmd.extend(["-l", str(mp), abs_pdf, "-"])

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
            "error": f"pdftotext timed out after {timeout_seconds}s",
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": f"cannot execute pdftotext: {exc}",
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }

    stderr_txt = (proc.stderr or "").strip()
    stdout_txt = proc.stdout or ""

    if proc.returncode != 0:
        tail = stderr_txt[-4000:] if len(stderr_txt) > 4000 else stderr_txt
        return {
            "ok": False,
            "error": f"pdftotext exited with code {proc.returncode}",
            "stderr_tail": tail,
            "relative_pdf_path": source_rel,
            "pdf_bytes": pdf_bytes,
        }

    chars_full = len(stdout_txt)
    truncated = chars_full > mc
    clipped = stdout_txt if not truncated else stdout_txt[:mc]

    compact = "".join(clipped.split())
    density_floor = max(120, mp * 25)
    low_text_density = len(compact) < density_floor

    out: dict[str, Any] = {
        "ok": True,
        "relative_pdf_path": source_rel,
        "resolved_absolute_path": abs_pdf,
        "pdf_bytes": pdf_bytes,
        "pdf_mtime_utc": mtime_iso,
        "pdftotext_path": exe,
        "max_pages_requested": max_pages,
        "max_pages_effective": mp,
        "max_chars_requested": max_chars,
        "max_chars_effective": mc,
        "text": clipped,
        "chars_full": chars_full,
        "chars_returned": len(clipped),
        "truncated": truncated,
        "low_text_density": low_text_density,
        "layout_preserving": layout_preserving,
    }

    if truncated:
        out["truncation_reason"] = "max_chars"
        out["suggestion"] = _SUGGESTION_TRUNCATED_ZH if sug_loc == "zh" else _SUGGESTION_TRUNCATED_EN

    if warnings:
        out["warnings"] = warnings

    return out
