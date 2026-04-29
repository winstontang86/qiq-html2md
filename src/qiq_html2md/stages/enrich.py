"""Enrich Stage —— 处理 assets / tables / formulas / references。

架构约束（§8.3）：
- 四个子任务**只读 DOM 语义**：它们读内容，只在原始 DOM 上打占位锚点 `data-h2m-id`，不重写结构。
- 各子任务产出独立 artifact 列表。
- Emit Stage 通过锚点 id 找对应 artifact，再生成 Markdown 片段。

阶段二已落地：
- assets（图片下载，同阶段一）
- tables：复杂度评分 + Markdown/HTML 两级（图片级降级留到阶段四）
- formulas：LaTeX（MathJax annotation） + MathML；inline/block 自动判断
- references：保留参考文献列表，供 Emit 在文末渲染

不依赖浏览器渲染；公式依赖页面自带 MathML / TeX annotation。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from qiq_html2md.adapters_site.base import resolve as resolve_adapter
from qiq_html2md.core.errors import RetryableError
from qiq_html2md.core.types import Context, StageResult
from qiq_html2md.infra import http
from qiq_html2md.infra.fs_sandbox import FsSandbox
from qiq_html2md.infra.html_attrs import class_str, int_attr, str_attr

# DOM 锚点属性名：Emit 据此找到 artifact
ANCHOR_ATTR = "data-h2m-id"


class EnrichStage:
    name: Literal["enrich"] = "enrich"

    def __init__(self, *, allow_file_scheme: bool = False) -> None:
        self.allow_file_scheme = allow_file_scheme

    def mutate(self, delta: dict[str, Any]) -> EnrichStage:  # noqa: ARG002
        return EnrichStage(allow_file_scheme=self.allow_file_scheme)

    def run(self, ctx: Context) -> StageResult:
        t0 = time.monotonic()
        if ctx.extract is None:
            raise RetryableError("extract_missing", reason="text_too_short")

        clean_html: str = ctx.extract["clean_html"]
        final_url: str = ctx.acquire["final_url"] if ctx.acquire else ctx.request.url
        adapter = resolve_adapter(final_url)

        image_mode: str = ctx.strategy.get("image_mode", "download")
        table_mode: str = ctx.strategy.get("table_mode", "auto")
        formula_mode: str = ctx.strategy.get("formula_mode", "auto")
        include_refs: bool = ctx.request.include_references

        soup = BeautifulSoup(clean_html, "lxml")

        # --- 1) 参考文献：先抽出整节（pop 出 DOM，避免 Emit 重复渲染） ---
        refs, refs_warnings = _extract_refs(soup, adapter.refs_selector, include_refs)

        # --- 2) 公式：打锚点 + artifact ---
        formulas, formula_warnings = _process_formulas(soup, formula_mode)

        # --- 3) 表格：打锚点 + artifact ---
        tables, table_warnings = _process_tables(soup, table_mode)

        # --- 4) 图片：下载；阶段一行为保留 ---
        sandbox = FsSandbox(ctx.output_dir)
        sandbox.mkdirp("assets/images")
        images, image_warnings = _process_images(
            soup,
            base_url=final_url,
            sandbox=sandbox,
            mode=image_mode,
            allow_file_scheme=self.allow_file_scheme,
        )
        # 内联 SVG：作为独立 artifact（kind=image, mode=svg_inline）
        svg_items, svg_warnings = _process_inline_svgs(soup, start_idx=len(images) + 1)
        images.extend(svg_items)

        # --- 5) 截图降级：表格复杂度过高、公式无源、强制 image 模式 ---
        #       共享一次浏览器会话，减少开销
        screenshots_warnings = _apply_screenshot_fallback(
            ctx=ctx,
            sandbox=sandbox,
            soup=soup,
            tables=tables,
            formulas=formulas,
            table_mode=table_mode,
            formula_mode=formula_mode,
        )

        output = {
            "images": images,
            "tables": tables,
            "formulas": formulas,
            "refs": refs,
            # Enrich 可能对 DOM 打过锚点/摘除过 refs，把新的 HTML 回写给 Emit
            "annotated_html": str(soup),
            "enrich_stats": {
                "image_total": len(images) + sum(
                    1 for w in image_warnings if w.get("code") == "image_no_src"
                ),
                "image_ok": sum(1 for i in images if i.get("local_path")),
                "image_failed": sum(
                    1 for w in image_warnings if w.get("code") == "image_fetch_failed"
                ),
                "table_total": len(tables),
                "table_markdown": sum(1 for t in tables if t.get("mode") == "markdown"),
                "table_html": sum(1 for t in tables if t.get("mode") == "html"),
                "table_image": sum(1 for t in tables if t.get("mode") == "image"),
                "formula_total": len(formulas),
                "formula_latex": sum(1 for f in formulas if f.get("mode") == "latex"),
                "formula_mathml": sum(1 for f in formulas if f.get("mode") == "mathml"),
                "formula_image": sum(1 for f in formulas if f.get("mode") == "image"),
                "ref_total": len(refs),
            },
        }

        warnings: list[dict[str, Any]] = []
        warnings.extend(refs_warnings)
        warnings.extend(formula_warnings)
        warnings.extend(table_warnings)
        warnings.extend(image_warnings)
        warnings.extend(svg_warnings)
        warnings.extend(screenshots_warnings)

        return StageResult(
            stage="enrich",
            output=output,
            warnings=warnings,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------


def _extract_refs(
    soup: BeautifulSoup,
    selector: str | None,
    include_refs: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """抽取参考文献。匹配到则从 DOM 中 pop 出，避免 Emit 重复渲染。"""
    if not include_refs:
        return [], []

    node: Tag | None = None
    if selector:
        picked = soup.select_one(selector)
        if isinstance(picked, Tag):
            node = picked

    if node is None:
        # 兜底：找 id/class 含 reference/bibliography 的节点；或 <h2>References</h2> 所在 section
        for candidate in soup.find_all(True):
            if not isinstance(candidate, Tag):
                continue
            cid = candidate.get("id")
            cid_str = cid if isinstance(cid, str) else ""
            attrs = f"{cid_str} {class_str(candidate)}".lower()
            if "bibliograph" in attrs or "reference" in attrs:
                node = candidate
                break
        if node is None:
            for h in soup.find_all(["h1", "h2", "h3"]):
                if isinstance(h, Tag) and "reference" in h.get_text("", strip=True).lower():
                    # 把同级后续节点合成 refs
                    parent = h.parent
                    if isinstance(parent, Tag):
                        node = parent
                        break

    if node is None:
        return [], []

    refs: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # 首选 <li>；其次 <p>
    items = node.find_all("li") or node.find_all("p")
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, Tag):
            continue
        text = item.get_text(" ", strip=True)
        if not text:
            continue
        refs.append({"idx": idx, "text": text})

    if not refs:
        warnings.append({"code": "refs_section_empty"})
    # 从 DOM 中移除
    node.decompose()
    return refs, warnings


# ---------------------------------------------------------------------------
# formulas
# ---------------------------------------------------------------------------


def _process_formulas(
    soup: BeautifulSoup,
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """提取公式并打锚点。

    识别顺序：
    - arXiv/LaTeXML 的公式包装 `<table class="ltx_equation|ltx_equationgroup|ltx_eqn_table">`：
      整张表格是一个公式块（可能含行号 `(1)` 等），将其替换为公式锚点节点，
      避免后续 `_process_tables` 误识别。
    - `<math>` 节点（MathML）；内部可能含 `<annotation encoding="application/x-tex">` → LaTeX。
    - `<mjx-container>` / `<span class="math">` / `script[type="math/tex"]` —— MathJax/KaTeX。
    """
    formulas: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # 0) arxiv/LaTeXML：<table class="ltx_equation..."> 公式包装
    #    需要在 `<math>` 扫描之前处理，否则内部 math 节点会先被 2) 号规则拿走锚点。
    _process_latex_equation_tables(soup, formulas, warnings, mode)

    # 1) <math> —— MathML 原生节点
    for m in soup.find_all("math"):
        if not isinstance(m, Tag):
            continue
        # 已被公式容器处理过的 math（equation table 内部）会被整体替换掉，这里查不到；
        # 若意外还有锚点（例如上游已打过），跳过。
        if m.get(ANCHOR_ATTR):
            continue
        latex = _find_tex_annotation(m)
        is_block = str_attr(m, "display").lower() == "block"
        fid = f"f{len(formulas) + 1:03d}"
        m.attrs[ANCHOR_ATTR] = fid
        if mode == "mathml" or (mode != "latex" and not latex):
            formulas.append(
                {
                    "id": fid,
                    "mode": "mathml",
                    "inline": not is_block,
                    "mathml": str(m),
                }
            )
        else:
            formulas.append(
                {
                    "id": fid,
                    "mode": "latex",
                    "inline": not is_block,
                    "latex": latex or "",
                }
            )
            if not latex:
                warnings.append({"code": "formula_latex_missing", "id": fid})

    # 2) script[type="math/tex"] —— MathJax 源脚本
    for s in soup.find_all("script"):
        if not isinstance(s, Tag):
            continue
        t = str_attr(s, "type").lower()
        if t not in ("math/tex", "math/tex; mode=display"):
            continue
        tex = s.get_text() or ""
        is_block = "mode=display" in t
        fid = f"f{len(formulas) + 1:03d}"
        s.attrs[ANCHOR_ATTR] = fid
        formulas.append(
            {
                "id": fid,
                "mode": "latex",
                "inline": not is_block,
                "latex": tex.strip(),
            }
        )

    # 3) mjx-container —— MathJax 渲染后的 DOM；一般靠近前后有 <script type="math/tex"> 源码
    for c in soup.find_all("mjx-container"):
        if not isinstance(c, Tag):
            continue
        # 如果同一公式已经被 script 规则命中（内部/外部），忽略
        if c.get(ANCHOR_ATTR):
            continue
        is_block = (
            str_attr(c, "display").lower() == "true"
            or str_attr(c, "jax").lower() == "chtml"
        )
        fid = f"f{len(formulas) + 1:03d}"
        c.attrs[ANCHOR_ATTR] = fid
        formulas.append(
            {
                "id": fid,
                "mode": "mathml",
                "inline": not is_block,
                "mathml": "",  # 无法稳定提取时留空
            }
        )
        warnings.append({"code": "formula_source_unknown", "id": fid})

    return formulas, warnings


def _find_tex_annotation(math: Tag) -> str | None:
    """从 <math> 中找 LaTeX 源，优先顺序：
    1. <annotation encoding="application/x-tex">（MathML semantics 块）；
    2. <math alttext="...">（LaTeXML/arxiv 常用，直接把 LaTeX 放属性里）。
    """
    for ann in math.find_all("annotation"):
        if not isinstance(ann, Tag):
            continue
        enc = str_attr(ann, "encoding").lower()
        if "tex" in enc:
            txt = ann.get_text() or ""
            txt = txt.strip()
            if txt:
                return txt
    # 回退：math 的 alttext 属性
    alt = str_attr(math, "alttext").strip()
    return alt or None


# arxiv / LaTeXML 等式包装的 CSS 类集合
_LATEX_EQ_CLASSES: tuple[str, ...] = (
    "ltx_equation",
    "ltx_equationgroup",
    "ltx_eqn_table",
)


def _is_latex_equation_table(tag: Tag) -> bool:
    if tag.name != "table":
        return False
    cls = class_str(tag).lower()
    return any(k in cls for k in _LATEX_EQ_CLASSES)


def _process_latex_equation_tables(
    soup: BeautifulSoup,
    formulas: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    mode: str,
) -> None:
    """把 arxiv/LaTeXML 的 equation / equationgroup 表格整体替换为公式锚点节点。

    行为：
    - 若表格里是 **单一** `<math>`：直接作为一条 latex/mathml 公式；
      自动探测同一 tr 内的等式编号（形如 `(1)`），附加到 LaTeX 末尾的 `\tag{...}`（可选）。
    - 若表格里有 **多个** `<math>`（equationgroup，每行一个）：每个 math 成为一条独立公式，
      在 DOM 里用一个 `<div>` 容器承载，每条公式放在自己的 `<div data-h2m-id="...">` 里，
      这样 Emit 会把它们当独立 block 输出。
    - 这些 equation table 都会从 DOM 中移除（replace_with）。
    """
    # 使用 list() 固化，迭代中会删除节点
    for tbl in list(soup.find_all("table")):
        if not isinstance(tbl, Tag):
            continue
        if not _is_latex_equation_table(tbl):
            continue

        # 收集表内的 math 节点（按文档顺序），以及每行的等式编号
        rows_info: list[dict[str, Any]] = []
        for tr in tbl.find_all("tr"):
            if not isinstance(tr, Tag):
                continue
            math_nodes: list[Tag] = [m for m in tr.find_all("math") if isinstance(m, Tag)]
            if not math_nodes:
                continue
            tag_num = _detect_equation_number(tr)
            rows_info.append({"math": math_nodes, "tag": tag_num})

        if not rows_info:
            # 没有 math 的 equation table（极端情况）：直接清除，避免污染表格列表
            tbl.decompose()
            continue

        # 每个 math 对应一条公式；公式块全部替换到一个容器 div 里
        container = soup.new_tag("div")
        container.attrs["data-h2m-eqblock"] = "1"

        for row in rows_info:
            row_tag_num: str | None = row.get("tag")
            for m in row["math"]:
                latex = _find_tex_annotation(m)
                fid = f"f{len(formulas) + 1:03d}"
                # 等式表格内一律按 block 渲染（display 属性有时为 inline，但实际是展示式）
                is_block = True

                if mode == "mathml" or (mode != "latex" and not latex):
                    mathml = str(m)
                    formulas.append(
                        {
                            "id": fid,
                            "mode": "mathml",
                            "inline": False,
                            "mathml": mathml,
                            "equation_number": row_tag_num,
                        }
                    )
                else:
                    tex = latex or ""
                    if row_tag_num and tex and r"\tag" not in tex:
                        tex = f"{tex} \\tag{{{row_tag_num}}}"
                    formulas.append(
                        {
                            "id": fid,
                            "mode": "latex",
                            "inline": False,
                            "latex": tex,
                            "equation_number": row_tag_num,
                        }
                    )
                    if not latex:
                        warnings.append({"code": "formula_latex_missing", "id": fid})

                # 构造占位节点供 Emit 走 artifact 渲染（不放 math，避免下游 2) 规则再次处理）
                placeholder = soup.new_tag("div")
                placeholder.attrs[ANCHOR_ATTR] = fid
                placeholder.attrs["data-h2m-kind"] = "formula"
                container.append(placeholder)
                _ = is_block  # 保留变量以供将来扩展

        tbl.replace_with(container)


_EQ_NUM_RE = re.compile(r"^\s*\(\s*(\d+[a-zA-Z]?)\s*\)\s*$")


def _detect_equation_number(tr: Tag) -> str | None:
    """在等式行中找形如 ``(1)`` 的编号单元格（通常是右侧的 eqn_cell）。"""
    for cell in tr.find_all(["td", "th"]):
        if not isinstance(cell, Tag):
            continue
        # 若单元格内还有 math 节点，它不是"编号"单元格
        if cell.find("math"):
            continue
        text = cell.get_text(" ", strip=True)
        if not text:
            continue
        m = _EQ_NUM_RE.match(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def _process_tables(
    soup: BeautifulSoup,
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """表格三级策略（MVP 阶段二只做前两级）。"""
    tables: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for t in soup.find_all("table"):
        if not isinstance(t, Tag):
            continue
        if t.get(ANCHOR_ATTR):
            continue

        score = _table_complexity(t)
        chosen = _choose_table_mode(mode, score)

        tid = f"t{len(tables) + 1:03d}"
        t.attrs[ANCHOR_ATTR] = tid

        caption_text = _table_caption(t)

        item: dict[str, Any] = {
            "id": tid,
            "complexity": score,
            "caption": caption_text,
        }

        if chosen == "markdown":
            md = _table_to_markdown(t)
            if md is None:
                # 回落到 html
                item["mode"] = "html"
                item["html"] = str(t)
                warnings.append({"code": "table_markdown_fallback", "id": tid})
            else:
                item["mode"] = "markdown"
                item["markdown"] = md
        elif chosen == "image":
            # 标记为 image 模式（由 _apply_screenshot_fallback 真正截图）；
            # 同时保留 HTML 备份，用于截图失败时回滚展示。
            item["mode"] = "image"
            item["html"] = str(t)
        else:  # html
            item["mode"] = "html"
            item["html"] = str(t)

        tables.append(item)

    return tables, warnings


def _table_complexity(table: Tag) -> int:
    rowspan = 0
    colspan = 0
    nested = 0
    has_formula = 0
    has_image = 0
    for cell in table.find_all(["td", "th"]):
        if not isinstance(cell, Tag):
            continue
        rs = int_attr(cell, "rowspan", 1)
        cs = int_attr(cell, "colspan", 1)
        if rs > 1:
            rowspan += 1
        if cs > 1:
            colspan += 1
        if cell.find("table"):
            nested += 1
        if cell.find(["math", "mjx-container"]):
            has_formula += 1
        if cell.find("img"):
            has_image += 1
    # large column penalty
    first_row = table.find("tr")
    penalty = 0
    if isinstance(first_row, Tag):
        cols = len(first_row.find_all(["td", "th"]))
        if cols > 8:
            penalty = 3
    return rowspan * 3 + colspan * 2 + nested * 5 + has_formula * 2 + has_image * 3 + penalty


def _choose_table_mode(requested: str, score: int) -> str:
    """按架构文档 §6.2 映射。

    auto 下优先尝试 Markdown（_table_to_markdown 本身会对 rowspan/colspan/nested 返回
    None 自动回落到 html），只有当复杂度非常高时才直接走图片降级。
    """
    if requested in ("markdown", "html", "image"):
        return requested
    # auto
    if score <= 10:
        return "markdown"
    return "image"


def _table_caption(table: Tag) -> str | None:
    cap = table.find("caption")
    if isinstance(cap, Tag):
        text = cap.get_text(" ", strip=True)
        return text or None
    return None


def _table_to_markdown(table: Tag) -> str | None:
    """把简单 table 转 Markdown；任何复杂特性（rowspan/colspan/nested）返回 None。"""
    rows: list[list[str]] = []
    header: list[str] | None = None

    # thead：任一 cell 含 rowspan/colspan 或 thead 行数>1（多级表头）都判定为复杂表格
    thead = table.find("thead")
    if isinstance(thead, Tag):
        thead_trs = [t for t in thead.find_all("tr") if isinstance(t, Tag)]
        if len(thead_trs) > 1:
            # 多级表头 Markdown 不支持（强制回落 html）
            return None
        if thead_trs:
            header_row = thead_trs[0]
            for c in header_row.find_all(["th", "td"]):
                if not isinstance(c, Tag):
                    continue
                if int_attr(c, "rowspan", 1) > 1 or int_attr(c, "colspan", 1) > 1:
                    return None
            header = [
                _cell_text(c)
                for c in header_row.find_all(["th", "td"])
                if isinstance(c, Tag)
            ]

    body_rows: list[Any] = []
    tbody = table.find("tbody")
    if isinstance(tbody, Tag):
        body_rows = tbody.find_all("tr")
    else:
        body_rows = table.find_all("tr")

    for i, tr in enumerate(body_rows):
        if not isinstance(tr, Tag):
            continue
        if header is None and i == 0 and tr.find("th"):
            header = [_cell_text(c) for c in tr.find_all(["th", "td"]) if isinstance(c, Tag)]
            continue
        # 复杂特性检测
        for cell in tr.find_all(["td", "th"]):
            if not isinstance(cell, Tag):
                continue
            if int_attr(cell, "rowspan", 1) > 1 or int_attr(cell, "colspan", 1) > 1:
                return None
            if cell.find("table"):
                return None
        rows.append([_cell_text(c) for c in tr.find_all(["td", "th"]) if isinstance(c, Tag)])

    if not rows and not header:
        return None

    width = max(len(header or []), max((len(r) for r in rows), default=0))
    if width == 0:
        return None
    header = header or ["" for _ in range(width)]
    header = header + [""] * (width - len(header))

    lines = []
    lines.append("| " + " | ".join(_escape_md_cell(h) for h in header) + " |")
    lines.append("|" + "|".join([" --- "] * width) + "|")
    for r in rows:
        r = r + [""] * (width - len(r))
        lines.append("| " + " | ".join(_escape_md_cell(c) for c in r) + " |")
    return "\n".join(lines)


def _cell_text(cell: Tag) -> str:
    """抽取单元格文本；对含 <math> 的单元格优先使用 LaTeX 源，避免 MathML 的
    Unicode fallback + TeX annotation 双写导致乱码。
    """
    # 若单元格没有 math，走原来的纯文本路径
    if not cell.find("math"):
        return " ".join(cell.get_text(" ", strip=True).split())

    # 有 math：克隆一份再逐个 math 替换为 `$...$` / `$$...$$`
    # （这里直接在原 cell 上替换无副作用——单元格本身已经被纳入 table artifact）
    buf_parts: list[str] = []
    for c in cell.children:
        buf_parts.append(_inline_cell_piece(c))
    text = " ".join(p for p in buf_parts if p)
    return " ".join(text.split())


def _inline_cell_piece(node: Any) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name == "math":
        latex = _find_tex_annotation(node)
        is_block = str_attr(node, "display").lower() == "block"
        if latex:
            tex = latex.strip()
            return f"$${tex}$$" if is_block else f"${tex}$"
        # 无 LaTeX 源：保留 MathML 原样（Markdown 允许内嵌 HTML）
        return str(node)
    # 其他行内元素：递归拼接
    parts: list[str] = []
    for c in node.children:
        parts.append(_inline_cell_piece(c))
    return " ".join(p for p in parts if p)


def _escape_md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def _process_images(
    soup: BeautifulSoup,
    *,
    base_url: str,
    sandbox: FsSandbox,
    mode: str,
    allow_file_scheme: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    images: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for idx, img in enumerate(soup.find_all("img"), start=1):
        if not isinstance(img, Tag):
            continue
        src = _best_src(img)
        if not src:
            warnings.append({"code": "image_no_src", "idx": idx})
            continue

        abs_url = urljoin(base_url, src)
        alt = str_attr(img, "alt").strip()

        iid = f"i{idx:03d}"
        img.attrs[ANCHOR_ATTR] = iid

        if mode == "link":
            images.append(
                {"id": iid, "idx": idx, "remote_url": abs_url, "local_path": None, "alt": alt}
            )
            continue

        try:
            resp = http.get(abs_url, timeout=15.0, allow_file_scheme=allow_file_scheme)
            ext = _guess_ext(abs_url, resp.headers.get("content-type", ""))
            rel = f"assets/images/fig-{idx:03d}{ext}"
            sandbox.write_bytes(rel, resp.content)
            images.append(
                {
                    "id": iid,
                    "idx": idx,
                    "remote_url": abs_url,
                    "local_path": rel,
                    "alt": alt,
                }
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(
                {
                    "code": "image_fetch_failed",
                    "url": abs_url,
                    "detail": repr(e)[:200],
                }
            )
            if mode == "both":
                images.append(
                    {
                        "id": iid,
                        "idx": idx,
                        "remote_url": abs_url,
                        "local_path": None,
                        "alt": alt,
                    }
                )

    return images, warnings


def _best_src(img: Tag) -> str | None:
    srcset = str_attr(img, "srcset")
    if srcset.strip():
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first:
            return first
    for k in ("src", "data-src", "data-original"):
        v = str_attr(img, k)
        if v.strip():
            return v
    return None


def _guess_ext(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        if path.endswith(ext):
            return ext
    ct = content_type.lower()
    if "png" in ct:
        return ".png"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "gif" in ct:
        return ".gif"
    if "webp" in ct:
        return ".webp"
    if "svg" in ct:
        return ".svg"
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f".{h}.bin"


def _process_inline_svgs(
    soup: BeautifulSoup,
    *,
    start_idx: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """抓取内联 <svg>，作为 image artifact 保留原 SVG 源。

    Emit 会将其内联到 Markdown（Markdown 允许内联 HTML）。
    跳过很小（<100 字节）或明显是装饰性的 SVG。
    """
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for i, svg in enumerate(soup.find_all("svg")):
        if not isinstance(svg, Tag):
            continue
        # 已被图片或公式处理过的 SVG（在 formula 内）跳过
        if svg.get(ANCHOR_ATTR):
            continue
        # 过滤很小的装饰 SVG
        raw = str(svg)
        if len(raw) < 100:
            continue
        idx = start_idx + i
        iid = f"s{idx:03d}"
        svg.attrs[ANCHOR_ATTR] = iid
        aria = str_attr(svg, "aria-label").strip() or "svg"
        items.append(
            {
                "id": iid,
                "idx": idx,
                "alt": aria,
                "mode": "svg_inline",
                "svg": raw,
                "local_path": None,
                "remote_url": None,
            }
        )
    return items, warnings


# 未用到但保留为将来跨模块引用
__all__ = ["ANCHOR_ATTR", "EnrichStage", "NavigableString"]


# ---------------------------------------------------------------------------
# 截图降级（阶段四）
# ---------------------------------------------------------------------------


def _apply_screenshot_fallback(
    *,
    ctx: Context,
    sandbox: FsSandbox,
    soup: BeautifulSoup,
    tables: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    table_mode: str,
    formula_mode: str,
) -> list[dict[str, Any]]:
    """为需要降级为图片的 table / formula 启一次浏览器会话统一截图。

    触发条件：
    - table: `mode=image`（auto 复杂度>10 或用户强制），且当前尚未落盘。
    - formula: `mode=image`（用户强制）或无 LaTeX/MathML 源的 MathML 占位。

    已有浏览器截图（从 Acquire 透传的 ctx.acquire.screenshots）可直接消费。
    否则通过 `browser.screenshot_nodes(html, selectors)` 二次截图。

    返回 warnings 列表；同时 in-place 改 artifact 的 mode/local_path。
    """
    warnings: list[dict[str, Any]] = []

    # 1) 收集需要截图的 selector
    wanted_table_ids: list[str] = [
        t["id"] for t in tables
        if t.get("mode") == "image"
        or (table_mode == "image")
        or (_needs_table_image(t))
    ]
    # 对 table_mode == 'image' 强制升级：mode 设为 image
    if table_mode == "image":
        for t in tables:
            t["mode"] = "image"
            if t["id"] not in wanted_table_ids:
                wanted_table_ids.append(t["id"])

    wanted_formula_ids: list[str] = []
    if formula_mode == "image":
        # 强制所有公式截图
        for f in formulas:
            wanted_formula_ids.append(f["id"])
    else:
        # 无源公式自动降级
        for f in formulas:
            if _formula_is_empty(f):
                wanted_formula_ids.append(f["id"])

    selectors: list[str] = []
    selectors.extend(f'[{ANCHOR_ATTR}="{tid}"]' for tid in wanted_table_ids)
    selectors.extend(f'[{ANCHOR_ATTR}="{fid}"]' for fid in wanted_formula_ids)

    if not selectors:
        return warnings

    # 2) 优先用 Acquire 传来的 screenshots
    acquire_screenshots: dict[str, bytes] = {}
    if ctx.acquire:
        acquire_screenshots = ctx.acquire.get("screenshots") or {}

    missing = [s for s in selectors if s not in acquire_screenshots]

    # 3) 缺的部分再跑一次浏览器
    from_browser: dict[str, bytes] = {}
    if missing:
        try:
            from qiq_html2md.infra import browser as browser_mod
            driver = browser_mod.get_driver()
            # 用 annotated_html 替代 ctx.enrich.annotated_html（还未写回 ctx）
            annotated_html = str(soup) if soup else (ctx.extract or {}).get("clean_html", "")
            from_browser = driver.screenshot_nodes(
                annotated_html,
                missing,
                base_url=(ctx.acquire or {}).get("final_url"),
                timeout_ms=15000,
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(
                {"code": "screenshot_browser_failed", "detail": repr(e)[:200]}
            )

    screenshots = {**acquire_screenshots, **from_browser}

    # 4) 把截图写入 assets，并更新 artifact
    sandbox.mkdirp("assets/tables")
    sandbox.mkdirp("assets/formulas")

    for tid in wanted_table_ids:
        sel = f'[{ANCHOR_ATTR}="{tid}"]'
        png = screenshots.get(sel)
        t_art = next((x for x in tables if x["id"] == tid), None)
        if t_art is None:
            continue
        if png:
            rel = f"assets/tables/{tid}.png"
            sandbox.write_bytes(rel, png)
            # 保留原 HTML 作为备份，mode 升级为 image
            t_art["html_backup"] = t_art.get("html") or t_art.get("markdown")
            t_art["mode"] = "image"
            t_art["image_path"] = rel
        else:
            warnings.append({"code": "table_image_missing", "id": tid})
            # 截图失败：回落到 html 展示
            if t_art.get("mode") == "image":
                t_art["mode"] = "html"
                if not t_art.get("html"):
                    t_art["html"] = t_art.get("html_backup") or ""

    for fid in wanted_formula_ids:
        sel = f'[{ANCHOR_ATTR}="{fid}"]'
        png = screenshots.get(sel)
        f_art = next((x for x in formulas if x["id"] == fid), None)
        if f_art is None:
            continue
        if png:
            rel = f"assets/formulas/{fid}.png"
            sandbox.write_bytes(rel, png)
            f_art["mode"] = "image"
            f_art["image_path"] = rel
        else:
            warnings.append({"code": "formula_image_missing", "id": fid})
            # 截图失败：保持原 mode（latex/mathml）。若是空占位（f_source_unknown），置为空字符串
            if f_art.get("mode") not in ("latex", "mathml"):
                f_art["mode"] = "mathml"
                f_art.setdefault("mathml", "")

    return warnings


def _needs_table_image(t: dict[str, Any]) -> bool:
    """复杂度>10 且未已经是 image 模式 → 需要图片降级。"""
    score = int(t.get("complexity", 0))
    return score > 10


def _formula_is_empty(f: dict[str, Any]) -> bool:
    """判断公式是否缺少稳定源码。"""
    mode = f.get("mode")
    if mode == "latex" and (f.get("latex") or "").strip():
        return False
    if mode == "mathml" and (f.get("mathml") or "").strip():
        return False
    return True
