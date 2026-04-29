# html2md Skill

一个面向**论文类 HTML 页面**的高保真 Markdown 转换 skill，支持 WorkBuddy / OpenClaw / CLI / MCP 等智能体宿主复用。

## 作用

给定一个 URL，自动：

1. 获取 HTML（静态优先，必要时浏览器渲染）。
2. 抽取正文、元信息、图片、表格、公式、参考文献。
3. 生成 Markdown + 本地资源目录，并给出质量评分。
4. 质量不达标时按失败原因做**局部重试 + 策略突变**。
5. 全流程落结构化事件/指标/快照到 `_diag/` 供异常复盘。

## 适用场景

- arXiv / ar5iv 论文 HTML
- PubMed Central（PMC）文章
- 技术报告、学术机构发布的长文
- 含图表、公式、脚注、参考文献的结构化文档

## 不适合

- 需要登录、强交互才能访问的页面
- 主要内容为扫描件 PDF 图片的页面
- 反爬强、本地无法访问的页面

## 调用

详细契约见 `schemas/request.schema.json` / `schemas/response.schema.json`。

最小请求：

```json
{ "url": "https://ar5iv.labs.arxiv.org/html/2501.12345" }
```

CLI：

```bash
python -m qiq_html2md --url "https://ar5iv.labs.arxiv.org/html/2501.12345" --output-dir ./output
```

## 返回关键字段

- `status`：`passed` / `degraded` / `failed`
- `artifact.markdown_path`：Markdown 文件路径
- `diag_dir`：诊断包目录（events.jsonl / metrics.json / stages / retries / TRACE.md）
- `risk_level`：`low` / `medium` / `high`

## 超时

默认 600 秒，上限 600 秒。

## 浏览器模式（可选）

`render_mode=browser` 或 `auto` 且页面看起来由 JS 渲染时，会启动 Playwright Chromium：

```bash
pip install 'qiq-html2md[browser]'
playwright install chromium
```

未安装 Playwright 时自动降级为静态抓取并记 warning。

## 缓存

默认启用两级缓存：
- HTTP 级：带 ETag/Last-Modified，304 命中；
- 抽取结果级：`sha256(url + render_mode + adapter_version + profile)` 指纹。

缓存目录：`$XDG_CACHE_HOME/qiq-html2md` 或 `~/.cache/qiq-html2md`，通过环境变量 `QIQ_HTML2MD_CACHE_DIR` 覆盖。
