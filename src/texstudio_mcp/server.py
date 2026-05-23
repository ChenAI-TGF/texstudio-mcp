"""MCP server: stdio transport."""

from __future__ import annotations

import platform
import shutil
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root, resolve_under_workspace
from texstudio_mcp.bib_validate import validate_bib_file as validate_bib_file_fs
from texstudio_mcp.pdf_metadata import read_pdf_metadata as read_pdf_metadata_fs
from texstudio_mcp.pdf_text_preview import extract_pdf_text_preview as extract_pdf_text_preview_fs
from texstudio_mcp.synctex_resolve import (
    resolve_synctex_backward as synctex_backward_fs,
    resolve_synctex_forward as synctex_forward_fs,
)
from texstudio_mcp.latex_biblog import analyze_bibliography_log as analyze_bibliography_log_fs
from texstudio_mcp.latex_bib_hints import guess_job_bibliography_backend as guess_job_bibliography_backend_fs
from texstudio_mcp.latex_bib_runner import (
    run_biber_on_job as run_biber_on_job_impl,
    run_bibtex_on_job as run_bibtex_on_job_impl,
)
from texstudio_mcp.latex_compile import analyze_latex_log_file, run_latexmk
from texstudio_mcp.latex_compile_bib_pipeline import (
    compile_latex_then_run_bibliography_on_job as compile_latex_then_run_bibliography_on_job_fs,
)
from texstudio_mcp.latex_chktex import (
    batch_run_chktex as batch_run_chktex_fs,
    run_chktex as run_chktex_fs,
    run_chktex_on_workspace as run_workspace_chktex_fs,
)
from texstudio_mcp.texstudio_profile_read import read_texstudio_profile_snapshot as peek_texstudio_profile_fs
from texstudio_mcp.latex_dependencies import parse_tex_dependencies as parse_tex_dependencies_fs
from texstudio_mcp.workspace_fs import (
    grep_project_files,
    list_latex_related_files as list_latex_related_files_fs,
    read_project_file_segment,
    replace_lines_in_project_file,
    write_project_text_file as write_project_text_file_fs,
)

mcp = FastMCP(
    "texstudio-mcp",
    instructions=(
        "LaTeX project helper for TeXstudio users: sandbox all paths under workspace_root; "
        "read/grep/list/replace/write sources inside it; compile with latexmk; optionally a one-shot latexmk→bibtex/biber "
        "pipeline under one workspace lock; "
        "run bibtex/biber on a job basename "
        "(cwd under workspace_root) and skim .log/.blg diagnostics; "
        "guess bibliography backend heuristically from JOB.aux/JOB.bcf filenames; "
        "parse static \\input/\\include/bibliography directives in a single .tex file; "
        "validate .bib files (duplicate keys / coarse brace scan; optional LF-safe whitespace normalize); "
        "read PDF metadata via pdfinfo for sandboxed PDFs; extract plain-text previews via pdftotext (first pages); "
        "forward/backward Synctex via synctex view/edit on workspace PDFs; "
        "probe Python and TeX executables; optional workspace-wide static checks via chktex on discovered .tex paths; "
        "weak TeXstudio linkage reads only texstudio.ini or lastSession.txss from OS profile folders (outside workspace); "
        "optional parsed_hints on texstudio.ini and lastSession.txss; "
        "compile+bib pipeline can derive job_name from main.tex and optional TeXstudio hints."
    ),
)

_TEX_CMDS = (
    "latexmk",
    "pdflatex",
    "xelatex",
    "lualatex",
    "tectonic",
    "bibtex",
    "biber",
    "chktex",
    # Poppler / SyncTeX (aligned with ``read_pdf_metadata``, ``extract_pdf_text_preview``, synctex tools)
    "pdfinfo",
    "pdftotext",
    "synctex",
)


@mcp.tool()
def get_server_info() -> dict[str, Any]:
    """Return Python version, platform, and the texstudio-mcp package version."""
    try:
        import importlib.metadata as meta

        mcp_ver = meta.version("mcp")
    except meta.PackageNotFoundError:
        mcp_ver = None

    from texstudio_mcp import __version__ as pkg_version

    return {
        "package_version": pkg_version,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "mcp_sdk_version": mcp_ver,
    }


@mcp.tool()
def health_check_tex_toolchain() -> dict[str, Any]:
    """Probe common TeX-related commands on PATH (no compilation).

    Includes compilers/latexmk, bibliography tools (``bibtex``/``biber``), optional ``chktex`` static checker,
    plus Poppler ``pdfinfo``/``pdftotext`` and ``synctex`` when present—all via ``shutil.which`` (no subprocess).
    """
    resolved: dict[str, str | None] = {}
    for name in _TEX_CMDS:
        path = shutil.which(name)
        resolved[name] = path
    on_path = [k for k, v in resolved.items() if v]
    return {
        "commands": resolved,
        "summary": {
            "found": on_path,
            "missing": [k for k in _TEX_CMDS if k not in on_path],
        },
    }


@mcp.tool()
def validate_workspace_root(workspace_root: str) -> dict[str, Any]:
    """Check that workspace_root exists and is a directory; return canonical resolved path."""
    try:
        root = normalize_workspace_root(workspace_root)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "resolved_workspace_root": str(root)}


@mcp.tool()
def resolve_safe_project_path(workspace_root: str, relative_path: str) -> dict[str, Any]:
    """Resolve a relative path strictly inside workspace_root (blocks .. escapes). Empty relative_path means '.'."""
    try:
        root = normalize_workspace_root(workspace_root)
        target = resolve_under_workspace(root, relative_path)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "resolved_path": str(target)}


@mcp.tool()
def read_project_file(
    workspace_root: str,
    relative_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = 120_000,
) -> dict[str, Any]:
    """Read a UTF-8 text file under workspace_root. Lines are 1-based inclusive; omit both bounds to read whole file (still capped by max_chars)."""
    try:
        return read_project_file_segment(
            workspace_root,
            relative_path,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def grep_project(
    workspace_root: str,
    pattern: str,
    file_extensions: str = ".tex,.bib,.sty,.cls,.bst",
    ignore_case: bool = False,
    max_matches: int = 200,
    max_file_bytes: int = 2_000_000,
    max_depth: int = 12,
) -> dict[str, Any]:
    """Regex search across text files under workspace_root (skips huge/binary-ish files)."""
    try:
        return grep_project_files(
            workspace_root,
            pattern,
            file_extensions=file_extensions,
            ignore_case=ignore_case,
            max_matches=max_matches,
            max_file_bytes=max_file_bytes,
            max_depth=max_depth,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def list_latex_related_files(
    workspace_root: str,
    max_depth: int = 12,
    extra_extensions: str = "",
) -> dict[str, Any]:
    """List .tex/.bib/.sty/.cls/… files under workspace_root (common skips like .git/.venv). Optional comma-separated extra_extensions e.g. '.ltx,.fd'."""
    try:
        return list_latex_related_files_fs(workspace_root, max_depth=max_depth, extra_extensions=extra_extensions)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def parse_tex_dependencies(
    workspace_root: str,
    relative_tex_path: str = "",
    scan_mode: str = "tex_edges",
    max_edges: int = 500,
    max_file_bytes: int = 2_000_000,
    manifest_max_depth: int = 12,
    manifest_max_paths_per_bucket: int = 800,
) -> dict[str, Any]:
    """Static scan of one ``.tex`` under workspace_root for ``\\input``, ``\\include``, bibliography directives,
    ``\\includegraphics``, ``\\documentclass``, ``\\usepackage``, ``\\bibliographystyle`` (brace arguments only).

    ``scan_mode=tex_edges`` (default): returns ``edges``. Asset-like kinds carry ``workspace_asset_found`` when the resolved path exists (``.cls`` / ``.sty`` / ``.bst`` may live only in texmf). For ``\\includegraphics``, prefixes from ``\\graphicspath{{…}}`` in the **same file** are applied after the literal ``.tex``-relative path: ``edge.to`` chooses the first on-disk hit; prefix hits may set ``graphicspath_resolution`` / ``graphicspath_prefix_norm``.

    ``scan_mode=workspace_manifest``: skips parsing a single file and lists ``.tex`` / ``.bib`` / ``.bst`` / ``.cls`` / ``.sty`` under the workspace (same skip dirs as listing tools); optional ``relative_tex_path`` hints the main ``.tex`` (``hint_main_tex_valid``).

    Does not execute TeX. Targets containing ``\\`` or ``#`` go to ``unresolved`` (dynamic).
    """
    return parse_tex_dependencies_fs(
        workspace_root,
        relative_tex_path,
        scan_mode=scan_mode,
        max_edges=max_edges,
        max_file_bytes=max_file_bytes,
        manifest_max_depth=manifest_max_depth,
        manifest_max_paths_per_bucket=manifest_max_paths_per_bucket,
    )


@mcp.tool()
def replace_project_lines(
    workspace_root: str,
    relative_path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    max_file_bytes_before: int = 5_000_000,
    max_file_bytes_after: int = 5_000_000,
) -> dict[str, Any]:
    """Replace inclusive 1-based lines start_line..end_line with new_content (LF-normalized write). Empty file allows only span (1,1)."""
    try:
        return replace_lines_in_project_file(
            workspace_root,
            relative_path,
            start_line,
            end_line,
            new_content,
            max_file_bytes_before=max_file_bytes_before,
            max_file_bytes_after=max_file_bytes_after,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def write_project_file(
    workspace_root: str,
    relative_path: str,
    content: str,
    overwrite: bool = False,
    max_bytes: int = 5_000_000,
) -> dict[str, Any]:
    """Create a UTF-8 text file under workspace_root (mkdir parents). Set overwrite=true to replace an existing file."""
    try:
        return write_project_text_file_fs(
            workspace_root,
            relative_path,
            content,
            overwrite=overwrite,
            max_bytes=max_bytes,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def compile_latex_document(
    workspace_root: str,
    main_tex: str,
    timeout_seconds: int = 300,
    latexmk_extra_args: str = "",
    use_latexmk_cd: bool = True,
) -> dict[str, Any]:
    """Run latexmk -pdf from workspace_root targeting main_tex (relative path).

    When workspace_root is already the folder that contains main_tex, -cd is omitted
    automatically (passing only the .tex basename). When workspace_root is an ancestor,
    uses -cd by default so engines run from the subfolder (set use_latexmk_cd=false to disable).

    Returns a one-line ``summary`` plus short stdout/stderr tails; concurrent workspace-scoped build
    steps for the same ``workspace_root`` (this tool, ``compile_latex_then_run_bibliography_on_job``, ``run_bibtex_on_job``, or ``run_biber_on_job``)
    are rejected in-process via a non-blocking exclusive slot. Optional ``latexmk_extra_args`` as a
    quoted-shell fragment e.g. ``'-xelatex'``.
    """
    try:
        return run_latexmk(
            workspace_root,
            main_tex,
            timeout_seconds=timeout_seconds,
            latexmk_extra_args=latexmk_extra_args,
            use_latexmk_cd=use_latexmk_cd,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def compile_latex_then_run_bibliography_on_job(
    workspace_root: str,
    main_tex: str,
    job_name: str = "",
    bibliography_tool: str = "auto",
    relative_working_directory: str = ".",
    compile_timeout_seconds: int = 300,
    bibliography_timeout_seconds: float = 120.0,
    latexmk_extra_args: str = "",
    use_latexmk_cd: bool = True,
    bib_extra_args: str = "",
    preflight_bibliography_checks: bool = True,
    aux_peek_bytes: int = 65536,
    post_bibliography_latexmk_passes: int = 0,
    bibliography_cycles: int = 1,
    use_texstudio_job_hint: bool = False,
    texstudio_config_dir: str = "",
    texstudio_ini_path: str = "",
) -> dict[str, Any]:
    """``latexmk -pdf`` + bounded ``bibtex``/``biber`` under **one** per-workspace exclusive slot.

    Empty ``job_name`` derives from ``main_tex`` basename (``.tex`` stem). With ``bibliography_tool=auto`` and
    empty ``job_name``, also tries TeXstudio ``suggested_job_basename`` after stem derivation fails unless
    ``use_texstudio_job_hint=false`` and stem succeeded. ``bibliography_cycles`` (1..4) repeats bib +
    ``post_bibliography_latexmk_passes`` after the initial compile. ``post_bibliography_latexmk_passes`` (0..2)
    adds ``latexmk`` runs after each successful bib step. Not an open-ended latexmk loop.
    """
    return compile_latex_then_run_bibliography_on_job_fs(
        workspace_root,
        main_tex,
        job_name,
        bibliography_tool=bibliography_tool,
        relative_working_directory=relative_working_directory,
        compile_timeout_seconds=compile_timeout_seconds,
        bibliography_timeout_seconds=bibliography_timeout_seconds,
        latexmk_extra_args=latexmk_extra_args,
        use_latexmk_cd=use_latexmk_cd,
        bib_extra_args=bib_extra_args,
        preflight_bibliography_checks=preflight_bibliography_checks,
        aux_peek_bytes=aux_peek_bytes,
        post_bibliography_latexmk_passes=post_bibliography_latexmk_passes,
        bibliography_cycles=bibliography_cycles,
        use_texstudio_job_hint=use_texstudio_job_hint,
        texstudio_config_dir=texstudio_config_dir,
        texstudio_ini_path=texstudio_ini_path,
    )


@mcp.tool()
def analyze_latex_log(
    workspace_root: str,
    relative_log_path: str,
    tail_max_bytes: int = 900_000,
) -> dict[str, Any]:
    """Scan the tail of a LaTeX .log under workspace_root for errors/warnings (heuristic)."""
    try:
        return analyze_latex_log_file(workspace_root, relative_log_path, tail_max_bytes=tail_max_bytes)
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def analyze_bibliography_log(
    workspace_root: str,
    relative_blg_path: str,
    max_issues: int = 120,
    tail_max_chars: int = 8000,
    max_file_bytes: int = 2_000_000,
) -> dict[str, Any]:
    """Heuristic scan of a bibliography ``.blg`` (BibTeX or biber) under workspace_root.

    Detects typical ``Warning--`` lines (BibTeX), ``WARN``/``ERROR``/``FATAL`` prefixes (biber),
    plus common BibTeX ``I couldn't open …`` / duplicate-entry markers. Returns ``backend_guess``,
    structured ``issues``, and a UTF-8 ``tail`` for manual inspection (bounded by tail_max_chars).
    """
    return analyze_bibliography_log_fs(
        workspace_root,
        relative_blg_path,
        max_issues=max_issues,
        tail_max_chars=tail_max_chars,
        max_file_bytes=max_file_bytes,
    )


@mcp.tool()
def guess_job_bibliography_backend(
    workspace_root: str,
    job_name: str,
    relative_working_directory: str = ".",
    aux_peek_bytes: int = 65536,
) -> dict[str, Any]:
    """Heuristic bibliography-backend hint for a safe ``job_name`` basename.

    Read-only sandbox inspection: JOB.bcf existing strongly implies use ``run_biber_on_job`` (biblatex).
    Else samples the first ``aux_peek_bytes`` of JOB.aux for biblatex-style markers versus
    classic BibTeX ``\\bibstyle`` / ``\\bibdata`` lines.

    Does not run bibtex/biber subprocesses.
    """
    return guess_job_bibliography_backend_fs(
        workspace_root,
        job_name,
        relative_working_directory=relative_working_directory,
        aux_peek_bytes=aux_peek_bytes,
    )


@mcp.tool()
def run_bibtex_on_job(
    workspace_root: str,
    job_name: str,
    relative_working_directory: str = ".",
    preflight_checks: bool = True,
    extra_args: str = "",
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run ``bibtex JOB`` with working directory strictly under ``workspace_root``.

    ``job_name`` must be the base name only (``paper`` for ``paper.aux``). Default ``preflight_checks`` ensures
    ``JOB.aux`` exists before running. Capture short stdout/stderr tails, ``exit_code``, and whether ``JOB.blg`` exists.
    Shares the same in-process exclusive slot as ``compile_latex_document``, ``compile_latex_then_run_bibliography_on_job``, and ``run_biber_on_job`` for ``workspace_root``.
    Optional ``extra_args`` is parsed with POSIX-like rules (quoted shell fragment); no arbitrary shells.
    """
    return run_bibtex_on_job_impl(
        workspace_root,
        job_name,
        relative_working_directory=relative_working_directory,
        preflight_checks=preflight_checks,
        extra_args=extra_args,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def run_biber_on_job(
    workspace_root: str,
    job_name: str,
    relative_working_directory: str = ".",
    preflight_checks: bool = True,
    extra_args: str = "",
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run ``biber JOB`` with working directory strictly under ``workspace_root``.

    ``job_name`` is the base name (``main`` for ``main.bcf``). Default checks that ``JOB.bcf`` exists. Same tail / exit semantics as ``run_bibtex_on_job``.
    Shares the same in-process exclusive slot as ``compile_latex_document``, ``compile_latex_then_run_bibliography_on_job``, and ``run_bibtex_on_job`` for ``workspace_root``.
    """
    return run_biber_on_job_impl(
        workspace_root,
        job_name,
        relative_working_directory=relative_working_directory,
        preflight_checks=preflight_checks,
        extra_args=extra_args,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def validate_bib_file(
    workspace_root: str,
    relative_bib_path: str,
    normalize: bool = False,
    dry_run: bool = True,
    overwrite: bool = False,
    max_file_bytes: int = 5_000_000,
    preview_max_chars: int = 16000,
    use_bibtexparser: bool = False,
) -> dict[str, Any]:
    """Validate a BibTeX ``.bib`` under workspace_root.

    Detects duplicate citation keys / duplicate ``@string`` macro names (heuristic scan by default;
    ``use_bibtexparser=true`` uses optional ``bibtexparser`` dependency for entry/duplicate detection),
    plus a coarse brace/string scan for gross imbalance. Optional ``normalize=true`` trims
    trailing whitespace per line and uses LF endings; preview is returned unless truncated.
    Writing requires ``normalize=true``, ``dry_run=false``, and ``overwrite=true``.
    """
    return validate_bib_file_fs(
        workspace_root,
        relative_bib_path,
        normalize=normalize,
        dry_run=dry_run,
        overwrite=overwrite,
        max_file_bytes=max_file_bytes,
        preview_max_chars=preview_max_chars,
        use_bibtexparser=use_bibtexparser,
    )


@mcp.tool()
def read_pdf_metadata(
    workspace_root: str,
    relative_pdf_path: str,
    max_file_bytes: int = 52_428_800,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Run Poppler ``pdfinfo`` on a PDF under workspace_root (sandbox).

    Requires ``pdfinfo`` on PATH. Returns flattened ``metadata`` tags (keys lower_snake_case),
    plus convenience ``pages`` / ``pdf_version`` when parsable. Checks ``max_file_bytes`` before calling pdfinfo.
    """
    return read_pdf_metadata_fs(
        workspace_root,
        relative_pdf_path,
        max_file_bytes=max_file_bytes,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def extract_pdf_text_preview(
    workspace_root: str,
    relative_pdf_path: str,
    max_pages: int = 5,
    max_chars: int = 32000,
    max_file_bytes: int = 52_428_800,
    timeout_seconds: float = 45.0,
    layout_preserving: bool = False,
    suggestion_locale: str = "en",
) -> dict[str, Any]:
    """Extract UTF-8 plain text from the first ``max_pages`` of a sandboxed PDF via Poppler ``pdftotext``.

    Writes to stdout internally (``-`` output path). Truncates to ``max_chars``. When ``truncated`` is true,
    ``truncation_reason`` is ``max_chars`` and ``suggestion`` hints how to widen limits and interprets PDF vs source quirks.
    ``suggestion_locale`` selects English (``en``) or Simplified-Chinese helper text when ``truncated`` is true
    (invalid values rejected before calling ``pdftotext``).
    ``layout_preserving=false`` prefers flowing text over column layout (use ``layout_preserving=true`` for rough alignment).
    Sets ``low_text_density`` when the clipped text collapses to very little content (often scanned/image-only PDFs).
    """
    return extract_pdf_text_preview_fs(
        workspace_root,
        relative_pdf_path,
        max_pages=max_pages,
        max_chars=max_chars,
        max_file_bytes=max_file_bytes,
        timeout_seconds=timeout_seconds,
        layout_preserving=layout_preserving,
        suggestion_locale=suggestion_locale,
    )


@mcp.tool()
def resolve_synctex_forward(
    workspace_root: str,
    relative_tex_path: str,
    line: int,
    column: int = 1,
    page_hint: int = 0,
    relative_pdf_path: str = "",
    synctex_directory: str = "",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Map a TeX line/column to PDF page/box hints using ``synctex view``.

    ``relative_pdf_path`` defaults to the same path as the ``.tex`` file with a ``.pdf`` suffix.
    Optional ``synctex_directory`` maps to ``synctex -d`` when the ``.synctex.gz`` lives elsewhere.
    Parses ``SyncTeX Result`` blocks from stdout into ``hits`` (``page``, ``x``/``y``/``h``/``v``/``width``/``height`` when present).
    """
    return synctex_forward_fs(
        workspace_root,
        relative_tex_path,
        line,
        column=column,
        page_hint=page_hint,
        relative_pdf_path=relative_pdf_path,
        synctex_directory=synctex_directory,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def resolve_synctex_backward(
    workspace_root: str,
    relative_pdf_path: str,
    page: int,
    x: float,
    y: float,
    synctex_directory: str = "",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Map a PDF click (page + big-point coordinates) to TeX sources using ``synctex edit``.

    ``page`` is 1-based per the ``synctex`` CLI; ``x``/``y`` are from the top-left of the page (72 dpi big points).
    Returns ``hits`` with ``input`` paths and optional ``relative_tex_path`` when resolvable inside ``workspace_root``.
    """
    return synctex_backward_fs(
        workspace_root,
        relative_pdf_path,
        page,
        x,
        y,
        synctex_directory=synctex_directory,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def run_chktex_on_tex(
    workspace_root: str,
    relative_tex_path: str,
    timeout_seconds: int = 120,
    chktex_extra_args: str = "",
) -> dict[str, Any]:
    """Run ``chktex`` (if installed on PATH) against a UTF-8 ``.tex`` under workspace_root.

    Uses cwd=workspace_root and passes the sandboxed relative ``.tex`` path. Parses ChkTeX
    warning lines into ``warnings`` when they match ``Warning … in … line …:``. Non-zero exit
    is common when warnings exist; ``ok`` is True only when chktex exits 0 with no timeout.
    """
    try:
        return run_chktex_fs(
            workspace_root,
            relative_tex_path,
            timeout_seconds=timeout_seconds,
            chktex_extra_args=chktex_extra_args,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def batch_run_chktex_on_tex(
    workspace_root: str,
    relative_tex_paths: list[str],
    timeout_seconds: int = 120,
    max_tex_files: int = 40,
    chktex_extra_args: str = "",
    warnings_limit_per_file: int = 40,
) -> dict[str, Any]:
    """Run ``chktex`` on several ``relative_tex_paths`` under workspace_root sequentially.

    Deduplicates after stripping blanks; rejects more than ``max_tex_files`` (clamped …120).
    Each entry in ``results`` omits bulky single-file debug fields but keeps tails.
    Aggregate ``ok`` is True only when every file finishes cleanly (exit 0; no timeouts).
    """
    try:
        return batch_run_chktex_fs(
            workspace_root,
            relative_tex_paths,
            timeout_seconds=timeout_seconds,
            max_tex_files=max_tex_files,
            chktex_extra_args=chktex_extra_args,
            warnings_limit_per_file=warnings_limit_per_file,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def run_chktex_on_workspace(
    workspace_root: str,
    max_depth: int = 12,
    extra_extensions: str = "",
    timeout_seconds: int = 120,
    max_tex_files: int = 40,
    chktex_extra_args: str = "",
    warnings_limit_per_file: int = 40,
) -> dict[str, Any]:
    """Discover ``*.tex`` like ``list_latex_related_files`` (skips/prunes/depth) then batch chktex.

    Only filenames ending with ``.tex`` are linted (.ltx excluded unless named ``.tex``).
    Sorted relative paths determine scan order; if more than ``max_tex_files`` (<=120 clamp)
    ``.tex`` exist, scans the lexicographically first slice and reports ``paths_truncated=true``.
    """
    try:
        return run_workspace_chktex_fs(
            workspace_root,
            max_depth=max_depth,
            extra_extensions=extra_extensions,
            timeout_seconds=timeout_seconds,
            max_tex_files=max_tex_files,
            chktex_extra_args=chktex_extra_args,
            warnings_limit_per_file=warnings_limit_per_file,
        )
    except PathPolicyError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def read_texstudio_profile_snapshot(
    profile_file: str,
    max_chars: int = 160_000,
    texstudio_config_dir: str = "",
    texstudio_ini_path: str = "",
    include_parsed_hints: bool = False,
) -> dict[str, Any]:
    """Peek TeXstudio ``texstudio.ini`` or ``lastSession.txss``.

    Uses ``workspace_root`` **never**. Basename-only allow-list unchanged. Lookup order:

    optional ``texstudio_ini_path`` (absolute/relative resolved on the host OS) ;

    optional ``texstudio_config_dir`` (scan only inside that portable folder) ;

    else standard dirs (Windows ``%APPDATA%\\texstudio`` then ``%LOCALAPPDATA%\\texstudio``; macOS
    adds ``~/Library/Application Support/texstudio`` plus ``~/.config``; POSIX
    ``$XDG_CONFIG_HOME`` / ``~/.config``). When ``texstudio_ini_path`` and ``texstudio_config_dir``
    are both non-empty strings, **the file path wins** (See ``note`` field in responses).
    UTF-8 text, possibly truncated by ``max_chars``.

    ``include_parsed_hints``: for ``texstudio.ini`` or ``lastSession.txss`` (JSON or INI-like),
    returns lightweight ``parsed_hints`` and optional ``suggested_job_basename`` when a ``.tex`` path
    is recognized.
    """
    return peek_texstudio_profile_fs(
        profile_file,
        max_chars=max_chars,
        texstudio_config_dir=texstudio_config_dir,
        texstudio_ini_path=texstudio_ini_path,
        include_parsed_hints=include_parsed_hints,
    )
