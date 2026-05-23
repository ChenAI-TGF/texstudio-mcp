"""Sandboxed bibliography backend runners: ``bibtex`` and ``biber`` (Batch 2)."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from texstudio_mcp.latex_compile import (
    WORKSPACE_EXCLUSIVE_BUSY_ERROR,
    end_exclusive_workspace_command,
    try_begin_exclusive_workspace_command,
)
from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace

DEFAULT_TIMEOUT_SECONDS = 120.0
STDOUT_TAIL_MAX = 8_192
STDERR_TAIL_MAX = 4_096
MAX_EXTRA_ARGS = 16
_MAX_TOKEN_LEN = 256


_JOB_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def _validate_job_name(job_name: str) -> tuple[str | None, str | None]:
    raw = job_name.strip()
    if not raw:
        return None, "job_name must be non-empty"
    if ".." in raw or "/" in raw or "\\" in raw:
        return None, "job_name must not contain path separators or .."
    if not _JOB_NAME_OK.match(raw):
        return (
            None,
            "job_name must be 1..200 chars, start alnum, "
            "and use only ASCII letters/digits/./_/- ",
        )
    return raw, None


def _parse_extra_args(fragment: str) -> tuple[list[str] | None, str | None]:
    if not fragment.strip():
        return [], None
    try:
        parts = shlex.split(fragment.strip(), posix=os.name != "nt")
    except ValueError as exc:
        return None, f"invalid extra_args fragment: {exc}"
    if len(parts) > MAX_EXTRA_ARGS:
        return None, f"extra_args expands to too many tokens (max {MAX_EXTRA_ARGS})"
    for tok in parts:
        if len(tok) > _MAX_TOKEN_LEN:
            return None, f"extra_args token exceeds max length ({_MAX_TOKEN_LEN})"
        if tok.startswith("@"):
            return None, "extra_args tokens must not start with '@'"
    return parts, None


def _resolve_work_dir(root: Path, relative_working_directory: str) -> tuple[Path | None, str | None]:
    rel = relative_working_directory.strip() or "."
    try:
        wd = resolve_under_workspace(root, rel)
    except PathPolicyError as exc:
        return None, str(exc)
    try:
        wd.resolve().relative_to(root.resolve())
    except ValueError:
        return None, "working directory must stay under workspace_root"
    if not wd.is_dir():
        return None, "relative_working_directory is not a directory"
    return wd, None


def _tails(stdout: str, stderr: str) -> dict[str, Any]:
    out_trunc = len(stdout) > STDOUT_TAIL_MAX
    err_trunc = len(stderr) > STDERR_TAIL_MAX
    return {
        "stdout_tail": stdout if not out_trunc else stdout[-STDOUT_TAIL_MAX:],
        "stderr_tail": stderr if not err_trunc else stderr[-STDERR_TAIL_MAX:],
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
    }


def _one_line_summary(*, exe: str, timed_out: bool, exit_code: int | None, stderr_txt: str) -> str:
    if timed_out:
        return f"{exe}: timed out before completion"
    if exit_code == 0:
        return f"{exe}: exited 0"
    tail = (stderr_txt or "").strip().splitlines()
    hint = ""
    if tail:
        hint = tail[-1].strip()[:240]
        return f"{exe}: exit={exit_code}; {hint}"
    return f"{exe}: exit={exit_code}"


def run_bibtex_on_job(
    workspace_root: str,
    job_name: str,
    *,
    relative_working_directory: str = ".",
    preflight_checks: bool = True,
    extra_args: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    hold_exclusive_workspace_slot: bool = True,
) -> dict[str, Any]:
    """Run ``bibtex JOB`` with ``cwd`` = ``relative_working_directory`` under ``workspace_root``.

    ``job_name`` is the base name only (no ``.aux``). When ``preflight_checks`` is true, requires
    ``JOB.aux`` to exist under that directory before invoking ``bibtex``.

    When ``hold_exclusive_workspace_slot`` is false (orchestration), the caller must already hold the workspace slot.
    """
    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    jn, jerr = _validate_job_name(job_name)
    if jerr:
        return {"ok": False, "error": jerr}

    wd, werr = _resolve_work_dir(root, relative_working_directory)
    if werr:
        return {"ok": False, "error": werr}
    assert wd is not None

    extras, eerr = _parse_extra_args(extra_args)
    if eerr:
        return {"ok": False, "error": eerr}
    assert extras is not None

    aux_path = wd / f"{jn}.aux"
    if preflight_checks and not aux_path.is_file():
        return {
            "ok": False,
            "error": f"missing expected file {jn}.aux under relative_working_directory (run pdflatex first or set preflight_checks=false)",
            "relative_working_directory": wd.resolve().relative_to(root.resolve()).as_posix(),
            "job_name": jn,
        }

    exe = shutil.which("bibtex")
    if not exe:
        return {
            "ok": False,
            "error": "bibtex not found on PATH",
            "hint": "Install TeX Live / MiKTeX (or equivalent) so bibtex is available.",
            "job_name": jn,
        }

    locked_exclusive = False
    if hold_exclusive_workspace_slot:
        if not try_begin_exclusive_workspace_command(root):
            return {
                "ok": False,
                "error": WORKSPACE_EXCLUSIVE_BUSY_ERROR,
                "concurrent_compile_blocked": True,
                "concurrent_workspace_exclusive_blocked": True,
                "summary": "bibtex skipped: workspace busy (exclusive slot held)",
                "job_name": jn,
            }
        locked_exclusive = True

    cmd: list[str] = [exe, *extras, jn]
    rel_wd = wd.resolve().relative_to(root.resolve()).as_posix()

    t0 = time.perf_counter()
    try:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(wd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=float(timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired:
            ms = int((time.perf_counter() - t0) * 1000)
            return {
                "ok": False,
                "error": f"bibtex timed out after {timeout_seconds}s",
                "timed_out": True,
                "bibtex_path": exe,
                "job_name": jn,
                "relative_working_directory": rel_wd,
                "wall_clock_ms": ms,
            }
        except OSError as exc:
            return {
                "ok": False,
                "error": f"cannot execute bibtex: {exc}",
                "bibtex_path": exe,
                "job_name": jn,
                "relative_working_directory": rel_wd,
            }

        wall = int((time.perf_counter() - t0) * 1000)
        out_t = _tails(proc.stdout or "", proc.stderr or "")
        ok = proc.returncode == 0
        blg_path_obj = wd / f"{jn}.blg"
        blg_rel = blg_path_obj.resolve().relative_to(root.resolve()).as_posix()
        payload: dict[str, Any] = {
            "ok": ok,
            "bibtex_path": exe,
            "job_name": jn,
            "relative_working_directory": rel_wd,
            "exit_code": proc.returncode,
            "timed_out": False,
            "wall_clock_ms": wall,
            "blg_found": blg_path_obj.is_file(),
            "blg_relative_path": blg_rel,
            "summary": _one_line_summary(
                exe="bibtex",
                timed_out=False,
                exit_code=proc.returncode,
                stderr_txt=proc.stderr or "",
            ),
            **out_t,
        }
        if not ok:
            payload.setdefault("error", f"bibtex exited with code {proc.returncode}")
        return payload
    finally:
        if locked_exclusive:
            end_exclusive_workspace_command(root)


def run_biber_on_job(
    workspace_root: str,
    job_name: str,
    *,
    relative_working_directory: str = ".",
    preflight_checks: bool = True,
    extra_args: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    hold_exclusive_workspace_slot: bool = True,
) -> dict[str, Any]:
    """Run ``biber JOB`` with ``cwd`` = ``relative_working_directory`` under ``workspace_root``.

    ``job_name`` is the base name only (no ``.bcf``). When ``preflight_checks`` is true, requires
    ``JOB.bcf`` to exist under that directory before invoking ``biber``.

    When ``hold_exclusive_workspace_slot`` is false (orchestration), the caller must already hold the workspace slot.
    """
    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}

    jn, jerr = _validate_job_name(job_name)
    if jerr:
        return {"ok": False, "error": jerr}

    wd, werr = _resolve_work_dir(root, relative_working_directory)
    if werr:
        return {"ok": False, "error": werr}
    assert wd is not None

    extras, eerr = _parse_extra_args(extra_args)
    if eerr:
        return {"ok": False, "error": eerr}
    assert extras is not None

    bcf_path = wd / f"{jn}.bcf"
    if preflight_checks and not bcf_path.is_file():
        return {
            "ok": False,
            "error": (
                f"missing expected file {jn}.bcf under relative_working_directory "
                "(run pdflatex/biber chain first or set preflight_checks=false)"
            ),
            "relative_working_directory": wd.resolve().relative_to(root.resolve()).as_posix(),
            "job_name": jn,
        }

    exe = shutil.which("biber")
    if not exe:
        return {
            "ok": False,
            "error": "biber not found on PATH",
            "hint": "Install a TeX distribution with biber (biblatex backend) enabled.",
            "job_name": jn,
        }

    locked_exclusive = False
    if hold_exclusive_workspace_slot:
        if not try_begin_exclusive_workspace_command(root):
            return {
                "ok": False,
                "error": WORKSPACE_EXCLUSIVE_BUSY_ERROR,
                "concurrent_compile_blocked": True,
                "concurrent_workspace_exclusive_blocked": True,
                "summary": "biber skipped: workspace busy (exclusive slot held)",
                "job_name": jn,
            }
        locked_exclusive = True

    cmd: list[str] = [exe, *extras, jn]
    rel_wd = wd.resolve().relative_to(root.resolve()).as_posix()

    t0 = time.perf_counter()
    try:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(wd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=float(timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired:
            ms = int((time.perf_counter() - t0) * 1000)
            return {
                "ok": False,
                "error": f"biber timed out after {timeout_seconds}s",
                "timed_out": True,
                "biber_path": exe,
                "job_name": jn,
                "relative_working_directory": rel_wd,
                "wall_clock_ms": ms,
            }
        except OSError as exc:
            return {
                "ok": False,
                "error": f"cannot execute biber: {exc}",
                "biber_path": exe,
                "job_name": jn,
                "relative_working_directory": rel_wd,
            }

        wall = int((time.perf_counter() - t0) * 1000)
        out_t = _tails(proc.stdout or "", proc.stderr or "")
        ok = proc.returncode == 0
        blg_path_obj = wd / f"{jn}.blg"
        blg_rel = blg_path_obj.resolve().relative_to(root.resolve()).as_posix()
        payload: dict[str, Any] = {
            "ok": ok,
            "biber_path": exe,
            "job_name": jn,
            "relative_working_directory": rel_wd,
            "exit_code": proc.returncode,
            "timed_out": False,
            "wall_clock_ms": wall,
            "blg_found": blg_path_obj.is_file(),
            "blg_relative_path": blg_rel,
            "summary": _one_line_summary(
                exe="biber",
                timed_out=False,
                exit_code=proc.returncode,
                stderr_txt=proc.stderr or "",
            ),
            **out_t,
        }
        if not ok:
            payload.setdefault("error", f"biber exited with code {proc.returncode}")
        return payload
    finally:
        if locked_exclusive:
            end_exclusive_workspace_command(root)
