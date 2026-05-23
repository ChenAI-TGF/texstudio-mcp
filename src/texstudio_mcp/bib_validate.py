"""Sandbox validation (and optional whitespace normalization) for ``.bib`` files."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from texstudio_mcp.bib_validate_bibtexparser import bibtexparser_available, validate_with_bibtexparser
from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace
from texstudio_mcp.workspace_fs import write_project_text_file

DEFAULT_MAX_FILE_BYTES = 5_000_000
DEFAULT_PREVIEW_MAX_CHARS = 16_000

_RX_ENTRY_HEAD = re.compile(r"@(?P<etype>[A-Za-z]+)\s*\{")
_RX_STRING_ASSIGN = re.compile(r"\s*(?P<name>[^\s=]+)\s*=")


def _workspace_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _lineno(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _extract_citation_key_after_brace(text: str, pos: int) -> str | None:
    """First token after ``{`` up to first comma or closing brace at shallow depth."""
    i = pos
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i >= n:
        return None
    start = i
    while i < n:
        ch = text[i]
        if ch == ",":
            tok = text[start:i].strip()
            return tok if tok else None
        if ch == "}":
            return None
        i += 1
    return None


def _extract_string_macro_name(text: str, pos: int) -> str | None:
    m = _RX_STRING_ASSIGN.match(text, pos)
    if not m:
        return None
    return m.group("name").strip() or None


def _brace_balance_scan(text: str) -> tuple[int, list[dict[str, Any]]]:
    """Brace depth outside double-quoted spans (TeX-ish escaping simplified)."""
    depth = 0
    in_string = False
    escape = False
    lineno = 1
    errors: list[dict[str, Any]] = []

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            lineno += 1

        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue

        if ch == '"' and not in_string:
            in_string = True
            i += 1
            continue
        if ch == '"' and in_string:
            in_string = False
            i += 1
            continue

        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    errors.append(
                        {
                            "code": "unexpected_close_brace",
                            "message": "closing brace exceeds opening braces",
                            "line": lineno,
                        }
                    )
                    depth = 0
        i += 1

    if depth != 0:
        errors.append(
            {
                "code": "unbalanced_braces",
                "message": f"brace depth at EOF is {depth} (expected 0)",
                "line": None,
            }
        )

    return depth, errors


def _index_after_matching_brace(text: str, brace_open_idx: int) -> int | None:
    """Return index **after** the ``}`` matching ``brace_open_idx`` (which must point at ``{``)."""
    if brace_open_idx >= len(text) or text[brace_open_idx] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    i = brace_open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"' and not in_string:
            in_string = True
            i += 1
            continue
        if ch == '"' and in_string:
            in_string = False
            i += 1
            continue
        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return None


def _normalize_bib_whitespace(text: str) -> str:
    lines = text.splitlines()
    stripped = [ln.rstrip() for ln in lines]
    body = "\n".join(stripped)
    return body + "\n" if body else ""


def _canonical_compare_form(text: str) -> str:
    body = "\n".join(text.splitlines())
    return body + "\n" if body else ""


def validate_bib_file(
    workspace_root: str,
    relative_bib_path: str,
    *,
    normalize: bool = False,
    dry_run: bool = True,
    overwrite: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    preview_max_chars: int = DEFAULT_PREVIEW_MAX_CHARS,
    use_bibtexparser: bool = False,
) -> dict[str, Any]:
    """Validate a ``.bib`` under workspace_root; optionally LF-normalize trailing whitespace."""
    warnings: list[dict[str, Any]] = []
    infos: list[dict[str, Any]] = []

    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        bib_file = resolve_under_workspace(root, relative_bib_path)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if not bib_file.exists():
        return {"ok": False, "error": "path does not exist"}
    if not bib_file.is_file():
        return {"ok": False, "error": "not a regular file"}

    if normalize and not dry_run and not overwrite:
        return {
            "ok": False,
            "error": "normalize with dry_run=false requires overwrite=true (refusing implicit overwrite)",
        }

    source_rel = _workspace_relative(bib_file, root).replace("\\", "/")
    low = source_rel.lower()
    if not low.endswith(".bib"):
        warnings.append(
            {
                "code": "unexpected_suffix",
                "message": "relative_bib_path does not end with .bib; validation proceeds anyway",
            }
        )

    try:
        raw = bib_file.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}"}

    if len(raw) > max_file_bytes:
        return {
            "ok": False,
            "error": f"file exceeds max_file_bytes ({len(raw)} > {max_file_bytes})",
        }

    if b"\x00" in raw[:8192]:
        return {"ok": False, "error": "file appears binary (nul byte in header)"}

    try:
        text = raw.decode("utf-8")
        utf8_strict_ok = True
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        utf8_strict_ok = False
        warnings.append(
            {
                "code": "utf8_decode_relaxed",
                "message": "file is not strict UTF-8; decoded with replacement characters",
            }
        )

    validation_backend = "heuristic"
    citation_lines: dict[str, list[int]] = defaultdict(list)
    string_lines: dict[str, list[int]] = defaultdict(list)

    if use_bibtexparser:
        if not bibtexparser_available():
            return {
                "ok": False,
                "error": (
                    "use_bibtexparser=true but bibtexparser is not installed; "
                    "pip install 'texstudio-mcp[bibtex]'"
                ),
                "validation_backend": "bibtexparser_missing",
            }
        bp = validate_with_bibtexparser(text)
        if not bp.get("ok") and bp.get("error"):
            return {
                "ok": False,
                "error": bp["error"],
                "validation_backend": bp.get("validation_backend", "bibtexparser"),
                **{k: v for k, v in bp.items() if k not in ("ok",)},
            }
        validation_backend = "bibtexparser"
        errors = list(bp.get("errors") or [])
        warnings = list(bp.get("warnings") or [])
        duplicate_keys = list(bp.get("duplicate_keys") or [])
        duplicate_strings: list[dict[str, Any]] = []
        entry_count = bp.get("entry_count")
    else:
        entry_count = None
        errors = []
        duplicate_keys = []
        duplicate_strings = []

    if not use_bibtexparser:
        pos = 0
        while True:
            m = _RX_ENTRY_HEAD.search(text, pos)
            if not m:
                break
            etype = m.group("etype").lower()
            open_brace_idx = m.start() + m.group(0).rfind("{")
            inner_start = open_brace_idx + 1
            lineno = _lineno(text, m.start())

            if etype == "comment":
                pass
            elif etype == "string":
                name = _extract_string_macro_name(text, inner_start)
                if name:
                    string_lines[name].append(lineno)
            elif etype == "preamble":
                pass
            else:
                key = _extract_citation_key_after_brace(text, inner_start)
                if key:
                    citation_lines[key].append(lineno)

            nxt = _index_after_matching_brace(text, open_brace_idx)
            if nxt is None:
                pos = inner_start + 1
            else:
                pos = nxt

        duplicate_keys = []
        for key, lines in sorted(citation_lines.items()):
            if len(lines) > 1:
                duplicate_keys.append({"key": key, "lines": lines, "count": len(lines)})
                warnings.append(
                    {
                        "code": "duplicate_citation_key",
                        "message": f"citation key {key!r} appears {len(lines)} times",
                        "key": key,
                        "lines": lines,
                    }
                )

        duplicate_strings = []
        for name, lines in sorted(string_lines.items()):
            if len(lines) > 1:
                duplicate_strings.append({"name": name, "lines": lines, "count": len(lines)})
                warnings.append(
                    {
                        "code": "duplicate_string_macro",
                        "message": f"@string macro {name!r} appears {len(lines)} times",
                        "name": name,
                        "lines": lines,
                    }
                )

        _, brace_errors = _brace_balance_scan(text)
        errors = list(brace_errors)
    else:
        _, brace_errors = _brace_balance_scan(text)
        for be in brace_errors:
            errors.append(be)

    normalized_preview: str | None = None
    normalized_changed: bool | None = None
    write_payload: dict[str, Any] | None = None

    if normalize:
        normalized_text = _normalize_bib_whitespace(text)
        normalized_changed = normalized_text != _canonical_compare_form(text)
        cap = max(256, min(int(preview_max_chars), 600_000))
        normalized_preview = normalized_text if len(normalized_text) <= cap else normalized_text[:cap]
        if len(normalized_text) > cap:
            infos.append(
                {
                    "code": "normalized_preview_truncated",
                    "message": f"normalized_preview truncated to preview_max_chars={cap}",
                }
            )

        if not dry_run and overwrite:
            write_payload = write_project_text_file(
                workspace_root,
                relative_bib_path,
                normalized_text,
                overwrite=True,
                max_bytes=max_file_bytes,
            )

    ok = len(errors) == 0 and (
        write_payload is None or (isinstance(write_payload, dict) and write_payload.get("ok") is True)
    )

    out: dict[str, Any] = {
        "ok": ok,
        "relative_bib_path": source_rel,
        "utf8_strict": utf8_strict_ok,
        "errors": errors,
        "warnings": warnings,
        "infos": infos,
        "duplicate_keys": duplicate_keys,
        "duplicate_string_macros": duplicate_strings,
        "validation_backend": validation_backend,
        "entry_head_scan_note": (
            "duplicate detection uses bibtexparser"
            if use_bibtexparser
            else "duplicate detection uses @type{ key , } heuristic only"
        ),
        "normalized_changed": normalized_changed,
        "normalized_preview": normalized_preview,
        "dry_run": dry_run,
        "overwrite_attempted": bool(normalize and not dry_run and overwrite),
    }

    if write_payload is not None:
        out["write_result"] = write_payload
        if not write_payload.get("ok"):
            out["ok"] = False

    if entry_count is not None:
        out["bibtexparser_entry_count"] = entry_count

    return out
