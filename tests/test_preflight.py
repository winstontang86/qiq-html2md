"""依赖预检（preflight）的测试：覆盖 4 种场景。"""

from __future__ import annotations

import sys
from unittest import mock

import pytest

from qiq_html2md.infra import preflight


def _fake_playwright_module(chromium_path: str | None):
    """构造一个伪 playwright 模块，让 `from playwright.sync_api import sync_playwright` 可用。

    `chromium_path`:
    - None → `executable_path` 返回空字符串（表示缺失）
    - 字符串 → 返回该路径
    """

    class FakeChromium:
        def __init__(self, path: str | None) -> None:
            self.executable_path = path or ""

    class FakePlaywright:
        def __init__(self, path: str | None) -> None:
            self.chromium = FakeChromium(path)

    class FakeCtxMgr:
        def __init__(self, path: str | None) -> None:
            self.path = path

        def __enter__(self) -> FakePlaywright:
            return FakePlaywright(self.path)

        def __exit__(self, *a: object) -> None:
            return None

    def sync_playwright() -> FakeCtxMgr:
        return FakeCtxMgr(chromium_path)

    sync_api = mock.MagicMock()
    sync_api.sync_playwright = sync_playwright
    playwright_mod = mock.MagicMock()
    playwright_mod.__version__ = "1.99.0"
    playwright_mod.sync_api = sync_api
    return playwright_mod, sync_api


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, chromium_path: str | None) -> None:
    pw_mod, sync_api = _fake_playwright_module(chromium_path)
    monkeypatch.setitem(sys.modules, "playwright", pw_mod)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def _uninstall_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    # 强制 import 失败
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


# ---------------------------------------------------------------------------
# 场景 1：playwright 未安装
# ---------------------------------------------------------------------------


def test_preflight_playwright_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _uninstall_playwright(monkeypatch)
    report = preflight.check_runtime_deps()
    assert not report.all_ok
    names = {c.name for c in report.missing}
    assert "playwright" in names
    assert "chromium" in names
    text = preflight.format_install_hints(report)
    assert "playwright" in text
    assert "recommended" in text or "browser" in text
    assert "playwright install chromium" in text


# ---------------------------------------------------------------------------
# 场景 2：playwright 装了，但 chromium 缺失（路径为空）
# ---------------------------------------------------------------------------


def test_preflight_chromium_missing_empty_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_playwright(monkeypatch, chromium_path=None)
    report = preflight.check_runtime_deps()
    pw_check = next(c for c in report.checks if c.name == "playwright")
    ch_check = next(c for c in report.checks if c.name == "chromium")
    assert pw_check.installed is True
    assert ch_check.installed is False
    assert "executable_path is empty" in ch_check.detail
    assert not report.all_ok


# ---------------------------------------------------------------------------
# 场景 3：playwright 装了，路径指向不存在的文件
# ---------------------------------------------------------------------------


def test_preflight_chromium_missing_path_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    fake_path = "/definitely/does/not/exist/chromium"
    _install_fake_playwright(monkeypatch, chromium_path=fake_path)
    report = preflight.check_runtime_deps()
    ch = next(c for c in report.checks if c.name == "chromium")
    assert ch.installed is False
    assert "executable not found" in ch.detail


# ---------------------------------------------------------------------------
# 场景 4：全齐
# ---------------------------------------------------------------------------


def test_preflight_all_ok(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_chromium = tmp_path / "chromium"
    fake_chromium.write_text("#!/bin/sh\n", encoding="utf-8")
    _install_fake_playwright(monkeypatch, chromium_path=str(fake_chromium))
    report = preflight.check_runtime_deps()
    assert report.all_ok
    assert not report.missing
    text = preflight.format_install_hints(report)
    assert "all optional runtime deps OK" in text


# ---------------------------------------------------------------------------
# 场景 5：序列化为 dict（给 _diag 用）
# ---------------------------------------------------------------------------


def test_preflight_report_to_dict(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_chromium = tmp_path / "chromium"
    fake_chromium.write_text("x", encoding="utf-8")
    _install_fake_playwright(monkeypatch, chromium_path=str(fake_chromium))
    report = preflight.check_runtime_deps()
    data = report.to_dict()
    assert data["all_ok"] is True
    assert isinstance(data["checks"], list)
    assert all("name" in c and "level" in c for c in data["checks"])


# ---------------------------------------------------------------------------
# CLI：--check-deps 子功能
# ---------------------------------------------------------------------------


def test_cli_check_deps_all_ok(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_chromium = tmp_path / "chromium"
    fake_chromium.write_text("x", encoding="utf-8")
    _install_fake_playwright(monkeypatch, chromium_path=str(fake_chromium))
    monkeypatch.setattr(sys, "argv", ["qiq-html2md", "--check-deps"])
    from qiq_html2md import __main__ as cli

    rc = cli.main()
    assert rc == 0


def test_cli_check_deps_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _uninstall_playwright(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["qiq-html2md", "--check-deps"])
    from qiq_html2md import __main__ as cli

    rc = cli.main()
    assert rc == 1


def test_cli_strict_deps_blocks_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """--strict-deps 且依赖缺失时，不应该启动 pipeline，直接返回退出码 2。"""
    _uninstall_playwright(monkeypatch)

    called = {"run": False}

    def fake_run(*a: object, **kw: object) -> object:
        called["run"] = True
        raise AssertionError("pipeline should not be invoked when strict-deps blocks")

    from qiq_html2md import __main__ as cli

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["qiq-html2md", "--url", "https://example.com/a", "--strict-deps"],
    )
    rc = cli.main()
    assert rc == 2
    assert called["run"] is False


def test_cli_default_warn_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 CLI 在依赖缺失时仅 warn，不阻塞 pipeline。"""
    _uninstall_playwright(monkeypatch)

    class DummyResp:
        status = "passed"

        def model_dump(self) -> dict:
            return {"status": "passed"}

    from qiq_html2md import __main__ as cli

    monkeypatch.setattr(cli, "run", lambda *a, **kw: DummyResp())
    monkeypatch.setattr(sys, "argv", ["qiq-html2md", "--url", "https://example.com/a"])
    rc = cli.main()
    assert rc == 0
