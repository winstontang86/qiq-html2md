# html2md-skill

一个面向论文类 HTML 页面 → Markdown 的通用 skill。可被 WorkBuddy / OpenClaw / CLI / MCP 等智能体宿主复用。

## 快速开始

```bash
# 安装基础依赖
uv sync --extra dev
source .venv/bin/activate

# 可选：装浏览器支持（Playwright）
uv sync --extra dev --extra browser
.venv/bin/playwright install chromium

# 运行测试
pytest
ruff check src/ tests/
mypy --strict src/html2md_skill/

# 端到端（URL）
python -m html2md_skill --url "https://arxiv.org/html/2501.xxx" --output-dir ./output

# 端到端（本地 fixture）
python -m html2md_skill --allow-file-scheme \
  --url "file://$(pwd)/tests/fixtures/paper_rich.html" \
  --output-dir ./output

# 从 JSON 请求读入
echo '{"url":"https://arxiv.org/html/2501.xxx","output_dir":"./output"}' | python -m html2md_skill -
```

## 关键能力

- 静态抓取 + 可选浏览器渲染（Playwright Chromium，Pool 复用）。
- 四阶段线性管线：Acquire → Extract → Enrich → Emit。
- 质量六维评分（text/structure/image/table/formula/link_reference），不达标触发**局部重试 + 策略突变**。
- 表格三级输出：Markdown / HTML / 图片（复杂度自动判定）。
- 公式 LaTeX / MathML 提取；无源时截图降级。
- 图片下载、SVG 内联保留、懒加载滚动。
- SSRF 护栏、fs sandbox、HTTP 缓存 + 抽取指纹缓存。
- 全流程结构化事件 + 指标 + `_diag/TRACE.md` 时间轴。

## 架构文档

- `docs/architecture_design.md` — 架构主文档（v2-final r3）
- `docs/architecture_diagram.puml` — 架构图
- `docs/skill_contract.md` — Skill 契约
- `docs/observability.md` — 可观测性规范
- `docs/functional_spec.md` — 功能规范

## 契约

- `SKILL.md` — 智能体发现入口
- `manifest.yaml` — 机读声明
- `schemas/request.schema.json` / `schemas/response.schema.json`

## 目录结构

```
src/html2md_skill/
  core/          # types / pipeline / context / budget / errors
  stages/        # acquire / extract / enrich / emit
  adapters_site/ # base + arxiv/pmc/jats
  infra/         # http / fs_sandbox / browser / browser_pool / cache
  obs/           # events / metrics
  quality.py     # 六条质量规则 + 评分
  __main__.py    # CLI 入口
```

## 运行产物（`output_dir/`）

```
article.md            # Markdown 主产物
metadata.json         # 标题/作者/description/source_url
quality_report.json   # 六维评分 + risk_level
warnings.json         # 非致命告警
assets/               # images/ tables/ formulas/
_diag/
  events.jsonl        # 结构化事件流
  metrics.json        # 时间/预算/重试指标
  TRACE.md            # 人类可读时间轴
  stages/*.json       # 每个 Stage finished 快照
  retries/*.json      # 每次 RetryPlan 落盘
```

## 环境变量

- `HTML2MD_SKILL_CACHE_DIR`：缓存目录（默认 `$XDG_CACHE_HOME/html2md-skill` 或 `~/.cache/html2md-skill`）。

## 开发约定

- Python 3.10+
- `ruff`（lint + format）、`mypy --strict`、`pytest`
- 所有"substantive"工作进 `.workbuddy/memory/YYYY-MM-DD.md`（本地，不入仓）
