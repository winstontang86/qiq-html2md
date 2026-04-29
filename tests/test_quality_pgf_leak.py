"""兜底规则 formula_pgf_leak 的单测。"""

from __future__ import annotations

from qiq_html2md.core.types import Context, SkillRequest
from qiq_html2md.quality import _rule_formula


def _make_ctx(formulas: list[dict], md: str = "# Title\n\ntext") -> Context:
    req = SkillRequest(url="https://example.com/x")
    ctx = Context.new(req, trace_id="test-trace", deadline_ts=0.0)
    ctx.strategy = {
        "render_mode": "auto",
        "table_mode": "auto",
        "formula_mode": "auto",
        "image_mode": "download",
        "extractor_profile": "adapter",
        "clean_rules": ["default"],
        "flags": {},
    }
    ctx.extract = {
        "extract_stats": {
            "text_len": 5000,
            "paragraph_count": 10,
            "heading_count": 5,
            "image_count": 0,
            "link_count": 5,
            "table_count": 0,
            "figure_with_table_count": 0,
        },
    }
    ctx.enrich = {"formulas": formulas, "tables": [], "images": [], "refs": []}
    ctx.emit = {"markdown_text": md}
    ctx.warnings = []
    return ctx


def test_pgf_leak_in_artifact_latex_triggers_rule() -> None:
    """artifact.latex 含 \\pgfsys 应触发 formula_pgf_leak。"""
    formulas = [
        {
            "id": "f001",
            "mode": "latex",
            "inline": True,
            "latex": r"q\leftarrow\pgfsys@rect{-53pt}{-4pt}\pgfsys@stroke",
        }
    ]
    ctx = _make_ctx(formulas)
    result = _rule_formula(ctx)
    assert result.failure_reason == "formula_pgf_leak"
    assert result.score <= 70.0  # 100 - 30 = 70


def test_pgf_leak_three_triggers_critical() -> None:
    """≥3 条泄漏升 critical。"""
    formulas = [
        {"id": f"f{i:03d}", "mode": "latex", "inline": True, "latex": r"x\pgfpicture{}"}
        for i in range(1, 4)
    ]
    ctx = _make_ctx(formulas)
    result = _rule_formula(ctx)
    assert result.failure_reason == "formula_pgf_leak"
    assert result.critical is True
    assert result.score <= 10.0


def test_pgf_leak_in_markdown_body_triggers_rule() -> None:
    """artifact 干净但 md 公式块里仍含 pgf 碎片 → 同样命中。"""
    formulas = [
        {
            "id": "f001",
            "mode": "latex",
            "inline": False,
            "latex": r"\sum_{i=0}^{N} x_i",
        }
    ]
    md = """
# Title

some text

$$
\\sum_{i=0}^{N} x_i \\pgfpicture\\makeatletter
$$
"""
    ctx = _make_ctx(formulas, md=md)
    result = _rule_formula(ctx)
    assert result.failure_reason == "formula_pgf_leak"


def test_clean_latex_does_not_trigger() -> None:
    """干净 LaTeX 不应触发。"""
    formulas = [
        {"id": "f001", "mode": "latex", "inline": True, "latex": r"\sum_{i=0}^N x_i"},
        {"id": "f002", "mode": "latex", "inline": False, "latex": r"E = mc^2 \tag{1}"},
    ]
    ctx = _make_ctx(formulas)
    result = _rule_formula(ctx)
    assert result.failure_reason is None
    assert result.score == 100.0


def test_empty_formulas_not_penalized() -> None:
    """无公式时 formula 规则满分。"""
    ctx = _make_ctx([])
    result = _rule_formula(ctx)
    assert result.failure_reason is None
    assert result.score == 100.0
