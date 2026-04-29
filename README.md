# qiq-html2md

一个面向论文类 HTML 页面 → Markdown 的通用 skill。可被 WorkBuddy / OpenClaw / CLI / MCP 等智能体宿主复用。

## 快速开始

```bash
# 基础安装（L1 硬依赖，适合只处理纯静态论文页面的场景）
uv sync --extra dev
source .venv/bin/activate

# 推荐安装（L1 + L2：Playwright 浏览器渲染/截图降级，效果更好）
uv sync --extra dev --extra recommended
.venv/bin/playwright install chromium

# 或者仅装 browser 能力（与 recommended 等价）
uv sync --extra dev --extra browser

# 运行测试
pytest
ruff check src/ tests/
mypy --strict src/qiq_html2md/

# 端到端（URL）
python -m qiq_html2md --url "https://arxiv.org/html/2501.xxx" --output-dir ./output

# 端到端（本地 fixture）
python -m qiq_html2md --allow-file-scheme \
  --url "file://$(pwd)/tests/fixtures/paper_rich.html" \
  --output-dir ./output

# 从 JSON 请求读入
echo '{"url":"https://arxiv.org/html/2501.xxx","output_dir":"./output"}' | python -m qiq_html2md -

# 仅做依赖预检（不跑转换）
python -m qiq_html2md --check-deps            # 退出码 0=全齐，1=缺失

# 构建可安装的 skill zip 包（供 agent 宿主分发）
python -m qiq_html2md.build                  # 默认 dist/qiq-html2md-<version>.zip
python -m qiq_html2md.build --with-tests     # 附带 tests/
python -m qiq_html2md.build --no-docs        # 不带 docs/
SOURCE_DATE_EPOCH=1700000000 python -m qiq_html2md.build  # 可复现构建（字节级一致）
```

## 依赖分层

| 层级 | 内容 | 必要性 | 安装 |
|------|------|------|------|
| **L1 硬依赖** | `httpx` / `lxml` / `beautifulsoup4` / `readability-lxml` / `pydantic` / `python-ulid` | 必需（无则无法启动） | 自动随 `pip install qiq-html2md` 装上 |
| **L2 效果增强** | `playwright` + Chromium 浏览器 | 强烈推荐：启用 JS 渲染页面、复杂表格/公式**截图降级**、懒加载图滚动触发 | `pip install 'qiq-html2md[recommended]'`（或 `[browser]`）+ `playwright install chromium` |

**缺 L2 时的影响**：纯静态论文（如 arXiv HTML）依然可转换；但含复杂表格（复杂度>10）的页面无法走截图降级，会停留在 HTML 内嵌输出并触发 quality warning；JS 渲染页面会抓到空白 HTML。

## 运行时依赖检查

每次启动 CLI 时会自动做一次轻量预检（只查 Python 包与 Chromium 二进制路径，不启动浏览器）：

- 默认：缺失仅 warn 到 stderr 并给出 `pip install` / `playwright install` 指引，**不阻塞**流程。
- `--strict-deps`：缺失直接退出码 2，不启动管线（适合 CI 与自动化场景强制校验）。
- `--check-deps`：仅跑预检后退出（0=全齐，1=缺失），不执行转换。
- `--skip-deps-check`：跳过启动预检（适合已确认依赖齐全、想节省 ~50ms 的场景）。

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
src/qiq_html2md/
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

- `QIQ_HTML2MD_CACHE_DIR`：缓存目录（默认 `$XDG_CACHE_HOME/qiq-html2md` 或 `~/.cache/qiq-html2md`）。

## 开发约定

- Python 3.10+
- `ruff`（lint + format）、`mypy --strict`、`pytest`
- 所有"substantive"工作进 `.workbuddy/memory/YYYY-MM-DD.md`（本地，不入仓）

## Skill 分发包（zip）

`python -m qiq_html2md.build` 产出 `dist/qiq-html2md-<version>.zip`，供 agent 宿主（WorkBuddy、OpenClaw 等）扫描安装。

### 包内结构

```
qiq-html2md-<version>/
  SKILL.md               # 宿主发现入口
  manifest.yaml          # 机读声明（entry / schemas / permissions）
  README.md · LICENSE
  requirements.txt       # 运行时依赖（browser 作为注释 extras）
  dist_info.json         # name/version/built_at/SHA-256 清单
  schemas/*.json         # request/response JSON Schema
  src/qiq_html2md/**   # 源码
  docs/**                # 可选
  tests/**               # 可选（--with-tests）
```

### 安装方（agent 宿主）典型用法

```bash
# 1. 解压
unzip qiq-html2md-0.1.0.zip
cd qiq-html2md-0.1.0

# 2. 装依赖（L1 基础）
pip install -r requirements.txt
# 推荐：装 L2 浏览器能力以启用完整效果（复杂表格截图降级、JS 渲染）
# pip install 'playwright>=1.45' && playwright install chromium

# 3. 运行
PYTHONPATH=src python -m qiq_html2md --url "..." --output-dir ./out
# 运行前检查依赖：
PYTHONPATH=src python -m qiq_html2md --check-deps
```

### 校验

宿主可对比 `dist_info.json` 中每个文件的 `sha256` 与解压后的实际字节，确认包未被篡改。

### 可复现

设置 `SOURCE_DATE_EPOCH` 后，两次构建产出字节级一致的 zip（同一哈希），方便审计与镜像。
