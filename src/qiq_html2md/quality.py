"""质量规则 + 评分（阶段三·完整版）。

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
"""

from __future__ import annotations

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
    """表格保留率 + 复杂表格保真。"""
    tables = []
    if ctx.enrich:
        tables = ctx.enrich.get("tables", [])
    if not tables:
        return RuleResult("table", 100.0, False, None, "no table")

    # 是否存在降级警告
    warnings: list[dict[str, Any]] = ctx.warnings
    # complex_table_damaged：复杂表格（score>10）但未以 image 输出
    damaged = 0
    for t in tables:
        score = int(t.get("complexity", 0))
        mode = t.get("mode", "")
        if score > 10 and mode != "image":
            damaged += 1
    if damaged > 0:
        # 只有当存在专门 warning 时才算 critical
        return RuleResult(
            "table",
            60.0,
            False,
            "complex_table_damaged",
            f"damaged={damaged}",
        )

    # markdown_fallback warning 比例
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
    """公式保留率：以 Enrich 统计为准。"""
    formulas = []
    if ctx.enrich:
        formulas = ctx.enrich.get("formulas", [])
    if not formulas:
        # 无公式不是问题
        return RuleResult("formula", 100.0, False, None, "no formula")

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
