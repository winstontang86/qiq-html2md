"""Acquire Stage —— 获取 HTML。

阶段四更新：
- 真实支持 `render_mode=browser`，用 infra.browser.get_driver()。
- 若 `strategy['request_screenshots']` 提供选择器列表，浏览器一次会话内顺带截图（给表格/公式用）。
- Adapter 的 `hints.math_wait` 用作浏览器等待选择器。
- `render_mode=auto` 时，先静态抓取，如果 raw_html 看起来像"全靠 JS 渲染"（body 文本极短或包含 "loading"）则自动切浏览器。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from qiq_html2md.adapters_site.base import SiteAdapter
from qiq_html2md.adapters_site.base import resolve as resolve_adapter
from qiq_html2md.core.errors import FatalError, RetryableError
from qiq_html2md.core.types import Context, StageResult
from qiq_html2md.infra import browser as browser_mod
from qiq_html2md.infra import http
from qiq_html2md.obs.events import EventBus


class AcquireStage:
    name: Literal["acquire"] = "acquire"

    def __init__(self, *, bus: EventBus | None = None, allow_file_scheme: bool = False) -> None:
        self.bus = bus
        self.allow_file_scheme = allow_file_scheme

    def mutate(self, delta: dict[str, Any]) -> AcquireStage:  # noqa: ARG002
        return AcquireStage(bus=self.bus, allow_file_scheme=self.allow_file_scheme)

    def run(self, ctx: Context) -> StageResult:
        t0 = time.monotonic()
        url = ctx.request.url
        render_mode = ctx.strategy.get("render_mode", "auto")
        adapter: SiteAdapter = resolve_adapter(url)

        raw_html: str | None = None
        rendered_html: str | None = None
        final_url: str = url
        render_mode_used = "static"
        page_bytes: int = 0
        screenshots: dict[str, bytes] = {}

        # --- 第一阶段：静态抓取（除非强制 browser）---
        if render_mode != "browser":
            try:
                resp = http.get(url, timeout=20.0, allow_file_scheme=self.allow_file_scheme)
                raw_html = resp.text
                final_url = resp.final_url
                page_bytes = len(resp.content)
            except FatalError:
                # file:// / scheme 被拒绝等不可重试 —— 直接抛
                raise
            except RetryableError:
                # 网络可重试错误 —— 继续尝试 browser（auto/browser）
                if render_mode == "static":
                    raise
                raw_html = None

        # --- 判定是否需要 browser ---
        need_browser = render_mode == "browser" or (
            render_mode == "auto" and _looks_too_short(raw_html)
        )

        if need_browser:
            # file:// 不走浏览器（Playwright 不支持）
            if url.startswith("file://"):
                if self.bus:
                    self.bus.emit(
                        "warning.raised",
                        {"code": "browser_on_file_skipped"},
                        stage="acquire",
                    )
            else:
                try:
                    driver = browser_mod.get_driver()
                    wait_sel = adapter.hints.get("math_wait") if adapter.hints else None
                    screenshot_sels: list[str] = ctx.strategy.get("request_screenshots", []) or []
                    rr = driver.render(
                        url,
                        timeout_ms=30000,
                        wait_selector=wait_sel,
                        scroll_to_bottom=True,
                        screenshot_selectors=screenshot_sels,
                    )
                    rendered_html = rr.html
                    final_url = rr.final_url
                    screenshots = rr.screenshots
                    render_mode_used = "browser"
                    page_bytes = len(rr.html.encode("utf-8"))
                except FatalError as fe:
                    # Playwright 未安装等 —— 记 warning，尝试沿用 raw_html
                    if self.bus:
                        self.bus.emit(
                            "warning.raised",
                            {"code": fe.args[0] if fe.args else "browser_fatal"},
                            stage="acquire",
                        )
                    if raw_html is None:
                        raise  # 没有回退产物，直接失败
                except RetryableError as re:
                    if self.bus:
                        self.bus.emit(
                            "warning.raised",
                            {"code": "browser_render_retryable", "detail": str(re.payload)},
                            stage="acquire",
                        )
                    if raw_html is None:
                        raise

        # 如果 browser 没跑但用户指定 browser，最后再静态兜底（能拿到啥算啥）
        if raw_html is None:
            resp = http.get(url, timeout=20.0, allow_file_scheme=self.allow_file_scheme)
            raw_html = resp.text
            final_url = resp.final_url
            page_bytes = len(resp.content)

        # 产出检查
        effective_html = rendered_html or raw_html
        if not effective_html or not effective_html.strip():
            raise RetryableError(
                "empty_response",
                reason="text_too_short",
                final_url=final_url,
            )

        output = {
            "final_url": final_url,
            "raw_html": raw_html,
            "rendered_html": rendered_html,
            "adapter_name": adapter.name,
            "render_mode_used": render_mode_used,
            "screenshots": screenshots,  # selector -> PNG bytes（内部传递，不落盘）
            "page_stats": {
                "bytes": page_bytes,
                "chars": len(effective_html),
            },
        }

        return StageResult(
            stage="acquire",
            output=output,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


def _looks_too_short(raw_html: str | None) -> bool:
    """粗判静态 HTML 是否可能被 JS 渲染。"""
    if not raw_html:
        return True
    length = len(raw_html)
    if length < 3000:
        return True
    lower = raw_html.lower()
    # 简单信号：body 很小、script 占主导、或有 loading 占位
    if "<noscript>" in lower and "please enable javascript" in lower:
        return True
    return False
