"""时间预算管理。

职责
----
- 全局 deadline（monotonic）。
- 为每个 Stage 预留子预算。
- with budget.checkout(stage) 代码块计时，退出时把 `reserved - actual` 归还全局池。
- 查询剩余时间 / 判断是否还能重试。

默认分配（总 600s）见架构文档 §5.6。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from html2md_skill.core.errors import FatalError

DEFAULT_BUDGET: dict[str, int] = {
    "acquire": 140,
    "extract": 30,
    "enrich": 180,
    "emit": 40,
    # 以下两个只作为预留，不对应具体 Stage：
    "__retry_reserve__": 190,
    "__safety__": 20,
}


class Budget:
    """时间预算分配与跟踪。

    单位一律为秒（float）。所有时间基于 `time.monotonic()`。
    """

    def __init__(self, total_seconds: int) -> None:
        if total_seconds <= 0:
            raise FatalError("invalid_budget", total_seconds=total_seconds)
        self._start = time.monotonic()
        self._total = float(total_seconds)
        self._deadline = self._start + self._total
        # 已预留但尚未花掉：stage -> seconds
        self._reserved: dict[str, float] = {}
        # 已花掉：stage -> seconds
        self._spent: dict[str, float] = {}

    # ---------- 基础查询 ----------

    @property
    def deadline_ts(self) -> float:
        return self._deadline

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def global_left(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def left_for(self, stage: str) -> float:
        """返回该 Stage 可用的预算 = 已预留未花 + 全局未分配。"""
        reserved_unused = self._reserved.get(stage, 0.0) - self._spent.get(stage, 0.0)
        reserved_unused = max(0.0, reserved_unused)
        pool = self._unreserved_pool()
        # 不能超过全局剩余时间
        return min(self.global_left(), reserved_unused + pool)

    def _unreserved_pool(self) -> float:
        """返回尚未被任何 stage 预留的时间。"""
        spent_total = sum(self._spent.values())
        reserved_total = sum(self._reserved.values())
        # pool = 全局总量 - (已预留 + 已花)；但已花可能大于已预留（超支）
        # 超支部分从未预留池扣除
        overspend = max(0.0, spent_total - reserved_total)
        base = self._total - reserved_total - overspend
        return max(0.0, base)

    # ---------- 预留与归还 ----------

    def reserve(self, stage: str, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        self._reserved[stage] = self._reserved.get(stage, 0.0) + seconds

    def release_unused(self, stage: str) -> None:
        """Stage 结束后调用：把未花掉的预留归还给全局未分配池。"""
        reserved = self._reserved.get(stage, 0.0)
        spent = self._spent.get(stage, 0.0)
        unused = max(0.0, reserved - spent)
        if unused > 0:
            self._reserved[stage] = max(0.0, reserved - unused)

    # ---------- 计时上下文 ----------

    @contextmanager
    def checkout(self, stage: str) -> Iterator[None]:
        if self.global_left() <= 0:
            raise FatalError("budget_exhausted", stage=stage)
        t0 = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - t0
            self._spent[stage] = self._spent.get(stage, 0.0) + elapsed

    # ---------- 重试相关 ----------

    def can_retry(self, extra_seconds: int) -> bool:
        return self.global_left() >= extra_seconds

    # ---------- 导出 ----------

    def stats(self) -> dict[str, Any]:
        return {
            "total_ms": int(self._total * 1000),
            "elapsed_ms": int(self.elapsed() * 1000),
            "left_ms": int(self.global_left() * 1000),
            "spent_by_stage_ms": {k: int(v * 1000) for k, v in self._spent.items()},
            "reserved_by_stage_ms": {k: int(v * 1000) for k, v in self._reserved.items()},
        }


def new_default_budget(total_seconds: int) -> Budget:
    """创建预算并按默认分配预留各 Stage。"""
    b = Budget(total_seconds)
    # 按比例缩放（如果 total 小于 600）
    scale = min(1.0, total_seconds / 600.0)
    for stage, secs in DEFAULT_BUDGET.items():
        b.reserve(stage, int(secs * scale))
    return b
