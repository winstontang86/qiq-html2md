from __future__ import annotations

import time

import pytest

from html2md_skill.core.budget import Budget, new_default_budget
from html2md_skill.core.errors import FatalError, RetryableError, SkillError


def test_budget_basic() -> None:
    b = Budget(10)
    assert 9.9 <= b.global_left() <= 10.0
    assert b.elapsed() < 0.1


def test_budget_reserve_and_left_for() -> None:
    b = Budget(10)
    b.reserve("acquire", 4)
    b.reserve("extract", 2)
    # acquire 有预留 4s，且 10-6=4s 未预留
    assert b.left_for("acquire") > 7.5
    # 未预留的 stage 只能拿到未分配池
    assert 3.5 <= b.left_for("unknown") <= 4.0


def test_budget_checkout_releases_unused() -> None:
    b = Budget(5)
    b.reserve("acquire", 3)
    with b.checkout("acquire"):
        time.sleep(0.05)
    b.release_unused("acquire")
    # 花了 ~0.05s，剩余全局应 ~ 4.95
    assert b.global_left() > 4.8


def test_budget_exhausted_raises() -> None:
    b = Budget(1)
    time.sleep(1.05)
    with pytest.raises(FatalError):
        with b.checkout("acquire"):
            pass


def test_can_retry() -> None:
    b = Budget(5)
    assert b.can_retry(3) is True
    assert b.can_retry(100) is False


def test_default_budget_allocation() -> None:
    b = new_default_budget(600)
    # acquire 140 + extract 30 + enrich 180 + emit 40 + retry 190 + safety 20 = 600
    stats = b.stats()
    assert stats["reserved_by_stage_ms"]["acquire"] == 140_000
    assert stats["reserved_by_stage_ms"]["enrich"] == 180_000


def test_default_budget_scales_when_total_small() -> None:
    b = new_default_budget(60)  # 10%
    stats = b.stats()
    assert stats["reserved_by_stage_ms"]["acquire"] == 14_000


def test_errors_hierarchy() -> None:
    assert issubclass(RetryableError, SkillError)
    assert issubclass(FatalError, SkillError)
    try:
        raise RetryableError("quality_failed", reason="text_too_short", score=60)
    except RetryableError as e:
        assert e.payload["reason"] == "text_too_short"
        assert e.payload["score"] == 60
