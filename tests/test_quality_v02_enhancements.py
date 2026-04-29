"""v0.2 quality 增强规则测试。

覆盖：
- formula_as_table：LaTeX 被当成 markdown 表格行。
- formula_mathml_garbage：LaTeX + MathML Unicode 数学字母双写。
- table_in_figure_dropped：figure 内嵌 table 在 md 里完全不可见。
- table_retention_low：extract vs enrich 对账。
- latex_source_missing：mode=latex 但 latex 字段为空比例过高。
- extract_stats 新增字段回归：table_count / figure_with_table_count。
"""

from __future__ import annotations

import time
from pathlib import Path

from qiq_html2md import quality as quality_mod
from qiq_html2md.core.types import Context, SkillRequest


def _make_ctx(
    *,
    tmp_path: Path,
    markdown_text: str = "",
    tables: list[dict] | None = None,
    formulas: list[dict] | None = None,
    extract_stats: dict | None = None,
) -> Context:
    req = SkillRequest(
        url="https://example.com/",
        output_dir=str(tmp_path),
        include_references=True,
        quality_check=True,
    )
    ctx = Context.new(
        request=req,
        trace_id="TESTTRACE",
        deadline_ts=time.time() + 60,
    )
    # 伪造 extract 和 enrich 最小数据
    ctx.extract = {
        "clean_html": "<html><body>hi</body></html>",
        "text": "x" * 2000,
        "metadata": {},
        "extract_stats": {
            "text_len": 2000,
            "heading_count": 3,
            "paragraph_count": 10,
            "image_count": 0,
            "link_count": 0,
            "table_count": 0,
            "figure_with_table_count": 0,
            **(extract_stats or {}),
        },
    }
    ctx.enrich = {
        "images": [],
        "tables": tables or [],
        "formulas": formulas or [],
        "refs": [],
        "annotated_html": "",
        "enrich_stats": {},
    }
    ctx.emit = {
        "markdown_path": str(tmp_path / "article.md"),
        "markdown_text": markdown_text
        or "# Title\n\nBody paragraph one.\n\nBody paragraph two.\n\nBody paragraph three.\n",
        "assets_dir": str(tmp_path / "assets"),
        "metadata_path": None,
        "warnings_path": None,
    }
    return ctx


# ---------------------------------------------------------------------------
# formula_as_table
# ---------------------------------------------------------------------------


def test_formula_as_table_is_detected(tmp_path: Path) -> None:
    """公式被当 markdown 表格单元格：应当触发 critical 失败。"""
    bad_md = (
        "# Title\n\nIntro paragraph.\n\n"
        "|  |  |  |\n"
        "| --- | --- | --- |\n"
        "| \\mathbf{W}_1 \\propto \\nabla f({\\bm{x}};\\bm{\\theta}) |  | (1) |\n\n"
        "Another paragraph.\n"
    )
    ctx = _make_ctx(tmp_path=tmp_path, markdown_text=bad_md)
    report = quality_mod.evaluate(ctx)
    assert report.sub_scores["formula"] <= 40
    assert "formula_as_table" in report.critical_failures
    assert report.passed is False
    assert report.risk_level == "high"


def test_formula_as_table_false_positive_safe(tmp_path: Path) -> None:
    """表格里合法内嵌 $...$ 公式不应该被误判。"""
    good_md = (
        "# Title\n\nIntro paragraph.\n\n"
        "| Section | Approach |\n"
        "| --- | --- |\n"
        "| 2.1 | width $\\rightarrow\\infty$ |\n\n"
        "More text here.\n"
    )
    ctx = _make_ctx(tmp_path=tmp_path, markdown_text=good_md)
    report = quality_mod.evaluate(ctx)
    # 不应触发 formula_as_table
    assert "formula_as_table" not in report.failed_rules
    assert "formula_as_table" not in report.critical_failures


# ---------------------------------------------------------------------------
# formula_mathml_garbage
# ---------------------------------------------------------------------------


def test_formula_mathml_garbage_is_detected(tmp_path: Path) -> None:
    """同一公式块同时含 \\mathbf 命令与 MathML Unicode（𝐖 / ℝ）。"""
    bad_md = (
        "# Title\n\nPara one.\n\nPara two.\n\nPara three.\n\n"
        "$$\n"
        "𝐖_1^⊤ 𝐖_1 \\propto \\mathbb{E}_{𝒙\\sim 𝒫_{data}}"
        "[\\nabla f({\\bm{x}};{\\bm{\\theta}})]\n"
        "$$\n"
    )
    ctx = _make_ctx(tmp_path=tmp_path, markdown_text=bad_md)
    report = quality_mod.evaluate(ctx)
    assert "formula_mathml_garbage" in report.failed_rules
    assert report.sub_scores["formula"] < 100


def test_formula_clean_latex_passes(tmp_path: Path) -> None:
    """纯 LaTeX、无 Unicode 字母，规则给满分。"""
    good_md = (
        "# Title\n\nPara.\n\nPara.\n\nPara.\n\n"
        "$$\n\\mathbf{W}_1^\\top \\mathbf{W}_1 \\propto \\nabla f(x;\\theta)\n$$\n"
    )
    ctx = _make_ctx(
        tmp_path=tmp_path,
        markdown_text=good_md,
        formulas=[{"id": "f001", "mode": "latex", "inline": False, "latex": r"\mathbf{W}_1"}],
    )
    report = quality_mod.evaluate(ctx)
    assert report.sub_scores["formula"] == 100.0
    assert report.passed is True


# ---------------------------------------------------------------------------
# table_in_figure_dropped
# ---------------------------------------------------------------------------


def test_table_in_figure_dropped_is_detected(tmp_path: Path) -> None:
    """extract 看到 figure 含 table，但 md 里没有任何 table 内容。"""
    md = "# Title\n\nIntro text.\n\nBody with no table at all.\n"
    ctx = _make_ctx(
        tmp_path=tmp_path,
        markdown_text=md,
        extract_stats={"figure_with_table_count": 1, "table_count": 1},
        tables=[],
    )
    report = quality_mod.evaluate(ctx)
    assert "table_in_figure_dropped" in report.critical_failures
    assert report.passed is False


def test_table_in_figure_ok_when_html_fallback(tmp_path: Path) -> None:
    """figure 内 table 以 <table> HTML 形式保留到 md 里是合法情形。"""
    md = (
        "# Title\n\nIntro.\n\n"
        "<table><tr><td>A</td><td>B</td></tr></table>\n\nMore text.\n"
    )
    ctx = _make_ctx(
        tmp_path=tmp_path,
        markdown_text=md,
        extract_stats={"figure_with_table_count": 1, "table_count": 1},
        tables=[{"id": "t001", "mode": "html", "complexity": 5, "html": "<table/>"}],
    )
    report = quality_mod.evaluate(ctx)
    assert "table_in_figure_dropped" not in report.critical_failures


# ---------------------------------------------------------------------------
# table_retention_low
# ---------------------------------------------------------------------------


def test_table_retention_low(tmp_path: Path) -> None:
    """extract 声称 5 个表，enrich 只拿到 1 个 → 保留率 20%。"""
    md = (
        "# Title\n\nIntro.\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\nMore.\n"
    )
    ctx = _make_ctx(
        tmp_path=tmp_path,
        markdown_text=md,
        extract_stats={"table_count": 5, "figure_with_table_count": 0},
        tables=[{"id": "t001", "mode": "markdown", "complexity": 1}],
    )
    report = quality_mod.evaluate(ctx)
    assert "table_retention_low" in report.failed_rules


# ---------------------------------------------------------------------------
# latex_source_missing
# ---------------------------------------------------------------------------


def test_latex_source_missing_detected(tmp_path: Path) -> None:
    """mode=latex 的公式里一半以上 latex 为空 → latex_source_missing。"""
    formulas = [
        {"id": f"f{i:03}", "mode": "latex", "inline": True, "latex": ""}
        for i in range(1, 5)
    ] + [
        {"id": "f005", "mode": "latex", "inline": True, "latex": "x"},
    ]
    ctx = _make_ctx(tmp_path=tmp_path, formulas=formulas)
    report = quality_mod.evaluate(ctx)
    assert "latex_source_missing" in report.failed_rules
    assert report.sub_scores["formula"] < 100
