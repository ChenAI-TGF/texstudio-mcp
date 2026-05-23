# texstudio-mcp

A **Model Context Protocol (stdio)** server for **TeXstudio / LaTeX** workflows.

- **Phase A**: environment probes (`get_server_info`, `health_check_tex_toolchain`).
- **Phase B**: all project paths are resolved inside a `workspace_root` sandbox.
- **Phase C-1**: read, grep, list, and static `.tex` dependency scanning; `.bib` validation and optional normalization.
- **Phase C-2**: line-based replace and create/overwrite writes (UTF-8, LF).
- **Phase C-3**: `latexmk` compile; sandboxed **`bibtex` / `biber`**; heuristic `.log` / `.blg` analysis; PDF metadata (`pdfinfo`) and text preview (`pdftotext`); SyncTeX forward/backward (`synctex` on PATH).
- **Phase D**: **`chktex`** (single file, batch, workspace scan).
- **Phase E**: read-only TeXstudio profile snapshots (`texstudio.ini`, `lastSession.txss`) from OS config dirs — **not** scoped by `workspace_root`.

**Current version**: see `pyproject.toml` / `src/texstudio_mcp/__init__.py`.

## Requirements

- Python 3.10+
- Recommended: virtualenv at the project root

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Optional:

```powershell
pip install -e ".[bibtex]"
```

Enables `validate_bib_file(..., use_bibtexparser=true)`.

## Run (stdio)

```powershell
python -m texstudio_mcp
```

After install:

```powershell
texstudio-mcp
```

## Register in Cursor (example)

Add a **stdio** MCP server; point `command` at your Python and pass `-m texstudio_mcp`:

```json
{
  "mcpServers": {
    "texstudio-mcp": {
      "command": "c:\\Users\\YOU\\path\\to\\texstudio-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "texstudio_mcp"],
      "cwd": "c:\\Users\\YOU\\path\\to\\texstudio-mcp"
    }
  }
}
```

Replace paths with your clone root and venv `python.exe`.

## Tools

### Phase A

| Tool | Description |
|------|-------------|
| `get_server_info` | Python, platform, and package version |
| `health_check_tex_toolchain` | `shutil.which` for `latexmk`, `pdflatex`, `xelatex`, `lualatex`, `tectonic`, `bibtex`, `biber`, `chktex`, `pdfinfo`, `pdftotext`, `synctex` |

### Phase B (path policy)

| Tool | Description |
|------|-------------|
| `validate_workspace_root` | Ensure path exists and is a directory; return normalized absolute path |
| `resolve_safe_project_path` | Resolve a relative path under `workspace_root`; block absolute paths and `..` escapes |

### Phase C-1 (read & search)

| Tool | Description |
|------|-------------|
| `read_project_file` | Read one UTF-8 text file (optional 1-based line range; capped by `max_chars`) |
| `grep_project` | Regex search over small text files (default `.tex`/`.bib`/`.sty`/`.cls`/`.bst`) |
| `list_latex_related_files` | Recursively list common LaTeX suffixes (skips `.git`, `.venv`, etc.) |
| `parse_tex_dependencies` | Static scan of one `.tex`: `\input`, `\include`, `\bibliography`, `\addbibresource`, `\InputIfFileExists` (first arg only); `\includegraphics` (optional `[…]` skipped; **same-file** `\graphicspath{…}` prefixes tried after literal path; edges may include `graphicspath_resolution` / `graphicspath_prefix_norm`); `\documentclass`, `\usepackage`, `\bibliographystyle` → `.bst`; paths normalized relative to the `.tex` dir and checked to stay in the sandbox; asset edges include `workspace_asset_found` (texmf-only styles often **false**). Dynamic targets (`\`, `#`) go to `unresolved`. **Does not run TeX.** Optional `scan_mode=workspace_manifest`: enumerate `.tex`/`.bib`/`.bst`/`.cls`/`.sty` in the workspace; optional `hint_main_tex_valid` for `relative_tex_path` |

### Phase C-2 (writes)

| Tool | Description |
|------|-------------|
| `replace_project_lines` | Replace inclusive 1-based line range; empty file only allows `(1, 1)`; size limits via `max_file_bytes_*` |
| `write_project_file` | Create UTF-8 file (mkdir parents); existing files need `overwrite=true` |

### Phase C-3 (build & logs)

| Tool | Description |
|------|-------------|
| `compile_latex_document` | `latexmk -pdf` under `workspace_root` (see TeXstudio notes for `-cd` behavior). Returns `summary`, truncated `stdout_tail` / `stderr_tail`, `wall_clock_ms`. **Per MCP process**, `compile_latex_document`, `run_bibtex_on_job`, `run_biber_on_job`, and `compile_latex_then_run_bibliography_on_job` share one **exclusive slot** per `workspace_root`; overlapping calls get `concurrent_compile_blocked` and `concurrent_workspace_exclusive_blocked` (v0.15.3+). |
| `compile_latex_then_run_bibliography_on_job` | **Pipeline** (v0.15.6+): one slot, **latexmk → bib** (`bibliography_tool`: `auto` / `bibtex` / `biber`). Empty `job_name` defaults to `main_tex` stem (v0.15.9+); with `bibliography_tool=auto` and invalid stem, may read TeXstudio `suggested_job_basename`. `bibliography_cycles` (1..4, default 1) repeats bib + `post_bibliography_latexmk_passes` (0..2). Returns `compile_latex_document`, `bibliography_cycle_results`, optional `guess_job_bibliography_backend`, `run_bibliography`, optional `post_compile_latex_document`, `summary`, `stage_failed` on failure. **Not** an unbounded latexmk loop. |
| `analyze_latex_log` | Tail-read `.log` (heuristic `file_line` / `latex_error` / warnings) |
| `analyze_bibliography_log` | Read `.blg` (BibTeX or biber): `backend_guess`, `issues`, `severity_counts`, truncated `tail` |
| `guess_job_bibliography_backend` | **Read-only**: inspect `JOB.bcf` / `JOB.aux` prefix; `recommended_tool` `biber` / `bibtex` / `unknown`, `confidence`, `aux_exists` / `bcf_exists` / `blg_exists` |
| `run_bibtex_on_job` | `bibtex JOB` with `cwd` = sandboxed `relative_working_directory`; `job_name` is basename only; default preflight requires `JOB.aux`; same exclusive slot as compile |
| `run_biber_on_job` | `biber JOB`; default preflight requires `JOB.bcf`; same semantics as bibtex runner |
| `validate_bib_file` | Duplicate keys / `@string` macros (heuristic, or `use_bibtexparser=true` with `[bibtex]` extra); coarse brace balance; optional `normalize` + `overwrite=true` to write back |
| `read_pdf_metadata` | `pdfinfo` on sandboxed PDF; `metadata`, `pages`, `pdf_version`, size limits |
| `extract_pdf_text_preview` | `pdftotext` for first `max_pages`; truncate by `max_chars`; `truncation_reason` / `suggestion`; `suggestion_locale` `en` or `zh` when truncated |
| `resolve_synctex_forward` | `synctex view`: TeX line → PDF `hits` |
| `resolve_synctex_backward` | `synctex edit`: PDF page + coordinates → TeX `hits` |

### Phase D (ChkTeX)

| Tool | Description |
|------|-------------|
| `run_chktex_on_tex` | Run `chktex` on one `.tex`; `warnings`, tails; `ok` may be false when ChkTeX exits non-zero |
| `batch_run_chktex_on_tex` | Sequential chktex on a path list; `results`, `clean_count` |
| `run_chktex_on_workspace` | Discover `.tex` files (like listing tools), then batch chktex; caps and `paths_truncated` metadata |

### Phase E (TeXstudio, outside sandbox)

| Tool | Description |
|------|-------------|
| `read_texstudio_profile_snapshot` | **No `workspace_root`**. Read `texstudio.ini` or `lastSession.txss` (basename allow-list only). Optional `texstudio_config_dir` / `texstudio_ini_path` (`texstudio_ini_path` wins). `include_parsed_hints=true` (v0.15.9+): lightweight `parsed_hints` and optional `suggested_job_basename` for ini or txss. Use only in trusted sessions. |

### TeXstudio vs MCP notes

- **`workspace_root` / `main_tex`**: TeXstudio often uses the main `.tex` folder as the working directory. If `workspace_root` **is** that folder and `main_tex` is only a basename (e.g. `manuscript.tex`), the server **omits** redundant `latexmk -cd`. If `workspace_root` is an ancestor, `-cd` is used by default so engines run in the subfolder.
- **Recommendation**: Point `workspace_root` at the folder that contains the main `.tex` and pass `main_tex` as a filename; or use a repo root plus a relative path like `subdir/manuscript.tex` with `-cd`.
- **Latency**: Each tool call spawns processes; `latexmk` may run multiple engine passes — often slower than a single IDE build. Check `wall_clock_ms` in responses. `summary` is derived from truncated stdout/stderr tails, not full multi‑MB logs (use `read_project_file` on `.log` for full text).
- **Timeouts**: `timeout_seconds` covers spawn through process end; the process tree is killed on timeout.
- **No parallel writers on the same workspace**: Do not overlap compile/bib/pipeline tools on the same `workspace_root` in one MCP process; the second call is rejected. **Different MCP processes** are not coordinated across machines.
- **Profile snapshot**: Reads host TeXstudio config paths; not sandboxed — use only when you trust the environment.

## License

MIT — see [LICENSE](LICENSE).
