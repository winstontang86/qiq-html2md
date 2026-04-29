# HTML 页面转 Markdown Skill 架构设计（v2-final, r3 · 精简版）

本设计面向 WorkBuddy、OpenClaw 等智能体宿主复用的**通用 skill**。r3 在 r2 基础上按"中小型 skill"标准砍掉过度工程：17 个核心文件、3 种异常、6 种核心事件、无独立 FSM / Cache / Browser Pool。

---

## 1. 架构目标

- 面向论文类 HTML 页面到 Markdown 的高保真转换。
- 以**通用 skill** 形态接入多种智能体宿主，契约与宿主解耦。
- 页面获取支持静态优先 + 浏览器 fallback。
- 正文、图、表、公式、脚注、参考文献尽量保真。
- 具备质量检查闭环，失败时按原因做**局部重试**。
- 全流程受 10 分钟全局 deadline 控制。
- 提供**结构化可观测能力**，异常可在本地完整复盘。

---

## 2. 设计原则

1. **契约先行**：`SKILL.md` + `manifest.yaml` + JSON Schema，宿主只认 JSON。
2. **四层极简**：Skill 契约 / 编排 / 管线 / 基础设施；可观测横切。
3. **线性管线**：Acquire → Extract → Enrich → Emit，Enrich 内部可并发（只读 DOM）。
4. **局部重试**：失败按原因映射到最近可恢复的 Stage，只重跑受影响部分。
5. **单一数据来源**：7 个核心数据类集中在 `core/types.py`。
6. **够用就好**：不为可能的需求预建基础设施（Cache / Browser Pool / FSM 类）。
7. **站点适配器是数据**：仅 selectors / cleaners / hints。
8. **可观测是一等公民**：事件总线 + 指标 + Stage 快照。

---

## 3. 总体分层

```text
┌──────────────────────────────────────────┐
│  L1  Skill 契约层                         │
│  SKILL.md · manifest.yaml · JSON Schema   │
├──────────────────────────────────────────┤
│  L2  编排层                               │
│  types · pipeline · context · budget ·    │
│  errors                                   │
├──────────────────────────────────────────┤
│  L3  管线层（四 Stage 顺序执行）           │
│  Acquire → Extract → Enrich → Emit        │
├──────────────────────────────────────────┤
│  L4  基础设施层                            │
│  http · fs_sandbox                        │
└──────────────────────────────────────────┘
        ▲
        │（横切）
   ┌────┴───────────────┐
   │  Observability      │
   │  events · metrics   │
   └────────────────────┘
```

PlantUML 图详见 `/Users/winstontang/code/github/qiq-html2md/docs/architecture_diagram.puml`。

---

## 4. 处理主线

1. 宿主通过契约入口发起 `SkillRequest`。
2. 编排层创建 `Context`（含 `trace_id`）、初始化 `Budget`。
3. 顺序执行四个 Stage：Acquire → Extract → Enrich → Emit。
4. Emit 内的质量检查通过 → 返回 `SkillResponse(status=passed)`。
5. 不通过 → `pipeline.plan_retry()` 生成 `RetryPlan`；对目标 Stage 执行 `mutate()` 并从该 Stage 起局部重跑。
6. 达到 `max_retry` 或预算耗尽 → 返回 `SkillResponse(status=degraded)`。
7. 全程 Observability 层记录事件、指标与快照。

---

## 5. 实现契约表（Single Source of Truth）

本节是**工程师 agent 的代码生成蓝本**。共 **7 个核心数据类**，统一定义于 `core/types.py`。Stage 内部产出允许使用 dict 或轻量 dataclass 自行组织，不做硬约束。

### 5.1 `SkillRequest`

| 字段 | 类型 | 默认 | 必填 | 说明 |
|---|---|---|---|---|
| `url` | str | —— | ✓ | http/https |
| `output_dir` | str | `./output` |  | 输出根目录（fs_sandbox 约束） |
| `timeout_seconds` | int | 600 |  | 全局 deadline，上限 600 |
| `render_mode` | enum(`auto`/`static`/`browser`) | `auto` |  |  |
| `table_mode` | enum(`auto`/`markdown`/`html`/`image`) | `auto` |  |  |
| `formula_mode` | enum(`auto`/`latex`/`mathml`/`image`) | `auto` |  |  |
| `image_mode` | enum(`download`/`link`/`both`) | `download` |  |  |
| `quality_check` | bool | true |  |  |
| `max_retry` | int | 2 |  |  |
| `include_references` | bool | true |  |  |
| `include_metadata` | bool | true |  |  |
| `debug` | enum(`lite`/`full`) | `lite` |  |  |
| `preserve_intermediate` | bool | false |  |  |
| `idempotency_key` | str\|None | None |  |  |

### 5.2 `SkillResponse`

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | enum(`passed`/`degraded`/`failed`) | 终态 |
| `trace_id` | str | ULID |
| `artifact.markdown_path` | str\|None | failed 时为 None |
| `artifact.assets_dir` | str\|None |  |
| `metadata_path` | str\|None |  |
| `quality_report_path` | str\|None |  |
| `warnings_path` | str | 始终存在 |
| `diag_dir` | str | `_diag/` |
| `stats.duration_ms` | int |  |
| `stats.retries` | int |  |
| `risk_level` | enum(`low`/`medium`/`high`) |  |
| `events_tail` | list\[Event\] | events.jsonl 最后 20 条快照 |

### 5.3 `Context`

```python
class Context:
    request: SkillRequest
    output_dir: Path
    deadline_ts: float             # monotonic
    trace_id: str
    span_id: str | None

    strategy: dict                 # 当前策略（由 Stage 读写；mutate 会生成新字典）

    # Stage 产出域（只由对应 Stage 写入）
    acquire: dict | None
    extract: dict | None
    enrich:  dict | None
    emit:    dict | None

    warnings: list[dict]
    retry_history: list[RetryPlan]
    quality_report: QualityReport | None

    def apply(self, result: StageResult) -> None: ...
    def reset_from(self, stage_name: str) -> None: ...
```

**不变式**：`apply(StageResult)` 只能写入同名 Stage 域；`reset_from(stage)` 清除该 Stage 及下游产出，用于局部重跑。

**`strategy` 字典建议字段**（非强制）：`render_mode / table_mode / formula_mode / image_mode / extractor_profile / clean_rules / flags`。

### 5.4 `StageResult`

```python
@dataclass(frozen=True)
class StageResult:
    stage: Literal['acquire','extract','enrich','emit']
    output: dict                   # 进入 ctx.<stage>
    warnings: list[dict] = ()
    duration_ms: int = 0
```

### 5.5 `Stage` 接口

```python
class Stage(Protocol):
    name: Literal['acquire','extract','enrich','emit']
    def run(self, ctx: Context) -> StageResult: ...
    def mutate(self, delta: dict) -> "Stage": ...   # 返回新实例
```

`delta` 直接合并进 `ctx.strategy`。

### 5.6 `Budget`

```python
class Budget:
    def __init__(self, total_seconds: int): ...
    def reserve(self, stage: str, seconds: int) -> None: ...
    def checkout(self, stage: str) -> ContextManager[None]: ...   # with 块计时
    def left_for(self, stage: str) -> float: ...
    def global_left(self) -> float: ...
    def can_retry(self, extra_seconds: int) -> bool: ...
    def release_unused(self, stage: str) -> None: ...
```

**默认预算分配**（总 600s）：

| 阶段 | 预留 | 备注 |
|---|---:|---|
| Acquire | 140s | 静态 20s + 浏览器 120s |
| Extract | 30s |  |
| Enrich | 180s | 资源下载 + 截图 |
| Emit | 40s | Markdown + Quality |
| 重试储备 | 190s |  |
| 预留 | 20s |  |

**归还规则**：Stage `checkout` 退出时，`reserved - actual` 回流全局池。

### 5.7 `RetryPlan`

```python
@dataclass(frozen=True)
class RetryPlan:
    reason: str
    target_stage: Literal['acquire','extract','enrich','emit']
    delta: dict                    # 合并进 strategy
    budget_seconds: int
```

### 5.8 `QualityReport`

```python
@dataclass
class QualityReport:
    passed: bool
    final_score: float
    sub_scores: dict               # text/structure/image/table/formula/ref
    critical_failures: list[str]
    failed_rules: list[str]
    risk_level: str                # low / medium / high
```

### 5.9 `Event`

```python
@dataclass(frozen=True)
class Event:
    ts: str                        # ISO8601 UTC ms
    trace_id: str
    span_id: str | None
    stage: str
    seq: int
    name: str
    payload: dict                  # 含 level/error 等可选字段
```

---

## 6. L1 Skill 契约层

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 智能体发现入口 |
| `manifest.yaml` | 机读声明 |
| `schemas/request.schema.json` | 由 `SkillRequest` 派生 |
| `schemas/response.schema.json` | 由 `SkillResponse` 派生 |

详见 `skill_contract.md`。

---

## 7. L2 编排层

### 7.1 `core/pipeline.py`

线性 Stage 执行器 + **内置 `plan_retry()` 函数**（无独立 FSM 类）。主循环见 §10。

```python
def plan_retry(report: QualityReport, budget: Budget,
               attempts: int, max_retry: int) -> RetryPlan | None:
    if attempts >= max_retry or not budget.can_retry(30):
        return None
    # 按 §9.3 表映射 failure → (target_stage, delta, budget_seconds)
    ...
```

### 7.2 `core/context.py`
实现 §5.3。强制分域写入。

### 7.3 `core/budget.py`
实现 §5.6。

### 7.4 `core/errors.py`（精简为 3 种）

```python
class SkillError(Exception): ...          # 顶层

class RetryableError(SkillError): ...     # 允许进入 plan_retry
class FatalError(SkillError): ...         # 直接 degraded 或 failed
```

处理原则：
- Stage 抛 `RetryableError(reason, ...)` → 交 `plan_retry` 决策。
- Stage 或基础设施抛 `FatalError` → 立即 `degraded`（有部分产出）或 `failed`（无产出）。
- 所有异常冒泡前记录 `stage.finished(error=..., level='error')` 事件。

> 入参 schema 校验失败直接抛 `FatalError("schema_invalid")`，不再独立异常类型。

---

## 8. L3 管线层（四 Stage）

### 8.1 Acquire — `stages/acquire.py`

单文件内聚：fetcher + renderer + adapter resolver。

Stage 产出（进入 `ctx.acquire`，dict 建议字段）：
`final_url / raw_html / rendered_html / adapter_name / render_mode_used / page_stats`。

浏览器启动：首版每任务启一次 `sync_playwright()`，无独立 Pool。批量场景在阶段五再优化。

### 8.2 Extract — `stages/extract.py`

单文件内聚：extractor + normalizer + metadata。

产出（`ctx.extract`）：`dom / main_node_id / metadata / extract_stats`。

### 8.3 Enrich — `stages/enrich.py`

单文件内聚：assets + tables + formulas + references。

**并发约束（强制）**：
- 四个子任务**只读 DOM**。
- 各自产出独立 artifact 列表。
- Emit 统一组装。

产出（`ctx.enrich`）：`images / tables / formulas / refs / enrich_stats`。

### 8.4 Emit — `stages/emit.py`

单文件内聚：markdown_writer + packager + quality（调用 `quality.py`）。

产出（`ctx.emit`）：`markdown_path / assets_dir / metadata_path / warnings_path / quality_report`。

质量检查失败时抛 `RetryableError(reason=quality_report.failed_rules[0], ...)`。

---

## 9. 质量检查与重试

### 9.1 `quality.py`（单文件）

包含：六条评分规则（text / structure / image / table / formula / ref） + 加权打分 + 通过判定。

### 9.2 评分模型

```text
final_score =
  text_score        * 0.30 +
  structure_score   * 0.15 +
  image_score       * 0.15 +
  table_score       * 0.15 +
  formula_score     * 0.15 +
  link_reference_score * 0.10
```

通过条件：`final_score >= 80 AND len(critical_failures) == 0`。

### 9.3 风险分级

| risk_level | 条件 |
|---|---|
| low | final_score ≥ 80 |
| medium | 60 ≤ final_score < 80 |
| high | final_score < 60 或存在 critical_failures |

### 9.4 失败原因 → 局部重试目标

| 失败原因 | 回到 Stage | 关键策略突变 |
|---|---|---|
| `text_too_short` | Acquire | `render_mode=browser` + 换 extractor_profile |
| `heading_retention_low` | Extract | 放宽 clean_rules + `flags.fix_headings=true` |
| `reference_missing` | Extract | 关 refs 清理 + 启 refs selector |
| `image_retention_low` | Enrich | `flags.scroll_load=true` + 解析 srcset/data-src |
| `missing_local_resource` | Enrich | 重下载 + 修正引用 |
| `table_retention_low` | Enrich | `table_mode=html/image` + HTML 备份 |
| `complex_table_damaged` | Enrich | `table_mode=image` |
| `formula_retention_low` | Acquire | 强制浏览器渲染 + MathJax/KaTeX 提取 |
| `formula_image_missing` | Enrich | 重截图 + 保留 MathML/原始 HTML |
| `markdown_structure_invalid` | Emit | 结构修复 + 重生成 |

### 9.5 降级路径

| 内容 | 首选 | 二级 | 最终 |
|---|---|---|---|
| 正文 | 站点 Adapter | 密度算法 | `body` 清洗 |
| 图片 | 本地下载 | 远程链接 | 记录缺失 |
| SVG | 保留 SVG | 转 PNG | 远程链接 |
| 简单表格 | Markdown | HTML | 图片 |
| 复杂表格 | HTML | 图片 | 原始 HTML 附件 |
| 公式 | LaTeX | MathML | 截图 → 原始 HTML |
| 质量失败 | 按原因局部重试 | 输出最佳结果 | 标记 high |

---

## 10. 主循环伪代码

```python
def run(request: SkillRequest) -> SkillResponse:
    ctx = Context.new(request, trace_id=ulid())
    obs.bind(ctx)

    budget = Budget(request.timeout_seconds)
    for s, secs in DEFAULT_BUDGET.items():
        budget.reserve(s, secs)

    stages: list[Stage] = [AcquireStage(), ExtractStage(), EnrichStage(), EmitStage()]
    cursor, attempts = 0, 0

    try:
        while True:
            for i in range(cursor, len(stages)):
                if not budget.can_retry(1):
                    raise FatalError("budget_exhausted")
                with budget.checkout(stages[i].name), obs.span(stages[i].name):
                    result = stages[i].run(ctx)
                ctx.apply(result)
                budget.release_unused(stages[i].name)

            q = ctx.quality_report
            if q.passed:
                return SkillResponse.build(ctx, status='passed')

            plan = plan_retry(q, budget, attempts, request.max_retry)
            if plan is None:
                return SkillResponse.build(ctx, status='degraded')

            obs.emit('retry.planned', plan.__dict__)
            stages[plan.target_idx] = stages[plan.target_idx].mutate(plan.delta)
            ctx.reset_from(stages[plan.target_idx].name)
            cursor = plan.target_idx
            attempts += 1

    except FatalError as e:
        status = 'degraded' if ctx.emit else 'failed'
        return SkillResponse.build(ctx, status=status)
    except RetryableError as e:
        return SkillResponse.build(ctx, status='degraded')
    except Exception as e:
        obs.emit('skill.finished', {'level':'error','error':repr(e)})
        return SkillResponse.build(ctx, status='failed')
    finally:
        obs.finalize(ctx)
```

---

## 11. Observability 横切层

**首版最小集**：事件流（含日志+trace+快照） + 指标本地 JSON。

### 11.1 组件

| 模块 | 职责 |
|---|---|
| `obs/events.py` | 事件总线 + JSONL 落盘 + trace_id 生成 + `stage.finished` 事件同步写 `_diag/stages/<name>.json` 快照 |
| `obs/metrics.py` | 指标埋点，写 `metrics.json` |

### 11.2 首版核心事件清单（6 种，强约束）

| 事件名 | 触发点 | payload 要点 |
|---|---|---|
| `skill.started` | 入口 | url, render_mode, debug |
| `skill.finished` | 收尾（含失败） | status, duration_ms, retries, level, error |
| `stage.started` | Stage 进入 | stage, strategy |
| `stage.finished` | Stage 退出（含异常） | stage, duration_ms, stats, level, error |
| `quality.scored` | 评分完成 | final_score, sub_scores, passed |
| `retry.planned` | 局部重试决策 | reason, target_stage, delta |

> `warning.raised` / `budget.low` / `cache.hit` 等作为预留事件，payload 里用 `level='warn'` 和特定 `name` 自行记录，不纳入首版强约束。

### 11.3 `_diag/` 诊断包

```text
<output_dir>/_diag/
  TRACE.md                 # 人类可读时间轴
  events.jsonl             # 事件流（含日志）
  metrics.json             # 指标快照
  stages/
    acquire.json           # stage.finished 的 payload 镜像
    extract.json
    enrich.json
    emit.json
  retries/
    retry-01.plan.json
  raw/                     # 仅 debug=full 或 preserve_intermediate
    page.html
    rendered.html
```

### 11.4 debug 模式

- `lite`（默认）：6 种核心事件 + 基础快照。
- `full`：附加 `context_slice` 到 stage 快照；保留 raw 文件；失败/degraded 时自动升级。

### 11.5 `events_tail` 语义

`SkillResponse.events_tail` = `events.jsonl` 的**最后 20 条**的即时内存快照，不重复持久化。

详细规范见 `observability.md`。

---

## 12. 站点适配器（策略数据）

`adapters_site/` 仅含**数据**，单文件收敛 registry。

```python
# adapters_site/base.py
@dataclass(frozen=True)
class SiteAdapter:
    name: str
    match: Callable[[str], bool]
    main_selector: str | None
    refs_selector: str | None
    cleaners: list[str]
    hints: dict

DEFAULT = SiteAdapter(name='generic', match=lambda _: True, ...)

# 注册表（按顺序匹配，DEFAULT 兜底）
from .arxiv import ARXIV
from .pmc   import PMC
from .jats  import JATS
REGISTRY: list[SiteAdapter] = [ARXIV, PMC, JATS, DEFAULT]

def resolve(url: str) -> SiteAdapter:
    for a in REGISTRY:
        if a.match(url): return a
    return DEFAULT
```

具体站点：`arxiv.py` / `pmc.py` / `jats.py`，各导出一个 `SiteAdapter` 实例。

---

## 13. L4 基础设施层

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `infra/http.py` | 连接复用 + SSRF 护栏 + ETag + 大小限制 | `http.get(url) -> HttpResponse` |
| `infra/fs_sandbox.py` | 统一写操作；禁 `..`、符号链接 | `fs.write(relpath, data)` |
| `infra/browser.py` | Playwright 驱动抽象（L1 强制依赖） | `get_driver().render(url)` / `screenshot_nodes(html, selectors)` |
| `infra/browser_pool.py` | Chromium 进程单例与 context 复用 | `get_pool()` / `reset_pool()` |
| `infra/cache.py` | HTTP 级 + 抽取级两级缓存 | `HttpCacheEntry` / `make_extract_key` |
| `infra/preflight.py` | 运行时依赖**只读预检**（Playwright 包 + Chromium 二进制） | `check_runtime_deps() -> PreflightReport` / `format_install_hints(report)` |

SSRF 黑名单：`127.0.0.0/8 · 10.0.0.0/8 · 172.16.0.0/12 · 192.168.0.0/16 · 169.254.0.0/16 · ::1/128 · fc00::/7`。

**依赖与 preflight 契约（v0.3.0）**：

- **所有依赖均为 L1 强制依赖**（httpx / lxml / bs4 / readability-lxml / pydantic / python-ulid / **playwright**）——由 `pyproject.toml.dependencies` 声明；Python 包缺失会在 import 时报错。
- **Chromium 二进制**是 pip 无法自动安装的外部依赖，需要 `playwright install chromium` 单独执行；preflight 负责探测其就位状态。
- **preflight 是只读的**：不启动浏览器、不修改任何全局状态，只做包 import 与可执行文件 `Path.exists()` 检查。
- CLI **默认 strict**：缺失任一依赖直接退出码 2，不启动 pipeline；`--check-deps` 仅跑预检后退出；`--skip-deps-check` 供 CI 场景跳过预检。

> **r3 的"砍 cache/browser_pool"说明仅适用于 MVP 阶段；阶段五之后这两个模块已回归**，preflight 为阶段六新增，v0.3.0 升级为强制依赖预检。

---

## 14. 目录结构（r3 精简后）

```text
qiq_html2md/
  SKILL.md
  manifest.yaml
  schemas/
    request.schema.json
    response.schema.json

  core/                        # L2 编排 + 类型 + 异常
    types.py                   # 7 个核心数据类
    pipeline.py                # 主循环 + plan_retry()
    context.py
    budget.py
    errors.py                  # 3 种异常

  stages/                      # L3 四 Stage
    acquire.py
    extract.py
    enrich.py
    emit.py

  adapters_site/               # 站点策略（纯数据）
    base.py                    # SiteAdapter + DEFAULT + REGISTRY + resolve
    arxiv.py
    pmc.py
    jats.py

  quality.py                   # 规则 + 评分合一（单文件）

  infra/                       # L4 基础设施
    http.py                    # SSRF + ETag
    fs_sandbox.py              # 路径穿越防护
    browser.py                 # Playwright 驱动抽象（L1 强制）
    browser_pool.py            # Chromium 进程单例
    cache.py                   # HTTP + 抽取两级缓存
    preflight.py               # L1 依赖只读预检

  obs/                         # 可观测横切层
    events.py                  # 总线 + 落盘 + trace + 快照 sink
    metrics.py

  tests/
    fixtures/
    e2e/
```

核心文件数：**17**（相比 r2 的 24 再精简 7 个）。

---

## 15. 实施阶段

### 阶段一（骨架 MVP）
范围：
- `core/types.py` 按 §5 完整定义 7 个数据类。
- `core/{pipeline,context,budget,errors}.py` 可运行骨架。
- `stages/acquire.py`（static fetch） + `stages/extract.py`（密度算法） + `stages/emit.py`（基础 Markdown + 简单质量）。
- `stages/enrich.py` 仅图片下载子任务（其他返回空）。
- `adapters_site/base.py` + 一个 arxiv 占位。
- `quality.py` 只实现 `text_too_short / markdown_structure_invalid`。
- `obs/events.py` 最小可用（6 种核心事件）。
- `infra/http.py` + `infra/fs_sandbox.py`。
- 契约三件套 + JSON Schema。

MVP 交付验收：一个静态 arXiv HTML 页面能输出 `article.md` + `_diag/events.jsonl`。

### 阶段二（论文保真）
- figure/figcaption；参考文献保留。
- 表格三级策略；公式 LaTeX/MathML 提取。

### 阶段三（质量闭环）
- 六条质量规则齐全。
- `plan_retry()` 完整失败→Stage 映射，局部重跑 + 策略突变。

### 阶段四（高保真降级）
- 表格/公式节点截图；SVG 与懒加载。
- 站点 Adapter：arxiv / pmc / jats 完整规则。

### 阶段五（工程化）
- 视需求加回 `infra/cache.py` 与 `infra/browser_pool.py`。
- `_diag/` 完整落地 + TRACE.md 自动生成。
- `metrics.json` 完整 + OTel 预留接口实现。
- 压测与稳定性打磨。

---

## 16. r2 → r3 精简清单

| 项 | r2 | r3 |
|---|---|---|
| 核心文件数 | 24 | **17** |
| 异常类型 | 8 种（含 4 Stage 级、Schema） | **3 种**（Skill/Retryable/Fatal） |
| Retry-FSM | 独立 `retry_fsm.py` 类 | **砍掉**，变 `pipeline.plan_retry()` 函数 |
| `infra/cache.py` | 有 | **砍掉**（阶段五加回） |
| `infra/browser_pool.py` | 有 | **砍掉**（阶段五加回） |
| `adapters_site/registry.py` | 独立 | **并入** `base.py` |
| `quality/` 目录 | `rules.py + checker.py` | **合一**为 `quality.py` 单文件 |
| `obs/snapshot.py` | 独立 | **并入** `events.py`（sink 机制） |
| `obs/` 文件数 | 3 | **2** |
| 事件清单 | 10 核心 | **6 核心** |
| 数据类 | 7（字段细粒度 dataclass） | **7**（Stage 输出可用 dict） |

---

## 17. 架构结论

r3 以"中小型 skill 够用就好"为原则，把 r2 中"为大型项目预建"的东西全部砍掉：

- Retry-FSM 类 → 函数；
- Cache / Browser Pool → 阶段五再加；
- 8 种异常 → 3 种；
- 10 事件 → 6 事件；
- obs/quality/adapters 进一步合文件。

核心价值完整保留（契约化 / 分层 / Stage / 局部重试 / 可观测 / 沙盒 / SSRF），工程师 agent 可以用最小负担按 §5 + §15 开工。
