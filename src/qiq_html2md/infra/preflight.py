"""运行时依赖预检（preflight）。

只做**只读检查**，不启动任何浏览器实例、不修改全局状态，可安全地在任意时刻调用。

目标依赖层
----------
- L1（硬依赖）：已在 `dependencies` 中声明，import 失败时 Python 会直接报错，
  不在本模块范围内。
- L2（效果增强）：
  - `playwright` 包是否可 import
  - Chromium 浏览器可执行文件是否已安装

返回结构
--------
`PreflightReport` 是一个 dataclass，含三个字段：
- `checks`: list[DepCheck]，每项记录 name / installed / hint
- `all_ok`: 所有 check 均通过
- `missing`: 未通过的 check 列表（便于快速判空）

用法
----
```python
from qiq_html2md.infra.preflight import check_runtime_deps, format_install_hints

report = check_runtime_deps()
if not report.all_ok:
    print(format_install_hints(report))
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class DepCheck:
    """单个依赖检查条目。"""

    name: str
    level: Literal["L1", "L2"]
    installed: bool
    detail: str = ""  # 成功/失败的说明
    install_hint: str = ""  # 安装指引（缺失时非空）


@dataclass(frozen=True)
class PreflightReport:
    checks: list[DepCheck] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(c.installed for c in self.checks)

    @property
    def missing(self) -> list[DepCheck]:
        return [c for c in self.checks if not c.installed]

    def to_dict(self) -> dict[str, object]:
        return {
            "all_ok": self.all_ok,
            "checks": [
                {
                    "name": c.name,
                    "level": c.level,
                    "installed": c.installed,
                    "detail": c.detail,
                    "install_hint": c.install_hint,
                }
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# 具体检查
# ---------------------------------------------------------------------------


def _check_playwright_package() -> DepCheck:
    try:
        import playwright  # noqa: F401
    except ImportError as e:
        return DepCheck(
            name="playwright",
            level="L2",
            installed=False,
            detail=f"import failed: {e}",
            install_hint=(
                "pip install 'qiq-html2md[recommended]'  "
                "# 或仅安装浏览器能力：pip install 'qiq-html2md[browser]'"
            ),
        )
    try:
        version = getattr(playwright, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        version = "unknown"
    return DepCheck(
        name="playwright",
        level="L2",
        installed=True,
        detail=f"version={version}",
    )


def _check_chromium_binary() -> DepCheck:
    """检查 Chromium 可执行文件是否存在。

    不启动浏览器，只通过 `playwright.sync_api.sync_playwright().chromium.executable_path`
    读取预期路径并检查文件是否存在。playwright 包缺失时直接给出 skip 结果。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return DepCheck(
            name="chromium",
            level="L2",
            installed=False,
            detail="playwright package not installed (skipped)",
            install_hint=(
                "先安装 playwright 包，然后执行：playwright install chromium"
            ),
        )

    try:
        with sync_playwright() as pw:
            exec_path = pw.chromium.executable_path
    except Exception as e:  # noqa: BLE001
        return DepCheck(
            name="chromium",
            level="L2",
            installed=False,
            detail=f"failed to query executable_path: {e}",
            install_hint="playwright install chromium",
        )

    if not exec_path:
        return DepCheck(
            name="chromium",
            level="L2",
            installed=False,
            detail="executable_path is empty",
            install_hint="playwright install chromium",
        )

    path = Path(exec_path)
    if not path.exists():
        return DepCheck(
            name="chromium",
            level="L2",
            installed=False,
            detail=f"executable not found at {exec_path}",
            install_hint="playwright install chromium",
        )

    return DepCheck(
        name="chromium",
        level="L2",
        installed=True,
        detail=f"executable at {exec_path}",
    )


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def check_runtime_deps() -> PreflightReport:
    """扫描 L2 运行时依赖，返回只读报告。

    L1 硬依赖已在 `dependencies` 中声明；若缺失，Python import 时会直接失败，
    不进入本函数。
    """
    checks: list[DepCheck] = [
        _check_playwright_package(),
        _check_chromium_binary(),
    ]
    return PreflightReport(checks=checks)


def format_install_hints(report: PreflightReport) -> str:
    """返回人类可读的安装指导文本。

    - 若 `report.all_ok`，返回简短 OK 摘要。
    - 否则列出每一项缺失的依赖与对应命令。
    """
    if report.all_ok:
        lines = ["[preflight] all optional runtime deps OK:"]
        for c in report.checks:
            lines.append(f"  - {c.name} ({c.level}): {c.detail}")
        return "\n".join(lines)

    lines = [
        "[preflight] 以下可选依赖缺失，部分增强功能将不可用：",
    ]
    for c in report.missing:
        lines.append(f"  - {c.name} ({c.level}): {c.detail}")
        if c.install_hint:
            lines.append(f"      修复：{c.install_hint}")
    lines.append("")
    lines.append("一键安装推荐依赖：")
    lines.append("  pip install 'qiq-html2md[recommended]'")
    lines.append("  playwright install chromium")
    return "\n".join(lines)
