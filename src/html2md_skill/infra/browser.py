"""Browser 层（基于 Playwright）—— 惰性加载 + 可测试替身。

职责
----
- `fetch(url)` —— 启动 Chromium、访问 URL、等待网络空闲，返回渲染后 HTML。
- `screenshot_node(html, base_url, selector)` —— 为任意 DOM 节点截图，返回 PNG bytes。
- 自动滚动页面触发懒加载（`scroll_to_bottom` flag）。

设计
----
- Playwright 是 optional extra（`pip install .[browser]`）。
- 模块级 `_driver` 变量可被 monkeypatch 为 Mock，避免测试启动真实浏览器。
- 所有失败统一抛 RetryableError（软失败）或 FatalError（如 playwright 未安装且用户强制 browser 模式）。
- 不做跨任务池化（r3 明确砍了 browser_pool，阶段五再加）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from html2md_skill.core.errors import FatalError, RetryableError


@dataclass(frozen=True)
class RenderResult:
    final_url: str
    html: str
    screenshots: dict[str, bytes]  # selector → png bytes


@runtime_checkable
class BrowserDriver(Protocol):
    """浏览器驱动抽象。测试中用 Mock，生产用 Playwright。"""

    def render(
        self,
        url: str,
        *,
        timeout_ms: int = 30000,
        wait_selector: str | None = None,
        scroll_to_bottom: bool = True,
        screenshot_selectors: list[str] | None = None,
    ) -> RenderResult: ...

    def screenshot_nodes(
        self,
        html: str,
        selectors: list[str],
        *,
        base_url: str | None = None,
        timeout_ms: int = 15000,
    ) -> dict[str, bytes]: ...


# 模块级驱动句柄。测试通过 set_driver() 注入 Mock。
_driver: BrowserDriver | None = None


def set_driver(driver: BrowserDriver | None) -> None:
    global _driver
    _driver = driver


def get_driver() -> BrowserDriver:
    if _driver is not None:
        return _driver
    return _playwright_driver()


def _playwright_driver() -> BrowserDriver:
    """真实 Playwright 驱动；未安装则抛 FatalError。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:
        raise FatalError(
            "playwright_not_installed",
            hint="pip install 'html2md-skill[browser]' 并执行 playwright install chromium",
        ) from e
    return _PlaywrightDriver()


# ---------------------------------------------------------------------------
# Playwright 驱动实现
# ---------------------------------------------------------------------------


class _PlaywrightDriver:
    """真实浏览器驱动（同步 API）。

    借助 `browser_pool.get_pool()` 复用 Chromium 进程；每任务使用独立 context，
    任务结束 context 自动关闭。
    """

    def render(
        self,
        url: str,
        *,
        timeout_ms: int = 30000,
        wait_selector: str | None = None,
        scroll_to_bottom: bool = True,
        screenshot_selectors: list[str] | None = None,
    ) -> RenderResult:
        from html2md_skill.infra.browser_pool import get_pool

        screenshots: dict[str, bytes] = {}
        pool = get_pool()
        try:
            with pool.context() as context:
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(url, wait_until="networkidle")

                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=min(10000, timeout_ms))
                    except Exception:  # noqa: BLE001
                        pass

                if scroll_to_bottom:
                    _auto_scroll(page)

                final_url = page.url
                html = page.content()

                for sel in screenshot_selectors or []:
                    try:
                        handle = page.query_selector(sel)
                        if handle is not None:
                            png = handle.screenshot(type="png")
                            screenshots[sel] = png
                    except Exception:  # noqa: BLE001
                        pass

                return RenderResult(
                    final_url=final_url,
                    html=html,
                    screenshots=screenshots,
                )
        except FatalError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RetryableError(
                "browser_render_failed",
                url=url,
                detail=repr(e)[:300],
            ) from e

    def screenshot_nodes(
        self,
        html: str,
        selectors: list[str],
        *,
        base_url: str | None = None,
        timeout_ms: int = 15000,
    ) -> dict[str, bytes]:
        from html2md_skill.infra.browser_pool import get_pool

        if not selectors:
            return {}
        out: dict[str, bytes] = {}
        pool = get_pool()
        try:
            with pool.context() as context:
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_content(html, wait_until="domcontentloaded")
                for sel in selectors:
                    try:
                        handle = page.query_selector(sel)
                        if handle is None:
                            continue
                        png = handle.screenshot(type="png")
                        out[sel] = png
                    except Exception:  # noqa: BLE001
                        continue
        except FatalError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RetryableError(
                "browser_screenshot_failed",
                detail=repr(e)[:300],
            ) from e
        return out


def _auto_scroll(page: Any) -> None:
    """把页面滚到底部，触发懒加载。"""
    page.evaluate(
        """
        async () => {
            await new Promise((resolve) => {
                let total = 0;
                const step = 400;
                const timer = setInterval(() => {
                    window.scrollBy(0, step);
                    total += step;
                    if (total >= document.body.scrollHeight) {
                        clearInterval(timer);
                        resolve();
                    }
                }, 100);
            });
        }
        """
    )


__all__ = ["BrowserDriver", "RenderResult", "get_driver", "set_driver"]
