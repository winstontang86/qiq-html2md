"""复杂表格降级策略 & complex_table_damaged 规则收紧的单测。"""

from __future__ import annotations

from typing import cast

import pytest

from qiq_html2md.core.types import Context, SkillRequest
from qiq_html2md.quality import _rule_table
from qiq_html2md.stages.enrich import _choose_table_mode

# ---------------------------------------------------------------------------
# _choose_table_mode 行为
# ---------------------------------------------------------------------------


def test_auto_simple_table_goes_markdown() -> None:
    assert _choose_table_mode("auto", score=0) == "markdown"
    assert _choose_table_mode("auto", score=5) == "markdown"
    assert _choose_table_mode("auto", score=10) == "markdown"


def test_auto_complex_table_goes_html_not_image() -> None:
    """v0.3.0 改动：auto 下复杂表格不再 image，而是 html。"""
    assert _choose_table_mode("auto", score=11) == "html"
    assert _choose_table_mode("auto", score=50) == "html"


def test_explicit_modes_respected() -> None:
    assert _choose_table_mode("markdown", score=100) == "markdown"
    assert _choose_table_mode("html", score=0) == "html"
    assert _choose_table_mode("image", score=0) == "image"


# ---------------------------------------------------------------------------
# quality 规则 complex_table_damaged 仅在 table_mode=image 时触发
# ---------------------------------------------------------------------------


def _make_ctx(
    table_mode: str,
    tables: list[dict],
    extract_stats: dict | None = None,
) -> Context:
    req = SkillRequest(url="https://example.com/x")
    ctx = Context.new(req, trace_id="test-trace", deadline_ts=0.0)
    ctx.strategy = {
        "render_mode": "auto",
        "table_mode": table_mode,
        "formula_mode": "auto",
        "image_mode": "download",
        "extractor_profile": "adapter",
        "clean_rules": ["default"],
        "flags": {},
    }
    ctx.extract = {
        "extract_stats": extract_stats
        or {
            "text_len": 5000,
            "paragraph_count": 10,
            "heading_count": 5,
            "image_count": 0,
            "link_count": 5,
            "table_count": len(tables),
            "figure_with_table_count": 0,
        },
    }
    ctx.enrich = {"tables": tables, "images": [], "formulas": [], "refs": []}
    ctx.emit = {"markdown_text": "# Title\n\nSome text.\n"}
    ctx.warnings = []
    return ctx


@pytest.mark.parametrize("table_mode", ["auto", "html", "markdown"])
def test_complex_table_html_not_penalized_in_auto_modes(table_mode: str) -> None:
    """auto/html/markdown 模式下，复杂表格 html 输出视为合法降级 → 满分。"""
    tables = [
        {
            "id": "t001",
            "complexity": 15,
            "mode": "html",
            "html": "<table>...</table>",
            "caption": None,
        },
    ]
    ctx = _make_ctx(table_mode, tables)
    result = _rule_table(ctx)
    assert result.failure_reason is None, f"{table_mode}: 应不扣分，实际 {result.failure_reason}"
    assert result.score == 100.0


def test_complex_table_damaged_triggers_only_when_image_requested() -> None:
    """显式 table_mode=image 且未输出 image 时才触发 complex_table_damaged。"""
    tables = [
        {
            "id": "t001",
            "complexity": 15,
            "mode": "html",  # 应是 image 但退化到 html
            "html": "<table>...</table>",
            "caption": None,
        },
    ]
    ctx = _make_ctx("image", tables)
    result = _rule_table(ctx)
    assert result.failure_reason == "complex_table_damaged"
    assert result.score == 60.0


def test_complex_table_image_ok_passes() -> None:
    """显式 image 且成功截图时不扣分。"""
    tables = [
        {
            "id": "t001",
            "complexity": 15,
            "mode": "image",
            "image_path": "assets/tables/t001.png",
            "caption": None,
        },
    ]
    ctx = _make_ctx("image", tables)
    result = _rule_table(ctx)
    assert result.failure_reason is None
    assert result.score == 100.0


_ = cast  # noqa: B018  silence unused import warning if linter picks it up
