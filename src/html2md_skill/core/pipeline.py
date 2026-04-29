"""Pipeline —— 主循环 + plan_retry。

架构文档 §10 的主循环实现。阶段三补齐：
- plan_retry 利用 retry_history 做 reason 去重 + 策略升级
- mutate 后强校验：至少改变一项关键策略，否则直接 degraded
- 每次 retry 落盘到 `_diag/retries/retry-NN.plan.json`
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

from html2md_skill.core.budget import Budget, new_default_budget
from html2md_skill.core.errors import FatalError, RetryableError, SkillError
from html2md_skill.core.types import (
    Context,
    QualityReport,
    RetryPlan,
    SkillArtifact,
    SkillRequest,
    SkillResponse,
    SkillStats,
    StageName,
)
from html2md_skill.infra.fs_sandbox import FsSandbox
from html2md_skill.obs.events import EventBus, new_trace_id, write_trace_md
from html2md_skill.obs.metrics import export as metrics_export
from html2md_skill.obs.metrics import write_metrics
from html2md_skill.stages.acquire import AcquireStage
from html2md_skill.stages.emit import EmitStage
from html2md_skill.stages.enrich import EnrichStage
from html2md_skill.stages.extract import ExtractStage

_STAGE_ORDER: tuple[StageName, ...] = ("acquire", "extract", "enrich", "emit")


# 失败原因 → [stages of escalation]
# 每个元素是 (target_stage, strategy_delta) ——
# 列表顺序即"再次出现同一 reason 时的升级路径"。
_RETRY_MAP: dict[str, list[tuple[StageName, dict[str, Any]]]] = {
    "text_too_short": [
        ("acquire", {"render_mode": "browser", "extractor_profile": "density"}),
        ("extract", {"extractor_profile": "body", "clean_rules": ["loose"]}),
    ],
    "heading_retention_low": [
        ("extract", {"flags": {"fix_headings": True}}),
        ("extract", {"clean_rules": ["loose"], "flags": {"fix_headings": True}}),
    ],
    "reference_missing": [
        ("extract", {"flags": {"keep_refs": True}}),
    ],
    "image_retention_low": [
        ("enrich", {"flags": {"scroll_load": True}}),
        ("enrich", {"image_mode": "both"}),
    ],
    "missing_local_resource": [
        ("enrich", {"image_mode": "both"}),
    ],
    "table_retention_low": [
        ("enrich", {"table_mode": "html"}),
        ("enrich", {"table_mode": "image"}),
    ],
    "complex_table_damaged": [
        ("enrich", {"table_mode": "image"}),
        ("enrich", {"table_mode": "html"}),
    ],
    "formula_retention_low": [
        ("acquire", {"render_mode": "browser"}),
        ("enrich", {"formula_mode": "image"}),
    ],
    "formula_image_missing": [
        ("enrich", {"formula_mode": "image"}),
    ],
    "markdown_structure_invalid": [
        ("emit", {"flags": {"fix_structure": True}}),
        ("extract", {"clean_rules": ["loose"], "flags": {"fix_headings": True}}),
    ],
}


def plan_retry(
    report: QualityReport,
    budget: Budget,
    ctx: Context,
    max_retry: int,
) -> RetryPlan | None:
    """产出下一步重试计划。

    规则：
    - 已达到 max_retry 或预算不足 → None。
    - 对 failed_rules 按顺序挑第一个有效的 reason：
      - 取该 reason 的升级路径中、之前"尚未用过的"那一步；
      - 若路径全部用过，跳过该 reason 继续尝试下一个 failed reason。
    - 若全部 reason 都用尽，返回 None。
    """
    attempts = len(ctx.retry_history)
    if attempts >= max_retry:
        return None
    if not budget.can_retry(10):
        return None
    if not report.failed_rules:
        return None

    used_reason_count: dict[str, int] = {}
    for rp in ctx.retry_history:
        used_reason_count[rp.reason] = used_reason_count.get(rp.reason, 0) + 1

    for reason in report.failed_rules:
        path = _RETRY_MAP.get(reason)
        if not path:
            continue
        step = used_reason_count.get(reason, 0)
        if step >= len(path):
            continue
        target, delta = path[step]
        # 守卫：delta 必须带来实际变化
        if not _delta_effective(delta, ctx.strategy):
            # 继续尝试同 reason 的下一档
            for next_step in range(step + 1, len(path)):
                nxt_target, nxt_delta = path[next_step]
                if _delta_effective(nxt_delta, ctx.strategy):
                    target, delta = nxt_target, nxt_delta
                    break
            else:
                continue
        return RetryPlan(
            reason=reason,
            target_stage=target,
            delta=delta,
            budget_seconds=min(120, int(budget.global_left())),
        )
    return None


def _delta_effective(delta: dict[str, Any], strategy: dict[str, Any]) -> bool:
    """判断 delta 应用到 strategy 后是否至少改变一项。"""
    for k, v in delta.items():
        if k == "flags" and isinstance(v, dict):
            cur = strategy.get("flags", {}) or {}
            for fk, fv in v.items():
                if cur.get(fk) != fv:
                    return True
            continue
        if strategy.get(k) != v:
            return True
    return False


def run(request: SkillRequest, *, allow_file_scheme: bool = False) -> SkillResponse:
    """主入口：执行 skill。"""
    trace_id = new_trace_id()
    output_dir = FsSandbox(request.output_dir).root  # 创建输出目录
    diag_dir = output_dir / "_diag"
    bus = EventBus(trace_id=trace_id, diag_dir=diag_dir)

    budget = new_default_budget(request.timeout_seconds)
    ctx = Context.new(request, trace_id=trace_id, deadline_ts=budget.deadline_ts)

    bus.emit(
        "skill.started",
        {
            "url": request.url,
            "render_mode": request.render_mode,
            "debug": request.debug,
        },
    )

    stages: list[Any] = [
        AcquireStage(bus=bus, allow_file_scheme=allow_file_scheme),
        ExtractStage(),
        EnrichStage(allow_file_scheme=allow_file_scheme),
        EmitStage(),
    ]

    cursor = 0
    t_run0 = time.monotonic()
    final_status = "failed"
    last_error: str | None = None

    try:
        while True:
            try:
                for i in range(cursor, len(stages)):
                    if not budget.can_retry(1):
                        raise FatalError("budget_exhausted")
                    stage = stages[i]
                    with bus.span(stage.name):
                        bus.emit(
                            "stage.started",
                            {"stage": stage.name, "strategy": dict(ctx.strategy)},
                        )
                        try:
                            with budget.checkout(stage.name):
                                result = stage.run(ctx)
                        except RetryableError as re:
                            bus.emit(
                                "stage.finished",
                                {
                                    "stage": stage.name,
                                    "duration_ms": 0,
                                    "level": "error",
                                    "error": repr(re),
                                    "reason": re.payload.get("reason"),
                                },
                            )
                            raise
                        except FatalError:
                            raise
                        ctx.apply(result)
                        budget.release_unused(stage.name)
                        bus.emit(
                            "stage.finished",
                            {
                                "stage": stage.name,
                                "duration_ms": result.duration_ms,
                                "stats": result.output.get(f"{stage.name}_stats", {}),
                            },
                        )
            except RetryableError as re:
                q = ctx.quality_report
                if q is None:
                    reason = re.payload.get("reason") or "text_too_short"
                    q = QualityReport(
                        passed=False,
                        final_score=0.0,
                        failed_rules=[reason],
                        critical_failures=[reason],
                        risk_level="high",
                    )
                bus.emit(
                    "quality.scored",
                    {
                        "final_score": q.final_score,
                        "sub_scores": q.sub_scores,
                        "passed": q.passed,
                    },
                )
                plan = plan_retry(q, budget, ctx, request.max_retry)
                if plan is None:
                    final_status = "degraded" if ctx.emit else "failed"
                    break
                bus.emit("retry.planned", plan.model_dump())
                ctx.retry_history.append(plan)
                ctx.merge_strategy(plan.delta)
                target_idx = _STAGE_ORDER.index(plan.target_stage)
                stages[target_idx] = stages[target_idx].mutate(plan.delta)
                ctx.reset_from(plan.target_stage)
                cursor = target_idx
                retries_dir = diag_dir / "retries"
                retries_dir.mkdir(parents=True, exist_ok=True)
                attempt_no = len(ctx.retry_history)
                (retries_dir / f"retry-{attempt_no:02d}.plan.json").write_text(
                    json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                continue
            else:
                if ctx.quality_report and ctx.quality_report.passed:
                    final_status = "passed"
                    break
                final_status = "degraded"
                break

    except FatalError as fe:
        last_error = repr(fe)
        final_status = "degraded" if ctx.emit else "failed"
    except SkillError as se:
        last_error = repr(se)
        final_status = "failed"
    except Exception as e:  # noqa: BLE001
        last_error = repr(e)
        final_status = "failed"
    finally:
        duration_ms = int((time.monotonic() - t_run0) * 1000)
        attempts = len(ctx.retry_history)
        metrics_data = {
            "trace_id": trace_id,
            "status": final_status,
            "duration_ms": duration_ms,
            "retries": attempts,
            "retry_reasons": [r.reason for r in ctx.retry_history],
            "budget": budget.stats(),
        }
        write_metrics(diag_dir / "metrics.json", metrics_data)
        # 把指标广播给已注册的 exporter（OTel/Prometheus 等）
        try:
            metrics_export(metrics_data)
        except Exception:  # noqa: BLE001 exporter 失败不阻塞
            pass
        bus.emit(
            "skill.finished",
            {
                "status": final_status,
                "duration_ms": duration_ms,
                "retries": attempts,
                "error": last_error,
                "level": "error" if final_status == "failed" else "info",
            },
        )
        # 生成人类可读的 TRACE.md（必须在 skill.finished emit 之后，确保事件流完整）
        try:
            write_trace_md(bus)
        except Exception:  # noqa: BLE001
            pass

    return _build_response(
        request=request,
        ctx=ctx,
        bus=bus,
        status=final_status,
        duration_ms=duration_ms,
        attempts=len(ctx.retry_history),
    )


def _build_response(
    *,
    request: SkillRequest,  # noqa: ARG001 保留供未来扩展
    ctx: Context,
    bus: EventBus,
    status: str,
    duration_ms: int,
    attempts: int,
) -> SkillResponse:
    diag_dir = ctx.output_dir / "_diag"
    emit = ctx.emit or {}
    risk = (ctx.quality_report.risk_level if ctx.quality_report else "high")

    return SkillResponse(
        status=cast(Any, status),
        trace_id=ctx.trace_id,
        artifact=SkillArtifact(
            markdown_path=emit.get("markdown_path"),
            assets_dir=emit.get("assets_dir"),
        ),
        metadata_path=emit.get("metadata_path"),
        quality_report_path=emit.get("quality_report_path"),
        warnings_path=emit.get("warnings_path") or str(ctx.output_dir / "warnings.json"),
        diag_dir=str(diag_dir),
        stats=SkillStats(duration_ms=duration_ms, retries=attempts),
        risk_level=cast(Any, risk),
        events_tail=bus.tail(),
    )

