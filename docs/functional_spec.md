# HTML 页面转 Markdown Skill 功能说明（v2-final r3）

> 本功能规范与 v2-final r3 精简版架构（见 `/Users/winstontang/code/github/qiq-html2md/docs/architecture_design.md`）配套。Skill 契约见 `skill_contract.md`，可观测性规范见 `observability.md`。核心数据类型的权威定义在架构文档 §5 与 `core/types.py`。

## 1. 功能目标

本 skill 用于将输入 URL 对应的 HTML 页面转换为 Markdown 文件，主要面向论文类材料的预处理场景，作为**通用 skill** 服务于 WorkBuddy / OpenClaw / CLI / MCP 等智能体宿主。

核心目标：

- 输入一个 URL，自动获取页面 HTML 内容。
- 尽量完整保留论文正文、标题、摘要、图、图注、表格、公式、脚注和参考文献。
- 输出 Markdown 主文件和本地资源目录。
- 不依赖外部 API 或大模型，尽量使用本地处理能力完成转换。
- 单个 URL 的处理总耗时不超过 10 分钟。
- 内置质量检查模块，检查不通过时按失败原因自动**局部重试**。
- 提供结构化可观测能力，异常可通过 `_diag/` 诊断包完整复盘。

## 2. 适用场景

适合处理：

- 论文 HTML 页面。
- 技术报告页面。
- 学术机构发布的在线文章。
- 包含图表、公式、参考文献的长文档。
- 需要进入 Markdown 处理链路的网页材料。

不优先支持：

- 需要登录或强交互才能访问的页面。
- 主要内容由图片扫描件构成的页面。
- 反爬限制极强且无法本地正常访问的页面。
- 内容需要外部 API 或大模型理解才能还原的页面。

## 3. 输入参数

完整契约见 `/Users/winstontang/code/github/qiq-html2md/docs/skill_contract.md`。入口参数（SkillRequest）：

```json
{
  "url": "https://example.com/paper.html",
  "output_dir": "./output",
  "timeout_seconds": 600,
  "render_mode": "auto",
  "table_mode": "auto",
  "formula_mode": "auto",
  "image_mode": "download",
  "quality_check": true,
  "max_retry": 2,
  "include_references": true,
  "include_metadata": true,
  "debug": "lite",
  "preserve_intermediate": false,
  "idempotency_key": null
}
```

参数说明：

- `url`：必填，待处理页面 URL。
- `output_dir`：输出目录，受 fs_sandbox 约束。
- `timeout_seconds`：整体超时时间，默认 600 秒，上限 600 秒。
- `render_mode`：页面获取模式，可选 `auto`、`static`、`browser`。
- `table_mode`：表格处理模式，可选 `auto`、`markdown`、`html`、`image`。
- `formula_mode`：公式处理模式，可选 `auto`、`latex`、`mathml`、`image`。
- `image_mode`：图片处理模式，可选 `download`、`link`、`both`。
- `quality_check`：是否启用质量检查闭环。
- `max_retry`：质量检查不通过后的最大重试次数，默认 2。
- `include_references`：是否保留参考文献。
- `include_metadata`：是否输出元信息。
- `debug`：调试模式，`lite`（默认）或 `full`。
- `preserve_intermediate`：是否保留 raw / rendered HTML 等中间产物。
- `idempotency_key`：幂等键，命中缓存可直接返回已有结果。

## 4. 输出结果

默认输出目录结构：

```text
output/
  article.md
  metadata.json
  quality_report.json
  warnings.json
  assets/
    images/
      fig-001.png
      fig-002.jpg
    tables/
      table-001.png
      table-002.html
    formulas/
      formula-001.png
  _diag/
    TRACE.md
    run.log.jsonl
    events.jsonl
    metrics.json
    stages/
      acquire.snapshot.json
      extract.snapshot.json
      enrich.snapshot.json
      emit.snapshot.json
    retries/
      retry-01.plan.json
    raw/                      # 仅在 debug=full 或 preserve_intermediate=true 时保留
      page.html
      rendered.html
```

文件说明：

- `article.md`：最终 Markdown 文件。
- `metadata.json`：页面标题、作者、来源 URL、处理耗时、渲染模式、资源统计等信息。
- `quality_report.json`：质量检查结果、评分、失败原因、重试记录。
- `warnings.json`：资源下载失败、公式降级、表格降级等非致命问题。
- `assets/`：本地资源目录（图片、表格截图/HTML、公式截图）。
- `_diag/`：可观测诊断包，异常复盘用。详细字段见 `observability.md`。

## 5. 功能范围

### 5.1 页面获取

- 优先使用静态 HTTP 抓取获取 HTML。
- 静态内容不足时，自动使用 Headless Chromium 渲染页面。
- 支持重定向、编码识别、压缩响应处理。
- 支持保存原始 HTML 和渲染后 HTML。

### 5.2 正文抽取

- 支持基于站点适配器的正文抽取。
- 支持 `article`、`main`、`[role=main]` 等语义标签识别。
- 支持正文密度算法兜底。
- 必要时 fallback 到清洗后的 `body`。

### 5.3 Markdown 生成

- 支持标题、段落、列表、引用、代码块、链接、图片、表格、公式转换。
- 支持嵌套列表和代码块围栏处理。
- 支持 HTML fallback，避免复杂结构在强转 Markdown 时失真。

### 5.4 图片处理

- 支持下载普通图片。
- 支持解析 `srcset`、`picture`、`data-src` 等懒加载图片信息。
- 支持处理 base64 图片。
- 支持 SVG 保留或转 PNG。
- 图片下载失败时，可降级为远程链接并记录 warning。

### 5.5 表格处理

表格采用三级策略：

1. 简单表格转换为 Markdown 表格。
2. 中等复杂表格保留为 HTML 表格。
3. 高复杂表格转成图片，并保存原始 HTML 备份。

表格处理需要尽量保留：

- 表格标题。
- 表格编号。
- 表头。
- 单元格内容。
- 表格脚注。
- 表格中的公式和图片。

### 5.6 公式处理

公式采用多级保真策略：

1. 优先提取 LaTeX。
2. 无法提取 LaTeX 时保留 MathML。
3. 无法可靠保留源码时，对公式节点截图。
4. 截图失败时保留原始 HTML。

支持来源包括：

- MathJax。
- KaTeX。
- MathML。
- SVG 公式。
- 图片公式。
- HTML/CSS 排版公式。

### 5.7 元信息提取

尽量提取：

- 页面标题。
- 作者。
- 发布时间或更新时间。
- DOI。
- 来源 URL。
- 站点名称。
- 摘要。
- 关键词。

## 6. 质量检查功能

质量检查用于判断候选 Markdown 是否达到可接受质量，并决定是否需要重新处理。

### 6.1 检查输入

质量检查模块接收：

- 候选 Markdown 内容。
- 抽取前后的 DOM 统计。
- 原始页面统计。
- 元信息。
- 图片、表格、公式处理结果。
- warnings 列表。
- 当前处理策略和重试次数。
- 剩余时间预算。

### 6.2 检查维度

质量检查覆盖：

- 基础文件检查。
- 正文完整性检查。
- 图片完整性检查。
- 表格完整性检查。
- 公式完整性检查。
- 链接和引用检查。
- Markdown 结构检查。

### 6.3 建议通过标准

```text
final_score >= 80
critical_failures == 0
```

建议关键阈值：

```text
markdown_text_length >= original_main_text_length * 0.75
heading_retention_ratio >= 0.70
paragraph_retention_ratio >= 0.65
image_retention_ratio >= 0.80
table_retention_ratio >= 0.90
formula_retention_ratio >= 0.85
```

关键失败项包括：

- Markdown 为空。
- 正文长度严重不足。
- 本地资源引用大量缺失。
- 表格或公式处理模块异常退出。
- 超过关键资源失败阈值。

## 7. 自动重试功能

质量检查不通过时，系统不会无差别整管线重跑，而是根据失败原因生成**局部重试计划**（Retry-FSM 驱动），从目标 Stage 起重跑。

重试原则：

- 根据失败原因映射到最近可恢复的 Stage。
- 每次重试必须改变至少一个关键策略（`mutate()` 注入）。
- 默认最多重试 2 次。
- 重试受整体 10 分钟 deadline 限制。
- 如果剩余时间不足，则不再重试，输出当前最佳结果和质量风险（`status=degraded`）。

失败原因 / 回到的 Stage / 重试策略：

| 失败原因 | 回到 Stage | 重试策略 |
|---|---|---|
| `text_too_short` | Acquire | 强制 `render_mode=browser`；更换正文抽取策略；fallback 到 `body` 清洗 |
| `heading_retention_low` | Extract | 放宽清洗规则；启用标题修复规则 |
| `reference_missing` | Extract | 禁用参考文献清理；使用参考文献 selector 重新抽取 |
| `image_retention_low` | Enrich | 使用浏览器渲染并滚动；解析 `srcset` / `data-src`；失败时保留远程链接 |
| `missing_local_resource` | Enrich | 重新下载缺失资源；修正 Markdown 引用路径 |
| `table_retention_low` | Enrich | 切换 `table_mode=html` 或 `table_mode=image`；保存原始 HTML 备份 |
| `complex_table_damaged` | Enrich | 改为 HTML 或图片输出 |
| `formula_retention_low` | Acquire | 强制浏览器渲染；启用 MathJax/KaTeX 提取；失败时截图 |
| `formula_image_missing` | Enrich | 重新截图；失败后保留 MathML 或原始 HTML |
| `markdown_structure_invalid` | Emit | 启用结构修复；重新生成 Markdown |

## 8. 性能要求

单个 URL 处理总耗时不超过 10 分钟。

建议时间预算：

| 阶段 | 默认预算 |
|---|---:|
| 静态抓取 | 20 秒 |
| 浏览器渲染 | 120 秒 |
| 资源下载 | 180 秒 |
| 表格和公式截图 | 180 秒 |
| Markdown 生成 | 40 秒 |
| 质量检查与重试决策 | 20 秒 |
| 预留 | 40 秒 |

性能策略：

- 所有模块共享同一个 deadline。
- 资源下载应支持并发。
- 剩余时间不足时，禁用昂贵 fallback。
- 超时前输出当前最佳结果和质量风险。
- 少量非关键资源失败不应导致整体失败。

## 9. 安全要求

URL 安全：

- 只允许 `http` 和 `https`。
- 禁止访问 localhost。
- 禁止访问私有网段。
- 限制重定向次数。
- 限制响应大小。

文件安全：

- 输出文件名必须 sanitize。
- 禁止路径穿越。
- 所有资源必须写入 `output_dir` 内。

浏览器安全：

- 使用独立 browser context。
- 禁用不必要权限。
- 限制页面执行时间。
- 禁止自动下载任意文件。

## 10. 验收标准

### 10.1 功能验收

- 输入 URL 后可以生成 `article.md`。
- 正文、图片、表格、公式、参考文献尽量完整保留。
- 本地资源路径可用。
- 生成 `metadata.json`、`warnings.json`、`quality_report.json`。
- 质量检查不通过时能够自动重新处理。

### 10.2 质量验收

- 普通论文页面 Markdown 正文长度不少于原正文的 75%。
- 图片保留比例不低于 80%。
- 表格保留比例不低于 90%。
- 公式保留比例不低于 85%。
- 复杂表格无法准确转 Markdown 时，有 HTML 或图片 fallback。
- 无法可靠保留的公式，有截图或原始 HTML fallback。

### 10.3 性能验收

- 单个页面处理总耗时不超过 10 分钟。
- 对少量资源失败不整体失败。
- 超时前能输出当前最佳结果和质量风险。

### 10.4 可观测验收

- 每次运行在 `output_dir/_diag/` 下产生日志、事件流、指标与 stage 快照。
- 所有日志、事件、指标、快照共享同一 `trace_id`。
- 失败或 `status=degraded` 时自动升级到 `debug=full` 并生成完整诊断包。
- 详细规范见 `/Users/winstontang/code/github/qiq-html2md/docs/observability.md`。

## 11. 运行时依赖

### 11.1 依赖分层

| 层级 | 内容 | 必要性 | 缺失影响 |
|------|------|------|----------|
| **L1** | `httpx` / `lxml` / `beautifulsoup4` / `readability-lxml` / `pydantic` / `python-ulid` | 硬依赖（装不上就无法启动） | Python import 报错 |
| **L2** | `playwright>=1.45` + Chromium 二进制 | 效果增强（强烈推荐） | JS 渲染页面抓到空白；复杂表格无法截图降级，质量分上限受限；无法触发懒加载滚动 |

L1 由 `pyproject.toml` 的 `dependencies` 声明，随 `pip install qiq-html2md` 自动安装。
L2 由 `[project.optional-dependencies].browser` 与其友好别名 `recommended` 声明。

### 11.2 一键安装命令

```bash
# 基础安装（仅 L1，可处理纯静态论文）
pip install qiq-html2md

# 推荐安装（L1 + L2）
pip install 'qiq-html2md[recommended]'
playwright install chromium
```

### 11.3 运行时预检契约

Skill 启动时自动执行**只读**依赖预检，行为如下：

| CLI 开关 | 行为 | 退出码语义 |
|---------|------|-----------|
| 默认 | 缺失仅 warn 到 stderr + 写 `_diag/preflight.json`，不阻塞管线 | 与管线一致（0/1/2） |
| `--strict-deps` | 缺失直接硬失败，不启动管线 | 依赖缺失时返回 `2` |
| `--check-deps` | 仅跑预检后退出，不执行转换 | 全齐返回 `0`，缺失返回 `1` |
| `--skip-deps-check` | 跳过启动预检（适合 CI 已确认依赖齐全） | 与管线一致 |

预检覆盖项：
- Playwright 包是否可 import
- Playwright 报告的 Chromium 可执行文件是否存在

预检规则：
- 不启动任何浏览器实例，仅查询路径并做 `Path.exists()`。
- 所有失败都带有 `install_hint` 字段，指明精确的修复命令。
- 报告可序列化为 JSON 供宿主 agent 解析（字段：`all_ok` / `checks[].name` / `checks[].level` / `checks[].installed` / `checks[].detail` / `checks[].install_hint`）。
