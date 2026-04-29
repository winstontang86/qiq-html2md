"""针对图片 URL 解析逻辑 _resolve_image_url 的回归测试。

修复背景：urljoin 对 "目录式" base_url 不带尾斜杠时，会将 base 末段当文件名替换
掉，导致 arXiv 类论文（src="x1.png" 形式）图片 URL 丢失文章路径段而 404。
同时需要保留另一类 arXiv HTML（src 已带前缀 "2604.21691v1/x1.png"）的正确行为。
"""

from __future__ import annotations

import pytest

from qiq_html2md.stages.enrich import _resolve_image_url


@pytest.mark.parametrize(
    ("base_url", "src", "expected"),
    [
        # --- 核心 bug 场景（修复目标）: base 不带尾斜杠 + src 为裸文件名 ---
        (
            "https://arxiv.org/html/2501.05366v1",
            "x1.png",
            "https://arxiv.org/html/2501.05366v1/x1.png",
        ),
        (
            "https://arxiv.org/html/2501.05366v1",
            "figs/sub/img.png",
            "https://arxiv.org/html/2501.05366v1/figs/sub/img.png",
        ),
        # --- 保留既有正确行为: src 已带 base 末段前缀（B 类 arXiv 页面） ---
        (
            "https://arxiv.org/html/2604.21691v1",
            "2604.21691v1/x1.png",
            "https://arxiv.org/html/2604.21691v1/x1.png",
        ),
        (
            "https://arxiv.org/html/2604.21691v1",
            "2604.21691v1/figs/limits/lazy_rich_plots.png",
            "https://arxiv.org/html/2604.21691v1/figs/limits/lazy_rich_plots.png",
        ),
        (
            "https://arxiv.org/html/2604.21691",
            "2604.21691/x1.png",
            "https://arxiv.org/html/2604.21691/x1.png",
        ),
        # --- base 已带尾斜杠，纯相对路径 ---
        (
            "https://arxiv.org/html/2501.05366v1/",
            "x1.png",
            "https://arxiv.org/html/2501.05366v1/x1.png",
        ),
        # --- 站点绝对路径 ---
        (
            "https://arxiv.org/html/2501.05366v1",
            "/static/browse/foo.svg",
            "https://arxiv.org/static/browse/foo.svg",
        ),
        # --- 绝对 URL 原样返回 ---
        (
            "https://arxiv.org/html/2501.05366v1",
            "https://cdn.example.com/a.png",
            "https://cdn.example.com/a.png",
        ),
        # --- 协议相对 URL ---
        (
            "https://arxiv.org/html/2501.05366v1",
            "//example.com/a.png",
            "https://example.com/a.png",
        ),
        # --- base 末段是明显文件名（含扩展名），保持 urljoin 原语义 ---
        (
            "https://example.com/docs/page.html",
            "img.png",
            "https://example.com/docs/img.png",
        ),
        # --- 相对路径带 ../（保留原 urljoin 规范化） ---
        (
            "https://arxiv.org/html/2501.05366v1/sub/",
            "../x1.png",
            "https://arxiv.org/html/2501.05366v1/x1.png",
        ),
    ],
)
def test_resolve_image_url(base_url: str, src: str, expected: str) -> None:
    assert _resolve_image_url(base_url, src) == expected


def test_resolve_image_url_empty_src() -> None:
    assert _resolve_image_url("https://arxiv.org/html/foo", "") == ""
