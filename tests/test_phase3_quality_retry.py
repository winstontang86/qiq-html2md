"""阶段三测试：质量规则 + plan_retry + 局部重跑闭环。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qiq_html2md.core.budget import Budget
from qiq_html2md.core.pipeline import plan_retry, run
from qiq_html2md.core.types import (
    Context,
    QualityReport,
    SkillRequest,
)
from qiq_html2md.quality import evaluate

# ---------------------------------------------------------------------------
# plan_retry 单元测试
# ---------------------------------------------------------------------------


def _mk_ctx() -> Context:
    return Context.new(
        SkillRequest(url="https://example.com/x", max_retry=2),
        trace_id="t",
        deadline_ts=0.0,
    )


def test_plan_retry_returns_none_when_max_retry_reached() -> None:
    ctx = _mk_ctx()
    # 模拟已经重试过 2 次
    from qiq_html2md.core.types import RetryPlan
    ctx.retry_history = [
        RetryPlan(reason="text_too_short", target_stage="acquire", delta={}),
        RetryPlan(reason="text_too_short", target_stage="acquire", delta={}),
    ]
    q = QualityReport(passed=False, final_score=40.0, failed_rules=["text_too_short"], critical_failures=["text_too_short"])
    plan = plan_retry(q, Budget(600), ctx, max_retry=2)
    assert plan is None


def test_plan_retry_none_when_no_failed_rules() -> None:
    ctx = _mk_ctx()
    q = QualityReport(passed=True, final_score=90.0)
    assert plan_retry(q, Budget(600), ctx, max_retry=2) is None


def test_plan_retry_picks_escalation_path() -> None:
    """同一 reason 第二次出现时应走升级路径的第二步。"""
    ctx = _mk_ctx()
    q = QualityReport(
        passed=False,
        final_score=40.0,
        failed_rules=["text_too_short"],
        critical_failures=["text_too_short"],
    )
    # 第一次 plan
    plan1 = plan_retry(q, Budget(600), ctx, max_retry=3)
    assert plan1 is not None
    assert plan1.reason == "text_too_short"
    # _RETRY_MAP["text_too_short"] 第一步 target=acquire
    assert plan1.target_stage == "acquire"

    # 模拟第一次重试已执行
    ctx.retry_history.append(plan1)
    ctx.merge_strategy(plan1.delta)

    plan2 = plan_retry(q, Budget(600), ctx, max_retry=3)
    assert plan2 is not None
    # 第二步应升级到 extract + body profile
    assert plan2.target_stage == "extract"
    assert plan2.delta.get("extractor_profile") == "body"


def test_plan_retry_skips_reason_when_delta_ineffective() -> None:
    """如果 delta 不会改变 strategy，plan_retry 应尝试同 reason 的下一档或下一个 reason。"""
    ctx = _mk_ctx()
    # 预先把 strategy 设为 delta 目标值，让第一档失效
    ctx.merge_strategy({"render_mode": "browser", "extractor_profile": "density"})
    q = QualityReport(
        passed=False,
        final_score=40.0,
        failed_rules=["text_too_short"],
        critical_failures=["text_too_short"],
    )
    plan = plan_retry(q, Budget(600), ctx, max_retry=3)
    assert plan is not None
    # 跳到下一档（extract + body）
    assert plan.target_stage == "extract"
    assert plan.delta.get("extractor_profile") == "body"


# ---------------------------------------------------------------------------
# quality.evaluate 单元测试
# ---------------------------------------------------------------------------


def test_quality_all_rules_pass_when_everything_is_fine() -> None:
    ctx = _mk_ctx()
    ctx.extract = {
        "clean_html": "<body><p>hi</p></body>",
        "extract_stats": {
            "text_len": 2000,
            "heading_count": 3,
            "paragraph_count": 10,
            "image_count": 0,
            "link_count": 0,
        },
    }
    ctx.enrich = {
        "images": [],
        "tables": [],
        "formulas": [],
        "refs": [],
    }
    ctx.emit = {"markdown_text": "# Title\n\n" + "Paragraph. " * 50 + "\n\n## Sub\n\npara"}
    report = evaluate(ctx)
    assert report.passed is True
    assert report.final_score >= 80


def test_quality_text_too_short_is_critical() -> None:
    ctx = _mk_ctx()
    ctx.extract = {
        "clean_html": "<body></body>",
        "extract_stats": {
            "text_len": 100,
            "heading_count": 1,
            "paragraph_count": 1,
            "image_count": 0,
            "link_count": 0,
        },
    }
    ctx.emit = {"markdown_text": "# hi\n"}
    report = evaluate(ctx)
    assert report.passed is False
    assert "text_too_short" in report.critical_failures
    assert report.risk_level == "high"


def test_quality_image_retention_detected() -> None:
    ctx = _mk_ctx()
    ctx.extract = {
        "clean_html": "<body><p>x</p></body>",
        "extract_stats": {
            "text_len": 1500,
            "heading_count": 3,
            "paragraph_count": 8,
            "image_count": 10,
            "link_count": 0,
        },
    }
    # 只成功 1 张
    ctx.enrich = {
        "images": [
            {"id": "i001", "local_path": "assets/images/fig-001.png"},
        ],
        "tables": [],
        "formulas": [],
        "refs": [],
    }
    ctx.emit = {"markdown_text": "# Title\n\n" + "Paragraph. " * 40}
    report = evaluate(ctx)
    # image 规则应该产生 missing_local_resource 或 image_retention_low
    assert "missing_local_resource" in report.failed_rules or "image_retention_low" in report.failed_rules


def test_quality_formula_retention() -> None:
    ctx = _mk_ctx()
    ctx.extract = {
        "clean_html": "<body></body>",
        "extract_stats": {
            "text_len": 1500,
            "heading_count": 3,
            "paragraph_count": 8,
            "image_count": 0,
            "link_count": 0,
        },
    }
    ctx.enrich = {
        "images": [],
        "tables": [],
        "formulas": [
            # 1 个 ok latex
            {"id": "f001", "mode": "latex", "latex": "E=mc^2"},
            # 4 个不完整 (既无 latex 也无 mathml) —— 应该触发 formula_retention_low
            {"id": "f002", "mode": "mathml", "mathml": ""},
            {"id": "f003", "mode": "mathml", "mathml": ""},
            {"id": "f004", "mode": "mathml", "mathml": ""},
            {"id": "f005", "mode": "mathml", "mathml": ""},
        ],
        "refs": [],
    }
    ctx.emit = {"markdown_text": "# Title\n\n" + "Paragraph. " * 40}
    report = evaluate(ctx)
    assert "formula_retention_low" in report.failed_rules


# ---------------------------------------------------------------------------
# 局部重跑 E2E：通过 monkeypatch 让第一次 extract 产出短文触发重试
# ---------------------------------------------------------------------------


def test_local_retry_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次跑的时候强制 Extract 返回短文，之后检查第二轮能 passed。"""
    from qiq_html2md.stages import extract as extract_mod

    original_run = extract_mod.ExtractStage.run
    call_counter = {"n": 0}

    def patched_run(self, ctx):  # type: ignore[no-untyped-def]
        call_counter["n"] += 1
        result = original_run(self, ctx)
        if call_counter["n"] == 1:
            # 人为把统计调低以触发 text_too_short
            stats = dict(result.output.get("extract_stats", {}))
            stats["text_len"] = 100
            new_output = {**result.output, "extract_stats": stats}
            from qiq_html2md.core.types import StageResult
            return StageResult(
                stage="extract",
                output=new_output,
                warnings=list(result.warnings),
                duration_ms=result.duration_ms,
            )
        return result

    monkeypatch.setattr(extract_mod.ExtractStage, "run", patched_run)

    p = Path(__file__).parent / "fixtures" / "paper_rich.html"
    req = SkillRequest(
        url=f"file://{p}",
        output_dir=str(tmp_path / "out"),
        timeout_seconds=60,
        max_retry=2,
    )
    resp = run(req, allow_file_scheme=True)

    # 第一次 extract 文本不足 → 至少 1 次重试；第二次产出正常 → passed
    assert resp.stats.retries >= 1
    # 该 fixture 内容充足，第二次应能通过
    assert resp.status == "passed", f"retries={resp.stats.retries} status={resp.status}"

    # retry 计划落盘
    retries_dir = Path(resp.diag_dir) / "retries"
    plans = list(retries_dir.glob("retry-*.plan.json"))
    assert len(plans) >= 1
    plan = json.loads(plans[0].read_text())
    assert plan["reason"] == "text_too_short"
