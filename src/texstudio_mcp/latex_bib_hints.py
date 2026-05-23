"""Heuristic bibliography-backend hints from ``JOB.aux`` / ``JOB.bcf`` under workspace_root."""

from __future__ import annotations

import re
from typing import Any

from texstudio_mcp.latex_bib_runner import _resolve_work_dir, _validate_job_name
from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root

_DEFAULT_AUX_PEEK = 65_536
_MIN_AUX_PEEK = 4_096
_MAX_AUX_PEEK = 393_216

_RX_BIBSTYLE = re.compile(r"\\bibstyle\s*\{", re.IGNORECASE)
_RX_BIBDATA = re.compile(r"\\bibdata\s*\{", re.IGNORECASE)


def _clamp_aux_peek(raw: int) -> int:
    return max(_MIN_AUX_PEEK, min(int(raw), _MAX_AUX_PEEK))


def _aux_suggests_biblatex(aux_text: str) -> bool:
    """Cheap cues that pdflatex was run under biblatex (even before ``.bcf`` is written reliably)."""
    if not aux_text:
        return False
    low = aux_text.lower()
    if "biblatex-control" in low:
        return True
    # biblatex-internal control sequences / sidecar references
    if "\\abx" in aux_text or "\\blx@" in aux_text:
        return True
    if "biblatex" in low:
        return True
    return False


def _aux_suggests_classic_bibtex(aux_text: str) -> bool:
    return bool(_RX_BIBSTYLE.search(aux_text) or _RX_BIBDATA.search(aux_text))


def guess_job_bibliography_backend(
    workspace_root: str,
    job_name: str,
    *,
    relative_working_directory: str = ".",
    aux_peek_bytes: int = _DEFAULT_AUX_PEEK,
) -> dict[str, Any]:
    """Read-only heuristic: infer whether ``run_biber_on_job`` or ``run_bibtex_on_job`` fits ``job_name``.

    Never runs external commands. Uses existence of ``JOB.bcf``, a capped read of ``JOB.aux``, plus light regexes.
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
    assert jn is not None

    rr = root.resolve()
    wd_r = wd.resolve()
    rel_wd = wd_r.relative_to(rr).as_posix()

    bcf_path = wd / f"{jn}.bcf"
    aux_path = wd / f"{jn}.aux"
    blg_path = wd / f"{jn}.blg"

    aux_rel = aux_path.resolve().relative_to(rr).as_posix()
    bcf_rel = bcf_path.resolve().relative_to(rr).as_posix()
    blg_rel = blg_path.resolve().relative_to(rr).as_posix()

    peek = _clamp_aux_peek(aux_peek_bytes)
    aux_text = ""
    aux_truncated = False
    aux_exists = aux_path.is_file()
    bcf_exists = bcf_path.is_file()
    blg_exists = blg_path.is_file()

    if aux_exists:
        try:
            blob = aux_path.read_bytes()
            truncated = len(blob) > peek
            aux_truncated = truncated
            head = blob if not truncated else blob[:peek]
            aux_text = head.decode("utf-8", errors="replace")
        except OSError as exc:
            return {
                "ok": False,
                "error": f"cannot read {aux_rel}: {exc}",
                "job_name": jn,
                "relative_working_directory": rel_wd,
            }

    if bcf_exists:
        return _ok_payload(
            jn,
            rel_wd,
            aux_rel,
            bcf_rel,
            blg_rel,
            aux_exists,
            True,
            blg_exists,
            recommended_tool="biber",
            confidence="high",
            summary=(
                "JOB.bcf exists under relative_working_directory — use run_biber_on_job for this basename "
                "(biblatex / biber workflow)."
            ),
            auxiliary_aux_peek_bytes=peek,
            auxiliary_aux_truncated=aux_truncated,
        )

    if not aux_exists:
        return _ok_payload(
            jn,
            rel_wd,
            aux_rel,
            bcf_rel,
            blg_rel,
            False,
            False,
            blg_exists,
            recommended_tool="unknown",
            confidence="low",
            summary="Neither JOB.aux nor JOB.bcf was found — run pdflatex/latexmk once, then probe again.",
            auxiliary_aux_peek_bytes=peek,
            auxiliary_aux_truncated=False,
        )

    la = _aux_suggests_biblatex(aux_text)
    ct = _aux_suggests_classic_bibtex(aux_text)

    if la:
        mid = (
            "Auxiliary lines look like biblatex/biber (.bcf not present yet)"
            "; run compile_latex_document once so JOB.bcf is generated, then run_biber_on_job."
        )
        if ct:
            mid = (
                "Both biblatex-style markers and classical \\bibdata/\\bibstyle appear in JOB.aux "
                "(unusual stale state). Prefer biblatex/biber unless you intentionally use BibTeX only."
            )
        return _ok_payload(
            jn,
            rel_wd,
            aux_rel,
            bcf_rel,
            blg_rel,
            True,
            False,
            blg_exists,
            recommended_tool="biber",
            confidence="medium",
            summary=mid,
            auxiliary_detected={"biblatex_markers_in_aux": True, "classic_bibtex_markers_in_aux": ct},
            auxiliary_aux_peek_bytes=peek,
            auxiliary_aux_truncated=aux_truncated,
        )

    if ct:
        return _ok_payload(
            jn,
            rel_wd,
            aux_rel,
            bcf_rel,
            blg_rel,
            True,
            False,
            blg_exists,
            recommended_tool="bibtex",
            confidence="medium",
            summary=(
                r"Traditional \bibdata/\bibstyle lines detected in JOB.aux — classic BibTeX workflow; "
                "use run_bibtex_on_job."
            ),
            auxiliary_detected={"biblatex_markers_in_aux": False, "classic_bibtex_markers_in_aux": True},
            auxiliary_aux_peek_bytes=peek,
            auxiliary_aux_truncated=aux_truncated,
        )

    return _ok_payload(
        jn,
        rel_wd,
        aux_rel,
        bcf_rel,
        blg_rel,
        True,
        False,
        blg_exists,
        recommended_tool="unknown",
        confidence="low",
        summary=(
            "JOB.aux exists but lacked obvious biblatex or \\bibdata/\\bibstyle markers in the sampled prefix "
            "(or aux is unusually short)."
        ),
        auxiliary_detected={"biblatex_markers_in_aux": False, "classic_bibtex_markers_in_aux": False},
        auxiliary_aux_peek_bytes=peek,
        auxiliary_aux_truncated=aux_truncated,
    )


def _ok_payload(
    job_name: str,
    relative_wd_display: str,
    aux_rel: str,
    bcf_rel: str,
    blg_rel: str,
    aux_exists: bool,
    bcf_exists: bool,
    blg_exists: bool,
    *,
    recommended_tool: str,
    confidence: str,
    summary: str,
    auxiliary_detected: dict[str, Any] | None = None,
    auxiliary_aux_peek_bytes: int,
    auxiliary_aux_truncated: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "job_name": job_name,
        "relative_working_directory": relative_wd_display,
        "recommended_tool": recommended_tool,
        "confidence": confidence,
        "summary": summary,
        "aux_relative_path": aux_rel,
        "bcf_relative_path": bcf_rel,
        "blg_relative_path": blg_rel,
        "aux_exists": aux_exists,
        "bcf_exists": bcf_exists,
        "blg_exists": blg_exists,
        "auxiliary_aux_peek_bytes": auxiliary_aux_peek_bytes,
        "auxiliary_aux_truncated": auxiliary_aux_truncated,
    }
    if auxiliary_detected is not None:
        out["auxiliary_detected"] = auxiliary_detected
    return out
