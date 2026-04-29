"""Extract Stage —— 正文抽取 + 规范化 + 元信息。

阶段三：按 `ctx.strategy.extractor_profile` 切换抽取路径。
  - adapter（默认）：按站点 adapter 的 main_selector
  - density        ：readability-lxml 密度算法
  - body           ：直接清洗 <body>（重试兜底）

`ctx.strategy.clean_rules` 控制清洗力度：
  - default：删除 script/style/nav/aside 等噪声
  - loose  ：只删 script/style（保留标题修复用）
`ctx.strategy.flags`：
  - fix_headings：若正文无 h1，用 <title> 作为 h1 补齐
  - keep_refs   ：保留原始 refs 区域（让 Enrich 走 selector 重抽）
"""

from __future__ import annotations

import time
from typing import Any, Literal

from bs4 import BeautifulSoup, Tag
from readability import Document

from html2md_skill.adapters_site.base import resolve as resolve_adapter
from html2md_skill.core.errors import RetryableError
from html2md_skill.core.types import Context, StageResult
from html2md_skill.infra import cache as cache_mod

_DEFAULT_JUNK_TAGS = (
    "script", "style", "noscript", "nav", "aside", "header", "footer", "form", "iframe",
)

_LOOSE_JUNK_TAGS = ("script", "style", "noscript", "iframe")


def _class_str(t: Tag) -> str:
    """把 tag.class 归一为空格连接的字符串，避免 bs4 新版的 Union 类型麻烦。"""
    raw = t.get("class")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return " ".join(str(x) for x in raw)
    if raw is None:
        return ""
    # AttributeValueList 类似 list 可迭代
    try:
        return " ".join(str(x) for x in raw)
    except TypeError:
        return str(raw)


class ExtractStage:
    name: Literal["extract"] = "extract"

    def mutate(self, delta: dict[str, Any]) -> ExtractStage:  # noqa: ARG002
        return ExtractStage()

    def run(self, ctx: Context) -> StageResult:
        t0 = time.monotonic()
        if ctx.acquire is None:
            raise RetryableError("acquire_missing", reason="text_too_short")

        raw_html: str = ctx.acquire["raw_html"]
        final_url: str = ctx.acquire["final_url"]
        adapter = resolve_adapter(final_url)

        profile: str = ctx.strategy.get("extractor_profile", "adapter")
        clean_rules = ctx.strategy.get("clean_rules", ["default"])
        flags: dict[str, bool] = ctx.strategy.get("flags", {}) or {}

        # 0. 抽取结果缓存（若启用且命中 → 直接回写）
        cache_key = cache_mod.make_extract_key(
            final_url,
            render_mode=ctx.acquire.get("render_mode_used", "static"),
            adapter_version=adapter.name + ":v1",
            extractor_profile=profile,
        )
        if cache_mod.enabled():
            cached = cache_mod.get_extract(cache_key)
            if cached is not None:
                # 命中时补一个特殊 stat 让 metrics 知道
                cached.setdefault("extract_stats", {})["cache_hit"] = True
                return StageResult(
                    stage="extract",
                    output=cached,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )

        # 1. 元信息
        metadata = _extract_metadata(raw_html, final_url)

        # 2. 正文抽取
        main_html = _extract_main(raw_html, adapter.main_selector, profile=profile)

        # 3. 清洗
        soup = BeautifulSoup(main_html, "lxml")
        loose = "loose" in clean_rules or flags.get("keep_refs", False)
        _clean(soup, loose=loose, keep_refs=flags.get("keep_refs", False))

        # 4. 标题修复
        if flags.get("fix_headings") and not soup.find(["h1"]):
            title = metadata.get("title")
            if title:
                new_h1 = soup.new_tag("h1")
                new_h1.string = title
                # 插在正文最前
                if soup.body:
                    soup.body.insert(0, new_h1)
                else:
                    soup.insert(0, new_h1)

        # 5. 提取统计
        text = soup.get_text(" ", strip=True)
        stats = {
            "text_len": len(text),
            "heading_count": len(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])),
            "paragraph_count": len(soup.find_all("p")),
            "image_count": len(soup.find_all("img")),
            "link_count": len(soup.find_all("a")),
            "profile_used": profile,
            "clean_rules_used": list(clean_rules),
        }

        output = {
            "clean_html": str(soup),
            "text": text,
            "metadata": metadata,
            "extract_stats": stats,
        }

        # 写抽取缓存（仅首次，避免被 retry 的结果污染；缓存 key 已包含 profile）
        if cache_mod.enabled() and not ctx.retry_history:
            try:
                cache_mod.put_extract(cache_key, output)
            except OSError:
                pass

        return StageResult(
            stage="extract",
            output=output,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


def _extract_main(raw_html: str, main_selector: str | None, *, profile: str) -> str:
    if profile == "body":
        # 直接取整个 body 做兜底；
        # 注意保留原 DOM，避免 readability 结构重建导致信息丢失。
        soup = BeautifulSoup(raw_html, "lxml")
        body = soup.body
        return str(body) if body is not None else raw_html

    if profile == "density":
        return _density_extract(raw_html)

    # adapter（默认）—— main_selector 可以是逗号分隔的多选择器组
    if main_selector:
        soup = BeautifulSoup(raw_html, "lxml")
        node = soup.select_one(main_selector)
        if node is not None and len(node.get_text(strip=True)) > 200:
            return str(node)
    return _density_extract(raw_html)


def _density_extract(raw_html: str) -> str:
    try:
        summary = Document(raw_html).summary(html_partial=True)
        return str(summary)
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(raw_html, "lxml")
        body = soup.body
        return str(body) if body is not None else raw_html


def _clean(soup: BeautifulSoup, *, loose: bool, keep_refs: bool) -> None:
    junk = _LOOSE_JUNK_TAGS if loose else _DEFAULT_JUNK_TAGS
    for tag_name in junk:
        for t in soup.find_all(tag_name):
            t.decompose()

    for t in soup.find_all(True):
        if not isinstance(t, Tag):
            continue
        cls_lower = _class_str(t).lower()

        # refs 区域在 keep_refs 模式下跳过
        if keep_refs and ("bibliograph" in cls_lower or "reference" in cls_lower):
            continue
        if any(k in cls_lower for k in ("sidebar", "advert", "cookie")):
            t.decompose()
        elif not loose and "nav" in cls_lower:
            t.decompose()


def _extract_metadata(raw_html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(raw_html, "lxml")
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    desc = None
    dm = soup.find("meta", attrs={"name": "description"})
    if dm and isinstance(dm, Tag):
        raw_desc = dm.get("content")
        if isinstance(raw_desc, str):
            desc = raw_desc.strip() or None

    authors: list[str] = []
    for tag in soup.find_all("meta", attrs={"name": "author"}):
        if isinstance(tag, Tag):
            v = tag.get("content")
            if isinstance(v, str) and v.strip():
                authors.append(v.strip())

    return {
        "title": title,
        "description": desc,
        "authors": authors,
        "source_url": url,
    }
