"""端到端冒烟测试：静态 HTML fixture → article.md + _diag/events.jsonl。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qiq_html2md.core.pipeline import run
from qiq_html2md.core.types import SkillRequest


@pytest.fixture()
def fixture_url() -> str:
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    return f"file://{p}"


def test_mvp_smoke(tmp_path: Path, fixture_url: str) -> None:
    req = SkillRequest(
        url=fixture_url,
        output_dir=str(tmp_path / "out"),
        timeout_seconds=60,
        max_retry=0,  # MVP 不触发重试
        quality_check=True,
    )
    resp = run(req, allow_file_scheme=True)

    # 1. 基本契约
    assert resp.status in ("passed", "degraded")
    assert resp.trace_id
    assert resp.diag_dir
    assert Path(resp.diag_dir).exists()

    # 2. 产出 article.md
    assert resp.artifact.markdown_path is not None
    md_path = Path(resp.artifact.markdown_path)
    assert md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    assert len(md) > 500
    assert md.startswith("# ")
    assert "RAS-Paper" in md
    assert "Abstract" in md

    # 3. metadata.json
    meta = json.loads(Path(resp.metadata_path).read_text())  # type: ignore[arg-type]
    assert meta["title"]
    assert "RAS-Paper" in meta["title"] or "Summar" in meta["title"]

    # 4. quality_report.json
    qr = json.loads(Path(resp.quality_report_path).read_text())  # type: ignore[arg-type]
    assert "final_score" in qr
    assert "sub_scores" in qr

    # 5. _diag/events.jsonl 含核心事件
    events_path = Path(resp.diag_dir) / "events.jsonl"
    assert events_path.exists()
    lines = events_path.read_text().strip().splitlines()
    names = [json.loads(line)["name"] for line in lines]
    assert "skill.started" in names
    assert "skill.finished" in names
    assert names.count("stage.started") == 4
    assert names.count("stage.finished") >= 3  # emit 在 quality 未通过时不会 finished

    # 6. Stage 快照
    assert (Path(resp.diag_dir) / "stages" / "acquire.json").exists()
    assert (Path(resp.diag_dir) / "stages" / "extract.json").exists()

    # 7. metrics.json
    metrics = json.loads((Path(resp.diag_dir) / "metrics.json").read_text())
    assert metrics["trace_id"] == resp.trace_id
    assert metrics["duration_ms"] >= 0

    # 8. events_tail
    assert len(resp.events_tail) > 0
    assert resp.events_tail[-1].name == "skill.finished"


def test_mvp_unreachable_url_fails_fast(tmp_path: Path) -> None:
    """非法 scheme 应该直接 FatalError → status=failed。"""
    req = SkillRequest(
        url="ftp://example.com/x.html",
        output_dir=str(tmp_path / "out"),
        timeout_seconds=10,
    )
    resp = run(req, allow_file_scheme=False)
    assert resp.status in ("failed", "degraded")
    events_path = Path(resp.diag_dir) / "events.jsonl"
    assert events_path.exists()
