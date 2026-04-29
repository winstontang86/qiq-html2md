"""站点适配器 —— 纯数据。

阶段四补齐：
- arXiv / ar5iv：LaTeXML 导出的 HTML 有 `ltx_*` 前缀类名。
- PMC：NIH 的文章容器是 `.jig-ncbiinpagenav` 或 `.tsec`。
- JATS：检测 `<article>` with JATS 命名空间（留占位，真正实现看页面结构）。

registry 按顺序匹配，DEFAULT 兜底。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SiteAdapter:
    name: str
    match: Callable[[str], bool]
    main_selector: str | None = None
    refs_selector: str | None = None
    cleaners: list[str] = field(default_factory=list)
    hints: dict[str, str] = field(default_factory=dict)


DEFAULT = SiteAdapter(
    name="generic",
    match=lambda _url: True,
    cleaners=["default"],
)


# ----------------------------------------------------------------------
# arXiv （优先 ar5iv 版本，回退到 abs 页）
# ----------------------------------------------------------------------


def _match_arxiv(url: str) -> bool:
    lu = url.lower()
    return "arxiv.org" in lu or "ar5iv.labs.arxiv.org" in lu or "ar5iv.org" in lu


ARXIV = SiteAdapter(
    name="arxiv",
    match=_match_arxiv,
    # ar5iv/LaTeXML：.ltx_page_main 是整页容器，.ltx_document 是文档体
    main_selector="article.ltx_document, .ltx_page_main, article, main",
    refs_selector=".ltx_bibliography, section.bibliography, .references",
    cleaners=["default", "strip_nav"],
    hints={
        "math_wait": "mjx-container, .ltx_Math",
        "arxiv_ar5iv_hint": "优先访问 ar5iv.labs.arxiv.org 以获取 HTML 版",
    },
)


# ----------------------------------------------------------------------
# PubMed Central
# ----------------------------------------------------------------------


def _match_pmc(url: str) -> bool:
    lu = url.lower()
    return "ncbi.nlm.nih.gov/pmc" in lu or "pmc.ncbi.nlm.nih.gov" in lu


PMC = SiteAdapter(
    name="pmc",
    match=_match_pmc,
    # NIH 的老版和新版容器并存，最保险匹配多个
    main_selector="main#main-content, #maincontent, article.article, .article",
    refs_selector=".ref-list, section.ref-list, ol.references",
    cleaners=["default"],
    hints={
        "math_wait": ".mwe-math-element, math",
    },
)


# ----------------------------------------------------------------------
# JATS（结构化 XML 渲染）
# ----------------------------------------------------------------------


def _match_jats(url: str) -> bool:
    # JATS 通常以 application/xml 返回。URL 规则难单凭正则判定；
    # 这里保留占位由 Extract 侧通过内容嗅探启用（阶段五完善）。
    return False


JATS = SiteAdapter(
    name="jats",
    match=_match_jats,
    main_selector="body",
    refs_selector="ref-list, back > ref-list",
    cleaners=["default"],
)


REGISTRY: list[SiteAdapter] = [ARXIV, PMC, JATS, DEFAULT]


def resolve(url: str) -> SiteAdapter:
    for a in REGISTRY:
        if a.match(url):
            return a
    return DEFAULT
