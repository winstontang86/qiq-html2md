from __future__ import annotations

from html2md_skill.core.types import (
    Context,
    Event,
    QualityReport,
    RetryPlan,
    SkillRequest,
    SkillResponse,
    StageResult,
)


def test_skill_request_defaults() -> None:
    r = SkillRequest(url="https://example.com/a")
    assert r.timeout_seconds == 600
    assert r.render_mode == "auto"
    assert r.max_retry == 2
    assert r.debug == "lite"


def test_skill_request_frozen() -> None:
    r = SkillRequest(url="https://example.com/a")
    try:
        r.url = "x"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("SkillRequest must be frozen")


def test_context_apply_and_reset() -> None:
    req = SkillRequest(url="https://example.com/a")
    ctx = Context.new(req, trace_id="t1", deadline_ts=9999999999.0)
    assert ctx.acquire is None

    ctx.apply(StageResult(stage="acquire", output={"raw_html": "<html/>"}))
    assert ctx.acquire == {"raw_html": "<html/>"}

    ctx.apply(StageResult(stage="extract", output={"text_len": 100}))
    assert ctx.extract == {"text_len": 100}

    # reset_from 清掉 extract 及下游
    ctx.reset_from("extract")
    assert ctx.acquire == {"raw_html": "<html/>"}
    assert ctx.extract is None
    assert ctx.enrich is None
    assert ctx.emit is None


def test_context_strategy_merge() -> None:
    req = SkillRequest(url="https://example.com/a", render_mode="auto")
    ctx = Context.new(req, trace_id="t", deadline_ts=0)
    ctx.merge_strategy({"render_mode": "browser", "flags": {"scroll_load": True}})
    assert ctx.strategy["render_mode"] == "browser"
    assert ctx.strategy["flags"] == {"scroll_load": True}


def test_quality_report() -> None:
    q = QualityReport(passed=True, final_score=85.0, sub_scores={"text": 30.0})
    assert q.passed is True


def test_retry_plan_frozen() -> None:
    p = RetryPlan(reason="text_too_short", target_stage="acquire", delta={"render_mode": "browser"})
    assert p.target_stage == "acquire"


def test_event() -> None:
    e = Event(ts="2026-04-29T10:00:00.000Z", trace_id="t", stage="acquire", seq=1, name="stage.started")
    assert e.seq == 1


def test_skill_response_minimal() -> None:
    r = SkillResponse(status="failed", trace_id="t", warnings_path="/x/w.json", diag_dir="/x/_diag")
    assert r.status == "failed"
    assert r.events_tail == []
