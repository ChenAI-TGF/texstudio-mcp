"""Static lint for LaTeX sources via ``chktex`` (when installed)."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from typing import Any

from texstudio_mcp.path_policy import normalize_workspace_root, resolve_under_workspace
from texstudio_mcp.workspace_fs import list_latex_related_files

DEFAULT_TIMEOUT_SECONDS = 120
STDOUT_TAIL_MAX = 24_000
STDERR_TAIL_MAX = 12_000
SUMMARY_TAIL_CHARS = 32_768
MAX_WARNINGS_PARSED = 200
DEFAULT_MAX_BATCH_TEX_FILES = 40
BATCH_STDOUT_TAIL_MAX = 6_144
BATCH_STDERR_TAIL_MAX = 3_072


def _kill_process_tree(pid: int) -> None:
    """Terminate ``pid`` and (best effort) all children (mirror of compile helper)."""
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        return
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


_WARNING_LINE_RX = re.compile(
    r"^Warning\s+(\d+)\s+in\s+(.+?)\s+line\s+(\d+):\s*(.*)$",
    re.IGNORECASE,
)


def _tail_str(s: str, lim: int) -> str:
    return s if len(s) <= lim else s[-lim:]


def _lint_summary(stdout_txt: str, stderr_txt: str, *, timed_out: bool, exit_code: int | None, n_warn: int) -> str:
    if timed_out:
        return "chktex: timed out (did not finish within timeout_seconds)"
    if n_warn > 0:
        return f"chktex: found {n_warn} warning line(s); exit_code={exit_code}"
    tail = _tail_for_parse(stdout_txt, SUMMARY_TAIL_CHARS)
    for line in reversed(tail.splitlines()):
        t = line.strip()
        if t:
            low = t.lower()
            if "warning" in low or "no errors printed" in low or "no warnings" in low:
                return f"chktex: exit={exit_code}; {t[:280]}"
    for line in reversed(_tail_for_parse(stderr_txt, SUMMARY_TAIL_CHARS).splitlines()):
        t = line.strip()
        if t:
            return f"chktex: exit={exit_code}; stderr: {t[:220]}"
    return f"chktex: completed; exit_code={exit_code}; warnings parsed={n_warn}"


def _tail_for_parse(blob: str, max_chars: int) -> str:
    if not blob:
        return ""
    return blob if len(blob) <= max_chars else blob[-max_chars:]


def parse_chktex_warnings(text: str, *, limit: int = MAX_WARNINGS_PARSED) -> list[dict[str, Any]]:
    """Extract ChkTeX ``Warning … in … line …:`` rows from stdout (best-effort)."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _WARNING_LINE_RX.match(line.strip())
        if not m:
            continue
        out.append(
            {
                "chktex_code": int(m.group(1)),
                "file": m.group(2).strip(),
                "line": int(m.group(3)),
                "message": (m.group(4) or "").strip()[:2000],
                "text": line.strip()[:2400],
            }
        )
        if len(out) >= limit:
            break
    return out


def _dedupe_nonempty_tex_relative_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        if not isinstance(raw, str):
            continue
        t = raw.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _compact_for_batch_payload(
    raw: dict[str, Any],
    *,
    warnings_limit_per_file: int,
) -> dict[str, Any]:
    """Drop heavy keys from ``run_chktex`` result for batch responses."""
    warnings = raw.get("warnings") or []
    capped = warnings[:warnings_limit_per_file]
    stdout_src = raw.get("stdout_tail") if isinstance(raw.get("stdout_tail"), str) else ""
    stderr_src = raw.get("stderr_tail") if isinstance(raw.get("stderr_tail"), str) else ""

    compact: dict[str, Any] = {
        "relative_tex_path": raw.get("relative_tex_path", ""),
        "ok": bool(raw.get("ok")),
        "summary": raw.get("summary"),
        "warning_count": int(raw.get("warning_count") or 0),
        "warnings": capped,
        "warnings_truncated": len(warnings) > warnings_limit_per_file,
        "exit_code": raw.get("exit_code"),
        "timed_out": bool(raw.get("timed_out")),
        "wall_clock_ms": raw.get("wall_clock_ms"),
        "stdout_tail": _tail_str(stdout_src, BATCH_STDOUT_TAIL_MAX),
        "stderr_tail": _tail_str(stderr_src, BATCH_STDERR_TAIL_MAX),
        "stdout_truncated": len(stdout_src) > BATCH_STDOUT_TAIL_MAX,
        "stderr_truncated": len(stderr_src) > BATCH_STDERR_TAIL_MAX,
    }
    if raw.get("error"):
        compact["error"] = raw["error"]
    return compact


def run_chktex(
    workspace_root: str,
    relative_tex_path: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    chktex_extra_args: str = "",
) -> dict[str, Any]:
    """Run ``chktex`` with ``cwd`` = ``workspace_root`` and the resolved ``.tex`` as a relative argument."""
    root = normalize_workspace_root(workspace_root)
    stripped_rel = relative_tex_path.strip()
    tex_path = resolve_under_workspace(root, relative_tex_path)

    if not tex_path.exists():
        return {"ok": False, "error": "tex path does not exist", "relative_tex_path": stripped_rel}
    if not tex_path.is_file():
        return {"ok": False, "error": "tex path is not a regular file", "relative_tex_path": stripped_rel}
    if tex_path.suffix.lower() != ".tex":
        return {"ok": False, "error": "relative_tex_path must be a .tex file", "relative_tex_path": stripped_rel}

    try:
        rel_tex = tex_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return {"ok": False, "error": "tex path must live under workspace_root", "relative_tex_path": stripped_rel}

    exe = shutil.which("chktex")
    if not exe:
        return {"ok": False, "error": "chktex not found on PATH", "relative_tex_path": rel_tex}

    extras: list[str] = []
    if chktex_extra_args.strip():
        extras = shlex.split(chktex_extra_args, posix=os.name != "nt")

    cmd: list[str] = [exe, "-v0", rel_tex, *extras]

    popen_kw: dict[str, Any] = {
        "cwd": str(root),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name != "nt":
        popen_kw["start_new_session"] = True

    timed_out = False
    process_killed = False
    exit_code: int | None = None
    stdout_txt = ""
    stderr_txt = ""

    wall_start = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, **popen_kw)
    try:
        try:
            stdout_txt, stderr_txt = proc.communicate(timeout=timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            if proc.pid:
                _kill_process_tree(proc.pid)
                process_killed = True
                try:
                    stdout_txt, stderr_txt = proc.communicate(timeout=30)
                except Exception:  # noqa: BLE001
                    stdout_txt = ""
                    stderr_txt = ""
    finally:
        wall_ms = int((time.perf_counter() - wall_start) * 1000)

    so_raw = stdout_txt or ""
    se_raw = stderr_txt or ""
    parsed = parse_chktex_warnings(so_raw)

    lint_clean = exit_code == 0 and not timed_out
    summary = _lint_summary(so_raw, se_raw, timed_out=timed_out, exit_code=exit_code, n_warn=len(parsed))

    out: dict[str, Any] = {
        "ok": lint_clean,
        "summary": summary,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "process_killed": process_killed,
        "timeout_seconds": timeout_seconds,
        "wall_clock_ms": wall_ms,
        "command": cmd,
        "cwd": str(root),
        "relative_tex_path": rel_tex,
        "warning_count": len(parsed),
        "warnings": parsed if parsed else [],
        "stdout_tail": _tail_str(so_raw, STDOUT_TAIL_MAX),
        "stderr_tail": _tail_str(se_raw, STDERR_TAIL_MAX),
        "stdout_truncated": len(so_raw) > STDOUT_TAIL_MAX,
        "stderr_truncated": len(se_raw) > STDERR_TAIL_MAX,
    }
    if timed_out:
        out["error"] = (
            f"chktex exceeded timeout_seconds={timeout_seconds} "
            f"(process tree terminated={process_killed})"
        )
    elif exit_code not in (0, None):
        out["error"] = f"chktex exited with code {exit_code} (typically non-zero when issues were reported)"
    return out


def batch_run_chktex(
    workspace_root: str,
    relative_tex_paths: list[str],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_tex_files: int = DEFAULT_MAX_BATCH_TEX_FILES,
    chktex_extra_args: str = "",
    warnings_limit_per_file: int = 40,
) -> dict[str, Any]:
    """Invoke :func:`run_chktex` once per sandboxed relative path (sequentially)."""
    root = normalize_workspace_root(workspace_root)
    root_str = str(root.resolve())

    cap_paths = _clamp_int(int(max_tex_files), 1, 120)
    warn_cap = _clamp_int(int(warnings_limit_per_file), 0, MAX_WARNINGS_PARSED)
    uniq = _dedupe_nonempty_tex_relative_paths(relative_tex_paths)

    if not uniq:
        return {
            "ok": False,
            "summary": "batch chktex: no usable relative_tex_paths entries",
            "error": "relative_tex_paths is empty after stripping / dedupe",
            "resolved_workspace_root": root_str,
            "relative_tex_paths_used": [],
            "file_count": 0,
            "clean_count": 0,
            "timed_out_count": 0,
            "total_wall_clock_ms": 0,
            "timeout_seconds_per_file": timeout_seconds,
            "max_tex_files": cap_paths,
            "warnings_limit_per_file": warn_cap,
            "results": [],
        }

    if len(uniq) > cap_paths:
        return {
            "ok": False,
            "summary": f"batch chktex: too many files ({len(uniq)} > max_tex_files={cap_paths})",
            "error": f"provide at most max_tex_files={cap_paths} distinct paths after dedupe",
            "resolved_workspace_root": root_str,
            "relative_tex_paths_requested": uniq,
            "path_count_requested": len(uniq),
            "timeout_seconds_per_file": timeout_seconds,
            "max_tex_files": cap_paths,
            "warnings_limit_per_file": warn_cap,
            "results": [],
        }

    exe = shutil.which("chktex")
    if not exe:
        return {
            "ok": False,
            "summary": "batch chktex: chktex executable not on PATH",
            "error": "chktex not found on PATH",
            "resolved_workspace_root": root_str,
            "relative_tex_paths_used": uniq,
            "file_count": len(uniq),
            "clean_count": 0,
            "timed_out_count": 0,
            "total_wall_clock_ms": 0,
            "timeout_seconds_per_file": timeout_seconds,
            "max_tex_files": cap_paths,
            "warnings_limit_per_file": warn_cap,
            "results": [],
        }

    results: list[dict[str, Any]] = []
    total_wall_ms = 0
    timed_out_count = 0
    batch_clean = True

    for rp in uniq:
        raw = run_chktex(
            workspace_root,
            rp,
            timeout_seconds=timeout_seconds,
            chktex_extra_args=chktex_extra_args,
        )
        total_wall_ms += int(raw.get("wall_clock_ms") or 0)
        if raw.get("timed_out"):
            timed_out_count += 1
            batch_clean = False
        if raw.get("ok") is not True:
            batch_clean = False
        results.append(_compact_for_batch_payload(raw, warnings_limit_per_file=warn_cap))

    clean_count = sum(1 for r in results if r.get("ok") is True)

    tout_part = ""
    if timed_out_count > 0:
        tout_part = f"; timed_out_files={timed_out_count}"
    headline = f"batch chktex: {clean_count}/{len(results)} files clean (exit code 0)" + tout_part

    payload: dict[str, Any] = {
        "ok": batch_clean,
        "summary": headline,
        "resolved_workspace_root": root_str,
        "relative_tex_paths_used": uniq,
        "file_count": len(uniq),
        "clean_count": clean_count,
        "timed_out_count": timed_out_count,
        "total_wall_clock_ms": total_wall_ms,
        "timeout_seconds_per_file": timeout_seconds,
        "max_tex_files": cap_paths,
        "warnings_limit_per_file": warn_cap,
        "chktex_path": exe,
        "results": results,
    }
    if not batch_clean:
        payload["batch_error"] = (
            "one or more files failed "
            "(non-zero exit, timeout, invalid path, missing .tex, or chktex not found for that invocation)"
        )
    return payload


def run_chktex_on_workspace(
    workspace_root: str,
    *,
    max_depth: int = 12,
    extra_extensions: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_tex_files: int = DEFAULT_MAX_BATCH_TEX_FILES,
    chktex_extra_args: str = "",
    warnings_limit_per_file: int = 40,
) -> dict[str, Any]:
    """Enumerate ``*.tex`` (same skips/depth semantics as listing tool) then :func:`batch_run_chktex`."""
    depth_cap = _clamp_int(int(max_depth), 1, 48)
    listed = list_latex_related_files(workspace_root, max_depth=depth_cap, extra_extensions=extra_extensions)

    root = normalize_workspace_root(workspace_root)
    root_str = str(root.resolve())
    cap_paths = _clamp_int(int(max_tex_files), 1, 120)
    warn_cap = _clamp_int(int(warnings_limit_per_file), 0, MAX_WARNINGS_PARSED)

    paths_raw = listed.get("files") or []
    tex_only = [p for p in paths_raw if isinstance(p, str) and p.lower().endswith(".tex")]
    discovered = len(tex_only)
    truncated = discovered > cap_paths
    selected = tex_only[:cap_paths]

    meta: dict[str, Any] = {
        "resolved_workspace_root": root_str,
        "discovered_tex_count": discovered,
        "scanned_tex_count": len(selected),
        "paths_truncated": truncated,
        "listing_max_depth_effective": depth_cap,
        "listing_extra_extensions": extra_extensions,
        "truncated_unused_tex_count": max(0, discovered - len(selected)),
        "skipped_chktex": len(selected) == 0,
    }

    if not selected:
        return {
            "ok": True,
            "summary": "workspace chktex: listing found no `.tex` files matching filters",
            "timeout_seconds_per_file": timeout_seconds,
            "max_tex_files": cap_paths,
            "warnings_limit_per_file": warn_cap,
            **meta,
            "relative_tex_paths_used": [],
            "file_count": 0,
            "clean_count": 0,
            "timed_out_count": 0,
            "total_wall_clock_ms": 0,
            "results": [],
        }

    merged = batch_run_chktex(
        workspace_root,
        selected,
        timeout_seconds=timeout_seconds,
        max_tex_files=cap_paths,
        chktex_extra_args=chktex_extra_args,
        warnings_limit_per_file=warnings_limit_per_file,
    )
    tail = merged.get("summary") or ""
    if truncated:
        prefix = (
            f"workspace chktex: {discovered} `.tex` in listing; scanned first {len(selected)} sorted paths "
            f"({meta['truncated_unused_tex_count']} not run — increase max_tex_files)"
        )
    else:
        prefix = f"workspace chktex: {discovered} `.tex` in listing;"
    merged["summary"] = f"{prefix} {tail}"

    merged.update(meta)
    return merged
