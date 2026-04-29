from __future__ import annotations

from qiq_html2md.adapters_site.base import ARXIV, DEFAULT, PMC, resolve


def test_resolve_arxiv() -> None:
    a = resolve("https://arxiv.org/html/2501.12345")
    assert a is ARXIV


def test_resolve_pmc() -> None:
    a = resolve("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12345/")
    assert a is PMC


def test_resolve_default() -> None:
    a = resolve("https://example.com/article.html")
    assert a is DEFAULT
