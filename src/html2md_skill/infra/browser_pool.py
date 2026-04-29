"""Browser Pool —— 跨任务复用 Chromium 进程。

职责
----
- 单进程级全局单例：`get_pool()` 返回同一实例。
- 懒加载 `sync_playwright().start()` 与 `chromium.launch()`，首个任务触发。
- 每个任务通过 `with pool.context() as ctx:` 拿独立 BrowserContext，使用完毕自动关闭。
- 空闲超时自动关停（默认 120s，避免进程常驻）。
- 显式 `pool.shutdown()` 供调用方优雅关闭（测试 / CLI 退出）。

设计权衡
--------
- 同步 Playwright 不能跨线程共享 browser（`sync_playwright()` 是 greenlet 架构）；
  本实现假设单线程调度（与 r3 "线性管线 + Enrich 内部并发也走 thread-safe 数据"一致）。
- 多进程调用（gunicorn 多 worker）各自拥有独立 pool，无需 shared。

回退
----
- 若 Playwright 未安装，`get_pool()` 抛 FatalError（与 `infra.browser.get_driver()` 保持同样语义）。
"""

from __future__ import annotations

import atexit
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from html2md_skill.core.errors import FatalError

IDLE_TIMEOUT_SECONDS = 120.0  # 超过多久闲置则关停
_pool_singleton: BrowserPool | None = None
_pool_lock = threading.Lock()


class BrowserPool:
    """Chromium 进程级复用。"""

    def __init__(self, *, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._lock = threading.Lock()
        self._last_use = 0.0
        self._idle_timeout = idle_timeout
        self._context_checkouts = 0
        # 统计
        self.stats_launches = 0
        self.stats_context_opens = 0
        self.stats_closes = 0

    # ------- 懒加载 -------

    def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise FatalError(
                "playwright_not_installed",
                hint="pip install 'html2md-skill[browser]' 并 playwright install chromium",
            ) from e
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self.stats_launches += 1
        return self._browser

    # ------- context 生命周期 -------

    @contextmanager
    def context(
        self,
        *,
        viewport: dict[str, int] | None = None,
        user_agent: str | None = None,
    ) -> Iterator[Any]:
        with self._lock:
            browser = self._ensure_browser()
            self._context_checkouts += 1
            self.stats_context_opens += 1
        ctx = browser.new_context(
            viewport=viewport or {"width": 1280, "height": 1600},
            user_agent=user_agent or "html2md-skill/0.1",
        )
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            should_close = False
            with self._lock:
                self._last_use = time.monotonic()
                self._context_checkouts -= 1
                should_close = (
                    self._browser is not None
                    and self._context_checkouts == 0
                    and self._idle_timeout <= 0
                )
                if should_close:
                    self._close_locked()

    # ------- 空闲清理 -------

    def sweep_idle(self) -> bool:
        """调用方可周期性调用；空闲超时则关停 browser。"""
        with self._lock:
            if self._browser is None:
                return False
            if self._context_checkouts > 0:
                return False
            if time.monotonic() - self._last_use < self._idle_timeout:
                return False
            self._close_locked()
            return True

    def shutdown(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
            self.stats_closes += 1
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    # ------- 统计 -------

    def stats(self) -> dict[str, int]:
        return {
            "launches": self.stats_launches,
            "context_opens": self.stats_context_opens,
            "closes": self.stats_closes,
            "active_contexts": self._context_checkouts,
        }


def get_pool() -> BrowserPool:
    global _pool_singleton
    if _pool_singleton is not None:
        return _pool_singleton
    with _pool_lock:
        if _pool_singleton is None:
            _pool_singleton = BrowserPool()
            atexit.register(_pool_singleton.shutdown)
    return _pool_singleton


def reset_pool() -> None:
    """主要给测试用：关掉全局池并清空。"""
    global _pool_singleton
    with _pool_lock:
        if _pool_singleton is not None:
            _pool_singleton.shutdown()
        _pool_singleton = None
