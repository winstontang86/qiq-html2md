# 可观测性规范（v2-final r3）

Observability 贯穿 qiq-html2md 的四层，本文件规范首版落地细节。

**首版范围**：事件流（含日志 + trace + stage 快照 sink） + 指标本地 JSON。不对接 Prometheus / OTel（预留接口）。

---

## 1. 设计目标

- 任何一次失败都能在本地完整复盘。
- 宿主实时拿到进度。
- 事件、指标、快照共享同一 `trace_id`。
- 对业务代码零侵入。

---

## 2. 组件

| 模块 | 职责 | 载体 |
|---|---|---|
| `obs/events.py` | 事件总线 + JSONL 落盘 + trace_id 生成；`stage.finished` 事件同步镜像到 `_diag/stages/<name>.json` | `_diag/events.jsonl` + `_diag/stages/*.json` + EventBus |
| `obs/metrics.py` | 指标埋点 | `_diag/metrics.json` |

> r3 砍掉了独立的 `obs/snapshot.py` 和 `obs/logger.py`、`obs/trace.py`：日志即"level≥INFO 的事件"；快照是 `stage.finished` 的 sink 产物；trace_id 由 events 统一发号。

---

## 3. 事件规范

### 3.1 Event 数据类（与 types.py 一致）

```python
@dataclass(frozen=True)
class Event:
    ts: str             # ISO8601 UTC ms
    trace_id: str
    span_id: str | None
    stage: str          # orchestrator / acquire / extract / enrich / emit / infra / obs
    seq: int
    name: str
    payload: dict       # 含 level/error 等可选字段
```

### 3.2 首版核心事件（6 种，强约束）

| 事件名 | 触发点 | payload 要点 |
|---|---|---|
| `skill.started` | 入口 | url, render_mode, debug |
| `skill.finished` | 收尾（含失败） | status, duration_ms, retries, level, error |
| `stage.started` | Stage 进入 | stage, strategy |
| `stage.finished` | Stage 退出（含异常） | stage, duration_ms, stats, level, error |
| `quality.scored` | 评分完成 | final_score, sub_scores, passed |
| `retry.planned` | 局部重试决策 | reason, target_stage, delta |

### 3.3 预留事件（非强约束）

`warning.raised` / `budget.low` / `resource.fetched` / `table.processed` 等自由使用。通过 payload 里的 `level` 字段（`debug`/`info`/`warn`/`error`）区分严重性；无 `level` 时视为 `info`。

### 3.4 宿主订阅

```python
from qiq_html2md.obs import EventBus
EventBus.subscribe(lambda evt: ...)
```

或通过 `SkillResponse.events_tail` 取**最后 20 条**即时内存快照。

---

## 4. 指标规范

写入 `_diag/metrics.json`：

```json
{
  "trace_id": "01J...",
  "status": "passed",
  "duration_ms": 182340,
  "stage_duration_ms": {
    "acquire": 25123, "extract": 12045,
    "enrich": 130220, "emit": 14952
  },
  "retry_count": 1,
  "retry_reasons": ["table_retention_low"],
  "fallback_total": {"table": 2, "formula": 1, "image": 0},
  "resource_fail_ratio": 0.04,
  "budget_left_ms": 45210,
  "budget_used_ratio": 0.92
}
```

预留接口 `obs.metrics.export_otel(endpoint)` 留空实现。

---

## 5. Stage 快照（events sink）

`_diag/stages/<stage_name>.json` 由 `obs/events.py` 在 `stage.finished` 事件触发时写出，内容是该事件的 payload 镜像：

```json
{
  "trace_id": "01J...",
  "stage": "enrich",
  "started_at": "2026-04-29T10:25:11.456Z",
  "finished_at": "2026-04-29T10:27:20.778Z",
  "duration_ms": 129322,
  "strategy_used": {...},
  "inputs_digest": {...},
  "outputs_digest": {...},
  "warnings_in_stage": 2,
  "retry_attempt": 0,
  "level": "info",
  "error": null
}
```

`debug=full` 时附加 `context_slice`（脱敏 + 截断到 8KB）。

---

## 6. Trace / Span

- `trace_id`：ULID，入口生成，贯穿所有信号。
- `span_id`：Stage 进入时生成；Enrich 子任务可嵌套一层。
- 所有错误消息强制携带 `trace_id`。

---

## 7. `_diag/` 目录

```text
<output_dir>/_diag/
  TRACE.md                  # 人类可读时间轴（自动生成）
  events.jsonl              # 事件流（含日志）
  metrics.json              # 指标快照
  stages/
    acquire.json            # stage.finished payload 镜像
    extract.json
    enrich.json
    emit.json
  retries/
    retry-01.plan.json      # RetryPlan 序列化
  raw/                      # 仅 debug=full 或 preserve_intermediate=true
    page.html
    rendered.html
```

### TRACE.md 样例

```markdown
# Trace 01J... html2md run

- 10:25:00.123  skill.started    url=https://arxiv.org/html/2501.xxx
- 10:25:00.456  stage.started    acquire
- 10:25:20.911  stage.finished   acquire duration=20.5s
- 10:25:21.020  stage.started    extract
- ...
- 10:28:03.008  quality.scored   final_score=72 passed=false
- 10:28:03.010  retry.planned    reason=table_retention_low target=enrich
- ...
- 10:31:58.400  skill.finished   status=passed retries=1
```

---

## 8. 调试开关

- `debug: "lite"`（默认）
  - 事件：6 种核心。
  - 快照：outputs_digest。
  - raw 文件：不保留。
- `debug: "full"`
  - 事件：核心 + 预留自由使用。
  - 快照：附加 context_slice。
  - raw 文件：保留。
  - 失败或 `status=degraded` 时自动升级。

---

## 9. 脱敏与体积控制

- payload 单字段 >2KB 自动截断并加 `_truncated: true`。
- URL query 中匹配 `token|key|sig|password` 的参数值替换为 `***`。
- `raw/page.html` >10MB 只保留前 2MB + sha256。
- `_diag/` 总量 >50MB 时记一条 `warning.raised` 并裁剪最旧 snapshot。

---

## 10. 实施建议

- 阶段一：events（6 种核心） + trace_id + 基础 `_diag/`。
- 阶段二：stage.finished 的 outputs_digest 完整。
- 阶段三：metrics.json + retry 计划落盘。
- 阶段四：TRACE.md 自动生成 + debug=full 完整复盘。
- 阶段五：OTel/Prometheus 接口实现。
