"""Static extraction of \\input / \\include / bibliography / figures / styles from a .tex file (phase C-1 batch)."""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Any

from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace
from texstudio_mcp.workspace_fs import DEFAULT_SKIP_DIR_NAMES

DEFAULT_MAX_EDGES = 500
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MANIFEST_MAX_DEPTH = 12
DEFAULT_MANIFEST_MAX_PATHS_PER_BUCKET = 800

# Longest prefix first so ``\\InputIfFileExists`` wins over ``\\input``.
_KNOWN_TEX_COMMANDS: tuple[tuple[str, str], ...] = (
    ("InputIfFileExists", "InputIfFileExists"),
    ("addbibresource", "addbibresource"),
    ("bibliography", "bibliography"),
    ("bibliographystyle", "bibliographystyle"),
    ("documentclass", "documentclass"),
    ("usepackage", "usepackage"),
    ("includegraphics", "includegraphics"),
    ("include", "include"),
    ("input", "input"),
)

_ASSET_KINDS = frozenset({"documentclass", "usepackage", "includegraphics", "bibliographystyle"})
_INCLUDE_GRAPHICS_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg")

_MANIFEST_SUFFIX_BUCKETS: tuple[tuple[str, str], ...] = (
    (".tex", "tex_files"),
    (".bib", "bib_files"),
    (".bst", "bst_files"),
    (".cls", "cls_files"),
    (".sty", "sty_files"),
)


def _workspace_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _strip_tex_line_comment(line: str) -> str:
    """Remove an end-of-line TeX comment starting at unescaped ``%``."""
    i = 0
    while i < len(line):
        if line[i] == "%":
            bs = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:
                return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _logical_tex_source(raw_bytes: bytes) -> str:
    if b"\x00" in raw_bytes[:8192]:
        raise ValueError("file appears binary (nul byte in header)")
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = [_strip_tex_line_comment(L) for L in text.splitlines()]
    return "\n".join(lines)


def _extract_balanced_braces(text: str, open_brace_idx: int) -> tuple[str, int] | None:
    """``open_brace_idx`` points at ``{``. Returns inner content and index past closing ``}``.

    Honors ``\\`` escaping so ``\\{`` / ``\\}`` do not affect brace depth.
    """
    if open_brace_idx >= len(text) or text[open_brace_idx] != "{":
        return None
    depth = 0
    i = open_brace_idx
    inner_start: int | None = None
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2 if i + 1 < len(text) else 1
            continue
        if ch == "{":
            depth += 1
            if depth == 1:
                inner_start = i + 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                assert inner_start is not None
                return text[inner_start:i], i + 1
            i += 1
            continue
        i += 1
    return None


def _skip_ws_star_ws(text: str, start: int) -> int:
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    if i < len(text) and text[i] == "*":
        i += 1
        while i < len(text) and text[i].isspace():
            i += 1
    return i


def _skip_optional_square_brackets(text: str, start: int) -> int:
    """Skip LaTeX optional ``[...]`` starting at next non-whitespace if present."""
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != "[":
        return i
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2 if i + 1 < len(text) else 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _consume_optional_square_brackets_chain(text: str, start: int) -> int:
    """Consume zero or more consecutive ``[ ... ]`` optional argument blocks."""
    i = start
    while True:
        j = _skip_optional_square_brackets(text, i)
        if j == i:
            return i
        i = j


def _match_known_command(text: str, pos: int) -> tuple[str, int] | None:
    """If ``text[pos]`` is ``\\\\``, match longest known command."""
    if pos >= len(text) or text[pos] != "\\":
        return None
    tail = text[pos + 1 :]
    for name, kind in _KNOWN_TEX_COMMANDS:
        if tail.startswith(name):
            end_cmd = pos + 1 + len(name)
            if end_cmd < len(text) and text[end_cmd].isalpha():
                continue
            return kind, end_cmd
    return None


def _normalize_tex_relative(source_tex_rel: str, raw_target: str) -> str:
    raw_target = raw_target.strip()
    parent = posixpath.dirname(source_tex_rel.replace("\\", "/"))
    base_parent = parent if parent else "."
    joined = posixpath.normpath(posixpath.join(base_parent, raw_target))
    return joined.replace("\\", "/")


def _normalize_asset_relative(source_tex_rel: str, raw_target: str, suffix: str) -> str:
    """Normalize path relative to source ``.tex``; append ``suffix`` when missing."""
    joined = _normalize_tex_relative(source_tex_rel, raw_target)
    lower = joined.lower()
    if lower.endswith(suffix):
        return joined.replace("\\", "/")
    return (joined + suffix).replace("\\", "/")


def _bib_normalize_relative(source_tex_rel: str, raw_target: str) -> str:
    raw_target = raw_target.strip()
    norm = _normalize_tex_relative(source_tex_rel, raw_target)
    if not norm.lower().endswith(".bib"):
        return f"{norm}.bib"
    return norm


def _is_dynamic_target(expr: str) -> bool:
    e = expr.strip()
    if "#" in e:
        return True
    if "\\" in e:
        return True
    return False


def _split_bibliography_keys(inner: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\s*,\s*", inner.strip())]
    return [p for p in parts if p]


def _split_usepackage_targets(inner: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\s*,\s*", inner.strip())]
    return [p for p in parts if p]


_GSPATH_SEG_RE = re.compile(r"\{([^{}]*)\}")


def _parse_graphicspath_brace_segments(inner: str) -> list[str]:
    """Pull ``\\graphicspath{{a}{b}}`` segments; fall back to one trimmed atom."""
    segments = [_s.strip() for _s in _GSPATH_SEG_RE.findall(inner)]
    segments = [_s for _s in segments if _s]
    if segments:
        return segments
    stripped = inner.strip()
    return [stripped] if stripped else []


def _collect_graphicspath_prefixes_normalized(root: Path, logical: str, source_rel: str, warnings: list[str]) -> list[str]:
    """Ordered unique directory prefixes (POSIX workspace-relative) from ``\\graphicspath`` directives."""
    cmd = "\\graphicspath"
    clen = len(cmd)
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(logical):
        j = logical.find(cmd, i)
        if j < 0:
            break
        next_idx = j + clen
        if next_idx < len(logical) and logical[next_idx].isalpha():
            i = j + 1
            continue
        cursor = next_idx
        while cursor < len(logical) and logical[cursor].isspace():
            cursor += 1
        extracted = _extract_balanced_braces(logical, cursor)
        if extracted is None:
            warnings.append("graphicspath missing or unbalanced outer braces")
            i = j + 1
            continue
        inner, resume_at = extracted
        segments = _parse_graphicspath_brace_segments(inner)
        if not segments:
            warnings.append("graphicspath argument contained no directory segments")
            i = resume_at
            continue
        for seg in segments:
            norm = _normalize_tex_relative(source_rel, seg).replace("\\", "/")
            try:
                resolve_under_workspace(root, norm)
            except PathPolicyError:
                warnings.append(f"graphicspath_path_escapes_workspace:{norm}")
                continue
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
        i = resume_at
    return out


def _combine_prefix_and_graphics_token(prefix_rel: str, graphics_tok: str) -> str:
    """Resolve ``\\includegraphics`` token relative to one ``graphicspath`` directory."""
    tok = graphics_tok.replace("\\", "/").strip()
    pref = prefix_rel.replace("\\", "/").strip().rstrip("/")
    trimmed_tok = tok.lstrip("./")
    if not pref or pref == ".":
        merged = posixpath.normpath(trimmed_tok)
    else:
        merged = posixpath.normpath(posixpath.join(pref, trimmed_tok))
    return merged.replace("\\", "/")


def _resolve_graphics_file_under(root: Path, rel_posix: str) -> Path | None:
    """First existing file for a workspace-relative graphics path (optional extension)."""
    rel = rel_posix.replace("\\", "/")
    try:
        direct = resolve_under_workspace(root, rel)
    except PathPolicyError:
        return None
    if direct.is_file():
        return direct
    low = rel.lower()
    for ext in _INCLUDE_GRAPHICS_EXTENSIONS:
        if low.endswith(ext):
            continue
        try:
            with_ext = resolve_under_workspace(root, rel + ext)
        except PathPolicyError:
            continue
        if with_ext.is_file():
            return with_ext
    return None


def _resolve_include_graphics_edge(
    root: Path,
    source_rel: str,
    graphics_tok: str,
    direct_norm: str,
    graphicspath_prefixes: list[str],
) -> tuple[str, Path, dict[str, str]]:
    """Canonical ``edge.to``, workspace Path, optional meta."""

    dn = direct_norm.replace("\\", "/")

    dp = _resolve_graphics_file_under(root, dn)
    if dp is not None:
        rel_hit = _workspace_relative(dp, root).replace("\\", "/")
        return rel_hit, dp, {}

    for pref in graphicspath_prefixes:
        cand = _combine_prefix_and_graphics_token(pref, graphics_tok)
        rp = _resolve_graphics_file_under(root, cand)
        if rp is not None:
            rel_hit = _workspace_relative(rp, root).replace("\\", "/")
            meta = {"graphicspath_resolution": "prefix", "graphicspath_prefix_norm": pref}
            return rel_hit, rp, meta

    try:
        fallback = resolve_under_workspace(root, dn)
    except PathPolicyError:
        return dn, root / dn, {}
    return dn, fallback, {}


def _targets_for_kind(kind: str, inner: str) -> list[str]:
    inner_stripped = inner.strip()
    if kind == "bibliography":
        return _split_bibliography_keys(inner)
    if kind == "usepackage":
        return _split_usepackage_targets(inner)
    return [inner_stripped] if inner_stripped else []


def _normalize_target_for_kind(kind: str, source_rel: str, tok: str) -> str:
    if kind in {"bibliography", "addbibresource"}:
        return _bib_normalize_relative(source_rel, tok)
    if kind == "documentclass":
        return _normalize_asset_relative(source_rel, tok, ".cls")
    if kind == "usepackage":
        return _normalize_asset_relative(source_rel, tok, ".sty")
    if kind == "bibliographystyle":
        return _normalize_asset_relative(source_rel, tok, ".bst")
    return _normalize_tex_relative(source_rel, tok)


def _workspace_manifest(
    root: Path,
    *,
    hint_relative_tex: str,
    max_depth: int,
    max_per_bucket: int,
) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {bucket: [] for _, bucket in _MANIFEST_SUFFIX_BUCKETS}
    truncated: dict[str, bool] = {bucket: False for bucket in buckets}

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
            suffix = Path(name).suffix.lower()
            bucket_key = None
            for suf, bk in _MANIFEST_SUFFIX_BUCKETS:
                if suffix == suf:
                    bucket_key = bk
                    break
            if bucket_key is None:
                continue
            path = rp / name
            try:
                rel_file = _workspace_relative(path, root)
            except ValueError:
                continue
            lst = buckets[bucket_key]
            if len(lst) < max_per_bucket:
                lst.append(rel_file)
            else:
                truncated[bucket_key] = True

    for bk in buckets:
        buckets[bk].sort()

    hint_clean = hint_relative_tex.strip().replace("\\", "/")
    hint_exists: bool | None = None
    if hint_clean:
        try:
            hint_path = resolve_under_workspace(root, hint_clean)
            hint_exists = hint_path.is_file() and hint_path.suffix.lower() == ".tex"
        except PathPolicyError:
            hint_exists = False

    warnings: list[str] = []
    if any(truncated.values()):
        warnings.append(
            f"one_or_more_manifest_buckets_truncated_to_manifest_max_paths_per_bucket={max_per_bucket}",
        )

    return {
        "ok": True,
        "scan_mode": "workspace_manifest",
        "manifest": buckets,
        "manifest_truncated": truncated,
        "manifest_max_depth_effective": max_depth,
        "manifest_max_paths_per_bucket_effective": max_per_bucket,
        "hint_main_tex": hint_clean or None,
        "hint_main_tex_valid": hint_exists,
        "warnings": warnings,
    }


def parse_tex_dependencies(
    workspace_root: str,
    relative_tex_path: str,
    *,
    scan_mode: str = "tex_edges",
    max_edges: int = DEFAULT_MAX_EDGES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    manifest_max_depth: int = DEFAULT_MANIFEST_MAX_DEPTH,
    manifest_max_paths_per_bucket: int = DEFAULT_MANIFEST_MAX_PATHS_PER_BUCKET,
) -> dict[str, Any]:
    """Scan one ``.tex`` or produce a workspace asset manifest.

    * ``scan_mode=tex_edges`` (default): static ``\\input`` / ``\\include`` / bibliography /
      ``\\includegraphics`` (honors ``\\graphicspath`` directory prefixes when resolving assets) /
      ``\\documentclass`` / ``\\usepackage`` / ``\\bibliographystyle``.
    * ``scan_mode=workspace_manifest``: non-parsing inventory of ``.tex`` / ``.bib`` / ``.bst`` /
      ``.cls`` / ``.sty`` under ``workspace_root``; optional ``relative_tex_path`` hints the main ``.tex``.

    Does **not** run TeX.
    """
    mode = scan_mode.strip().lower().replace("-", "_")
    if mode in {"manifest", "workspace_manifest"}:
        mode = "workspace_manifest"

    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if mode == "workspace_manifest":
        return _workspace_manifest(
            root,
            hint_relative_tex=relative_tex_path,
            max_depth=manifest_max_depth,
            max_per_bucket=manifest_max_paths_per_bucket,
        )

    if mode != "tex_edges":
        return {"ok": False, "error": f"unknown scan_mode: {scan_mode!r}"}

    warnings: list[str] = []
    edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    try:
        tex_file = resolve_under_workspace(root, relative_tex_path)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if not tex_file.exists():
        return {"ok": False, "error": "path does not exist"}
    if not tex_file.is_file():
        return {"ok": False, "error": "not a regular file"}

    source_rel = _workspace_relative(tex_file, root).replace("\\", "/")
    low = source_rel.lower()
    if not low.endswith(".tex"):
        warnings.append("relative_tex_path does not end with .tex; parsing proceeds anyway")

    try:
        raw = tex_file.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}"}

    if len(raw) > max_file_bytes:
        return {
            "ok": False,
            "error": f"file exceeds max_file_bytes ({len(raw)} > {max_file_bytes})",
        }

    try:
        logical = _logical_tex_source(raw)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    gs_prefixes = _collect_graphicspath_prefixes_normalized(root, logical, source_rel, warnings)

    pos = 0
    truncated_edges = False

    while pos < len(logical):
        matched = _match_known_command(logical, pos)
        if matched is None:
            pos += 1
            continue
        kind, after_name = matched
        cursor = _skip_ws_star_ws(logical, after_name)
        cursor = _consume_optional_square_brackets_chain(logical, cursor)
        extracted = _extract_balanced_braces(logical, cursor)
        if extracted is None:
            unresolved.append({"kind": kind, "reason": "missing_or_unbalanced_braces"})
            pos = after_name
            continue
        inner, after_brace = extracted
        targets = _targets_for_kind(kind, inner)

        for raw_tok in targets:
            tok = raw_tok.strip()
            if not tok:
                continue
            if _is_dynamic_target(tok):
                unresolved.append({"kind": kind, "token": tok, "reason": "dynamic_path"})
                continue

            norm_rel = _normalize_target_for_kind(kind, source_rel, tok)

            try:
                base_resolved = resolve_under_workspace(root, norm_rel)
            except PathPolicyError:
                warnings.append(f"path_escapes_workspace:{norm_rel}")
                unresolved.append(
                    {"kind": kind, "token": tok, "normalized": norm_rel, "reason": "path_escapes_workspace"},
                )
                continue

            if len(edges) >= max_edges:
                truncated_edges = True
                break

            edge: dict[str, Any] = {"from": source_rel, "kind": kind}
            ig_meta: dict[str, str] = {}

            if kind == "includegraphics" and gs_prefixes:
                to_rel, rp, ig_meta = _resolve_include_graphics_edge(
                    root, source_rel, tok, norm_rel, gs_prefixes
                )
                edge["to"] = to_rel.replace("\\", "/")
            else:
                rp = base_resolved
                edge["to"] = norm_rel.replace("\\", "/")

            if ig_meta:
                edge.update(ig_meta)

            if kind in _ASSET_KINDS:
                edge["workspace_asset_found"] = rp.is_file()
            edges.append(edge)

        if truncated_edges:
            break

        pos = after_brace

    if truncated_edges:
        warnings.append(f"edges truncated to max_edges={max_edges}")

    return {
        "ok": True,
        "scan_mode": "tex_edges",
        "relative_tex_path": source_rel,
        "edges": edges,
        "unresolved": unresolved,
        "warnings": warnings,
        "edge_count": len(edges),
        "truncated_edges": truncated_edges,
    }
