# texstudio-mcp

MCP (Model Context Protocol) server over **stdio** for **TeXstudio** and **LaTeX** projects. It helps AI clients read and edit sources inside a project folder, run `latexmk` and bibliography tools, inspect logs, preview PDFs, use SyncTeX, run ChkTeX, and optionally read TeXstudio profile files on the host.

**Version**: see `pyproject.toml` or `texstudio_mcp.__version__`.

## What it does

- **Project sandbox**: Paths are resolved under a `workspace_root` you provide. Relative paths cannot escape the root (`..` and absolute paths are rejected).
- **Read & search**: Read `.tex`/`.bib`/etc., grep the tree, list LaTeX-related files, statically scan `\input`, `\includegraphics`, bibliographies, and more in a single `.tex` file.
- **Edit**: Replace line ranges or write new files (UTF-8, LF).
- **Build**: `latexmk -pdf`, plus `bibtex` / `biber` on a job basename, or a combined pipeline (latexmk → bib → optional extra latexmk passes). Heuristic `.log` / `.blg` parsing.
- **PDF & SyncTeX**: `pdfinfo` metadata, `pdftotext` previews, `synctex view` / `synctex edit` when those tools are on `PATH`.
- **Lint**: `chktex` on one file, a list of files, or all `.tex` files discovered in the workspace.
- **TeXstudio hints** (optional): Read `texstudio.ini` or `lastSession.txss` from OS config locations — **not** part of the project sandbox. Useful for `suggested_job_basename` when orchestrating builds.

External commands (`latexmk`, `pdflatex`, `bibtex`, `biber`, `pdfinfo`, `pdftotext`, `synctex`, `chktex`) must be installed separately (e.g. TeX Live, MiKTeX, Poppler). The server only checks `PATH` and invokes them with timeouts and truncated output.

## Install

- Python **3.10+**

```powershell
git clone https://github.com/ChenAI-TGF/texstudio-mcp.git
cd texstudio-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Optional BibTeX parsing via [bibtexparser](https://pypi.org/project/bibtexparser/):

```powershell
pip install -e ".[bibtex]"
```

Then use `validate_bib_file(..., use_bibtexparser=true)`.

## Run

```powershell
python -m texstudio_mcp
```

Or, after install:

```powershell
texstudio-mcp
```

## Cursor configuration (example)

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

Use your actual clone path and venv `python.exe`.

## Tool reference

### Server & toolchain

| Tool | Description |
|------|-------------|
| `get_server_info` | Python executable, platform, package version |
| `health_check_tex_toolchain` | Which of `latexmk`, `pdflatex`, `xelatex`, `lualatex`, `tectonic`, `bibtex`, `biber`, `chktex`, `pdfinfo`, `pdftotext`, `synctex` are on `PATH` |

### Workspace paths

| Tool | Description |
|------|-------------|
| `validate_workspace_root` | Confirm directory exists; return normalized absolute path |
| `resolve_safe_project_path` | Resolve a relative path inside `workspace_root` |

### Read & analyze sources

| Tool | Description |
|------|-------------|
| `read_project_file` | Read a UTF-8 file (optional line range, `max_chars` cap) |
| `grep_project` | Regex search across project text files |
| `list_latex_related_files` | List `.tex`, `.bib`, `.sty`, etc. (skips `.git`, `.venv`, …) |
| `parse_tex_dependencies` | Static dependency scan for one `.tex` (inputs, graphics with `\graphicspath`, packages, `.bst`, bibliographies). Optional `workspace_manifest` mode lists assets in the tree. Does not run TeX. |

### Edit sources

| Tool | Description |
|------|-------------|
| `replace_project_lines` | Replace a 1-based inclusive line range |
| `write_project_file` | Create or overwrite a file (`overwrite=true` to replace) |

### Compile & bibliography

| Tool | Description |
|------|-------------|
| `compile_latex_document` | Run `latexmk -pdf` for `main_tex` under `workspace_root`. Returns `summary`, short `stdout_tail` / `stderr_tail`, `wall_clock_ms`. |
| `compile_latex_then_run_bibliography_on_job` | One locked sequence: latexmk → `bibtex` or `biber` → optional extra latexmk passes (`post_bibliography_latexmk_passes` 0–2, `bibliography_cycles` 1–4). Empty `job_name` defaults from `main_tex`; `bibliography_tool=auto` picks backend from `.aux`/`.bcf`. |
| `guess_job_bibliography_backend` | Read-only hint: `biber` vs `bibtex` from `JOB.bcf` / `JOB.aux` |
| `run_bibtex_on_job` | Run `bibtex` with sandboxed working directory |
| `run_biber_on_job` | Run `biber` with sandboxed working directory |
| `analyze_latex_log` | Heuristic scan of a `.log` tail |
| `analyze_bibliography_log` | Heuristic scan of a `.blg` |

**Concurrency:** For a given `workspace_root`, only one of `compile_latex_document`, `run_bibtex_on_job`, `run_biber_on_job`, or `compile_latex_then_run_bibliography_on_job` runs at a time **inside the same MCP process**. A second call gets `concurrent_workspace_exclusive_blocked`. Separate MCP instances do not coordinate.

### Bibliography files & PDF

| Tool | Description |
|------|-------------|
| `validate_bib_file` | Check `.bib` for duplicate keys, rough brace balance; optional whitespace normalize / write-back |
| `read_pdf_metadata` | `pdfinfo` on a sandboxed PDF |
| `extract_pdf_text_preview` | `pdftotext` excerpt (`max_pages`, `max_chars`; optional `suggestion_locale` `en`/`zh` when truncated) |
| `resolve_synctex_forward` | TeX position → PDF (`synctex view`) |
| `resolve_synctex_backward` | PDF click → TeX (`synctex edit`) |

### ChkTeX

| Tool | Description |
|------|-------------|
| `run_chktex_on_tex` | Lint one `.tex` |
| `batch_run_chktex_on_tex` | Lint a list of `.tex` paths |
| `run_chktex_on_workspace` | Discover `.tex` files, then lint (with file count caps) |

### TeXstudio profile (host only)

| Tool | Description |
|------|-------------|
| `read_texstudio_profile_snapshot` | Read `texstudio.ini` or `lastSession.txss` from TeXstudio config dirs (or `texstudio_config_dir` / `texstudio_ini_path`). `include_parsed_hints=true` may return `suggested_job_basename`. **Not** restricted to `workspace_root` — use only in trusted environments. |

## Usage notes

**`workspace_root` and `main_tex`**

- If `workspace_root` is the folder that contains the main `.tex`, pass only the basename (e.g. `paper.tex`) — the server avoids redundant `latexmk -cd`.
- If `workspace_root` is the repository root, pass a relative path (e.g. `chapters/paper.tex`); `latexmk` runs in that subfolder via `-cd`.

**Build time and logs**

- Builds spawn real `latexmk`/engine processes; wall time is often longer than a single IDE compile. Use `wall_clock_ms` in the response.
- Tool output is truncated for MCP JSON size; read full `.log` files with `read_project_file`.

**Limits**

- Bibliography pipeline runs a **bounded** number of latexmk/bib cycles — not an open-ended “compile until done” loop.
- Profile and config reads access files outside your LaTeX project by design.

## License

MIT — see [LICENSE](LICENSE).
