"""Heuristic parsing of BibTeX / biber ``.blg`` bibliography logs under workspace_root."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace

DEFAULT_MAX_ISSUES = 120
DEFAULT_TAIL_CHARS = 8000
DEFAULT_MAX_FILE_BYTES = 2_000_000

_RX_BIBTEX_HEADER = re.compile(r"This\s+is\s+BibTeX", re.I)
_RX_BIBER_HEADER = re.compile(r"This\s+is\s+Biber\b|INFO\s+-\s+This\s+is\s+Biber", re.I)
_RX_BIBER_LINE = re.compile(r"^(WARN|ERROR|FATAL)\s+-\s+(.*)$", re.I)
_RX_BIBTEX_WARNING = re.compile(r"^Warning--(.*)$", re.I)
_RX_BIBTEX_DB_OPEN = re.compile(
    r"^I\s+couldn't\s+open\s+(?:database\s+file|style\s+file)\s*(.*)$",
    re.I,
)
_RX_BIBTEX_REPEATED = re.compile(r"^Repeated\s+entry", re.I)
_RX_BIBTEX_ILLEGAL = re.compile(r"^Illegal,", re.I)
_RX_BIBTEX_SUMMARY = re.compile(r"^\(There were\b", re.I)


def _workspace_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def guess_bibliography_backend(text: str) -> str:
    """Best-effort BibTeX vs biber discriminator based on header / idiomatic lines."""
    head = text[:24_000]
    has_bt = bool(_RX_BIBTEX_HEADER.search(head))
    has_br = bool(_RX_BIBER_HEADER.search(head))
    if has_bt and has_br:
        return "ambiguous"
    if has_bt:
        return "bibtex"
    if has_br:
        return "biber"
    if "WARN -" in head or "ERROR -" in head or "FATAL -" in head:
        return "biber_likely"
    if "Warning--" in head:
        return "bibtex_likely"
    return "unknown"


def _append_issue(
    issues: list[dict[str, Any]],
    *,
    max_issues: int,
    lineno: int,
    severity: str,
    code: str,
    message: str,
    truncated_flag: list[bool],
) -> None:
    if len(issues) >= max_issues:
        truncated_flag[0] = True
        return
    msg = message.strip()
    if len(msg) > 4_096:
        msg = msg[:4093] + "..."
    issues.append(
        {
            "severity": severity,
            "code": code,
            "line": lineno,
            "message": msg,
        }
    )


def analyze_bibliography_log(
    workspace_root: str,
    relative_blg_path: str,
    *,
    max_issues: int = DEFAULT_MAX_ISSUES,
    tail_max_chars: int = DEFAULT_TAIL_CHARS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Read a ``.blg`` inside workspace_root and extract heuristic BibTeX / biber diagnostics."""
    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        blg_file = resolve_under_workspace(root, relative_blg_path)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if not blg_file.exists():
        return {"ok": False, "error": "path does not exist"}
    if not blg_file.is_file():
        return {"ok": False, "error": "not a regular file"}

    source_rel = _workspace_relative(blg_file, root).replace("\\", "/")
    low = source_rel.lower()
    warnings_banner: list[str] = []
    if not low.endswith(".blg"):
        warnings_banner.append("relative_blg_path does not end with .blg; parsing proceeds anyway")

    try:
        raw = blg_file.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}"}

    if len(raw) > max_file_bytes:
        return {
            "ok": False,
            "error": f"file exceeds max_file_bytes ({len(raw)} > {max_file_bytes})",
        }

    if b"\x00" in raw[:8192]:
        return {"ok": False, "error": "file appears binary (nul byte in header)"}

    text = raw.decode("utf-8", errors="replace")
    backend_guess = guess_bibliography_backend(text)

    lines = text.splitlines()
    issues: list[dict[str, Any]] = []
    truncated_holder = [False]

    cap = max(1, min(int(max_issues), 500))

    for lineno, line in enumerate(lines, start=1):
        stripped = line.rstrip("\r\n")
        if not stripped.strip():
            continue

        m_biber = _RX_BIBER_LINE.match(stripped)
        if m_biber:
            level = m_biber.group(1).upper()
            severity = "error" if level in {"ERROR", "FATAL"} else "warning"
            _append_issue(
                issues,
                max_issues=cap,
                lineno=lineno,
                severity=severity,
                code="biber_line",
                message=m_biber.group(2),
                truncated_flag=truncated_holder,
            )
            continue

        m_bw = _RX_BIBTEX_WARNING.match(stripped)
        if m_bw:
            _append_issue(
                issues,
                max_issues=cap,
                lineno=lineno,
                severity="warning",
                code="bibtex_warning",
                message=m_bw.group(1),
                truncated_flag=truncated_holder,
            )
            continue

        if _RX_BIBTEX_DB_OPEN.match(stripped):
            _append_issue(
                issues,
                max_issues=cap,
                lineno=lineno,
                severity="error",
                code="bibtex_io",
                message=stripped,
                truncated_flag=truncated_holder,
            )
            continue

        if _RX_BIBTEX_REPEATED.match(stripped):
            _append_issue(
                issues,
                max_issues=cap,
                lineno=lineno,
                severity="warning",
                code="bibtex_duplicate",
                message=stripped,
                truncated_flag=truncated_holder,
            )
            continue

        if _RX_BIBTEX_ILLEGAL.match(stripped):
            _append_issue(
                issues,
                max_issues=cap,
                lineno=lineno,
                severity="error",
                code="bibtex_illegal",
                message=stripped,
                truncated_flag=truncated_holder,
            )
            continue

        if _RX_BIBTEX_SUMMARY.match(stripped):
            _append_issue(
                issues,
                max_issues=cap,
                lineno=lineno,
                severity="info",
                code="bibtex_summary",
                message=stripped,
                truncated_flag=truncated_holder,
            )

    tail_truncated = len(text) > tail_max_chars
    tail_out = text if not tail_truncated else text[-tail_max_chars:]

    errorish = sum(1 for i in issues if i["severity"] == "error")
    warnish = sum(1 for i in issues if i["severity"] == "warning")

    warnings_out = list(warnings_banner)
    if truncated_holder[0]:
        warnings_out.append(f"issues truncated to max_issues={cap}")

    out: dict[str, Any] = {
        "ok": True,
        "relative_blg_path": source_rel,
        "backend_guess": backend_guess,
        "issues": issues,
        "issue_count": len(issues),
        "truncated_issues": truncated_holder[0],
        "severity_counts": {"error": errorish, "warning": warnish, "info": len(issues) - errorish - warnish},
        "tail": tail_out,
        "tail_truncated": tail_truncated,
        "tail_max_chars": tail_max_chars,
        "line_count": len(lines),
    }
    if warnings_out:
        out["warnings"] = warnings_out
    return out
