# Skill 契约说明（v2-final r3）

本 skill 以通用形态接入 WorkBuddy、OpenClaw、CLI、MCP 等宿主，所有调用方只需遵守 JSON 契约，不感知 Python 实现。字段权威定义见 `architecture_design.md` §5 与 `core/types.py`。

---

## 1. 契约组成

| 文件 | 位置 | 作用 |
|---|---|---|
| `SKILL.md` | 项目根 | 智能体发现入口，描述能力、触发词、边界、示例 |
| `manifest.yaml` | 项目根 | 机读声明，含 entry、schema 引用、默认超时、权限 |
| `schemas/request.schema.json` | `schemas/` | SkillRequest JSON Schema |
| `schemas/response.schema.json` | `schemas/` | SkillResponse JSON Schema |

---

## 2. SKILL.md 建议内容

```markdown
# HTML to Markdown Skill

## 作用
将 URL 指向的 HTML 页面转换为高保真 Markdown，面向论文、技术报告、学术文章。

## 适用场景
- 论文 HTML 页面（arXiv、PubMed Central、JATS）
- 技术报告 / 学术机构文章 / 长文档
- 含图表、公式、参考文献

## 不适用
- 需要登录或强交互的页面
- 主要内容为扫描件图片
- 反爬强烈无法本地访问
- 需外部 API 或大模型理解才能还原的页面

## 调用方式
见 schemas/request.schema.json；最小必填字段仅 `url`。

## 返回
见 schemas/response.schema.json；关键字段：
- status: passed / degraded / failed
- artifact.markdown_path
- diag_dir （失败时用于复盘）

## 超时
默认 600 秒，强制上限 600 秒。
```

---

## 3. manifest.yaml 示例

```yaml
name: html2md
version: 2.0.0
entry: python -m html2md_skill
description: 将 HTML 页面转换为高保真 Markdown，面向论文与技术报告
timeout_default_seconds: 600
timeout_max_seconds: 600

inputs_schema: schemas/request.schema.json
outputs_schema: schemas/response.schema.json

permissions:
  network:
    allow_protocols: ["http", "https"]
    deny_hosts_cidr: ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "::1/128", "fc00::/7"]
    max_redirects: 5
    max_response_bytes: 52428800
  filesystem:
    write_within: ["${output_dir}"]
    sandbox: strict
  browser:
    engine: chromium
    isolated_context: true
    allow_download: false

observability:
  diag_dir: "${output_dir}/_diag"
  debug_default: lite
  metrics_file: "${output_dir}/_diag/metrics.json"

tags: [html, markdown, paper, document, extraction]
```

---

## 4. request.schema.json（核心字段说明）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `url` | string | —— | **必填**，http/https |
| `output_dir` | string | `./output` | 输出目录，受 fs_sandbox 约束 |
| `timeout_seconds` | integer | 600 | 总预算，上限 600 |
| `render_mode` | enum | `auto` | `auto` / `static` / `browser` |
| `table_mode` | enum | `auto` | `auto` / `markdown` / `html` / `image` |
| `formula_mode` | enum | `auto` | `auto` / `latex` / `mathml` / `image` |
| `image_mode` | enum | `download` | `download` / `link` / `both` |
| `quality_check` | bool | true | 是否启用质量检查闭环 |
| `max_retry` | integer | 2 | 最大重试次数 |
| `include_references` | bool | true | 是否保留参考文献 |
| `include_metadata` | bool | true | 是否输出元信息 |
| `debug` | enum | `lite` | `lite` / `full` |
| `preserve_intermediate` | bool | false | 是否保留 raw / rendered HTML |
| `idempotency_key` | string\|null | null | 幂等键，同 key 命中缓存直接返回 |

---

## 5. response.schema.json（核心字段说明）

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | enum | `passed` / `degraded` / `failed` |
| `trace_id` | string | 全局 trace，贯穿日志/事件/指标 |
| `artifact.markdown_path` | string | 最终 Markdown 路径 |
| `artifact.assets_dir` | string | 本地资源目录 |
| `metadata_path` | string | metadata.json |
| `quality_report_path` | string | quality_report.json |
| `warnings_path` | string | warnings.json |
| `diag_dir` | string | `_diag/` 诊断包目录 |
| `stats.duration_ms` | integer | 总耗时 |
| `stats.retries` | integer | 重试次数 |
| `stats.cache_hits` | integer | 命中次数 |
| `risk_level` | enum | `low` / `medium` / `high` |
| `events_tail` | array | `events.jsonl` 最后 **20 条** 的即时内存快照（不重复持久化） |

> 以上字段由 `core/types.py` 中的 `SkillRequest` / `SkillResponse` 单一定义，JSON Schema 由其派生。数据结构的权威描述见 `architecture_design.md` §5。

---

## 6. 错误语义

| status | 含义 | 产物完整度 |
|---|---|---|
| `passed` | 质量检查通过 | 完整 |
| `degraded` | 达到重试/预算上限仍未通过，但已输出最佳结果 | 部分完整 + risk_level 提示 |
| `failed` | 未能产生任何 Markdown（URL 无法访问、沙盒拒绝等） | 无 artifact，有 `_diag/` |

---

## 7. 宿主接入要点

- **WorkBuddy / OpenClaw**：读取 `SKILL.md` 发现、通过 `manifest.yaml` 的 `entry` 调用、按 schema 校验 IO。
- **CLI**：`python -m html2md_skill --request request.json`。
- **MCP**：以 `tools/call` 形态暴露，参数透传 `SkillRequest`。

所有宿主对 skill 而言是等价的，skill 不做差异化分支。
