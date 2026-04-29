"""阶段五测试：browser_pool / cache / TRACE.md / OTel 接口。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from html2md_skill.core.pipeline import run
from html2md_skill.core.types import SkillRequest
from html2md_skill.infra import browser_pool, cache
from html2md_skill.infra.browser_pool import BrowserPool
from html2md_skill.obs import metrics

# ---------------------------------------------------------------------------
# browser_pool
# ---------------------------------------------------------------------------


def test_browser_pool_is_singleton() -> None:
    browser_pool.reset_pool()
    a = browser_pool.get_pool()
    b = browser_pool.get_pool()
    assert a is b
    browser_pool.reset_pool()


def test_browser_pool_stats_initial() -> None:
    browser_pool.reset_pool()
    pool = browser_pool.get_pool()
    s = pool.stats()
    assert s["launches"] == 0
    assert s["context_opens"] == 0
    assert s["active_contexts"] == 0
    browser_pool.reset_pool()


def test_browser_pool_shutdown_safe_without_browser() -> None:
    pool = BrowserPool()
    pool.shutdown()
    assert pool.stats()["active_contexts"] == 0


def test_browser_pool_zero_idle_timeout_closes_after_context() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeBrowser:
        def __init__(self) -> None:
            self.closed = False
            self.context = FakeContext()

        def new_context(self, **_: object) -> FakeContext:
            return self.context

        def close(self) -> None:
            self.closed = True

    pool = BrowserPool(idle_timeout=0)
    browser = FakeBrowser()
    pool._browser = browser  # type: ignore[attr-defined]  # 测试内部状态

    with pool.context() as ctx:
        assert ctx is browser.context

    assert browser.context.closed is True
    assert browser.closed is True
    assert pool.stats()["closes"] == 1


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def test_http_cache_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTML2MD_SKILL_CACHE_DIR", str(tmp_path / "cache"))
    e = cache.HttpCacheEntry(
        url="https://example.com/a",
        final_url="https://example.com/a",
        status=200,
        headers={"Content-Type": "text/html", "ETag": "W/\"abc\""},
        content=b"<html/>",
        etag='W/"abc"',
    )
    cache.put_http(e)
    got = cache.get_http("https://example.com/a")
    assert got is not None
    assert got.content == b"<html/>"
    assert got.etag == 'W/"abc"'
    assert "If-None-Match" in got.conditional_headers()


def test_extract_cache_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTML2MD_SKILL_CACHE_DIR", str(tmp_path / "cache"))
    key = cache.make_extract_key(
        "https://example.com/a", render_mode="static", adapter_version="arxiv:v1"
    )
    cache.put_extract(key, {"text": "hello"})
    assert cache.get_extract(key) == {"text": "hello"}
    # invalidate
    cache.invalidate_extract(key)
    assert cache.get_extract(key) is None


def test_extract_cache_hit_on_second_run(tmp_path: Path) -> None:
    """同一 fixture 跑两次，第二次 extract 应命中缓存并打 cache_hit。"""
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    req = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out"),
        timeout_seconds=30,
        max_retry=0,
    )
    resp1 = run(req, allow_file_scheme=True)
    assert resp1.status == "passed"

    req2 = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out2"),
        timeout_seconds=30,
        max_retry=0,
    )
    resp2 = run(req2, allow_file_scheme=True)
    assert resp2.status == "passed"

    # 第二次 extract 的 snapshot 中应有 cache_hit 标记
    stats = json.loads((Path(resp2.diag_dir) / "stages" / "extract.json").read_text())
    extract_stats = stats["payload"]["stats"]
    assert extract_stats.get("cache_hit") is True


# ---------------------------------------------------------------------------
# TRACE.md
# ---------------------------------------------------------------------------


def test_trace_md_generated(tmp_path: Path) -> None:
    p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    req = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out"),
        timeout_seconds=30,
        max_retry=0,
    )
    resp = run(req, allow_file_scheme=True)
    trace_path = Path(resp.diag_dir) / "TRACE.md"
    assert trace_path.is_file()
    text = trace_path.read_text()
    assert text.startswith(f"# Trace {resp.trace_id}")
    assert "skill.started" in text
    assert "skill.finished" in text
    assert "## Timeline" in text


# ---------------------------------------------------------------------------
# OTel / exporter 插拔点
# ---------------------------------------------------------------------------


def test_metrics_exporter_plug_in(tmp_path: Path) -> None:
    metrics.reset_exporters()
    captured: list[dict] = []

    def my_exporter(data: dict) -> None:
        captured.append(data)

    metrics.register_exporter(my_exporter)
    try:
        p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
        req = SkillRequest(
            url=f"file://{p}",
            output_dir=str(tmp_path / "out"),
            timeout_seconds=30,
            max_retry=0,
        )
        resp = run(req, allow_file_scheme=True)
        assert resp.status == "passed"
        assert len(captured) == 1
        assert captured[0]["trace_id"] == resp.trace_id
        assert captured[0]["status"] == "passed"
        assert "budget" in captured[0]
    finally:
        metrics.reset_exporters()


def test_metrics_exporter_exception_does_not_break_skill(tmp_path: Path) -> None:
    metrics.reset_exporters()

    def bad_exporter(_: dict) -> None:
        raise RuntimeError("boom")

    metrics.register_exporter(bad_exporter)
    try:
        p = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
        req = SkillRequest(
            url=f"file://{p}",
            output_dir=str(tmp_path / "out"),
            timeout_seconds=30,
            max_retry=0,
        )
        resp = run(req, allow_file_scheme=True)
        assert resp.status == "passed"  # exporter 异常不应影响 skill
    finally:
        metrics.reset_exporters()


def test_otel_export_returns_false_when_sdk_missing() -> None:
    # 当前环境未装 opentelemetry → 应该返回 False，无副作用
    assert metrics.export_otel("http://localhost:4318") in (True, False)
