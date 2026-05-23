"""Sandbox Synctex forward (view) / backward (edit) helpers via ``synctex`` CLI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace

DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_STDOUT_CHARS = 48_000

_RX_RESULT_BEGIN = re.compile(r"(?im)^SyncTeX\s+Result\s+Begin\s*$")
_RX_RESULT_END = re.compile(r"(?im)^SyncTeX\s+Result\s+End\s*$")


def _workspace_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _truncate_stdout(text: str, limit: int = _MAX_STDOUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _split_result_blocks(stdout: str) -> list[str]:
    """Split Synctex stdout into logical blocks delimited by SyncTeX Result Begin/End."""
    lines = stdout.splitlines()
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for line in lines:
        if _RX_RESULT_BEGIN.match(line.strip()):
            if cur is not None:
                blocks.append(cur)
            cur = []
            continue
        if _RX_RESULT_END.match(line.strip()):
            if cur is not None:
                blocks.append(cur)
                cur = None
            continue
        if cur is not None:
            cur.append(line)
    if cur:
        blocks.append(cur)
    if not blocks and stdout.strip():
        return ["\n".join(lines)]
    return ["\n".join(b) for b in blocks]


def _parse_kv_block(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        k = key.strip().lower().replace(" ", "_")
        if not k:
            continue
        out[k] = rest.strip()
    return out


def _safe_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _safe_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _normalize_forward_hit(kv: dict[str, str]) -> dict[str, Any]:
    page = _safe_int(kv.get("sheet"))
    if page is None:
        out_val = kv.get("output")
        if out_val:
            m = re.search(r"pdfpage\s*=\s*(\d+)", out_val, re.I)
            if m:
                page = int(m.group(1))
    return {
        "page": page,
        "x": _safe_float(kv.get("x")),
        "y": _safe_float(kv.get("y")),
        "h": _safe_float(kv.get("h")),
        "v": _safe_float(kv.get("v")),
        "width": _safe_float(kv.get("width")),
        "height": _safe_float(kv.get("height")),
        "raw": kv,
    }


def _normalize_backward_hit(kv: dict[str, str], *, workspace_root: Path) -> dict[str, Any]:
    line = _safe_int(kv.get("line"))
    column = _safe_int(kv.get("column"))
    inp = kv.get("input") or kv.get("input_name") or ""
    rel: str | None = None
    if inp:
        raw_p = Path(inp)
        try:
            if raw_p.is_absolute():
                ip = Path(os.path.realpath(raw_p.expanduser()))
            else:
                ip = Path(os.path.realpath(workspace_root / raw_p))
            rel = ip.relative_to(workspace_root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = None
    hit: dict[str, Any] = {
        "input": inp,
        "line": line,
        "column": column,
        "relative_tex_path": rel,
        "raw": kv,
    }
    return hit


def _synctex_exe() -> str | None:
    return shutil.which("synctex")


def _coord_token(v: float | int) -> str:
    if isinstance(v, bool):  # pragma: no cover
        v = int(v)
    if isinstance(v, int):
        return str(v)
    text = format(float(v), "f").rstrip("0").rstrip(".")
    return text if text else "0"


def resolve_synctex_forward(
    workspace_root: str,
    relative_tex_path: str,
    line: int,
    *,
    column: int = 1,
    page_hint: int = 0,
    relative_pdf_path: str = "",
    synctex_directory: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``synctex view`` from TeX coordinates toward PDF."""
    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        tex_path = resolve_under_workspace(root, relative_tex_path)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if not tex_path.is_file():
        return {"ok": False, "error": "TeX path is not an existing file"}

    tex_rel = _workspace_relative(tex_path, root).replace("\\", "/")

    pdf_rel = relative_pdf_path.strip()
    if not pdf_rel:
        pdf_rel = str(Path(relative_tex_path).with_suffix(".pdf"))

    try:
        pdf_path = resolve_under_workspace(root, pdf_rel)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if not pdf_path.is_file():
        return {"ok": False, "error": f"PDF does not exist at resolved path ({pdf_rel}); pass relative_pdf_path explicitly"}

    synctex_dir_arg: list[str] = []
    if synctex_directory.strip():
        try:
            sd = resolve_under_workspace(root, synctex_directory.strip())
        except PathPolicyError as exc:
            return {"ok": False, "error": str(exc)}
        if not sd.is_dir():
            return {"ok": False, "error": "synctex_directory is not an existing directory"}
        synctex_dir_arg = ["-d", sd.as_posix()]

    exe = _synctex_exe()
    if not exe:
        return {
            "ok": False,
            "error": "synctex not found on PATH",
            "hint": "Install a TeX distribution that ships the synctex utility (e.g. TeX Live / MiKTeX).",
            "relative_tex_path": tex_rel,
        }

    ln = max(1, int(line))
    col = max(0, int(column))
    tex_abs_posix = Path(os.path.realpath(str(tex_path))).as_posix()
    pdf_abs_posix = Path(os.path.realpath(str(pdf_path))).as_posix()

    hint = max(0, int(page_hint))
    if hint > 0:
        input_spec = f"{ln}:{col}:{hint}:{tex_abs_posix}"
    else:
        input_spec = f"{ln}:{col}:{tex_abs_posix}"

    cmd = [exe, "view", "-i", input_spec, "-o", pdf_abs_posix, *synctex_dir_arg]

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
        return {"ok": False, "error": f"synctex timed out after {timeout_seconds}s", "relative_tex_path": tex_rel}

    stdout_txt = proc.stdout or ""
    stderr_txt = (proc.stderr or "").strip()
    out_stdout, truncated = _truncate_stdout(stdout_txt)

    hits: list[dict[str, Any]] = []
    for block in _split_result_blocks(stdout_txt):
        kv = _parse_kv_block(block)
        if kv:
            hits.append(_normalize_forward_hit(kv))

    ok = proc.returncode == 0

    payload: dict[str, Any] = {
        "ok": ok,
        "relative_tex_path": tex_rel,
        "relative_pdf_path": pdf_rel.replace("\\", "/"),
        "line": ln,
        "column": col,
        "page_hint": hint if hint > 0 else None,
        "hits": hits,
        "synctex_command": cmd,
        "synctex_stdout": out_stdout,
        "synctex_stdout_truncated": truncated,
        "synctex_stderr_tail": stderr_txt[-6000:] if stderr_txt else "",
        "synctex_exit_code": proc.returncode,
    }

    if not ok:
        payload["error"] = f"synctex view exited with code {proc.returncode}"

    return payload


def resolve_synctex_backward(
    workspace_root: str,
    relative_pdf_path: str,
    page: int,
    x: float,
    y: float,
    *,
    synctex_directory: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``synctex edit`` from PDF page coordinates toward TeX."""
    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        pdf_path = resolve_under_workspace(root, relative_pdf_path)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    if not pdf_path.is_file():
        return {"ok": False, "error": "PDF path is not an existing file"}

    pdf_rel = _workspace_relative(pdf_path, root).replace("\\", "/")

    synctex_dir_arg: list[str] = []
    if synctex_directory.strip():
        try:
            sd = resolve_under_workspace(root, synctex_directory.strip())
        except PathPolicyError as exc:
            return {"ok": False, "error": str(exc)}
        if not sd.is_dir():
            return {"ok": False, "error": "synctex_directory is not an existing directory"}
        synctex_dir_arg = ["-d", sd.as_posix()]

    exe = _synctex_exe()
    if not exe:
        return {
            "ok": False,
            "error": "synctex not found on PATH",
            "hint": "Install a TeX distribution that ships the synctex utility (e.g. TeX Live / MiKTeX).",
            "relative_pdf_path": pdf_rel,
        }

    pg = max(1, int(page))
    pdf_abs_posix = Path(os.path.realpath(str(pdf_path))).as_posix()
    edit_spec = f"{pg}:{_coord_token(x)}:{_coord_token(y)}:{pdf_abs_posix}"

    cmd = [exe, "edit", "-o", edit_spec, *synctex_dir_arg]

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
        return {"ok": False, "error": f"synctex timed out after {timeout_seconds}s", "relative_pdf_path": pdf_rel}

    stdout_txt = proc.stdout or ""
    stderr_txt = (proc.stderr or "").strip()
    out_stdout, truncated = _truncate_stdout(stdout_txt)

    hits: list[dict[str, Any]] = []
    for block in _split_result_blocks(stdout_txt):
        kv = _parse_kv_block(block)
        if kv:
            hits.append(_normalize_backward_hit(kv, workspace_root=root))

    ok = proc.returncode == 0

    payload: dict[str, Any] = {
        "ok": ok,
        "relative_pdf_path": pdf_rel,
        "page": pg,
        "x": float(x),
        "y": float(y),
        "hits": hits,
        "synctex_command": cmd,
        "synctex_stdout": out_stdout,
        "synctex_stdout_truncated": truncated,
        "synctex_stderr_tail": stderr_txt[-6000:] if stderr_txt else "",
        "synctex_exit_code": proc.returncode,
    }

    if not ok:
        payload["error"] = f"synctex edit exited with code {proc.returncode}"

    return payload
