"""Emit Stage —— Markdown 生成 + 打包 + 质量检查。

阶段二更新：
- 从 `ctx.enrich.annotated_html` 读 DOM（带 data-h2m-id 锚点）。
- 遇到锚点节点，根据 artifact 类型替换为对应 Markdown 片段：
  * image  → `![alt](path)`（含 figure/figcaption 时加 caption）
  * table  → Markdown 表格 / 原始 HTML
  * formula → `$...$` 或 `$$...$$` LaTeX；MathML 原样保留
- 文末追加 `## References` 列表（若启用）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from qiq_html2md import quality as quality_mod
from qiq_html2md.core.errors import RetryableError
from qiq_html2md.core.types import Context, StageResult
from qiq_html2md.infra.fs_sandbox import FsSandbox
from qiq_html2md.stages.enrich import ANCHOR_ATTR


class EmitStage:
    name: Literal["emit"] = "emit"

    def mutate(self, delta: dict[str, Any]) -> EmitStage:  # noqa: ARG002
        return EmitStage()

    def run(self, ctx: Context) -> StageResult:
        t0 = time.monotonic()
        if ctx.extract is None:
            raise RetryableError("extract_missing", reason="text_too_short")

        sandbox = FsSandbox(ctx.output_dir)

        metadata: dict[str, Any] = ctx.extract["metadata"]

        # 优先使用 Enrich 带锚点的 HTML；否则回退到 Extract 的 clean_html
        source_html: str
        if ctx.enrich and ctx.enrich.get("annotated_html"):
            source_html = ctx.enrich["annotated_html"]
        else:
            source_html = ctx.extract["clean_html"]

        artifacts = _build_artifact_map(ctx.enrich or {})

        markdown_text = _html_to_markdown(
            source_html,
            metadata=metadata,
            artifacts=artifacts,
        )

        # 参考文献附加
        refs = (ctx.enrich or {}).get("refs") or []
        if refs and ctx.request.include_references:
            markdown_text = markdown_text.rstrip() + "\n\n" + _render_references(refs) + "\n"

        # 写文件
        sandbox.write_text("article.md", markdown_text)
        metadata_path: str | None = None
        if ctx.request.include_metadata:
            sandbox.write_text(
                "metadata.json",
                json.dumps(metadata, ensure_ascii=False, indent=2),
            )
            metadata_path = str(sandbox.resolve("metadata.json"))
        sandbox.write_text(
            "warnings.json",
            json.dumps(ctx.warnings, ensure_ascii=False, indent=2),
        )

        output_partial: dict[str, Any] = {
            "markdown_path": str(sandbox.resolve("article.md")),
            "markdown_text": markdown_text,
            "assets_dir": str(sandbox.resolve("assets")),
            "metadata_path": metadata_path,
            "warnings_path": str(sandbox.resolve("warnings.json")),
        }
        ctx.emit = output_partial

        # 质量评分；quality_check=False 时仍写一个报告，便于宿主统一读取。
        if ctx.request.quality_check:
            report = quality_mod.evaluate(ctx)
        else:
            from qiq_html2md.core.types import QualityReport

            report = QualityReport(
                passed=True,
                final_score=100.0,
                sub_scores={"quality_check": 100.0},
                risk_level="low",
            )
        ctx.quality_report = report
        sandbox.write_text(
            "quality_report.json",
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
        )

        output_partial["quality_report_path"] = str(sandbox.resolve("quality_report.json"))
        output_partial["emit_stats"] = {
            "markdown_chars": len(markdown_text),
            "metadata_written": metadata_path is not None,
            "quality_checked": ctx.request.quality_check,
        }

        if ctx.request.quality_check and not report.passed:
            reason = report.failed_rules[0] if report.failed_rules else "quality_failed"
            raise RetryableError(
                "quality_failed",
                reason=reason,
                final_score=report.final_score,
            )

        return StageResult(
            stage="emit",
            output=output_partial,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# artifact 索引
# ---------------------------------------------------------------------------


def _build_artifact_map(enrich: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 enrich.{images,tables,formulas,algorithms} 扁平化为 id → artifact。"""
    out: dict[str, dict[str, Any]] = {}
    for img in enrich.get("images", []):
        aid = img.get("id")
        if aid:
            out[aid] = {"kind": "image", **img}
    for t in enrich.get("tables", []):
        aid = t.get("id")
        if aid:
            out[aid] = {"kind": "table", **t}
    for f in enrich.get("formulas", []):
        aid = f.get("id")
        if aid:
            out[aid] = {"kind": "formula", **f}
    for a in enrich.get("algorithms", []):
        aid = a.get("id")
        if aid:
            # 算法 artifact 自身已带 kind=algorithm，显式覆盖保底
            out[aid] = {"kind": "algorithm", **a}
    return out


# ---------------------------------------------------------------------------
# DOM → Markdown
# ---------------------------------------------------------------------------


def _html_to_markdown(
    source_html: str,
    *,
    metadata: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> str:
    soup = BeautifulSoup(source_html, "lxml")

    parts: list[str] = []
    title = metadata.get("title")
    if title:
        parts.append(f"# {title}\n")
    desc = metadata.get("description")
    if desc:
        parts.append(f"> {desc}\n")
    src = metadata.get("source_url")
    if src:
        parts.append(f"_Source: {src}_\n")

    root: Tag = soup.body if soup.body else soup
    for child in root.children:
        md = _walk_block(child, artifacts)
        if md:
            parts.append(md)

    # 去除连续空行 & 合并
    out = "\n\n".join(p.rstrip() for p in parts if p and p.strip())
    return out.strip() + "\n"


def _walk_block(node: Any, artifacts: dict[str, dict[str, Any]]) -> str:
    """块级 walker；返回当前节点的 Markdown 文本。"""
    if isinstance(node, NavigableString):
        text = str(node).strip()
        return text
    if not isinstance(node, Tag):
        return ""

    # 锚点优先
    aid = node.get(ANCHOR_ATTR) if isinstance(node.get(ANCHOR_ATTR), str) else None
    if aid and aid in artifacts:
        return _render_artifact(artifacts[aid])

    name = node.name.lower()

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        return f"{'#' * level} {_inline(node, artifacts)}"

    if name == "p":
        return _inline(node, artifacts)

    if name == "blockquote":
        inner = _inline(node, artifacts)
        lines = inner.splitlines() or [inner]
        return "\n".join(f"> {line}" for line in lines)

    if name in ("ul", "ol"):
        items: list[str] = []
        bullet = "-" if name == "ul" else None
        for i, li in enumerate(node.find_all("li", recursive=False), start=1):
            prefix = bullet or f"{i}."
            items.append(f"{prefix} {_inline(li, artifacts)}")
        return "\n".join(items)

    if name == "pre":
        code = node.get_text()
        return f"```\n{code}\n```"

    if name == "figure":
        # figure 可能包含 img / table / figcaption。保持出现顺序地渲染其中的子块。
        caption_tag = node.find("figcaption")
        parts: list[str] = []
        for c in node.children:
            if not isinstance(c, Tag):
                continue
            cname = c.name.lower()
            if cname == "figcaption":
                continue  # 放到最后统一处理，保持原 caption 位置贴近内容
            if cname == "img":
                img_aid = c.get(ANCHOR_ATTR) if isinstance(c.get(ANCHOR_ATTR), str) else None
                if img_aid and img_aid in artifacts:
                    parts.append(_render_artifact(artifacts[img_aid]))
                else:
                    parts.append(_img_fallback(c))
                continue
            if cname == "table":
                tbl_aid = c.get(ANCHOR_ATTR) if isinstance(c.get(ANCHOR_ATTR), str) else None
                if tbl_aid and tbl_aid in artifacts:
                    parts.append(_render_artifact(artifacts[tbl_aid]))
                else:
                    parts.append(str(c))
                continue
            # 其他元素（div/p 等）递归
            sub = _walk_block(c, artifacts)
            if sub:
                parts.append(sub)
        if isinstance(caption_tag, Tag):
            cap_text = caption_tag.get_text(" ", strip=True)
            if cap_text:
                parts.append(f"*{cap_text}*")
        return "\n\n".join(p for p in parts if p)

    # 表格节点若未命中锚点，直接用原始 HTML 作为兜底
    if name == "table":
        return str(node)

    if name in ("div", "section", "article", "main"):
        parts = []
        for c in node.children:
            m = _walk_block(c, artifacts)
            if m:
                parts.append(m)
        return "\n\n".join(parts)

    # 其他块：退化为纯文本
    return node.get_text(" ", strip=True)


def _inline(node: Tag, artifacts: dict[str, dict[str, Any]]) -> str:
    """行内文本 + 常见 inline 元素。"""
    parts: list[str] = []
    for c in node.children:
        # 锚点优先（行内公式、行内图片）
        if isinstance(c, Tag):
            aid = c.get(ANCHOR_ATTR) if isinstance(c.get(ANCHOR_ATTR), str) else None
            if aid and aid in artifacts:
                parts.append(_render_artifact(artifacts[aid], inline=True))
                continue

        if isinstance(c, NavigableString):
            parts.append(str(c))
            continue
        if not isinstance(c, Tag):
            continue
        n = c.name.lower()
        if n == "a":
            href = c.get("href") or ""
            parts.append(f"[{_inline(c, artifacts)}]({href})")
        elif n in ("strong", "b"):
            parts.append(f"**{_inline(c, artifacts)}**")
        elif n in ("em", "i"):
            parts.append(f"*{_inline(c, artifacts)}*")
        elif n == "code":
            parts.append(f"`{c.get_text()}`")
        elif n == "img":
            parts.append(_img_fallback(c))
        elif n == "br":
            parts.append("  \n")
        else:
            parts.append(_inline(c, artifacts))
    text = " ".join(p.strip() for p in parts if p and p.strip())
    text = text.replace("  \n ", "  \n")
    # 行内元素拼接会在英文标点前产生多余空格：`**B** .` / `[x](u) ,`
    text = re.sub(r"\s+([,.;:!?\)\]\}])", r"\1", text)
    text = re.sub(r"([\(\[\{])\s+", r"\1", text)
    return text


def _img_fallback(img: Tag) -> str:
    src = img.get("src") or img.get("data-src") or ""
    alt = img.get("alt") or ""
    return f"![{alt}]({src})"


# ---------------------------------------------------------------------------
# artifact 渲染
# ---------------------------------------------------------------------------


def _render_artifact(art: dict[str, Any], *, inline: bool = False) -> str:
    kind = art.get("kind")
    if kind == "image":
        mode = art.get("mode")
        if mode == "svg_inline":
            return art.get("svg") or ""
        alt = art.get("alt") or ""
        local = art.get("local_path") or art.get("remote_url") or ""
        return f"![{alt}]({local})"
    if kind == "table":
        caption = art.get("caption")
        mode = art.get("mode")
        if mode == "image":
            img_path = art.get("image_path") or ""
            alt = caption or "table"
            body = f"![{alt}]({img_path})"
            return f"**{caption}**\n\n{body}" if caption else body
        if mode == "markdown":
            body = art.get("markdown") or ""
            return f"**{caption}**\n\n{body}" if caption else body
        # html 降级
        body = art.get("html") or ""
        return f"**{caption}**\n\n{body}" if caption else body
    if kind == "formula":
        mode = art.get("mode")
        is_inline = inline or art.get("inline", True)
        if mode == "image":
            img_path = art.get("image_path") or ""
            return f"![formula]({img_path})"
        if mode == "latex":
            tex = (art.get("latex") or "").strip()
            if not tex:
                return ""
            return f"${tex}$" if is_inline else f"\n$$\n{tex}\n$$\n"
        # mathml
        mml = (art.get("mathml") or "").strip()
        if mml:
            return mml
        # 既无 LaTeX 也无 MathML 源（mjx-container 占位等）：输出占位注释
        return "<!-- formula source unavailable -->"
    if kind == "algorithm":
        title = art.get("title") or ""
        lines = art.get("lines") or []
        header = f"### {title}\n\n" if title else ""
        body = "\n".join(lines)
        return f"{header}```\n{body}\n```"
    return ""


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------


def _render_references(refs: list[dict[str, Any]]) -> str:
    lines = ["## References", ""]
    for r in refs:
        idx = r.get("idx")
        text = r.get("text") or ""
        lines.append(f"{idx}. {text}")
    return "\n".join(lines)
