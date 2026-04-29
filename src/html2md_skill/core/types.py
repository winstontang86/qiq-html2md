"""核心数据类型 —— 单一数据来源。

本模块定义 html2md skill 的全部对外/对内契约数据类，共 7 个：
- SkillRequest / SkillResponse —— 契约 IO
- Context —— 任务上下文
- StageResult —— Stage 产物
- RetryPlan —— 重试计划
- QualityReport —— 质量报告
- Event —— 可观测事件

字段定义对应架构文档 §5 实现契约表。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------

RenderMode = Literal["auto", "static", "browser"]
TableMode = Literal["auto", "markdown", "html", "image"]
FormulaMode = Literal["auto", "latex", "mathml", "image"]
ImageMode = Literal["download", "link", "both"]
DebugMode = Literal["lite", "full"]
Status = Literal["passed", "degraded", "failed"]
RiskLevel = Literal["low", "medium", "high"]
StageName = Literal["acquire", "extract", "enrich", "emit"]


# ---------------------------------------------------------------------------
# SkillRequest / SkillResponse
# ---------------------------------------------------------------------------


class SkillRequest(BaseModel):
    """Skill 入参契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    output_dir: str = "./output"
    timeout_seconds: int = Field(default=600, ge=1, le=600)
    render_mode: RenderMode = "auto"
    table_mode: TableMode = "auto"
    formula_mode: FormulaMode = "auto"
    image_mode: ImageMode = "download"
    quality_check: bool = True
    max_retry: int = Field(default=2, ge=0, le=5)
    include_references: bool = True
    include_metadata: bool = True
    debug: DebugMode = "lite"
    preserve_intermediate: bool = False
    idempotency_key: str | None = None


class SkillArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)
    markdown_path: str | None = None
    assets_dir: str | None = None


class SkillStats(BaseModel):
    model_config = ConfigDict(frozen=True)
    duration_ms: int = 0
    retries: int = 0


class SkillResponse(BaseModel):
    """Skill 出参契约。"""

    model_config = ConfigDict(frozen=True)

    status: Status
    trace_id: str
    artifact: SkillArtifact = Field(default_factory=SkillArtifact)
    metadata_path: str | None = None
    quality_report_path: str | None = None
    warnings_path: str
    diag_dir: str
    stats: SkillStats = Field(default_factory=SkillStats)
    risk_level: RiskLevel = "medium"
    events_tail: list[Event] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# QualityReport
# ---------------------------------------------------------------------------


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    final_score: float
    sub_scores: dict[str, float] = Field(default_factory=dict)
    critical_failures: list[str] = Field(default_factory=list)
    failed_rules: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "medium"


# ---------------------------------------------------------------------------
# RetryPlan
# ---------------------------------------------------------------------------


class RetryPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reason: str
    target_stage: StageName
    delta: dict[str, Any] = Field(default_factory=dict)
    budget_seconds: int = 60


# ---------------------------------------------------------------------------
# StageResult
# ---------------------------------------------------------------------------


class StageResult(BaseModel):
    """Stage 产物。

    `output` 是 dict；约定进入 `ctx.<stage>` 域。
    """

    model_config = ConfigDict(extra="forbid")
    stage: StageName
    output: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ts: str  # ISO8601 UTC ms
    trace_id: str
    span_id: str | None = None
    stage: str
    seq: int
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class Context(BaseModel):
    """贯穿全程的任务上下文。

    Stage 只能写入自己名下的产出域 (acquire/extract/enrich/emit)。
    reset_from(stage) 会清掉指定 stage 及其下游的产出域，用于局部重跑。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    request: SkillRequest
    output_dir: Path
    deadline_ts: float
    trace_id: str
    span_id: str | None = None

    # strategy 初始由 request 派生；mutate 会向其合并 delta
    strategy: dict[str, Any] = Field(default_factory=dict)

    # Stage 产出域
    acquire: dict[str, Any] | None = None
    extract: dict[str, Any] | None = None
    enrich: dict[str, Any] | None = None
    emit: dict[str, Any] | None = None

    warnings: list[dict[str, Any]] = Field(default_factory=list)
    retry_history: list[RetryPlan] = Field(default_factory=list)
    quality_report: QualityReport | None = None

    # ------- 行为方法 -------

    @classmethod
    def new(cls, request: SkillRequest, trace_id: str, deadline_ts: float) -> Context:
        """工厂方法：根据 request 初始化 Context。"""
        return cls(
            request=request,
            output_dir=Path(request.output_dir).resolve(),
            deadline_ts=deadline_ts,
            trace_id=trace_id,
            strategy=_initial_strategy(request),
        )

    def apply(self, result: StageResult) -> None:
        """将 StageResult 写入对应 Stage 的产出域。

        不变式：只能写入同名 Stage 的域；warnings 累加到全局。
        """
        setattr(self, result.stage, result.output)
        if result.warnings:
            self.warnings.extend(result.warnings)

    def reset_from(self, stage_name: StageName) -> None:
        """清除指定 Stage 及下游产出，用于局部重跑。"""
        order: tuple[StageName, ...] = ("acquire", "extract", "enrich", "emit")
        if stage_name not in order:
            raise ValueError(f"invalid stage: {stage_name}")
        idx = order.index(stage_name)
        for s in order[idx:]:
            setattr(self, s, None)
        # 质量报告总是跟随 emit，一起清
        self.quality_report = None

    def merge_strategy(self, delta: dict[str, Any]) -> None:
        """把 delta 合并进当前 strategy（浅合并）。"""
        for k, v in delta.items():
            self.strategy[k] = v


def _initial_strategy(req: SkillRequest) -> dict[str, Any]:
    return {
        "render_mode": req.render_mode,
        "table_mode": req.table_mode,
        "formula_mode": req.formula_mode,
        "image_mode": req.image_mode,
        "extractor_profile": "adapter",
        "clean_rules": ["default"],  # 用 list 确保 JSON 可序列化
        "flags": {},
    }


# 必须在模块末尾 rebuild 以解决前向引用（SkillResponse → Event）
SkillResponse.model_rebuild()
