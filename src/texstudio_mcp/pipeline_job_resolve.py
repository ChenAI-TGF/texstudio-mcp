"""Resolve bibliography ``job_name`` for compile+bib pipeline (main.tex stem / TeXstudio hints)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from texstudio_mcp.latex_bib_runner import _validate_job_name
from texstudio_mcp.texstudio_profile_read import read_texstudio_profile_snapshot


def job_name_stem_from_main_tex(main_tex: str) -> tuple[str | None, str | None]:
    """Derive a safe job basename from ``main_tex`` (POSIX normpath stem)."""
    raw = main_tex.strip()
    if not raw:
        return None, "main_tex must be non-empty to derive job_name"
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or ".." in Path(norm).parts:
        return None, "main_tex must be a relative path under workspace_root"
    stem = Path(norm).name
    if stem.lower().endswith(".tex"):
        stem = stem[:-4]
    if not stem:
        return None, "main_tex has no usable basename for job_name"
    return _validate_job_name(stem)


def _texstudio_suggested_job_basename(
    *,
    texstudio_config_dir: str = "",
    texstudio_ini_path: str = "",
) -> tuple[str | None, dict[str, Any]]:
    """Read ini then lastSession for ``suggested_job_basename`` (best-effort)."""
    meta: dict[str, Any] = {"texstudio_job_hint_profiles_tried": []}
    for profile in ("texstudio.ini", "lastSession.txss"):
        snap = read_texstudio_profile_snapshot(
            profile,
            texstudio_config_dir=texstudio_config_dir,
            texstudio_ini_path=texstudio_ini_path,
            include_parsed_hints=True,
        )
        meta["texstudio_job_hint_profiles_tried"].append(
            {
                "profile_file": profile,
                "ok": snap.get("ok"),
                "suggested_job_basename": snap.get("suggested_job_basename"),
            }
        )
        if snap.get("ok") and snap.get("suggested_job_basename"):
            jb = str(snap["suggested_job_basename"])
            valid, verr = _validate_job_name(jb)
            if valid:
                meta["texstudio_job_hint_source_profile"] = profile
                return valid, meta
            meta["texstudio_job_hint_reject_reason"] = verr
    return None, meta


def resolve_pipeline_job_name(
    main_tex: str,
    job_name: str,
    *,
    use_texstudio_job_hint: bool = False,
    texstudio_config_dir: str = "",
    texstudio_ini_path: str = "",
) -> dict[str, Any]:
    """Resolve ``job_name`` for pipeline tools.

    Order: explicit ``job_name`` → ``main_tex`` stem → (optional) TeXstudio hints.
    """
    explicit = job_name.strip()
    if explicit:
        valid, err = _validate_job_name(explicit)
        if err:
            return {"ok": False, "error": err}
        return {
            "ok": True,
            "job_name": valid,
            "job_name_source": "explicit",
        }

    stem, serr = job_name_stem_from_main_tex(main_tex)
    if stem:
        return {
            "ok": True,
            "job_name": stem,
            "job_name_source": "main_tex_stem",
        }

    if use_texstudio_job_hint:
        hinted, hint_meta = _texstudio_suggested_job_basename(
            texstudio_config_dir=texstudio_config_dir,
            texstudio_ini_path=texstudio_ini_path,
        )
        if hinted:
            out: dict[str, Any] = {
                "ok": True,
                "job_name": hinted,
                "job_name_source": "texstudio_profile_hint",
            }
            out.update(hint_meta)
            return out
        return {
            "ok": False,
            "error": (
                "could not derive job_name from main_tex and no valid TeXstudio "
                "suggested_job_basename was found"
            ),
            "main_tex_derive_error": serr,
            **hint_meta,
        }

    parts = ["job_name is empty and could not derive from main_tex"]
    if serr:
        parts.append(f"({serr})")
    parts.append("; set job_name explicitly or use_texstudio_job_hint=true")
    return {"ok": False, "error": " ".join(parts), "main_tex_derive_error": serr}
