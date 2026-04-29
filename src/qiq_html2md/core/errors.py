"""异常模型（3 种，精简版）。

- SkillError：顶层基类，一切 skill 内部异常都继承它。
- RetryableError：触发 plan_retry 决策的软失败。payload 中 `reason` 指示失败原因（对应 §9.3 表）。
- FatalError：不可重试，直接 degraded 或 failed。
"""

from __future__ import annotations

from typing import Any


class SkillError(Exception):
    """顶层异常。"""

    def __init__(self, message: str = "", **payload: Any) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = dict(payload)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({super().__str__()!r}, payload={self.payload!r})"


class RetryableError(SkillError):
    """软失败，交 plan_retry 决策。

    Example
    -------
    >>> raise RetryableError("quality_failed", reason="text_too_short")
    """


class FatalError(SkillError):
    """不可重试，直接收尾。

    常见场景：
    - 预算耗尽
    - 入参 schema 非法
    - URL 被 SSRF 护栏拒绝
    """
