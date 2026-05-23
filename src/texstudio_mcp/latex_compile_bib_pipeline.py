"""Single workspace-slot pipeline: ``latexmk -pdf`` then ``bibtex``/``biber`` (bounded cycles)."""



from __future__ import annotations



from typing import Any, Literal



from texstudio_mcp.latex_bib_hints import guess_job_bibliography_backend

from texstudio_mcp.latex_bib_runner import (

    DEFAULT_TIMEOUT_SECONDS as DEFAULT_BIB_TIMEOUT_SECONDS,

    run_biber_on_job,

    run_bibtex_on_job,

)

from texstudio_mcp.latex_compile import (

    DEFAULT_TIMEOUT_SECONDS as DEFAULT_COMPILE_TIMEOUT_SECONDS,

    WORKSPACE_EXCLUSIVE_BUSY_ERROR,

    end_exclusive_workspace_command,

    run_latexmk,

    try_begin_exclusive_workspace_command,

)

from texstudio_mcp.path_policy import PathPolicyError, normalize_workspace_root

from texstudio_mcp.pipeline_job_resolve import resolve_pipeline_job_name



_BibChoice = Literal["auto", "bibtex", "biber"]

_MAX_POST_BIB_LATEXMK_PASSES = 2

_MAX_BIBLIOGRAPHY_CYCLES = 4





def _normalize_bibliography_tool(s: str | _BibChoice) -> tuple[str | None, str | None]:

    t = str(s).strip().lower()

    if t in ("auto", "bibtex", "biber"):

        return t, None

    return None, f"invalid bibliography_tool {s!r}; use auto, bibtex, or biber"





def _normalize_post_bibliography_latexmk_passes(raw: int) -> tuple[int | None, str | None]:

    n = int(raw)

    if n < 0 or n > _MAX_POST_BIB_LATEXMK_PASSES:

        return (

            None,

            f"post_bibliography_latexmk_passes must be 0..{_MAX_POST_BIB_LATEXMK_PASSES} "

            f"(got {n}); 1 is typical after bibtex, 2 for classic BibTeX convergence",

        )

    return n, None





def _normalize_bibliography_cycles(raw: int) -> tuple[int | None, str | None]:

    n = int(raw)

    if n < 1 or n > _MAX_BIBLIOGRAPHY_CYCLES:

        return (

            None,

            f"bibliography_cycles must be 1..{_MAX_BIBLIOGRAPHY_CYCLES} (got {n}); "

            "each cycle runs one bib step plus post_bibliography_latexmk_passes",

        )

    return n, None





def compile_latex_then_run_bibliography_on_job(

    workspace_root: str,

    main_tex: str,

    job_name: str = "",

    *,

    bibliography_tool: str | _BibChoice = "auto",

    relative_working_directory: str = ".",

    compile_timeout_seconds: int | None = None,

    bibliography_timeout_seconds: float | None = None,

    latexmk_extra_args: str = "",

    use_latexmk_cd: bool = True,

    bib_extra_args: str = "",

    preflight_bibliography_checks: bool = True,

    aux_peek_bytes: int = 65_536,

    post_bibliography_latexmk_passes: int = 0,

    bibliography_cycles: int = 1,

    use_texstudio_job_hint: bool = False,

    texstudio_config_dir: str = "",

    texstudio_ini_path: str = "",

) -> dict[str, Any]:

    """Run latexmk, optionally infer backend, run bibtex OR biber (bounded cycles), optional latexmk reruns.



    Empty ``job_name`` derives from ``main_tex`` stem; with ``use_texstudio_job_hint`` also consults

    TeXstudio profile ``suggested_job_basename``. ``bibliography_cycles`` (1..4) repeats

    bib + ``post_bibliography_latexmk_passes`` after the initial compile (not an open-ended loop).

    """

    try:

        root = normalize_workspace_root(workspace_root)

    except PathPolicyError as exc:

        return {"ok": False, "error": str(exc)}



    hint_flag = use_texstudio_job_hint or (str(bibliography_tool).strip().lower() == "auto" and not job_name.strip())

    jres = resolve_pipeline_job_name(

        main_tex,

        job_name,

        use_texstudio_job_hint=hint_flag,

        texstudio_config_dir=texstudio_config_dir,

        texstudio_ini_path=texstudio_ini_path,

    )

    if not jres.get("ok"):

        return {"ok": False, "error": jres.get("error") or "job_name resolution failed", **jres}



    resolved_job = str(jres["job_name"])



    bib_mode, bem = _normalize_bibliography_tool(bibliography_tool)

    if bem:

        return {"ok": False, "error": bem}

    assert bib_mode is not None



    post_passes, pem = _normalize_post_bibliography_latexmk_passes(post_bibliography_latexmk_passes)

    if pem:

        return {"ok": False, "error": pem}

    assert post_passes is not None



    bib_cycles, cem = _normalize_bibliography_cycles(bibliography_cycles)

    if cem:

        return {"ok": False, "error": cem}

    assert bib_cycles is not None



    if not try_begin_exclusive_workspace_command(root):

        return {

            "ok": False,

            "error": WORKSPACE_EXCLUSIVE_BUSY_ERROR,

            "concurrent_compile_blocked": True,

            "concurrent_workspace_exclusive_blocked": True,

            "summary": "pipeline skipped: workspace busy (exclusive slot held)",

            "exclusive_pipeline_bibliography_tool": bib_mode,

            "job_name": resolved_job,

        }



    c_timeout = compile_timeout_seconds

    if c_timeout is None:

        c_timeout = DEFAULT_COMPILE_TIMEOUT_SECONDS

    bib_timeout = bibliography_timeout_seconds

    if bib_timeout is None:

        bib_timeout = float(DEFAULT_BIB_TIMEOUT_SECONDS)



    compile_kw: dict[str, Any] = {

        "timeout_seconds": c_timeout,

        "latexmk_extra_args": latexmk_extra_args,

        "use_latexmk_cd": use_latexmk_cd,

        "hold_exclusive_workspace_slot": False,

    }

    bib_kw: dict[str, Any] = {

        "relative_working_directory": relative_working_directory,

        "preflight_checks": preflight_bibliography_checks,

        "extra_args": bib_extra_args,

        "timeout_seconds": bib_timeout,

        "hold_exclusive_workspace_slot": False,

    }



    out: dict[str, Any] = {

        "exclusive_pipeline_bibliography_tool": bib_mode,

        "post_bibliography_latexmk_passes": post_passes,

        "bibliography_cycles": bib_cycles,

        "job_name": resolved_job,

        "job_name_source": jres.get("job_name_source"),

    }

    if jres.get("texstudio_job_hint_profiles_tried"):

        out["texstudio_job_hint_profiles_tried"] = jres["texstudio_job_hint_profiles_tried"]

    if jres.get("texstudio_job_hint_source_profile"):

        out["texstudio_job_hint_source_profile"] = jres["texstudio_job_hint_source_profile"]



    try:

        co = run_latexmk(workspace_root, main_tex, **compile_kw)

        out["compile_latex_document"] = co



        if not co.get("ok"):

            out["ok"] = False

            out["stage_failed"] = "compile_latex_document"

            out["error"] = co.get("error") or "latexmk step failed"

            out.setdefault("summary", co.get("summary") or out["error"])

            return out



        effective_tool: str | None = None

        if bib_mode != "auto":

            effective_tool = bib_mode



        cycle_results: list[dict[str, Any]] = []

        all_post_runs: list[dict[str, Any]] = []



        for cycle_idx in range(bib_cycles):

            cycle_no = cycle_idx + 1

            cycle_entry: dict[str, Any] = {"cycle": cycle_no}



            if bib_mode == "auto" and effective_tool is None:

                gh = guess_job_bibliography_backend(

                    workspace_root,

                    resolved_job,

                    relative_working_directory=relative_working_directory,

                    aux_peek_bytes=aux_peek_bytes,

                )

                cycle_entry["guess_job_bibliography_backend"] = gh

                if cycle_idx == 0:

                    out["guess_job_bibliography_backend"] = gh

                if not gh.get("ok"):

                    out["ok"] = False

                    out["stage_failed"] = f"guess_job_bibliography_backend_cycle_{cycle_no}"

                    out["error"] = gh.get("error") or "guess_job_bibliography_backend failed"

                    out["bibliography_cycle_results"] = cycle_results + [cycle_entry]

                    out.setdefault("summary", out["error"])

                    return out



                recommended = str(gh.get("recommended_tool") or "")

                if recommended not in ("bibtex", "biber"):

                    out["ok"] = False

                    out["stage_failed"] = f"guess_job_bibliography_backend_cycle_{cycle_no}"

                    out["error"] = (

                        "could not infer bibliography_tool from JOB.aux/JOB.bcf "

                        f"(recommended_tool={recommended!r}); rerun after a clean compile "

                        "or pass bibliography_tool=bibtex|biber explicitly."

                    )

                    out["bibliography_cycle_results"] = cycle_results + [cycle_entry]

                    out.setdefault("summary", out["error"])

                    return out

                effective_tool = recommended

            elif effective_tool is None:

                effective_tool = bib_mode



            assert effective_tool in ("bibtex", "biber")

            out["exclusive_pipeline_bibliography_tool_resolved"] = effective_tool



            bo = (

                run_biber_on_job(workspace_root, resolved_job, **bib_kw)

                if effective_tool == "biber"

                else run_bibtex_on_job(workspace_root, resolved_job, **bib_kw)

            )

            cycle_entry["run_bibliography"] = bo

            if cycle_idx == 0:

                out["run_bibliography"] = bo



            if not bo.get("ok"):

                out["ok"] = False

                out["stage_failed"] = f"run_bibliography_cycle_{cycle_no}"

                out["error"] = bo.get("error") or f"{effective_tool} step failed (cycle {cycle_no})"

                out["bibliography_cycle_results"] = cycle_results + [cycle_entry]

                parts = [f"compile: {co['summary']}"] if co.get("summary") else []

                if bo.get("summary"):

                    parts.append(f"bib{cycle_no}: {bo['summary']}")

                out["summary"] = " | ".join(parts) if parts else out["error"]

                return out



            post_runs: list[dict[str, Any]] = []

            for idx in range(post_passes):

                po = run_latexmk(workspace_root, main_tex, **compile_kw)

                post_runs.append(po)

                all_post_runs.append(po)

                if not po.get("ok"):

                    cycle_entry["post_compile_latex_document"] = post_runs

                    out["post_compile_latex_document"] = all_post_runs

                    out["ok"] = False

                    out["stage_failed"] = f"post_compile_latex_document_cycle_{cycle_no}_{idx + 1}"

                    out["error"] = (

                        po.get("error")

                        or f"post-bibliography latexmk pass {idx + 1} failed (cycle {cycle_no})"

                    )

                    out["bibliography_cycle_results"] = cycle_results + [cycle_entry]

                    parts = []

                    if co.get("summary"):

                        parts.append(f"compile: {co['summary']}")

                    if bo.get("summary"):

                        parts.append(f"bib{cycle_no}: {bo['summary']}")

                    if po.get("summary"):

                        parts.append(f"post{cycle_no}.{idx + 1}: {po['summary']}")

                    out["summary"] = " | ".join(parts) if parts else out["error"]

                    return out



            if post_runs:

                cycle_entry["post_compile_latex_document"] = post_runs

            cycle_results.append(cycle_entry)



        if cycle_results:

            out["bibliography_cycle_results"] = cycle_results

        if all_post_runs:

            out["post_compile_latex_document"] = all_post_runs



        summaries: list[str] = []

        if co.get("summary"):

            summaries.append(f"compile: {co['summary']}")

        for cr in cycle_results:

            cno = cr["cycle"]

            rb = cr.get("run_bibliography") or {}

            if rb.get("summary"):

                summaries.append(f"bib{cno}: {rb['summary']}")

            for idx, po in enumerate(cr.get("post_compile_latex_document") or []):

                if po.get("summary"):

                    summaries.append(f"post{cno}.{idx + 1}: {po['summary']}")



        out["ok"] = True

        out["summary"] = " | ".join(summaries) if summaries else co.get("summary") or ""

        return out

    finally:

        end_exclusive_workspace_command(root)


