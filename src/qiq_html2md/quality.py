"""质量规则 + 评分（阶段三·完整版 + v0.2 增强）。

六条规则对应架构文档 §9.2 权重：
- text (0.30)
- structure (0.15)
- image (0.15)
- table (0.15)
- formula (0.15)
- link_reference (0.10)

每条规则返回 RuleResult：
  (rule_name, sub_score_0_to_100, is_critical, failure_reason, detail)

failure_reason 对应架构文档 §9.3 的失败原因键（驱动 plan_retry）。
sub_score < 70 视为 failed_rules；is_critical=True 视为 critical_failures。
通过条件：final_score >= 80 AND critical_failures == []。

v0.2 增强：
- table 规则新增 table_retention_low（extract vs enrich artifact 对账）
  与 table_in_figure_dropped（figure 内嵌表格在 md 中完全不可见）。
- formula 规则新增 formula_mathml_garbage（公式里 LaTeX 命令与 MathML
  Unicode 数学字母共存的双写乱码）、formula_as_table（公式被误判为
  markdown 表格）与 latex_source_missing（mode=latex 却无 latex 源）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from qiq_html2md.core.types import Context, QualityReport


@dataclass(frozen=True)
class RuleResult:
    name: str
    score: float  # 0..100
    critical: bool
    failure_reason: str | None  # 若规则失败，对应 §9.3 key
    detail: str


# ---------------------------------------------------------------------------
# 规则实现
# ---------------------------------------------------------------------------


def _rule_text(ctx: Context) -> RuleResult:
    """正文长度评分。"""
    text_len = 0
    if ctx.extract:
        text_len = int(ctx.extract.get("extract_stats", {}).get("text_len", 0))
    if text_len < 300:
        return RuleResult("text", 0.0, True, "text_too_short", f"text_len={text_len}")
    if text_len < 1000:
        score = text_len / 1000.0 * 100
        reason = "text_too_short" if score < 70 else None
        return RuleResult("text", score, False, reason, f"text_len={text_len}")
    return RuleResult("text", 100.0, False, None, f"text_len={text_len}")


def _rule_structure(ctx: Context) -> RuleResult:
    """Markdown 结构评分：是否有标题、段落数量、是否空。"""
    md = ""
    if ctx.emit and ctx.emit.get("markdown_text"):
        md = ctx.emit["markdown_text"]
    if not md.strip():
        return RuleResult("structure", 0.0, True, "markdown_structure_invalid", "empty")

    lines = md.splitlines()
    headings = sum(1 for ln in lines if ln.lstrip().startswith("#"))
    paragraphs = sum(1 for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))

    if headings == 0:
        return RuleResult("structure", 30.0, False, "markdown_structure_invalid", "no heading")
    if paragraphs < 3:
        return RuleResult(
            "structure",
            50.0,
            False,
            "markdown_structure_invalid",
            f"paragraphs={paragraphs}",
        )

    # 保留比例：Extract 统计的 heading_count 与当前 md 的 heading 数量
    extract_heading = 0
    if ctx.extract:
        extract_heading = int(ctx.extract.get("extract_stats", {}).get("heading_count", 0))
    retention = 1.0
    if extract_heading > 0:
        retention = min(1.0, headings / extract_heading)
    if retention < 0.70:
        return RuleResult(
            "structure",
            retention * 100,
            False,
            "heading_retention_low",
            f"retention={retention:.2f}",
        )
    return RuleResult("structure", 100.0, False, None, f"headings={headings} paragraphs={paragraphs}")


def _rule_image(ctx: Context) -> RuleResult:
    """图片保留率。只要 Extract 统计里有图片，就要求 Enrich 至少有等量 artifact。"""
    extract_count = 0
    if ctx.extract:
        extract_count = int(ctx.extract.get("extract_stats", {}).get("image_count", 0))
    if extract_count == 0:
        return RuleResult("image", 100.0, False, None, "no image in source")

    enrich_count = 0
    local_count = 0
    if ctx.enrich:
        images = ctx.enrich.get("images", [])
        enrich_count = len(images)
        local_count = sum(1 for i in images if i.get("local_path"))

    if enrich_count == 0:
        return RuleResult(
            "image",
            0.0,
            False,
            "image_retention_low",
            f"extract={extract_count} enrich=0",
        )

    # 本地化率：以 extract_count 为基数
    local_ratio = local_count / extract_count
    if local_ratio < 0.50:
        return RuleResult(
            "image",
            local_ratio * 100,
            False,
            "missing_local_resource",
            f"local_ratio={local_ratio:.2f}",
        )
    retention = min(1.0, enrich_count / extract_count)
    if retention < 0.80:
        return RuleResult(
            "image",
            retention * 100,
            False,
            "image_retention_low",
            f"retention={retention:.2f}",
        )
    # 兼顾 retention 和 local_ratio
    score = (retention * 0.5 + local_ratio * 0.5) * 100
    return RuleResult(
        "image",
        score,
        False,
        None,
        f"retention={retention:.2f} local={local_ratio:.2f}",
    )


def _rule_table(ctx: Context) -> RuleResult:
    """表格保真度：

    - 保留率：`extract.table_count` vs Enrich artifact 数量。<80% 视为丢失。
    - figure 内嵌表格检测：`extract.figure_with_table_count` 大于 0 且 md 中
      既无 markdown 表格块也无 `<table` 裸标签 → table_in_figure_dropped。
    - 复杂表格保真：score>10 但 mode!=image。
    - markdown→html 降级占比过高。
    """
    tables = []
    if ctx.enrich:
        tables = ctx.enrich.get("tables", [])

    extract_stats: dict[str, Any] = {}
    if ctx.extract:
        extract_stats = ctx.extract.get("extract_stats", {}) or {}
    extract_count = int(extract_stats.get("table_count", 0))
    figure_with_table = int(extract_stats.get("figure_with_table_count", 0))

    md = ""
    if ctx.emit and ctx.emit.get("markdown_text"):
        md = ctx.emit["markdown_text"]

    # -- (a) figure 内嵌表格全部丢失：md 里完全找不到表格内容 ----------------
    if figure_with_table > 0:
        has_md_table = bool(re.search(r"(?m)^\s*\|.+\|\s*$", md))
        has_html_table = "<table" in md.lower()
        if not has_md_table and not has_html_table:
            return RuleResult(
                "table",
                0.0,
                True,
                "table_in_figure_dropped",
                f"figure_with_table={figure_with_table} but no table in md",
            )

    # 源文档没有表格：Enrich 也该没有，直接满分
    if extract_count == 0 and not tables:
        return RuleResult("table", 100.0, False, None, "no table")

    # -- (b) 保留率 ---------------------------------------------------------
    enrich_count = len(tables)
    if extract_count > 0:
        retention = min(1.0, enrich_count / extract_count)
        if retention < 0.80:
            return RuleResult(
                "table",
                retention * 100,
                False,
                "table_retention_low",
                f"enrich={enrich_count}/{extract_count}",
            )

    # 没有 tables artifact 直接返回；下面检查都基于 artifact
    if not tables:
        return RuleResult("table", 100.0, False, None, "no enrich tables")

    # -- (c) 复杂表格未走图片降级 -------------------------------------------
    # 仅在用户**显式**要求 `table_mode=image` 且实际没输出 image 时才判定为受损。
    # auto 模式下复杂表格以 html 展示被视为合法降级，不再拖分。
    requested_mode = ""
    if ctx.strategy:
        requested_mode = str(ctx.strategy.get("table_mode", ""))
    if requested_mode == "image":
        damaged = 0
        for t in tables:
            score = int(t.get("complexity", 0))
            mode = t.get("mode", "")
            if score > 10 and mode != "image":
                damaged += 1
        if damaged > 0:
            return RuleResult(
                "table",
                60.0,
                False,
                "complex_table_damaged",
                f"damaged={damaged}",
            )

    # -- (d) markdown 转换失败 fallback 过多 --------------------------------
    warnings: list[dict[str, Any]] = ctx.warnings
    fallback = sum(1 for w in warnings if w.get("code") == "table_markdown_fallback")
    if fallback > 0 and fallback / len(tables) > 0.5:
        return RuleResult(
            "table",
            70.0,
            False,
            "table_retention_low",
            f"fallback={fallback}/{len(tables)}",
        )
    return RuleResult("table", 100.0, False, None, f"count={len(tables)}")


def _rule_formula(ctx: Context) -> RuleResult:
    """公式质量：保留率 + 乱码 + 公式-当-表格。

    - latex_source_missing：mode=latex 但 latex 字段为空的比例过高。
    - formula_mathml_garbage：md 中同一公式块同时出现 LaTeX 反斜杠命令与
      MathML Unicode 数学字母（U+1D400–U+1D7FF / ℝ ℕ ℤ 等 U+2100 段）。
    - formula_as_table：md 表格行内包含典型 LaTeX 命令（\\displaystyle、
      \\mathbf、\\bm、\\tag 等）→ 公式被误判为 markdown 表格。
    - 兜底：formula_retention_low（mode != latex 且无 mathml 的比例）。
    """
    formulas = []
    if ctx.enrich:
        formulas = ctx.enrich.get("formulas", [])

    md = ""
    if ctx.emit and ctx.emit.get("markdown_text"):
        md = ctx.emit["markdown_text"]

    # -- (a0) formula_pgf_leak：兜底检查 artifact.latex 里是否残留 TikZ/PGF ---
    leaked = _count_pgf_leaks(formulas, md)
    if leaked > 0:
        # 单条 -30，上不封顶；>=3 条升 critical
        score = max(0.0, 100.0 - leaked * 30.0)
        is_critical = leaked >= 3
        return RuleResult(
            "formula",
            score,
            is_critical,
            "formula_pgf_leak",
            f"leaked={leaked}",
        )

    # -- (a) formula_as_table：最优先，因为这是"完全错类型"的严重 bug ------
    fat_count = _count_formula_as_table(md)
    if fat_count > 0:
        return RuleResult(
            "formula",
            30.0,
            True,
            "formula_as_table",
            f"suspicious_table_rows={fat_count}",
        )

    # -- (b) formula_mathml_garbage：每条公式块内 LaTeX + MathML Unicode 共存
    garbage_blocks = _count_formula_garbage_blocks(md)
    if garbage_blocks > 0:
        # 单块乱码即扣至阈值以下；多块加重处罚并升 critical。
        score = max(0.0, 65.0 - (garbage_blocks - 1) * 15.0)
        is_critical = garbage_blocks >= 3
        return RuleResult(
            "formula",
            score,
            is_critical,
            "formula_mathml_garbage",
            f"garbage_blocks={garbage_blocks}",
        )

    if not formulas:
        return RuleResult("formula", 100.0, False, None, "no formula")

    # -- (c) latex_source_missing ------------------------------------------
    latex_total = sum(1 for f in formulas if f.get("mode") == "latex")
    latex_empty = sum(
        1
        for f in formulas
        if f.get("mode") == "latex" and not (f.get("latex") or "").strip()
    )
    if latex_total > 0:
        empty_ratio = latex_empty / latex_total
        if empty_ratio > 0.15:
            score = (1.0 - empty_ratio) * 100
            return RuleResult(
                "formula",
                score,
                False,
                "latex_source_missing",
                f"empty={latex_empty}/{latex_total}",
            )

    # -- (d) formula_retention_low (既有逻辑保留) --------------------------
    unknown = sum(1 for f in formulas if f.get("mode") != "latex" and not f.get("mathml"))
    total = len(formulas)
    ok = total - unknown
    retention = ok / total if total else 1.0
    if retention < 0.85:
        return RuleResult(
            "formula",
            retention * 100,
            False,
            "formula_retention_low",
            f"ok={ok}/{total}",
        )
    return RuleResult("formula", retention * 100, False, None, f"ok={ok}/{total}")


# ---------------------------------------------------------------------------
# 公式 markdown 扫描 helpers
# ---------------------------------------------------------------------------

# 典型 LaTeX 控制命令（白名单匹配，避免英文正文里的 \ 误命中）。
_LATEX_CMD_RE = re.compile(
    r"\\(?:displaystyle|mathbf|mathbb|mathcal|mathrm|mathit|text|bm|nabla|tag|"
    r"left|right|frac|sum|prod|int|sqrt|times|geq|leq|infty|propto|partial|"
    r"qquad|quad|alpha|beta|gamma|theta|lambda|mu|sigma|pi|omega|forall|exists|"
    r"approx|equiv|sim|in|notin|subset|mathscr|Delta|Omega|Sigma)\b"
)

# MathML Unicode 数学字母（bold/italic/calligraphy blocks）；ℝ ℂ 𝑥 𝐖 等。
_MML_UNICODE_RE = re.compile(
    "[\U0001D400-\U0001D7FF"  # Mathematical Alphanumeric Symbols
    "\u2102\u210D\u2115\u2119\u211A\u211D\u2124"  # ℂ ℍ ℕ ℙ ℚ ℝ ℤ
    "\u2202"  # ∂ partial
    "]"
)


def _count_formula_garbage_blocks(md: str) -> int:
    """扫描 $$...$$ / $...$ 公式块，同时含 LaTeX 命令 + MathML Unicode 判为乱码。"""
    if not md:
        return 0
    count = 0
    # 块级 $$...$$
    for block in re.findall(r"\$\$(.+?)\$\$", md, flags=re.DOTALL):
        if _LATEX_CMD_RE.search(block) and _MML_UNICODE_RE.search(block):
            count += 1
    # 行内 $...$（排除 $$）：只抓形如 "$...$" 最短匹配
    for inline in re.findall(r"(?<!\$)\$([^$\n]{3,})\$(?!\$)", md):
        if _LATEX_CMD_RE.search(inline) and _MML_UNICODE_RE.search(inline):
            count += 1
    return count


# markdown 表格行：以 '|' 开头 '|' 结尾的一行
_MD_TABLE_ROW_RE = re.compile(r"(?m)^\s*\|.+\|\s*$")


def _count_formula_as_table(md: str) -> int:
    """统计 markdown 表格行中含典型 LaTeX 命令的行数。

    正常情形下表格里**可以**有 `$...$` 内嵌公式，所以必须同时满足两个条件才判
    为"公式被当表格"：
      1) 行内含 LaTeX 控制命令（\\mathbf / \\bm / \\displaystyle ...）；
      2) 行内不包含 `$` 定界符 —— 即 LaTeX 源被裸写在单元格里。
    """
    if not md:
        return 0
    count = 0
    for row in _MD_TABLE_ROW_RE.findall(md):
        if "$" in row:
            continue
        if _LATEX_CMD_RE.search(row):
            count += 1
    return count


# PGF/TikZ 渲染源码黑名单：命中任一即视为 latex 污染。
_PGF_LEAK_MARKERS: tuple[str, ...] = (
    r"\pgfsys@",
    r"\pgfpicture",
    r"\lxSVG",
    r"\hbox to",
    r"\leavevmode\hbox",
)


def _count_pgf_leaks(formulas: list[dict[str, Any]], md: str) -> int:
    """统计 LaTeX 渲染源码泄漏数量。

    - 检查 enrich.formulas 的 latex 字段是否含 pgf 特征；
    - 另外扫描 md 里的 `$...$` / `$$...$$` 是否残留 pgf 片段（防御 enrich 未改动
      但 md 侧仍被污染的情形）。
    两个维度取并集计数；重复命中只算一次不展开。
    """
    count = 0
    for f in formulas:
        tex = (f.get("latex") or "").strip()
        if tex and any(marker in tex for marker in _PGF_LEAK_MARKERS):
            count += 1
    # md 侧兜底扫描
    if md:
        for block in re.findall(r"\$\$(.+?)\$\$", md, flags=re.DOTALL):
            if any(m in block for m in _PGF_LEAK_MARKERS):
                count += 1
        for inline in re.findall(r"(?<!\$)\$([^$\n]{3,})\$(?!\$)", md):
            if any(m in inline for m in _PGF_LEAK_MARKERS):
                count += 1
    return count


def _rule_link_reference(ctx: Context) -> RuleResult:
    """链接与参考文献完整性。"""
    if not ctx.request.include_references:
        # 未要求保留 refs，跳过
        return RuleResult("link_reference", 100.0, False, None, "refs disabled")

    # 判断源 HTML 是否像是包含参考文献
    has_refs_in_source = False
    if ctx.extract:
        # extract_stats 里没有直接字段；简单用 text 搜关键词
        clean_html = ctx.extract.get("clean_html", "").lower()
        if any(kw in clean_html for kw in ("bibliograph", "references")):
            has_refs_in_source = True

    refs = []
    if ctx.enrich:
        refs = ctx.enrich.get("refs", [])

    if has_refs_in_source and not refs:
        return RuleResult(
            "link_reference",
            30.0,
            False,
            "reference_missing",
            "source had refs but none extracted",
        )

    # 链接保留：extract stats link_count
    link_count = 0
    if ctx.extract:
        link_count = int(ctx.extract.get("extract_stats", {}).get("link_count", 0))
    # 暂无精确 md 链接计数，保留近似：只要没有明显缺失，就给满分
    return RuleResult("link_reference", 100.0, False, None, f"refs={len(refs)} links={link_count}")


# ---------------------------------------------------------------------------
# 评分聚合
# ---------------------------------------------------------------------------


RuleFn = Callable[[Context], RuleResult]

_RULES: list[RuleFn] = [
    _rule_text,
    _rule_structure,
    _rule_image,
    _rule_table,
    _rule_formula,
    _rule_link_reference,
]

_WEIGHTS: dict[str, float] = {
    "text": 0.30,
    "structure": 0.15,
    "image": 0.15,
    "table": 0.15,
    "formula": 0.15,
    "link_reference": 0.10,
}

PASS_THRESHOLD = 80.0
FAIL_RULE_THRESHOLD = 70.0  # sub_score 低于此值进入 failed_rules


def evaluate(ctx: Context) -> QualityReport:
    sub_scores: dict[str, float] = {}
    failed_rules: list[str] = []
    critical: list[str] = []
    # 保存 failure_reason 以便 plan_retry 使用
    reason_map: dict[str, str] = {}

    weighted_sum = 0.0
    weight_total = 0.0

    for fn in _RULES:
        r = fn(ctx)
        sub_scores[r.name] = round(r.score, 2)
        w = _WEIGHTS.get(r.name, 0.0)
        weighted_sum += r.score * w
        weight_total += w
        if r.critical and r.failure_reason:
            critical.append(r.failure_reason)
            reason_map[r.name] = r.failure_reason
        if r.score < FAIL_RULE_THRESHOLD:
            # 使用 failure_reason 作为 failed_rules 的键（便于 plan_retry 直接消费）
            failure_key = r.failure_reason or r.name
            if failure_key not in failed_rules:
                failed_rules.append(failure_key)
            reason_map.setdefault(r.name, failure_key)

    final = weighted_sum / weight_total if weight_total else 0.0
    passed = (final >= PASS_THRESHOLD) and (not critical)

    return QualityReport(
        passed=passed,
        final_score=round(final, 2),
        sub_scores=sub_scores,
        critical_failures=critical,
        failed_rules=failed_rules,
        risk_level=_risk_level(final, critical),
    )


def _risk_level(final: float, critical: list[Any]) -> Literal["low", "medium", "high"]:
    if critical or final < 60:
        return "high"
    if final < 80:
        return "medium"
    return "low"
