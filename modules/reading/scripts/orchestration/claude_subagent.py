from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))
importlib.invalidate_caches()
_core_common_module = sys.modules.get("core.common")
if _core_common_module is not None:
    _core_common_path = Path(str(getattr(_core_common_module, "__file__", ""))).resolve(strict=False)
    if _core_common_path != (_SCRIPTS_ROOT / "core" / "common.py").resolve(strict=False):
        sys.modules.pop("core.common", None)
_core_module = sys.modules.get("core")
if _core_module is not None:
    _core_path = str((_SCRIPTS_ROOT / "core").resolve(strict=False))
    _core_package_paths = getattr(_core_module, "__path__", None)
    if _core_package_paths is None:
        sys.modules.pop("core", None)
    else:
        _core_paths = [str(Path(str(path)).resolve(strict=False)) for path in _core_package_paths]
        if _core_path not in _core_paths:
            _core_package_paths.insert(0, _core_path)
try:
    _core_common_spec = importlib.util.find_spec("core.common")
except ModuleNotFoundError:
    _core_common_spec = None
if _core_common_spec is None:
    import types

    _core_path = str((_SCRIPTS_ROOT / "core").resolve(strict=False))
    _core_package = sys.modules.get("core")
    if _core_package is None or getattr(_core_package, "__path__", None) is None:
        _core_package = types.ModuleType("core")
        sys.modules["core"] = _core_package
    _core_package_paths = [
        str(Path(str(path)).resolve(strict=False))
        for path in getattr(_core_package, "__path__", [])
    ]
    _core_package.__path__ = [_core_path, *[path for path in _core_package_paths if path != _core_path]]
    _core_common_spec = importlib.util.spec_from_file_location("core.common", _SCRIPTS_ROOT / "core" / "common.py")
    if _core_common_spec is None or _core_common_spec.loader is None:
        raise ModuleNotFoundError("core.common")
    _core_common_module = importlib.util.module_from_spec(_core_common_spec)
    sys.modules["core.common"] = _core_common_module
    _core_common_spec.loader.exec_module(_core_common_module)

from core.common import first_json_object, has_unresolved_prose_latex_markup, write_json, write_text
from core.common import READING_ROOT, RUNTIME_ROOT, ensure_inside_output, ensure_inside_runtime, make_reading_paths_relative, relative_to_reading


NONRUNTIME_ARTIFACT_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".pdf",
    ".tex",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
STATIC_TOP_LEVEL_FILES = {
    ".gitignore",
    "README.md",
    "__init__.py",
    "main.py",
    "script_manifest.json",
}

FORMULA_STYLE_RULES = """数学公式格式：
- 方法栏目的公式只服务于本文原创的一条创新主线，并紧接通俗中文说明；每个公式直接定义作者提出的目标、机制、变换或约束，公式数量由这条创新主线本身决定。
- 所有数学命令使用 KaTeX 支持的标准命令；可逆关系固定写成 `\\rightleftharpoons`，箭头标注使用 `\\underset{...}{\\rightleftharpoons}`、`\\overset{...}{\\rightleftharpoons}` 或 `\\xrightleftharpoons[下方]{上方}`。
- 行内公式固定写成 `$x$`，两个 `$` 分别紧贴公式的首字符和尾字符。
- 所有展示公式的开始行和结束行各自只写 `$$`，公式正文位于两行定界符之间；分段函数、矩阵、对齐式、多行推导和包含多个赋值的公式统一使用这种展示格式。
- `cases`、`aligned` 等环境完整放在一对展示公式定界符内。分段函数的每个分支独占一行，使用 `&` 对齐，行末使用 `\\\\`；附加行距写成 `\\\\[4pt]`。多项赋值在 `aligned` 中各自独占一行。
- 单字母变量和索引使用默认数学斜体，例如 `$h_V^t$`、`$t_x$`、`$v_\\theta$`；描述性标签使用直立体，例如 `$A_{\\mathrm{query}}$`、`$h_V^{\\mathrm{template}}$`。
- 数学函数和算子使用规范命令，例如 `\\operatorname{softmax}`、`\\argmin`、`\\max`、`D_{\\mathrm{KL}}`；多字符名称使用 `\\operatorname{...}` 或 `\\mathrm{...}`。
- 范数使用成对的 `\\lVert ... \\rVert`，绝对值使用 `\\lvert ... \\rvert`，条件概率使用 `\\mid`，平行关系使用 `\\parallel`。LaTeX 命令通过空格、花括号或运算符明确结束，例如 `$p_\\theta \\parallel p_{\\mathrm{prior}}$`。
- 公式间距交给 LaTeX 的关系符、运算符和定界符处理；语义分组需要留白时使用一次 `\\quad`。
- 反引号只承载真实代码、文件路径和字面 token；数学记号直接使用公式定界符，例如 `$v_\\theta$`、`$x_{\\mathrm{C}\\alpha}$`、`$t_x$`。
- 关系符、数值和量纲组成一个完整公式，例如 `$x \\approx 12\\%$`；百分号在公式内写成 `\\%`。
- 模型名、数据集名、指标名、蛋白/化学/材料标识、突变记号、版本号和单位按论文中的普通文本写法呈现；它们参与数学表达时放入完整公式。
- 所有符号、数值、量纲和解释均来自论文正文语境，Markdown 定界符、LaTeX 命令边界和环境必须完整闭合。
"""

JSON_OUTPUT_RULES = """机器回执格式：
- 使用 JSON serializer 写出合法 JSON object，并让回执文件与 stdout 返回同一对象。
- JSON 字符串中的双引号、反斜杠、换行和控制字符由 serializer 完成转义。
- 回执只记录机器状态、证据范围和文件路径；精读正文只保存在单篇 `read.md`。
"""


def _metadata_value(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _paper_metadata(paper: dict[str, Any]) -> dict[str, Any]:
    metadata = paper.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _presentation_type_from_input(paper: dict[str, Any]) -> str:
    metadata = _paper_metadata(paper)
    venue = str(paper.get("venue") or metadata.get("venue") or paper.get("conference") or metadata.get("conference") or "").strip()
    source = str(paper.get("source") or metadata.get("source") or "").lower()
    conference_like = bool(
        venue
        and (
            sum(char.isupper() for char in venue) >= 2
            or "conference" in venue.lower()
            or any(marker in source for marker in ["openreview", "conference", "proceedings"])
        )
    )
    if not conference_like:
        return ""
    value = str(paper.get("presentation_type") or "")
    for key, label in [("oral", "Oral"), ("spotlight", "Spotlight"), ("poster", "Poster")]:
        if re.search(rf"(?<![A-Za-z]){key}(?![A-Za-z])", value, re.I):
            return label
    return ""


def _date_only(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    return ""


def _source_date(paper: dict[str, Any]) -> str:
    metadata = _paper_metadata(paper)
    for value in [
        paper.get("published"),
        paper.get("publication_date"),
        paper.get("date"),
        paper.get("updated"),
        metadata.get("published"),
        metadata.get("publication_date"),
        metadata.get("date"),
        metadata.get("updated"),
    ]:
        date_text = _date_only(value)
        if date_text:
            return date_text
    return ""


def _source_label_for_prompt(paper: dict[str, Any]) -> str:
    metadata = _paper_metadata(paper)
    date_text = _source_date(paper)
    venue = _metadata_value(paper, "venue", "conference", "published_journal", "journal") or _metadata_value(metadata, "venue", "conference", "published_journal", "journal")
    year = _metadata_value(paper, "year") or _metadata_value(metadata, "year")
    if not year:
        date_match = re.search(r"\b(20\d{2}|19\d{2})\b", " ".join(str(value or "") for value in [date_text, paper.get("url"), paper.get("pdf_url")]))
        year = date_match.group(1) if date_match else ""
    presentation = _presentation_type_from_input(paper)
    if venue and presentation:
        label = venue if not year or re.search(rf"\b{re.escape(year)}\b", venue) else f"{venue} {year}"
        return label if presentation.lower() in label.lower() else f"{label} {presentation}"
    source_text = " ".join(
        str(value or "")
        for value in [
            paper.get("source"),
            paper.get("venue"),
            paper.get("journal"),
            paper.get("published_journal"),
            metadata.get("source"),
            metadata.get("server"),
            metadata.get("venue"),
            metadata.get("journal"),
            metadata.get("published_journal"),
            paper.get("url"),
            paper.get("pdf_url"),
        ]
    ).lower()
    dated_sources = [
        ("arxiv", "arXiv"),
        ("biorxiv", "bioRxiv"),
        ("medrxiv", "medRxiv"),
        ("nature", "Nature"),
        ("science", "Science"),
    ]
    for marker, label in dated_sources:
        if marker in source_text:
            return f"{label} {date_text}" if date_text else f"{label} 日期未提供"
    if venue and year:
        return venue if re.search(rf"\b{re.escape(year)}\b", venue) else f"{venue} {year}"
    if venue:
        return venue
    source = _metadata_value(paper, "source") or _metadata_value(metadata, "source", "server") or "来源未提供"
    return f"{source} {year}".strip()


def _paper_url_for_prompt(paper: dict[str, Any], packet: dict[str, Any] | None = None) -> str:
    metadata = _paper_metadata(paper)
    packet = packet if isinstance(packet, dict) else {}
    for value in [
        paper.get("url"),
        paper.get("abs_url"),
        paper.get("html_url"),
        paper.get("input_article"),
        metadata.get("url"),
        metadata.get("abs_url"),
        metadata.get("html_url"),
        metadata.get("detail_url"),
        metadata.get("venue_url"),
        metadata.get("openreview_url"),
        packet.get("url"),
        packet.get("source_url"),
    ]:
        text = str(value or "").strip()
        if text.startswith("http"):
            return text
    return "未提供"


def _pdf_url_for_prompt(paper: dict[str, Any], packet: dict[str, Any] | None = None) -> str:
    metadata = _paper_metadata(paper)
    packet = packet if isinstance(packet, dict) else {}
    for value in [
        paper.get("pdf_url"),
        metadata.get("pdf_url"),
        metadata.get("paper_pdf_url"),
        metadata.get("openreview_pdf_url"),
        packet.get("pdf_url"),
    ]:
        text = str(value or "").strip()
        if text.startswith("http"):
            return text
        openreview_match = re.fullmatch(r"openreview://([A-Za-z0-9_-]+)/pdf", text)
        if openreview_match:
            return f"https://openreview.net/pdf?id={openreview_match.group(1)}"
    return "未提供"


def _markdown_link(label: str, url: str) -> str:
    text = str(url or "").strip()
    if not text or text == "未提供":
        return "未提供"
    return f"[{label}](<{text}>)"


def article_metadata_markdown_lines(paper: dict[str, Any], packet: dict[str, Any] | None = None) -> list[str]:
    paper_url = _paper_url_for_prompt(paper, packet)
    pdf_url = _pdf_url_for_prompt(paper, packet)
    return [
        f"- **来源：** {_source_label_for_prompt(paper)}",
        f"- **论文链接：** URL：{_markdown_link('论文页面', paper_url)}；PDF：{_markdown_link('PDF', pdf_url)}",
    ]


def build_deep_read_prompt(
    *,
    paper: dict[str, Any],
    packet: dict[str, Any],
    run_path: Path,
    output_path: Path,
    article_md_path: Path | None = None,
) -> str:
    text_path = str(packet.get("text_path") or "")
    text_path_abs = Path(text_path) if text_path else Path()
    if text_path and not text_path_abs.is_absolute():
        text_path_abs = READING_ROOT / text_path
    try:
        text_path_local = relative_to_reading(text_path_abs) if text_path else ""
    except Exception:
        text_path_local = text_path
    try:
        text_path_run = text_path_abs.relative_to(run_path.resolve(strict=False)).as_posix() if text_path else ""
    except Exception:
        text_path_run = text_path_local
    try:
        output_path_run = output_path.resolve(strict=False).relative_to(run_path.resolve(strict=False)).as_posix()
    except Exception:
        output_path_run = relative_to_reading(output_path)
    article_md_path = article_md_path or (run_path / "read.md")
    try:
        article_md_run = article_md_path.resolve(strict=False).relative_to(run_path.resolve(strict=False)).as_posix()
    except Exception:
        article_md_run = relative_to_reading(article_md_path)
    title = str(paper.get("title") or packet.get("title") or "未命名论文")
    metadata = _paper_metadata(paper)
    abstract_en = str(paper.get("abstract") or paper.get("abstract_en") or metadata.get("abstract") or "").strip()
    raw_abstract_zh = str(paper.get("abstract_zh") or metadata.get("abstract_zh") or "").strip()
    abstract_zh = raw_abstract_zh
    if abstract_zh and (has_unresolved_prose_latex_markup(abstract_zh) or len(re.findall(r"[\u4e00-\u9fff]", abstract_zh)) < 4):
        abstract_zh = ""
    if abstract_zh:
        abstract_rule = "`摘要` 固定逐字写入下方中文摘要全文，包含原有标点。"
        abstract_label = "中文摘要（固定输入）"
        abstract_input = abstract_zh
    else:
        abstract_rule = "`摘要` 固定写成下方英文原文摘要的完整中文翻译。"
        abstract_label = "英文原文摘要（翻译为中文）"
        abstract_input = abstract_en or "未提供"
    source_label = _source_label_for_prompt(paper)
    paper_url = _paper_url_for_prompt(paper, packet)
    pdf_url = _pdf_url_for_prompt(paper, packet)
    metadata_lines = article_metadata_markdown_lines(paper, packet)
    return f"""你是 Reading 模块为这一篇论文启动的专用精读 subagent。请只处理下面这一篇论文，并直接产出单篇 Markdown 精读正文。

硬性规则：
1. 你就是本篇论文的阅读 subagent，本次调用由你直接完成阅读和落盘。
2. 固定只在当前单篇运行目录工作；读取范围限定为当前运行目录下的文件。
3. 可写文件严格限定为两个：用户阅读正文 `{article_md_run}`，以及同一篇论文的机器回执 `{output_path_run}`。临时目录限定为当前运行目录下的 `tmp`。
4. 输出必须是中文、论文内容导向，内容范围限定为论文精读正文和机器回执。
5. 精读必须基于正文文件 `{text_path_run or text_path_local}` 的全量内容；可以分段读取，覆盖范围必须超过文件开头、节选和摘要。正文证据充分时完成精读；证据缺口写入机器回执。
6. 单篇 Markdown 必须是完整用户阅读正文，并按顺序使用固定结构：
   - `# 论文标题`
   - `{metadata_lines[0]}`
   - `{metadata_lines[1]}`
   - `## 摘要`
   - `## 动机与核心创新`
   - `## 方法`
   - `## 实验结果`
   - `## 优缺点总结`
   单篇 `read.md` 的 Markdown 元素严格限定为上述固定标题、两行元数据、正文段落、数学公式和论文链接；标题后保留一个空行，两行元数据连续书写，元数据后保留一个空行，栏目之间只保留一个空行。
7. 顶部元数据只写上面两行：`来源` 和 `论文链接`，并使用 `- ` 项目符号。`来源` 必须精简为会议/期刊加年份，例如 `ICLR 2026`、`NeurIPS 2025`；预印本或期刊流使用来源名加精确日期，例如 `arXiv 2026-06-01`、`Nature 2026-07-03`。`论文链接` 必须把输入/元数据中给出的 URL 和 PDF 写成 Markdown 链接，格式为 `URL：[论文页面](<...>)；PDF：[PDF](<...>)`；缺失项写 `未提供`。顶部元数据的完整内容就是这两行。
8. 上述每个正文栏目都必须有内容，正文风格必须简明、论文内容导向：
   - {abstract_rule}
   - `动机与核心创新` 写两段：第一段以 `动机：` 开头，第二段以 `核心创新：` 开头。两段合计必须控制在 200-250 个中文字符，只写为什么需要这项工作以及本文真正新增的东西。
   - `方法` 聚焦作者自己提出的创新方法或机制。必须结合数学公式，用通俗中文解释公式如何支持创新机制；控制在 300-400 个中文字符内。
   - `实验结果` 只用几句话概括作者做了哪类实验以及总体效果，控制在 150 字以内。
   - `优缺点总结` 控制在 100 字以下，同时点出主要优点和主要缺点/风险边界。
9. 必须在本次调用内一次性写出格式正确的单篇 Markdown 和机器回执 JSON，并完成 Markdown/LaTeX 格式整理。
- 输入正文或摘要中，公式定界符之外的任意 LaTeX 命令均视为来源排版标记；理解其参数与相邻字符组成的完整原文后再翻译或概括，输出可读 Markdown，不得逐字复制这些命令。
{FORMULA_STYLE_RULES}
{JSON_OUTPUT_RULES}
10. `{output_path_run}` 必须是严格合法 JSON 对象；stdout 最终内容也必须返回同一份严格合法 JSON 对象。stdout 第一个非空字符必须是 `{{`，最后一个非空字符必须是 `}}`。stdout 内容严格限定为 JSON 对象本身。

论文信息：
- 标题：{title}
- 来源：{source_label}
- URL：{paper_url}
- PDF：{pdf_url}
- 正文字数：{packet.get("full_text_chars") or packet.get("text_chars") or 0}
- 正文路径（当前运行目录相对）：`{text_path_run or "未生成"}`
- 正文路径（本工作区相对）：`{text_path_local or "未生成"}`

{abstract_label}：
{abstract_input}

机器回执 JSON 顶层字段必须包括：
`status`, `source`, `paper_id`, `title`, `subagent_deep_read`, `article_markdown_path`, `deep_read_audit`。

`subagent_deep_read` 必须是布尔值：你作为本篇专用阅读 subagent 已基于正文完成精读并写出单篇 Markdown 时写 `true`；细节放入 `deep_read_audit`。
`article_markdown_path` 必须写为 `{article_md_run}`，且该文件必须已由你直接写好。
机器回执 JSON 只包含机器状态和路径。用户阅读正文只以 `{article_md_run}` 为准。

`deep_read_audit` 至少包括：
`mode=\"dedicated_claude_subagent\"`, `subagent_used=true`, `status`, `text_path`, `evidence_chars`, `article_markdown_path`, `article_markdown_written`。
其中 `subagent_used=true` 表示本次 Claude 调用本身就是专用阅读 subagent；`evidence_chars` 必须是你实际检查过的正文字符数，若成功完成全文精读，应不小于上方“正文字数”。

读取全文后写出 `{article_md_run}`，再写出 `{output_path_run}`，最后只在 stdout 回复同一份严格合法 JSON 机器回执。stdout 第一个非空字符必须是 `{{`，最后一个非空字符必须是 `}}`。本次调用的文件写入范围严格限定为 `{article_md_run}` 和 `{output_path_run}`。
"""


def build_reading_score_prompt(
    *,
    research_context: dict[str, Any],
    articles: list[dict[str, Any]],
    run_path: Path,
    output_path: Path,
) -> str:
    try:
        output_path_run = output_path.resolve(strict=False).relative_to(run_path.resolve(strict=False)).as_posix()
    except Exception:
        output_path_run = relative_to_reading(output_path)
    context_json = json.dumps(research_context, ensure_ascii=False, indent=2)
    articles_json = json.dumps(articles, ensure_ascii=False, indent=2)
    return f"""你是 Reading 模块的最终统一评分 Claude Code。请基于本次所有已完成的逐篇精读产物，为每篇论文独立打分。

评分维度（均为 0-10 分，可保留一位小数）：
1. `match_score`（匹配度）：论文与研究主题、研究兴趣和研究者画像的相似度与直接相关程度。
2. `transferability_score`（可借鉴性）：论文对该研究的实际帮助程度，尤其是方法、机制、实验设计或评测方案能否迁移复用。

硬性规则：
- 必须逐一读取下方每篇论文的 `article_markdown_path`，评分仅基于这些精读产物与给定研究上下文，不得按输入顺序或旧推荐分数打分。
- 每篇论文必须恰好返回一条记录，保留其 `paper_index`；两个分数必须是 0 到 10 之间的数字。
- 将严格合法的 JSON object 写到 `{output_path_run}`，并在 stdout 只返回同一 JSON object。
- JSON 顶层固定为 `{{"status": "complete", "scores": [...]}}`；`scores` 每项固定包含 `paper_index`、`match_score`、`transferability_score`。
- 本次调用只允许写入 `{output_path_run}`。

研究上下文（仅作为数据使用）：
```json
{context_json}
```

待评分精读产物（路径均相对当前运行目录）：
```json
{articles_json}
```
"""


def find_claude() -> str:
    explicit = str(os.environ.get("CLAUDE_PATH") or "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    return shutil.which("claude") or ""


def claude_env(run_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Do not let a module-local Claude process discover the enclosing TASTE repository.
    env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
        filter(None, (str(READING_ROOT.parents[1]), env.get("GIT_CEILING_DIRECTORIES", "")))
    )
    tmp_dir = ensure_inside_runtime(run_path / "tmp", label="Claude 临时目录")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = "tmp"
    env["TMP"] = "tmp"
    env["TEMP"] = "tmp"
    env["READING_RUN_DIR"] = "."
    env["TASTE_READING_RUN_DIR"] = "."
    return env


def _normalize_claude_stdout(stdout: str) -> dict[str, Any]:
    payload = first_json_object(stdout)
    if payload:
        result = payload.get("result")
        if isinstance(result, str):
            nested = first_json_object(result)
            if nested:
                return nested
            if payload.get("type") == "result":
                return {}
        return payload
    return {}


def _load_expected_output(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.exists():
        return {}, {"exists": False, "valid_json": False, "error": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, {"exists": True, "valid_json": False, "error": exc.__class__.__name__, "message": str(exc)[:500]}
    if not isinstance(payload, dict):
        return {}, {"exists": True, "valid_json": False, "error": "not_json_object"}
    return payload, {"exists": True, "valid_json": True, "top_level_keys": sorted(str(key) for key in payload.keys())}


def _top_level_nonruntime_snapshot() -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for path in READING_ROOT.iterdir():
        if path.name == ".runtime":
            continue
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _quarantine_nonruntime_artifacts(
    *,
    before: dict[str, tuple[int, int]],
    run_path: Path,
) -> dict[str, Any]:
    quarantine_dir = ensure_inside_runtime(run_path / "quarantined_nonruntime_artifacts", label="越界产物隔离目录")
    detected: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for path in sorted(READING_ROOT.iterdir(), key=lambda item: item.name):
        if path.name == ".runtime":
            continue
        if not path.is_file() or path.suffix.lower() not in NONRUNTIME_ARTIFACT_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        current = (stat.st_mtime_ns, stat.st_size)
        previous = before.get(str(path))
        if previous == current:
            continue
        should_quarantine = previous is None or path.name not in STATIC_TOP_LEVEL_FILES
        item = {
            "path": str(path),
            "name": path.name,
            "previously_existed": previous is not None,
            "size": stat.st_size,
            "quarantined": False,
        }
        if should_quarantine:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            target = quarantine_dir / path.name
            index = 1
            while target.exists():
                target = quarantine_dir / f"{path.stem}_{index}{path.suffix}"
                index += 1
            try:
                path.replace(target)
                item["quarantined"] = True
                item["quarantine_path"] = str(target)
            except OSError as exc:
                item["quarantine_error"] = exc.__class__.__name__
                item["quarantine_message"] = str(exc)[:300]
            detected.append(item)
        else:
            item["reason_not_quarantined"] = "static_top_level_file"
            ignored.append(item)
    return {
        "status": "passed" if not detected else "failed_nonruntime_artifact_detected",
        "problem_count": len(detected),
        "runtime_root": str(RUNTIME_ROOT),
        "run_path": str(run_path),
        "detected": detected,
        "ignored_static_top_level_changes": ignored,
    }


EXTERNAL_TEMP_PROCESS_MARKERS = [
    ("write_to_tmp", "cat > /tmp/"),
    ("write_to_tmp", "> /tmp/"),
    ("write_to_tmp", ">> /tmp/"),
    ("write_to_tmp", "open('/tmp/"),
    ("write_to_tmp", 'open("/tmp/'),
    ("write_to_tmp", "Path('/tmp/"),
    ("write_to_tmp", 'Path("/tmp/'),
    ("write_to_tmp", "cat > /var/tmp/"),
    ("write_to_tmp", "> /var/tmp/"),
    ("write_to_tmp", ">> /var/tmp/"),
    ("write_to_tmp", "open('/var/tmp/"),
    ("write_to_tmp", 'open("/var/tmp/'),
    ("write_to_tmp", "Path('/var/tmp/"),
    ("write_to_tmp", 'Path("/var/tmp/'),
    ("execute_tmp_helper", "python /tmp/"),
    ("execute_tmp_helper", "python3 /tmp/"),
    ("execute_tmp_helper", "bash /tmp/"),
    ("execute_tmp_helper", "sh /tmp/"),
    ("execute_tmp_helper", "python /var/tmp/"),
    ("execute_tmp_helper", "python3 /var/tmp/"),
    ("execute_tmp_helper", "bash /var/tmp/"),
    ("execute_tmp_helper", "sh /var/tmp/"),
    ("tmp_output_path", 'output_path = "/tmp/'),
    ("tmp_output_path", "output_path='/tmp/"),
    ("tmp_output_path", 'output_path = "/var/tmp/'),
    ("tmp_output_path", "output_path='/var/tmp/"),
]

READING_CONTENT_RECEIPT_KEYS = {
    "reading",
    "abstract_zh",
    "summary",
    "motivation_zh",
    "problem",
    "method_family_zh",
    "method_details_zh",
    "method",
    "experiments_zh",
    "experiments",
    "limitations_zh",
    "limitations",
    "method_advantages_zh",
    "method_disadvantages_zh",
    "evidence_boundary_zh",
}


def _strip_reading_content_from_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in READING_CONTENT_RECEIPT_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _proc_ppid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        after_name = stat.rsplit(") ", 1)[1]
        return int(after_name.split()[1])
    except Exception:
        return None


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _descendant_pids(root_pid: int) -> set[int]:
    ppid_by_pid: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppid = _proc_ppid(pid)
        if ppid is not None:
            ppid_by_pid[pid] = ppid
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in ppid_by_pid.items():
            if pid not in descendants and ppid in descendants:
                descendants.add(pid)
                changed = True
    return descendants


def _scan_external_temp_processes(root_pid: int, seen: set[tuple[int, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for pid in sorted(_descendant_pids(root_pid)):
        cmdline = _proc_cmdline(pid)
        if not cmdline:
            continue
        for code, marker in EXTERNAL_TEMP_PROCESS_MARKERS:
            key = (pid, marker)
            if key not in seen and marker in cmdline:
                seen.add(key)
                findings.append({"code": code, "marker": marker, "pid": pid, "cmdline": cmdline[:2000]})
    return findings


def _start_external_temp_process_monitor(root_pid: int) -> tuple[threading.Event, list[dict[str, Any]], threading.Thread]:
    stop_event = threading.Event()
    findings: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    def worker() -> None:
        while not stop_event.is_set():
            findings.extend(_scan_external_temp_processes(root_pid, seen))
            time.sleep(0.05)
        findings.extend(_scan_external_temp_processes(root_pid, seen))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return stop_event, findings, thread


def _external_temp_artifact_audit(
    stdout: str,
    stderr: str,
    run_path: Path,
    *,
    process_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Detect helper artifacts that bypass the per-paper runtime directory."""
    runtime_tmp = str(ensure_inside_runtime(run_path / "tmp", label="Claude 临时目录"))
    combined = "\n".join([stdout or "", stderr or ""])
    findings: list[dict[str, Any]] = []
    for code, marker in EXTERNAL_TEMP_PROCESS_MARKERS:
        if marker in combined:
            findings.append({"code": code, "marker": marker, "source": "stdout_stderr"})
    for finding in process_findings or []:
        item = dict(finding)
        item.setdefault("source", "process_tree")
        findings.append(item)
    cleanup: list[dict[str, Any]] = []
    for finding in findings:
        cmdline = str(finding.get("cmdline") or "")
        for candidate in sorted(set(re.findall(r"(?<![A-Za-z0-9_.-])(/(?:tmp|var/tmp)/[A-Za-z0-9_.-]+)", cmdline))):
            path = Path(candidate)
            item = {"path": candidate, "removed": False}
            try:
                if path.exists() and path.is_file():
                    path.unlink()
                    item["removed"] = True
            except OSError as exc:
                item["error"] = exc.__class__.__name__
                item["message"] = str(exc)[:300]
            cleanup.append(item)
    return {
        "status": "passed" if not findings else "failed_external_temp_artifact_detected",
        "problem_count": len(findings),
        "blocking": bool(findings),
        "enforcement": "blocking" if findings else "passed",
        "runtime_tmp": runtime_tmp,
        "findings": findings,
        "external_temp_cleanup": cleanup,
    }


def run_claude_deep_read(
    *,
    prompt_path: Path,
    run_path: Path,
    expected_output_path: Path,
    timeout_sec: int = 1800,
    mode: str = "auto",
    receipt_dir_name: str = "claude",
) -> dict[str, Any]:
    run_path = ensure_inside_output(run_path, label="Claude 运行目录")
    expected_output_path = ensure_inside_output(expected_output_path, label="Claude 输出文件")
    expected_output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_dir_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(receipt_dir_name or "claude")).strip("._-") or "claude"
    claude_dir = ensure_inside_output(run_path / receipt_dir_name, label="Claude 回执目录")
    claude_dir.mkdir(parents=True, exist_ok=True)
    prompt = prompt_path.read_text(encoding="utf-8")
    if mode == "prepare":
        return make_reading_paths_relative({
            "status": "prepared_for_claude_subagent",
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "run_executed": False,
            "nonruntime_artifact_audit": {
                "status": "skipped_prepare_mode",
                "problem_count": 0,
                "runtime_root": str(RUNTIME_ROOT),
                "run_path": str(run_path),
                "detected": [],
            },
        })
    claude = find_claude()
    if not claude:
        return make_reading_paths_relative({
            "status": "blocked_claude_code_not_found",
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "run_executed": False,
            "nonruntime_artifact_audit": {
                "status": "skipped_claude_missing",
                "problem_count": 0,
                "runtime_root": str(RUNTIME_ROOT),
                "run_path": str(run_path),
                "detected": [],
            },
        })
    try:
        expected_output_path.unlink(missing_ok=True)
    except OSError:
        pass
    cmd = [
        claude,
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--disallowedTools",
        "Agent,Task,EnterWorktree,ExitWorktree",
        "--add-dir",
        ".",
    ]
    started = time.time()
    nonruntime_before = _top_level_nonruntime_snapshot()
    process_findings: list[dict[str, Any]] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=run_path,
            env=claude_env(run_path),
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stop_monitor, process_findings, monitor_thread = _start_external_temp_process_monitor(proc.pid)
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=max(60, int(timeout_sec)))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            stdout, stderr = proc.communicate()
            stop_monitor.set()
            monitor_thread.join(timeout=2.0)
            exc.stdout = stdout
            exc.stderr = stderr
            raise exc
        stop_monitor.set()
        monitor_thread.join(timeout=2.0)
        completed = subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            stdout,
            stderr,
        )
        proc = completed
        stdout_path = claude_dir / "stdout.json"
        stderr_path = claude_dir / "stderr.log"
        write_text(stdout_path, proc.stdout)
        write_text(stderr_path, proc.stderr)
        file_payload, expected_output_audit = _load_expected_output(expected_output_path)
        stdout_payload = _normalize_claude_stdout(proc.stdout)
        payload_source = "expected_output_path" if isinstance(file_payload, dict) and file_payload else "stdout"
        payload = file_payload if payload_source == "expected_output_path" else stdout_payload
        if isinstance(payload, dict):
            stripped_payload = _strip_reading_content_from_receipt(payload)
            if stripped_payload != payload:
                payload = stripped_payload
                if payload_source == "expected_output_path":
                    write_json(expected_output_path, payload)
                    expected_output_audit = {
                        "exists": True,
                        "valid_json": True,
                        "top_level_keys": sorted(str(key) for key in payload.keys()),
                        "reading_content_stripped": True,
                    }
        status = "claude_completed" if proc.returncode == 0 else "claude_failed"
        if isinstance(payload, dict) and str(payload.get("status") or "").strip():
            status = str(payload.get("status"))
        expected_file_ok = expected_output_audit.get("exists") is True and expected_output_audit.get("valid_json") is True
        if proc.returncode == 0 and not expected_file_ok:
            status = "blocked_expected_output_missing_or_invalid"
        nonruntime_audit = _quarantine_nonruntime_artifacts(before=nonruntime_before, run_path=run_path)
        external_temp_audit = _external_temp_artifact_audit(proc.stdout, proc.stderr, run_path, process_findings=process_findings)
        if int(nonruntime_audit.get("problem_count") or 0) > 0:
            status = "blocked_nonruntime_artifact_created"
        if int(external_temp_audit.get("problem_count") or 0) > 0:
            status = "blocked_external_temp_artifact_created"
        receipt = {
            "status": status,
            "return_code": proc.returncode,
            "run_executed": True,
            "duration_seconds": round(time.time() - started, 3),
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "expected_output_audit": expected_output_audit,
            "nonruntime_artifact_audit": nonruntime_audit,
            "external_temp_artifact_audit": external_temp_audit,
            "payload_source": payload_source,
            "result_payload": payload if isinstance(payload, dict) else {},
        }
        write_json(claude_dir / "claude_receipt.json", receipt)
        return make_reading_paths_relative(receipt)

    except subprocess.TimeoutExpired as exc:
        receipt = {
            "status": "blocked_claude_timeout",
            "run_executed": True,
            "duration_seconds": round(time.time() - started, 3),
            "timeout_sec": timeout_sec,
            "prompt_path": str(prompt_path),
            "expected_output_path": str(expected_output_path),
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }
        receipt["nonruntime_artifact_audit"] = _quarantine_nonruntime_artifacts(before=nonruntime_before, run_path=run_path)
        receipt["external_temp_artifact_audit"] = _external_temp_artifact_audit(
            (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            run_path,
            process_findings=process_findings,
        )
        write_json(claude_dir / "claude_receipt.json", receipt)
        return make_reading_paths_relative(receipt)
