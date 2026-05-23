"""Read-only peek at fixed TeXstudio profile files outside ``workspace_root`` (phase E)."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ALLOWED_PROFILE_FILES = frozenset({"texstudio.ini", "lastSession.txss"})

PROFILE_READ_MIN_CHARS = 256
PROFILE_READ_DEFAULT_MAX_CHARS = 160_000
PROFILE_READ_HARD_MAX_CHARS = 600_000
PROFILE_HARD_MAX_BYTES = 8 * 1024 * 1024
_HINT_SCAN_MAX_LINES = 8_000
_HINT_LIST_CAP = 8

_RX_LAST_DOCUMENT = re.compile(r"^Last\\Document=(.+)$", re.IGNORECASE)
_RX_MASTER_DOCUMENT = re.compile(r"^MasterDocument=(.+)$", re.IGNORECASE)
_RX_DEFAULT_COMPILER = re.compile(r"^DefaultCompiler=(.+)$", re.IGNORECASE)
_RX_TXSS_CURRENT_FILE = re.compile(r"^CurrentFile=(.+)$", re.IGNORECASE)
_RX_TXSS_MASTER_FILE = re.compile(r"^MasterFile=(.+)$", re.IGNORECASE)
_RX_TXSS_FILE_NAME = re.compile(r"^File\d+\\FileName=(.+)$", re.IGNORECASE)


def _normalize_tex_path_token(raw: str) -> str:
    t = raw.strip().strip('"').strip("'")
    if t.lower().startswith("file:///"):
        return t[8:].replace("\\", "/")
    if t.lower().startswith("file://"):
        return t[7:].replace("\\", "/")
    return t.replace("\\", "/")


def _job_basename_from_tex_path(path_like: str) -> str | None:
    norm = _normalize_tex_path_token(path_like)
    base = Path(norm).name
    if not base.lower().endswith(".tex"):
        return None
    stem = base[:-4]
    if not stem or ".." in stem or "/" in stem or "\\" in stem:
        return None
    return stem


def _extract_texstudio_ini_hints(text: str) -> dict[str, Any]:
    """Best-effort scan of common TeXstudio.ini keys (not a full INI parser)."""
    last_docs: list[str] = []
    masters: list[str] = []
    compilers: list[str] = []
    lines = text.splitlines()
    truncated_scan = len(lines) > _HINT_SCAN_MAX_LINES

    for line in lines[:_HINT_SCAN_MAX_LINES]:
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        m = _RX_LAST_DOCUMENT.match(stripped)
        if m:
            last_docs.append(_normalize_tex_path_token(m.group(1))[:4000])
            continue
        m = _RX_MASTER_DOCUMENT.match(stripped)
        if m:
            masters.append(_normalize_tex_path_token(m.group(1))[:4000])
            continue
        m = _RX_DEFAULT_COMPILER.match(stripped)
        if m:
            compilers.append(m.group(1).strip()[:500])

    def _dedupe_cap(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))[:_HINT_LIST_CAP]

    parsed: dict[str, Any] = {
        "last_document": _dedupe_cap(last_docs),
        "master_document": _dedupe_cap(masters),
        "default_compiler": _dedupe_cap(compilers),
    }
    if truncated_scan:
        parsed["hint_scan_truncated"] = True

    suggested: str | None = None
    for candidate in (*masters, *last_docs):
        jb = _job_basename_from_tex_path(candidate)
        if jb:
            suggested = jb
            break
    out: dict[str, Any] = {"parsed_hints": parsed}
    if suggested:
        out["suggested_job_basename"] = suggested
    return out


def _session_paths_from_txss_json(data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Extract current/master/open file paths from TeXstudio JSON session blob."""
    sess = data.get("Session") or data.get("session")
    if not isinstance(sess, dict):
        return [], [], []
    currents: list[str] = []
    masters: list[str] = []
    open_files: list[str] = []
    cf = sess.get("CurrentFile")
    if isinstance(cf, str) and cf.strip():
        currents.append(_normalize_tex_path_token(cf)[:4000])
    mf = sess.get("MasterFile")
    if isinstance(mf, str) and mf.strip():
        masters.append(_normalize_tex_path_token(mf)[:4000])
    files = sess.get("Files") or sess.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            fn = item.get("FileName") or item.get("fileName")
            if isinstance(fn, str) and fn.strip():
                open_files.append(_normalize_tex_path_token(fn)[:4000])
    return currents, masters, open_files


def _extract_last_session_txss_hints(text: str) -> dict[str, Any]:
    """Best-effort scan of ``lastSession.txss`` / ``.txss2`` (JSON or INI-like)."""
    stripped = text.strip()
    currents: list[str] = []
    masters: list[str] = []
    open_files: list[str] = []
    format_guess = "ini_like"
    truncated_flag = False
    json_ok = False

    if stripped.startswith("{"):
        format_guess = "json"
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            format_guess = "json_parse_failed"
        else:
            if isinstance(data, dict):
                c, m, o = _session_paths_from_txss_json(data)
                currents.extend(c)
                masters.extend(m)
                open_files.extend(o)
                json_ok = True

    if not json_ok:
        if format_guess == "json_parse_failed":
            format_guess = "json_fallback_ini_like"
        lines = text.splitlines()
        truncated_scan = len(lines) > _HINT_SCAN_MAX_LINES
        truncated_flag = truncated_scan
        for line in lines[:_HINT_SCAN_MAX_LINES]:
            s = line.strip()
            if not s or s.startswith(";") or s.startswith("#"):
                continue
            m = _RX_TXSS_CURRENT_FILE.match(s)
            if m:
                currents.append(_normalize_tex_path_token(m.group(1))[:4000])
                continue
            m = _RX_TXSS_MASTER_FILE.match(s)
            if m:
                masters.append(_normalize_tex_path_token(m.group(1))[:4000])
                continue
            m = _RX_TXSS_FILE_NAME.match(s)
            if m:
                open_files.append(_normalize_tex_path_token(m.group(1))[:4000])

    def _dedupe_cap(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))[:_HINT_LIST_CAP]

    parsed: dict[str, Any] = {
        "session_format_guess": format_guess,
        "current_file": _dedupe_cap(currents),
        "master_file": _dedupe_cap(masters),
        "open_file_names": _dedupe_cap(open_files),
    }
    if truncated_flag:
        parsed["hint_scan_truncated"] = True

    suggested: str | None = None
    for candidate in (*masters, *currents, *open_files):
        jb = _job_basename_from_tex_path(candidate)
        if jb:
            suggested = jb
            break
    out: dict[str, Any] = {"parsed_hints": parsed}
    if suggested:
        out["suggested_job_basename"] = suggested
    return out


def _clamp_max_chars(raw: int) -> int:
    return max(PROFILE_READ_MIN_CHARS, min(int(raw), PROFILE_READ_HARD_MAX_CHARS))


def _planned_texstudio_config_directories() -> list[Path]:
    """Return hypothetical config directories (expanded, not necessarily existing)."""
    paths: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata).expanduser() / "texstudio")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            paths.append(Path(local).expanduser() / "texstudio")
    elif sys.platform == "darwin":
        home = Path.home()
        paths.append(home / "Library" / "Application Support" / "texstudio")
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            paths.append(Path(xdg).expanduser() / "texstudio")
        paths.append(home / ".config" / "texstudio")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            paths.append(Path(xdg).expanduser() / "texstudio")
        paths.append(Path.home() / ".config" / "texstudio")
    return paths


def _unique_existing_roots(iterable: Sequence[Path]) -> list[Path]:
    """Deduplicate by ``realpath``; keep dirs that exist on disk."""
    seen: set[str] = set()
    out: list[Path] = []
    for p in iterable:
        try:
            r = Path(os.path.realpath(p.expanduser()))
        except OSError:
            continue
        if not r.exists() or not r.is_dir():
            continue
        key = str(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _validated_profile_basename(raw: str) -> str | None:
    stripped = raw.strip()
    if not stripped or stripped != Path(stripped).name:
        return None
    if stripped not in ALLOWED_PROFILE_FILES:
        return None
    return stripped


def _file_resolved_inside_root(root_real: Path, candidate: Path) -> Path | None:
    try:
        file_real = Path(os.path.realpath(candidate.expanduser()))
    except OSError:
        return None
    try:
        file_real.relative_to(root_real)
    except ValueError:
        return None
    return file_real if file_real.is_file() else None


def _package_success(
    *,
    base: str,
    cap: int,
    resolved_file: Path,
    directories_scanned: list[str],
    directories_planned: list[str],
    resolution_mode: str,
    extra_meta: dict[str, Any] | None = None,
    include_parsed_hints: bool = False,
) -> dict[str, Any]:
    try:
        blob = resolved_file.read_bytes()
    except OSError as exc:
        err: dict[str, Any] = {
            "ok": False,
            "error": f"cannot read profile file: {exc}",
            "directories_scanned": directories_scanned,
            "directories_planned": directories_planned,
            "resolution_mode": resolution_mode,
        }
        if extra_meta:
            err.update(extra_meta)
        return err

    if len(blob) > PROFILE_HARD_MAX_BYTES:
        return {
            "ok": False,
            "error": f"profile file exceeds hard cap ({len(blob)} bytes > {PROFILE_HARD_MAX_BYTES})",
            "directories_scanned": directories_scanned,
            "directories_planned": directories_planned,
            "resolution_mode": resolution_mode,
            **(extra_meta or {}),
        }

    if b"\x00" in blob[:8192]:
        return {
            "ok": False,
            "error": "file appears binary (nul bytes in header)",
            "directories_scanned": directories_scanned,
            "directories_planned": directories_planned,
            "resolution_mode": resolution_mode,
            **(extra_meta or {}),
        }

    text = blob.decode("utf-8", errors="replace")
    truncated = len(text) > cap
    clipped = text if not truncated else text[:cap]

    payload: dict[str, Any] = {
        "ok": True,
        "profile_file": base,
        "resolved_absolute_path": str(resolved_file),
        "directories_scanned": directories_scanned,
        "directories_planned": directories_planned,
        "resolution_mode": resolution_mode,
        "char_count_full": len(text),
        "char_count_returned": len(clipped),
        "truncated": truncated,
        "text": clipped,
    }
    if extra_meta:
        payload.update(extra_meta)
    if include_parsed_hints:
        if base == "texstudio.ini":
            payload.update(_extract_texstudio_ini_hints(text))
        elif base == "lastSession.txss":
            payload.update(_extract_last_session_txss_hints(text))
    return payload


def read_texstudio_profile_snapshot(
    profile_file: str,
    max_chars: int = PROFILE_READ_DEFAULT_MAX_CHARS,
    *,
    texstudio_config_dir: str = "",
    texstudio_ini_path: str = "",
    include_parsed_hints: bool = False,
    _candidate_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Read UTF-8 text from ``texstudio.ini`` / ``lastSession.txss`` (basename allow-list only).

    Resolution order: ``_candidate_roots`` (tests) > ``texstudio_ini_path`` > ``texstudio_config_dir`` >
    OS standard locations. Does not use ``workspace_root``.

    When ``include_parsed_hints`` is true, adds ``parsed_hints`` and optional
    ``suggested_job_basename`` for ``texstudio.ini`` (``Last\\Document``, …) or
    ``lastSession.txss`` (JSON / INI-like ``CurrentFile``, ``FileN\\FileName``, …).
    """
    base = _validated_profile_basename(profile_file)
    if base is None:
        return {
            "ok": False,
            "error": (
                f"profile_file must be one of {sorted(ALLOWED_PROFILE_FILES)} (basename only, no paths)"
            ),
        }

    cap = _clamp_max_chars(max_chars)
    planned = _planned_texstudio_config_directories()
    plan_labels = [str(Path(p.expanduser())) for p in planned]

    explicit_ini = texstudio_ini_path.strip()
    explicit_dir = texstudio_config_dir.strip()

    extra_meta: dict[str, Any] = {}
    if explicit_ini:
        extra_meta["user_texstudio_ini_path_requested"] = explicit_ini
    if explicit_dir:
        extra_meta["user_texstudio_config_dir_requested"] = explicit_dir
    if explicit_ini and explicit_dir:
        extra_meta["note"] = "texstudio_ini_path takes precedence over texstudio_config_dir when both are set"

    # 1) Test injection
    if _candidate_roots is not None:
        search_roots = _unique_existing_roots(list(_candidate_roots))
        resolution_mode = "test_override"
    # 2) Explicit file path (portable / custom install)
    elif explicit_ini:
        try:
            raw_p = Path(explicit_ini).expanduser()
            fpath = Path(os.path.realpath(raw_p))
        except OSError as exc:
            return {
                "ok": False,
                "error": f"cannot resolve texstudio_ini_path: {exc}",
                "directories_planned": plan_labels,
                "resolution_mode": "explicit_file",
                **{k: v for k, v in extra_meta.items() if v},
            }
        if not fpath.is_file():
            return {
                "ok": False,
                "error": f"texstudio_ini_path is not an existing file ({fpath})",
                "directories_planned": plan_labels,
                "resolution_mode": "explicit_file",
                **{k: v for k, v in extra_meta.items() if v},
            }
        if fpath.name != base:
            return {
                "ok": False,
                "error": (
                    f"texstudio_ini_path basename {fpath.name!r} must match profile_file {base!r}"
                ),
                "directories_planned": plan_labels,
                "resolution_mode": "explicit_file",
                **{k: v for k, v in extra_meta.items() if v},
            }
        dirs_scanned = [str(fpath.parent)]
        return _package_success(
            base=base,
            cap=cap,
            resolved_file=fpath,
            directories_scanned=dirs_scanned,
            directories_planned=plan_labels,
            resolution_mode="explicit_file",
            extra_meta=extra_meta,
            include_parsed_hints=include_parsed_hints,
        )

    # 3) Explicit config directory only
    elif explicit_dir:
        roots = _unique_existing_roots([Path(explicit_dir)])
        if not roots:
            return {
                "ok": False,
                "error": (
                    f"texstudio_config_dir does not exist or is not a directory "
                    f"({explicit_dir!r} expanded)"
                ),
                "directories_planned": plan_labels,
                "resolution_mode": "explicit_config_dir",
                **extra_meta,
            }
        search_roots = roots
        resolution_mode = "explicit_config_dir"
    # 4) Default OS layout
    else:
        search_roots = _unique_existing_roots(planned)
        resolution_mode = "standard_locations"

    if _candidate_roots is None and not explicit_ini:
        if not search_roots:
            return {
                "ok": False,
                "error": (
                    "no TeXstudio configuration directory exists at standard locations "
                    f"(planned={plan_labels}); install TeXstudio or open it once, or pass "
                    "texstudio_config_dir / texstudio_ini_path. "
                    "TeXstudio 'Help -> Check LaTeX Installation' prints the settings path."
                ),
                "directories_planned": plan_labels,
                "resolution_mode": resolution_mode,
                **{k: v for k, v in extra_meta.items() if v},
            }

    dirs_scanned = [str(d) for d in search_roots]
    resolved_file: Path | None = None
    for root in search_roots:
        root_real = Path(os.path.realpath(root.expanduser()))
        candidate = root_real / base
        got = _file_resolved_inside_root(root_real, candidate)
        if got is None:
            continue
        resolved_file = got
        break

    if resolved_file is None:
        return {
            "ok": False,
            "error": (
                f"could not find {base} inside scanned configuration directories "
                f"(directories_scanned={dirs_scanned})"
            ),
            "profile_file_requested": base,
            "directories_planned": plan_labels,
            "directories_scanned": dirs_scanned,
            "resolution_mode": resolution_mode,
            **{k: v for k, v in extra_meta.items() if v},
        }

    return _package_success(
        base=base,
        cap=cap,
        resolved_file=resolved_file,
        directories_scanned=dirs_scanned,
        directories_planned=plan_labels,
        resolution_mode=resolution_mode,
        extra_meta=extra_meta,
        include_parsed_hints=include_parsed_hints,
    )
