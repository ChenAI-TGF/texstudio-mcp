"""LaTeX build (latexmk) and coarse log diagnostics."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from texstudio_mcp.path_policy import normalize_workspace_root, resolve_under_workspace

DEFAULT_TIMEOUT_SECONDS = 300
# Large logs are kept in memory until communicate() returns; we only expose short tails to the client.
COMPILE_STDOUT_TAIL_MAX = 12_000
COMPILE_STDERR_TAIL_MAX = 6_000
# `_compile_summary` must not scan megabyte-scale stdout (splitlines allocates heavily).
_SUMMARY_STDOUT_TAIL_CHARS = 98_304
_SUMMARY_STDERR_TAIL_CHARS = 16_384
MAX_STDOUT_CHARS = 120_000
MAX_STDERR_CHARS = 80_000
DEFAULT_LOG_TAIL_BYTES = 900_000
MAX_DIAGNOSTICS = 60

_compile_slot_lock = threading.Lock()
_compile_slots: dict[str, threading.Lock] = {}


def _compile_workspace_key(root_resolved: Path) -> str:
    return str(root_resolved.resolve())


def _try_begin_compile(workspace_key: str) -> bool:
    """Non-blocking: True if this call holds the compile slot for ``workspace_key``."""
    with _compile_slot_lock:
        if workspace_key not in _compile_slots:
            _compile_slots[workspace_key] = threading.Lock()
        slot = _compile_slots[workspace_key]
    return slot.acquire(blocking=False)


def _end_compile(workspace_key: str) -> None:
    with _compile_slot_lock:
        slot = _compile_slots.get(workspace_key)
    if slot is not None:
        try:
            slot.release()
        except RuntimeError:
            pass


# Shared across ``run_latexmk`` and bibliography runners in this process only.
WORKSPACE_EXCLUSIVE_BUSY_ERROR = (
    "another workspace-scoped build step is already running for this workspace_root "
    "(compile_latex_document or run_bibtex_on_job / run_biber_on_job) — wait for it to finish"
)


def try_begin_exclusive_workspace_command(root_normalized: Path) -> bool:
    """Non-blocking: True if this call holds the workspace exclusive slot."""
    return _try_begin_compile(_compile_workspace_key(root_normalized))


def end_exclusive_workspace_command(root_normalized: Path) -> None:
    """Release the workspace exclusive slot (safe if not held)."""
    _end_compile(_compile_workspace_key(root_normalized))


def _tail_for_summary(blob: str, max_chars: int) -> str:
    if not blob:
        return ""
    return blob if len(blob) <= max_chars else blob[-max_chars:]


def _compile_summary(
    *,
    success: bool,
    timed_out: bool,
    exit_code: int | None,
    stdout_txt: str,
    stderr_txt: str,
) -> str:
    """One-line human summary; only scans the *tail* of captured streams (cheap on huge logs)."""
    if timed_out:
        return "compile: timed out (latexmk did not finish within timeout_seconds)"
    if exit_code == 0 and success:
        status = "ok"
    else:
        status = "failed"

    out_tail = _tail_for_summary(stdout_txt or "", _SUMMARY_STDOUT_TAIL_CHARS)
    err_tail = _tail_for_summary(stderr_txt or "", _SUMMARY_STDERR_TAIL_CHARS)

    out_lines = out_tail.splitlines()
    for line in reversed(out_lines):
        t = line.strip()
        if not t:
            continue
        low = t.lower()
        if "latexmk:" in low or "nothing to do" in low or "up-to-date" in low or "all targets" in low:
            return f"compile {status}; exit={exit_code}; {t[:320]}"

    for line in reversed(err_tail.splitlines()):
        t = line.strip()
        if t:
            return f"compile {status}; exit={exit_code}; stderr: {t[:240]}"

    last = ""
    for line in reversed(out_lines):
        t = line.strip()
        if t:
            last = t
            break
    return f"compile {status}; exit={exit_code}; last_stdout={last[:240]}"


def _wait_latexmk_capturing_output(
    proc: subprocess.Popen[str],
    *,
    timeout_seconds: int,
) -> tuple[int | None, str, str, bool, bool]:
    """Wait for ``latexmk`` and return ``(exit_code, stdout, stderr, timed_out, process_killed)``.

    Uses temp files instead of ``PIPE`` so a chatty child cannot deadlock the parent on Windows.
    """
    timed_out = False
    process_killed = False
    exit_code: int | None = None
    stdout_txt = ""
    stderr_txt = ""

    try:
        exit_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        if proc.pid:
            _kill_process_tree(proc.pid)
            process_killed = True
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            pass

    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
    if proc.stderr is not None:
        try:
            proc.stderr.close()
        except OSError:
            pass

    cap = getattr(proc, "_texstudio_mcp_capture", None)
    if isinstance(cap, dict):
        for key, var in (("stdout", "stdout_txt"), ("stderr", "stderr_txt")):
            path = cap.get(key)
            if path and Path(path).is_file():
                try:
                    text = Path(path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                if key == "stdout":
                    stdout_txt = text
                else:
                    stderr_txt = text

    return exit_code, stdout_txt, stderr_txt, timed_out, process_killed


def _kill_process_tree(pid: int) -> None:
    """Terminate ``pid`` and (best effort) all children.

    On Windows ``latexmk`` typically spawns ``perl`` + ``pdflatex``; ``subprocess.run(timeout=…)``
    only kills the top process unless we traverse the tree. ``taskkill /T`` handles this.
    """
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


_FILE_LINE_RX = re.compile(
    r"^(.+\.(?:tex|bib|sty|cls|bst|clo|def)):(\d+):",
    re.IGNORECASE,
)


def run_latexmk(
    workspace_root: str,
    main_tex: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    latexmk_extra_args: str = "",
    use_latexmk_cd: bool = True,
    hold_exclusive_workspace_slot: bool = True,
) -> dict[str, Any]:
    """Run ``latexmk -pdf`` with cwd ``workspace_root`` and ``main_tex`` relative inside it.

    ``use_latexmk_cd`` (default True) adds ``-cd`` when ``workspace_root`` is **above** the
    directory that holds ``main_tex`` (e.g. repo root + ``styles/manuscript.tex``). When
    ``workspace_root`` **is** that directory, ``-cd`` is **omitted** automatically and only
    the ``.tex`` basename is passed—avoiding redundant ``-cd`` that can hang on some setups.

    When ``hold_exclusive_workspace_slot`` is false (internal orchestration only), skips workspace
    slot acquire/release; the caller must already hold ``try_begin_exclusive_workspace_command``.
    """
    root = normalize_workspace_root(workspace_root)
    main_path = resolve_under_workspace(root, main_tex)

    if not main_path.exists():
        return {"ok": False, "error": "main_tex path does not exist"}
    if not main_path.is_file():
        return {"ok": False, "error": "main_tex is not a regular file"}

    lm = shutil.which("latexmk")
    if not lm:
        return {"ok": False, "error": "latexmk not found on PATH"}

    try:
        rel_main = main_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return {"ok": False, "error": "main_tex must live under workspace_root"}

    root_resolved = root.resolve()
    main_dir = main_path.parent.resolve()
    workspace_is_main_dir = root_resolved == main_dir

    # If cwd is already the directory that contains the primary .tex, applying `latexmk -cd`
    # again can confuse latexmk (path stacking / blocking) — so run only the basename.
    if workspace_is_main_dir:
        effective_use_cd = False
        latexmk_tex_arg = main_path.name
        cd_omit_reason = "workspace_root is the directory of main_tex"
    else:
        effective_use_cd = use_latexmk_cd
        latexmk_tex_arg = rel_main
        cd_omit_reason = None

    extras: list[str] = []
    if latexmk_extra_args.strip():
        extras = shlex.split(latexmk_extra_args, posix=os.name != "nt")

    cmd: list[str] = [lm]
    if effective_use_cd:
        cmd.append("-cd")
    cmd.extend(
        [
            "-pdf",
            "-interaction=nonstopmode",
            "-file-line-error",
            latexmk_tex_arg,
            *extras,
        ]
    )

    locked_exclusive = False
    if hold_exclusive_workspace_slot:
        if not try_begin_exclusive_workspace_command(root):
            return {
                "ok": False,
                "error": WORKSPACE_EXCLUSIVE_BUSY_ERROR,
                "concurrent_compile_blocked": True,
                "concurrent_workspace_exclusive_blocked": True,
                "summary": "compile skipped: workspace busy (exclusive slot held)",
            }
        locked_exclusive = True

    timed_out = False
    process_killed = False
    exit_code: int | None = None
    stdout_txt = ""
    stderr_txt = ""

    wall_ms = 0
    wall_start = time.perf_counter()
    proc: subprocess.Popen[str] | None = None
    cap_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        cap_dir = tempfile.TemporaryDirectory(prefix="texstudio_mcp_latexmk_")
        cap_root = Path(cap_dir.name)
        stdout_path = cap_root / "stdout.txt"
        stderr_path = cap_root / "stderr.txt"
        so = open(stdout_path, "w", encoding="utf-8", errors="replace")
        se = open(stderr_path, "w", encoding="utf-8", errors="replace")
        try:
            popen_kw: dict[str, Any] = {
                "cwd": str(root),
                "stdout": so,
                "stderr": se,
                "stdin": subprocess.DEVNULL,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name != "nt":
                popen_kw["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kw)
            proc._texstudio_mcp_capture = {  # type: ignore[attr-defined]
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            }
            exit_code, stdout_txt, stderr_txt, timed_out, process_killed = _wait_latexmk_capturing_output(
                proc,
                timeout_seconds=timeout_seconds,
            )
        finally:
            so.close()
            se.close()
        wall_ms = int((time.perf_counter() - wall_start) * 1000)

        def tail(s: str, lim: int) -> str:
            return s if len(s) <= lim else s[-lim:]

        so_raw = stdout_txt or ""
        se_raw = stderr_txt or ""
        so_tail = tail(so_raw, COMPILE_STDOUT_TAIL_MAX)
        se_tail = tail(se_raw, COMPILE_STDERR_TAIL_MAX)

        # Timeout wins over exit 0: pdflatex children may finish while perl/latexmk hung
        success = (exit_code == 0) and (not timed_out)

        summary = _compile_summary(
            success=success,
            timed_out=timed_out,
            exit_code=exit_code,
            stdout_txt=so_raw,
            stderr_txt=se_raw,
        )

        out: dict[str, Any] = {
            "ok": success,
            "summary": summary,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "process_killed": process_killed,
            "timeout_seconds": timeout_seconds,
            "wall_clock_ms": wall_ms,
            "command": cmd,
            "cwd": str(root),
            "main_tex": rel_main,
            "latexmk_main_argument": latexmk_tex_arg,
            "workspace_is_main_tex_dir": workspace_is_main_dir,
            "use_latexmk_cd_requested": use_latexmk_cd,
            "effective_use_latexmk_cd": effective_use_cd,
            "cd_omit_reason": cd_omit_reason,
            "stdout_tail": so_tail,
            "stderr_tail": se_tail,
            "stdout_truncated": len(so_raw) > COMPILE_STDOUT_TAIL_MAX,
            "stderr_truncated": len(se_raw) > COMPILE_STDERR_TAIL_MAX,
        }
        if timed_out:
            out["error"] = (
                f"latexmk exceeded timeout_seconds={timeout_seconds} "
                f"(process tree terminated={process_killed})"
            )
        elif exit_code not in (0, None):
            out["error"] = f"latexmk exited with code {exit_code}"
        return out
    finally:
        if locked_exclusive:
            end_exclusive_workspace_command(root)


def read_log_tail_bytes(path: Path, max_tail_bytes: int) -> str:
    raw = path.read_bytes()
    if len(raw) <= max_tail_bytes:
        return raw.decode("utf-8", errors="replace")
    chunk = raw[-max_tail_bytes:]
    return chunk.decode("utf-8", errors="replace")


def extract_latex_diagnostics(log_text: str) -> list[dict[str, Any]]:
    """Best-effort extraction of errors/warnings from LaTeX log tail."""
    diagnostics: list[dict[str, Any]] = []
    lines = log_text.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        m = _FILE_LINE_RX.match(stripped)
        if m:
            diagnostics.append(
                {
                    "kind": "file_line",
                    "file": m.group(1).strip(),
                    "line": int(m.group(2)),
                    "text": stripped[:2000],
                }
            )
            continue

        if stripped.startswith("! "):
            block = "\n".join(lines[i : min(len(lines), i + 12)])
            diagnostics.append({"kind": "latex_error", "summary": stripped[:500], "context": block[:6000]})
            continue

        if "Fatal error" in stripped or stripped.startswith("Emergency stop"):
            diagnostics.append({"kind": "fatal", "summary": stripped[:500], "context": stripped[:2000]})
            continue

        if stripped.startswith("Package ") and "Error" in stripped:
            diagnostics.append({"kind": "package_error", "summary": stripped[:500], "context": stripped[:2000]})
            continue

        if stripped.startswith("LaTeX Warning:") or (
            stripped.startswith("Package ") and "Warning" in stripped
        ):
            diagnostics.append({"kind": "warning", "summary": stripped[:500], "context": stripped[:2000]})

    # De-dupe summaries while preserving order
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []

    def dedupe_key(d: dict[str, Any]) -> tuple[str, str]:
        kind = d["kind"]
        if kind == "file_line":
            return kind, f"{d['file']}:{d['line']}:{d['text'][:120]}"
        return kind, str(d.get("summary", ""))[:200]

    for d in diagnostics:
        key = dedupe_key(d)
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)
        if len(unique) >= MAX_DIAGNOSTICS:
            break

    return unique


def analyze_latex_log_file(
    workspace_root: str,
    relative_log_path: str,
    *,
    tail_max_bytes: int = DEFAULT_LOG_TAIL_BYTES,
) -> dict[str, Any]:
    """Read the tail of a ``.log`` inside workspace_root and extract coarse diagnostics."""
    root = normalize_workspace_root(workspace_root)
    log_path = resolve_under_workspace(root, relative_log_path)

    if not log_path.exists():
        return {"ok": False, "error": "log path does not exist"}
    if not log_path.is_file():
        return {"ok": False, "error": "log path is not a regular file"}

    try:
        text = read_log_tail_bytes(log_path, tail_max_bytes)
    except OSError as exc:
        return {"ok": False, "error": f"cannot read log: {exc}"}

    diagnostics = extract_latex_diagnostics(text)
    st = log_path.stat()

    return {
        "ok": True,
        "relative_log_path": log_path.relative_to(root.resolve()).as_posix(),
        "log_tail_bytes_read": min(tail_max_bytes, st.st_size),
        "log_truncated": st.st_size > tail_max_bytes,
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
    }
