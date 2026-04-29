"""阶段二功能测试：figure / tables / formulas / references。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from html2md_skill.core.pipeline import run
from html2md_skill.core.types import SkillRequest


@pytest.fixture()
def paper_url() -> str:
    p = Path(__file__).parent / "fixtures" / "paper_rich.html"
    return f"file://{p}"


def test_phase2_full_run(tmp_path: Path, paper_url: str) -> None:
    req = SkillRequest(
        url=paper_url,
        output_dir=str(tmp_path / "out"),
        timeout_seconds=60,
        max_retry=0,
    )
    resp = run(req, allow_file_scheme=True)

    # 因为 fixture 的图片 URL 用 example.invalid，quality 不受图片影响；仍应通过
    assert resp.status == "passed", resp.model_dump()

    md = Path(resp.artifact.markdown_path).read_text()  # type: ignore[arg-type]

    # --- figure/figcaption ---
    # 图片会因为 invalid 域名下载失败；Markdown 里退化为 remote url 或缺图
    # 关键是 figcaption 被保留
    assert "End-to-end pipeline diagram" in md

    # --- 表格：简单表格转 Markdown ---
    assert "| Model | Top-1 | Top-5 |" in md
    assert "| BaselineNet |" in md
    assert "| Ours |" in md
    # caption 被渲染
    assert "Accuracy comparison" in md

    # --- 复杂表格降级为 HTML ---
    assert "<table" in md  # 复杂表格以 HTML 输出
    assert "rowspan" in md
    # 但注意 caption 依然在
    # （复杂表格 caption 通过 **caption** 渲染到正文）

    # --- 公式：LaTeX 行内 ---
    assert "$a^2 + b^2 = c^2$" in md
    # --- 公式：LaTeX 块级 ---
    assert "$$" in md
    assert "E = m c^{2}" in md
    # --- 公式：MathML 保留（无 annotation 的那条）---
    assert "<math" in md  # MathML 保留原样

    # --- 参考文献 ---
    assert "## References" in md
    assert "Paper One" in md
    assert "Paper Two" in md
    assert "Paper Three" in md

    # --- enrich_stats ---
    stages_dir = Path(resp.diag_dir) / "stages"
    enrich_snap = json.loads((stages_dir / "enrich.json").read_text())
    stats = enrich_snap["payload"]["stats"]
    assert stats["formula_total"] >= 2
    assert stats["formula_latex"] >= 2
    assert stats["table_total"] == 2
    assert stats["table_markdown"] == 1
    assert stats["table_html"] == 1
    assert stats["ref_total"] == 3


def test_phase2_still_works_on_simple_fixture(tmp_path: Path) -> None:
    """阶段一的简单 fixture 仍应通过。"""
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    req = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out"),
        timeout_seconds=60,
        max_retry=0,
    )
    resp = run(req, allow_file_scheme=True)
    assert resp.status in ("passed", "degraded")
    md = Path(resp.artifact.markdown_path).read_text()  # type: ignore[arg-type]
    assert "RAS-Paper" in md
    # 参考文献应当自动渲染（该 fixture 的 section 是 .ltx_bibliography）
    assert "## References" in md
    assert "Neural Summarization" in md
