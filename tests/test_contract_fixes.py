"""审计修复回归测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from qiq_html2md.core.pipeline import run
from qiq_html2md.core.types import Context, SkillRequest
from qiq_html2md.stages.emit import _inline


@pytest.mark.parametrize("url", ["", "not a url at all"])
def test_skill_request_rejects_invalid_url(url: str) -> None:
    with pytest.raises(ValueError):
        SkillRequest(url=url)


def test_merge_strategy_deep_merges_flags() -> None:
    ctx = Context.new(SkillRequest(url="https://example.com/x"), trace_id="t", deadline_ts=0)
    ctx.merge_strategy({"flags": {"fix_headings": True}})
    ctx.merge_strategy({"flags": {"keep_refs": True}})
    assert ctx.strategy["flags"] == {"fix_headings": True, "keep_refs": True}


def test_idempotency_key_changes_output_dir(tmp_path: Path) -> None:
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    req = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out"),
        idempotency_key="job-1",
        max_retry=0,
    )
    resp = run(req, allow_file_scheme=True)
    assert "/job-1/" in resp.artifact.markdown_path  # type: ignore[operator]
    assert Path(resp.artifact.markdown_path).is_file()  # type: ignore[arg-type]


def test_failed_response_warnings_path_exists(tmp_path: Path) -> None:
    req = SkillRequest(url="ftp://example.com/x", output_dir=str(tmp_path / "out"), max_retry=0)
    resp = run(req)
    assert resp.status == "failed"
    assert Path(resp.warnings_path).is_file()
    assert json.loads(Path(resp.warnings_path).read_text()) == []


def test_include_metadata_false_skips_metadata_file(tmp_path: Path) -> None:
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    req = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out"),
        include_metadata=False,
        max_retry=0,
    )
    resp = run(req, allow_file_scheme=True)
    assert resp.status == "passed"
    assert resp.metadata_path is None
    assert not (tmp_path / "out" / "metadata.json").exists()


def test_quality_check_false_passes_short_document(tmp_path: Path) -> None:
    fixture = tmp_path / "short.html"
    fixture.write_text("<html><body><h1>T</h1><p>short</p></body></html>", encoding="utf-8")
    req = SkillRequest(
        url=f"file://{fixture}",
        output_dir=str(tmp_path / "out"),
        quality_check=False,
        max_retry=0,
    )
    resp = run(req, allow_file_scheme=True)
    assert resp.status == "passed"
    report = json.loads(Path(resp.quality_report_path).read_text())  # type: ignore[arg-type]
    assert report["sub_scores"] == {"quality_check": 100.0}


def test_preserve_intermediate_writes_snapshots(tmp_path: Path) -> None:
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    req = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out"),
        preserve_intermediate=True,
        max_retry=0,
    )
    resp = run(req, allow_file_scheme=True)
    assert resp.status == "passed"
    assert (Path(resp.diag_dir) / "intermediate" / "acquire.json").is_file()
    assert (Path(resp.diag_dir) / "intermediate" / "emit.json").is_file()


def test_stage_stats_mapping_in_diag(tmp_path: Path) -> None:
    p = Path(__file__).parent / "fixtures" / "paper_rich.html"
    req = SkillRequest(url=f"file://{p}", output_dir=str(tmp_path / "out"), max_retry=0)
    resp = run(req, allow_file_scheme=True)
    acquire = json.loads((Path(resp.diag_dir) / "stages" / "acquire.json").read_text())
    emit = json.loads((Path(resp.diag_dir) / "stages" / "emit.json").read_text())
    assert "chars" in acquire["payload"]["stats"]
    assert "markdown_chars" in emit["payload"]["stats"]


def test_inline_removes_extra_spaces_before_punctuation() -> None:
    from bs4 import BeautifulSoup

    samples = {
        '<p>A <strong>B</strong>.</p>': "A **B**.",
        '<p>See <a href="/x">ref</a>, thanks.</p>': "See [ref](/x), thanks.",
    }
    for html, expected in samples.items():
        p = BeautifulSoup(html, "lxml").find("p")
        assert _inline(p, {}) == expected


def test_cli_supports_advanced_options(tmp_path: Path) -> None:
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    out = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "qiq_html2md",
            "--allow-file-scheme",
            "--url",
            f"file://{p}",
            "--output-dir",
            str(out),
            "--max-retry",
            "0",
            "--no-metadata",
            "--idempotency-key",
            "cli-job",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    resp = json.loads(result.stdout)
    assert resp["status"] == "passed"
    assert resp["metadata_path"] is None
    assert "/cli-job/" in resp["artifact"]["markdown_path"]
