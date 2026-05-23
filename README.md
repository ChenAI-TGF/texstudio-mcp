# texstudio-mcp

面向 TeXstudio / LaTeX 工作流的 **Model Context Protocol（stdio）** 服务。**阶段 B** 起工程内路径均在 `workspace_root` 沙箱内解析；**阶段 C-1** 提供工程内读文件、`grep`、列举、单篇 `.tex` 静态依赖抽取，以及对 **`.bib`** 的校验与可选规范化；**阶段 C-2** 提供按行替换与新建/覆盖写入（UTF-8，LF）；**阶段 C-3** 提供 `latexmk` 编译；沙箱内 **`bibtex`/`biber`** 对文献 job 的运行；`.log` / **`.blg`** 粗略诊断，以及对沙箱内 **PDF** 的 **`pdfinfo`** 元数据读取与 **`pdftotext`** 前几页文本预览，以及 **`synctex view` / `synctex edit`** 的正反向跳转解析（需在 PATH 中找到 **`synctex`**）；**阶段 D** 提供 **`chktex`**（单文件 / 批量 / 列举后批量）；**阶段 E** 在**白名单**内只读访问 TeXstudio 用户目录下配置快照（刻意**不经过** `workspace_root`）；阶段 A（环境探测）保留。

## 环境

- Python 3.10+
- 建议虚拟环境：在项目根执行 `python -m venv .venv`，激活后执行 `pip install -e .`

## 运行（stdio）

```powershell
python -m texstudio_mcp
```

安装后可：

```powershell
texstudio-mcp
```

## 在 Cursor 中注册（示例）

在 Cursor MCP 设置里增加一条 **stdio** 服务，`command` 指向解释器，`args` 启动模块，例如：

```json
{
  "mcpServers": {
    "texstudio-mcp": {
      "command": "c:\\Users\\YOU\\Desktop\\texstudio-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "texstudio_mcp"],
      "cwd": "c:\\Users\\YOU\\Desktop\\texstudio-mcp"
    }
  }
}
```

将路径换成你本机项目根与 venv 中 `python.exe` 的实际位置。

可选依赖：

- **BibTeX 解析增强**：`pip install -e ".[bibtex]"`（`validate_bib_file` 的 `use_bibtexparser=true`）

## 工具一览

### 阶段 A

| 工具 | 说明 |
|------|------|
| `get_server_info` | Python / 平台 / 包版本 |
| `health_check_tex_toolchain` | 探测 `latexmk`、`pdflatex`、`xelatex`、`lualatex`、`tectonic`、`bibtex`、`biber`、`chktex`、`pdfinfo`、`pdftotext`、`synctex` 是否在 `PATH`（与 PDF 元数据/文本预览/SyncTez 工具链一致） |

### 阶段 B（路径契约）

| 工具 | 说明 |
|------|------|
| `validate_workspace_root` | 校验路径存在且为目录，返回规范化绝对路径 |
| `resolve_safe_project_path` | 在 `workspace_root` 内解析相对路径；禁止绝对路径与逃出根目录（含 `..`） |

### 阶段 C-1（读与搜）

| 工具 | 说明 |
|------|------|
| `read_project_file` | 读取单个 UTF-8 文本文件（可按 1-based 行号截取；全文仍受 `max_chars` 上限） |
| `grep_project` | 对工程内小体积文本文件做正则搜索（默认 `.tex`/`.bib`/`.sty`/`.cls`/`.bst`，可配置后缀与 `max_matches`） |
| `list_latex_related_files` | 递归列出常见 LaTeX 源后缀（跳过 `.git`/`.venv` 等目录） |
| `parse_tex_dependencies` | 静态扫描一篇 `.tex`：`\\input`、`\\include`、`\\bibliography`、`\\addbibresource`、`\\InputIfFileExists`（仅首参）；**另**：`\\includegraphics`（跳过可选 `[…]`；**同一文件内 `\\graphicspath{{…}}`** 作为搜索目录，先字面相对路径再各前缀，命中前缀时边可带 **`graphicspath_resolution`/`graphicspath_prefix_norm`**）、`\\documentclass` / `\\usepackage`（可多参逗号分隔）、`\\bibliographystyle`（映射到 `.bst` 文件名）；路径相对当前 `.tex` 目录规范化并校验留在沙箱内；插图 / 样式类边带 **`workspace_asset_found`**（工程内是否真有该文件；texmf 自带样式常为 **false**）。无法静态解析的目标（含 `\\`/`#`）记入 **`unresolved`**。**不运行 TeX**。可选 **`scan_mode=workspace_manifest`**：不按 `.tex` 解析，枚举沙箱内 **`.tex`/`.bib`/`.bst`/`.cls`/`.sty`**（跳过目录与 **`list_latex_related_files`** 一致）；可将 **`relative_tex_path`** 作为主文件提示并得到 **`hint_main_tex_valid`** |

### 阶段 C-2（可控写入）

| 工具 | 说明 |
|------|------|
| `replace_project_lines` | 按 **1-based** 闭区间替换若干行；空文件仅允许替换 `(1, 1)`；结果大小受 `max_file_bytes_*` 限制 |
| `write_project_file` | 新建文本文件（自动创建父目录）；已存在时需 **`overwrite=true`** 才覆盖 |

### 阶段 C-3（编译与日志）

| 工具 | 说明 |
|------|------|
| `compile_latex_document` | 在 `workspace_root` 下执行 **`latexmk -pdf`**（目录/`-cd` 逻辑见上文）。返回 **`summary`**（单行摘要，便于 UI 先展示）、**`stdout_tail`/`stderr_tail` 强截断**（各约 12k/6k 字符量级，减小微型 JSON）；**同一 `workspace_root` 在 MCP 进程内串行**：**`compile_latex_document`**、**`run_bibtex_on_job`**、**`run_biber_on_job`**、**`compile_latex_then_run_bibliography_on_job`** 共享独占槽，忙时第二次调用会 **`concurrent_compile_blocked: true`**，并附 **`concurrent_workspace_exclusive_blocked: true`**（**v0.15.3+**，语义为「工作区构建槽被占用」）。**`timeout_seconds`**、进程树杀死、**`wall_clock_ms`** 等同前 |
| `compile_latex_then_run_bibliography_on_job` | **编排**（**v0.15.6+**）：**同一** workspace 独占槽内 **latexmk → bib**（**`bibliography_tool`**：`auto`/`bibtex`/`biber`）。**`job_name`** 默认可空，从 **`main_tex`** stem 推导（**v0.15.9+**）；**`bibliography_tool=auto`** 且 stem 无效时可读 TeXstudio **`suggested_job_basename`**。**`bibliography_cycles`**（**1..4**，默认 **1**）重复 bib + **`post_bibliography_latexmk_passes`**（**0..2**）。返回 **`compile_latex_document`**、**`bibliography_cycle_results`**、（`auto` 时）**`guess_job_bibliography_backend`**、**`run_bibliography`**、可选 **`post_compile_latex_document`** 与 **`summary`**；**`stage_failed`** 标失败段。仍非无界 latexmk 循环 |
| `analyze_latex_log` | 读取沙箱内 **`.log`** 的尾部（默认最多约 900KB）并启发式抽取 **`file_line` / `latex_error` / warning** 等条目（非完整解析器） |
| `analyze_bibliography_log` | 读取沙箱内 **`.blg`**（BibTeX 或 biber），启发式区分后端（**`backend_guess`**）、抽取 **`issues`**（含 **`severity`** / **`code`** / **`line`**）、汇总 **`severity_counts`**；附带 **`tail`**（按 **`tail_max_chars`** 截断全文）；**`max_issues`** / **`max_file_bytes`** 封顶 |
| `guess_job_bibliography_backend` | **只读**（**不调用** bibtex/biber）：在同一 **`workspace_root`** 下的 **`relative_working_directory`** 内，根据 **`JOB.bcf`** 是否存在及对 **`JOB.aux`** 前缀（**`aux_peek_bytes`**，受限）的简单启发式，返回 **`recommended_tool`**：**`biber`** / **`bibtex`** / **`unknown`**；**`confidence`**（**`high`**/**`medium`**/**`low`**）； **`aux_exists`/`bcf_exists`/`blg_exists`**；可选 **`auxiliary_detected`**（是否见到典型 **`\bibdata`/`\bibstyle`** 或 biblatex 关键字）。供编排前先判断应跑 **`run_biber_on_job`** 还是 **`run_bibtex_on_job`** |
| `run_bibtex_on_job` | 在 **`workspace_root`** 下指定 **`relative_working_directory`**（必须为沙箱内目录）为 **`cwd`**，运行 **`bibtex JOB`**（**`job_name`** 仅为基名，对应 **`JOB.aux`**）。默认 **`preflight_checks`** 要求 **`JOB.aux`** 已存在；返回 **`exit_code`**、短 **`stdout_tail`/`stderr_tail`**、**`blg_relative_path`**、**`blg_found`**、**`wall_clock_ms`**；与 **`compile_latex_document`/`run_biber_on_job`** 同进程互斥（见上）；可选 **`extra_args`**（按引号规则分词，**非**任意 shell）；**`timeout_seconds`** 上限；PATH 缺 **`bibtex`** 则 **`ok: false`** |
| `run_biber_on_job` | 同上范式运行 **`biber JOB`**（默认检查 **`JOB.bcf`**）；语义与 tails 等与 **`run_bibtex_on_job`** 对齐（含同进程互斥） |
| `validate_bib_file` | 校验沙箱内 **`.bib`**：**重复 citation key**、**重复 `@string` 宏名**（默认启发式 **`@type{`** 扫描；**`use_bibtexparser=true`** 需 extras **`[bibtex]`**，**v0.15.9+**）、**粗略括号平衡**；**严格 UTF-8** 失败时降级解码并告警。可选 **`normalize=true`**：各行去尾随空白 + LF；**`dry_run=false`** 写回必须 **`overwrite=true`** |
| `read_pdf_metadata` | 对沙箱内 **`.pdf`** 调用 **`pdfinfo`**（须在 PATH）；解析 **`metadata`**（键转为 **`lower_snake_case`**）、便捷字段 **`pages`** / **`pdf_version`**（若可解析）；检查 **`max_file_bytes`**（默认 50MiB）；返回 **`pdf_mtime_utc`**、**`pdf_bytes`**；缺少 **`pdfinfo`** 时 **`ok: false`** 并附 **`hint`**（未捆绑额外 Python PDF 库） |
| `extract_pdf_text_preview` | 对沙箱内 **`.pdf`** 调用 **`pdftotext`**（须在 PATH，常见为 TeX Live / Poppler 自带），抽取 **`max_pages`**（默认 5，硬上限 50）内的文本至 **`stdout`**，再按 **`max_chars`**（默认 32000，硬下限 256 / 上限 600k）截断返回 **`text`**；**`truncated: true`** 时附带 **`truncation_reason`**（现为 **`max_chars`**）与 **`suggestion`**（默认英文；可用 **`suggestion_locale`**：**`en`** / **`zh`**（兼容 **`zh-CN`**、**`zh-Hans`**）切换为简体中文说明；非法取值在调用 **`pdftotext`** 前拒绝）。**`layout_preserving: false`**（默认）：流式段落，多栏会与目视版式不一致（正常）；改为 **`true`** 可更接近栏位占位（仍为启发式）；返回 **`chars_full`** / **`chars_returned`** / **`low_text_density`**（扫描版常见）；读前 **`max_file_bytes`** 门禁 |
| `resolve_synctex_forward` | 对沙箱内的 **`.tex`** / **`.pdf`** 调用 **`synctex view`**（须在 PATH）：按 **`line`**（与 **`synctex`** 一致的列语义）及可选 **`page_hint`** 解析 **`SyncTeX Result`** 块为 **`hits`**（含 **`page`**、`x`/`y`/`h`/`v`/`width`/`height` 等）；默认 PDF 为主 **`relative_tex_path`** 同 stem 的 **`.pdf`**；可选 **`synctex_directory`** 映射 **`synctex -d`** |
| `resolve_synctex_backward` | 对沙箱内 **`.pdf`** 调用 **`synctex edit`**：**`page`**（**1-based**）、**`x`**/**`y`**（72 bp，自页左上角）；解析 **`hits`**（**`input`**、**`line`**、**`column`**；若在 **`workspace_root`** 内可解析则填 **`relative_tex_path`**）；可选 **`synctex_directory`** |

### 阶段 D（静态检查）

| 工具 | 说明 |
|------|------|
| `run_chktex_on_tex` | 若 **`chktex` 在 PATH 中**：在 `workspace_root` 下对相对路径 **`*.tex`** 运行 `chktex -v0 …`（可附加 **`chktex_extra_args`**，按 shell 分词）；返回 **`warnings`**（解析 `Warning … in … line …:` 行）、尾部 **`stdout_tail`/`stderr_tail`**、**`wall_clock_ms`**。当 ChkTeX 报错退出（常见为存在告警）时 **`ok` 为 false** 且带 **`error`** 简述；需在本地安装 TeX Live / MiKTeX 等自带的 `chktex` |
| `batch_run_chktex_on_tex` | 对 **`relative_tex_paths`**（字符串列表）**依次**在同一 `workspace_root` 下调用与上相同的 **`chktex`** 封装；剔除空串与首尾空白后 **去重**；**`max_tex_files`** 默认 **40**，硬上限 **120**； **`warnings_limit_per_file`** 收窄每篇 **`warnings`** 体积。汇总 **`clean_count`**、**`results`**（每篇为小号结构体）；**每一篇均无超时且 ChkTeX 均以退出码 0 结束**时 **`ok`** 为 **true**，否则为 **false** 并附带 **`batch_error`** 说明 |
| `run_chktex_on_workspace` | 先按与 **`list_latex_related_files`** 相同的递归/跳过目录/后缀集合（含 **`extra_extensions`**、`max_depth` 有效上限 …48）枚举文件；**仅保留后缀为 `.tex`** 的路径（`.ltx` 等不会因 `extra_extensions` 进入 chktex，除非文件名以 `.tex` 结尾）；再调用与批量相同语义的 **`chktex` 封装**。若 `.tex` 数量超过 **`max_tex_files`**（≤120），按排序后的相对路径**从头截取**，并 **`paths_truncated=true`**、`truncated_unused_tex_count`。响应始终带 **`discovered_tex_count`、`scanned_tex_count`、`skipped_chktex`、`listing_max_depth_effective`、`listing_extra_extensions`** 等列举元数据；若有 `.tex` 被扫描，其余载荷与 **`batch_run_chktex_on_tex`** 一致（含 **`chktex_path`/`results`**）；若未发现 `.tex`，则 **`ok` 为 true**、`skipped_chktex` **为 true**、不含 **`chktex_path`**、**`results`** 为空 |

### 阶段 E（TeXstudio 弱联动）

| 工具 | 说明 |
|------|------|
| `read_texstudio_profile_snapshot` | **不传入 `workspace_root`**：默认在操作系统约定的 TeXstudio 配置目录中查找 **`texstudio.ini`** 或 **`lastSession.txss`**（`profile_file` 只允许这两个**纯文件名**，禁止带子路径）；可选 **`texstudio_config_dir`** / **`texstudio_ini_path`**（同时给出时 **`texstudio_ini_path` 优先**，见 **`note`**）。仍按 UTF-8 读出（可按 **`max_chars`** 截断）。**`include_parsed_hints=true`**（**`texstudio.ini`** 或 **`lastSession.txss`**，**v0.15.9+**）：启发式 **`parsed_hints`** 与可选 **`suggested_job_basename`**（**非**沙箱校验）。返回 **`resolution_mode`**、**`resolved_absolute_path`** 等。**非工程沙箱能力** |

#### 与 TeXstudio 的差异与建议

- **`workspace_root` / `main_tex`**：TeXstudio 往往把「当前工作目录」设为**主 `.tex` 所在文件夹**。若主文件在子目录里（例如 `某子目录/manuscript.tex`），本地 `cls`/`sty` 也常放在**同一目录**。**以前**若把仓库根当 `workspace_root` 但 `main_tex` 写成子目录路径，TeX 的输入搜索可能和 IDE 不一致。**实现上**：若 **`workspace_root` 等于主文件所在目录**（即你只传 `manuscript.tex` 且根就是那一层），会自动 **省略 `latexmk -cd`**，只传主文件名，避免重复 `-cd` 导致阻塞；若 **`workspace_root` 是祖先目录**（例如工程根 + `子路径/manuscript.tex`），默认仍会加 **`-cd`**。
- **仍建议**：若你在 IDE 里习惯「根目录就是主 `.tex` 那一层」，可以把 **`workspace_root` 指到该目录**，**`main_tex`** 只写文件名（如 `manuscript.tex`）；若习惯以仓库根为工程根，则用相对路径（如 `某子目录/manuscript.tex`）并依赖 `-cd`——子目录名随项目而定，没有固定名称。
- **耗时**：MCP 每次**新开进程**；`latexmk` 可能多轮调用 `pdflatex`/辅助工具，**总时间常长于 IDE 里「单次 pdflatex」**；杀毒软件扫描大量 `texmf`/临时文件也会拖慢——不以 TeXstudio 的秒级为硬指标。判因时先看返回里的 **`wall_clock_ms`**：它大说明主要在 TeX/`latexmk`；它小但总等待很久，多半是 **MCP / Agent** 侧的往返与 JSON。**`summary` 仅从 stdout/stderr 尾部截取后分析**（不整段扫描多 MB 输出），避免子进程结束后在 Python 里再卡住。
- **超时**：`timeout_seconds` 表示从 **spawn 起**到 **`communicate` 返回**的上限；超时后会 **kill 进程树**（见上）。若客户端仍长时间卡住，多为 **MCP/Agent 客户端**自身等待或管道未断开，与服务端返回时间分开排查。
- **编译返回体积**：工具结果仍为 **单次 JSON**（协议层无法真「流式分块」）；服务端在进程结束后立刻组包，并对 **stdout/stderr 做强截断**，同时提供 **`summary`**。需要全文日志请用 **`read_project_file`** 读工程内 `.log`，勿依赖超大 `stdout_tail`。
- **勿并行写同一产出目录**：对同一 `workspace_root` 不要无提示地并行 **`compile_latex_document`**、**`compile_latex_then_run_bibliography_on_job`**、**`run_bibtex_on_job`**、**`run_biber_on_job`**；服务端会以独占槽拒绝重叠调用（返回 **`concurrent_compile_blocked`**，并 **`concurrent_workspace_exclusive_blocked`** **v0.15.3+**）。**`compile_latex_then_run_bibliography_on_job`** 在执行期间会持续占用该槽直至 compile、bib 及可选的后续 latexmk 全部结束。多窗口/多 MCP 进程仍无法跨进程互斥。
- **TeXstudio 配置快照**：**`read_texstudio_profile_snapshot`** 会读 TeXstudio 配置文件（白名单：`texstudio.ini` / `lastSession.txss`）；可选用 **`texstudio_config_dir`** 或 **`texstudio_ini_path`**（同时给出时 **`texstudio_ini_path` 优先**，见返回 **`note`**），否则走系统约定目录。**不按 `workspace_root` 沙箱**；仅在信任的会话里使用。
