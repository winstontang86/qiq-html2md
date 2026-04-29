"""全局 pytest 配置：隔离缓存目录，避免测试间污染。"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 cache dir 指向临时路径，确保每次运行互不干扰。"""
    cache_root = tmp_path_factory.mktemp("h2m_cache")
    monkeypatch.setenv("HTML2MD_SKILL_CACHE_DIR", str(cache_root))
    os.environ["HTML2MD_SKILL_CACHE_DIR"] = str(cache_root)
