"""构建 skill 分发 zip 包。

用法
----
    python -m html2md_skill.build [--output dist/] [--with-tests] [--no-docs] [--name html2md]

产物
----
    dist/<name>-<version>.zip

包内结构（顶层目录 = `<name>-<version>/`）::

    <name>-<version>/
      SKILL.md                     # 宿主发现入口
      manifest.yaml                # 机读声明
      README.md
      LICENSE
      requirements.txt             # 运行时最小依赖（browser/otel 作为 extras 注释）
      dist_info.json               # 版本 / 构建时间 / SHA-256 清单
      schemas/
        request.schema.json
        response.schema.json
      src/html2md_skill/**         # 全部源码（不含 __pycache__）
      docs/**                      # 默认包含，--no-docs 关闭
      tests/**                     # --with-tests 开启时

设计原则
--------
- **自包含**：解压后直接 `python -m html2md_skill --url ...` 就能跑。
- **可复现**：ZIP 按文件名排序、mtime 固定为 1980-01-01、压缩级别固定。
  `SOURCE_DATE_EPOCH` 环境变量可覆盖 built_at（标准 reproducible-builds 实践）。
- **可校验**：`dist_info.json` 带每个文件的 SHA-256；宿主可重算比对。
- **瘦身**：默认不带 `tests/`、`.venv/` 与所有缓存。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import tomllib

# --------------------------- 路径工具 ---------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "dist"

# 永远排除的目录 / 后缀
_EXCLUDE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    ".git",
    ".codebuddy",
    ".workbuddy",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "output",
    "_diag",
}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".log"}
_EXCLUDE_NAMES = {".DS_Store", "Thumbs.db", ".gitignore"}


def _walk_files(root: Path) -> Iterable[Path]:
    """遍历 root 下所有文件，跳过 `_EXCLUDE_*`。"""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in _EXCLUDE_DIRS for part in rel_parts):
            continue
        if p.suffix in _EXCLUDE_SUFFIXES:
            continue
        if p.name in _EXCLUDE_NAMES:
            continue
        yield p


def _read_version(project_root: Path = PROJECT_ROOT) -> str:
    pyproject = project_root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _built_at_iso() -> str:
    """返回构建时间 ISO 字符串。

    优先读取环境变量 `SOURCE_DATE_EPOCH`（reproducible-builds 标准），
    未设置时使用当前 UTC 时间。
    """
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    if sde:
        try:
            ts = int(sde)
            return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except ValueError:
            pass
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------- requirements.txt 生成 ---------------------------


def _build_requirements_txt(project_root: Path = PROJECT_ROOT) -> str:
    """从 pyproject.toml 读依赖，生成安装方友好的 requirements.txt。"""
    pyproject = project_root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps: list[str] = list(data["project"].get("dependencies", []))
    extras = data["project"].get("optional-dependencies", {})
    browser_deps: list[str] = list(extras.get("browser", []))

    lines = ["# 运行时最小依赖（由构建工具从 pyproject.toml 生成）"]
    for d in deps:
        lines.append(d)
    if browser_deps:
        lines.append("")
        lines.append("# 可选：浏览器渲染（render_mode=browser 时需要）")
        lines.append("#   需要额外 `playwright install chromium`")
        for d in browser_deps:
            lines.append(f"# {d}")
    lines.append("")
    return "\n".join(lines)


# --------------------------- 主流程 ---------------------------


def _assemble_content_list(
    *,
    project_root: Path = PROJECT_ROOT,
    with_tests: bool,
    with_docs: bool,
) -> list[tuple[Path, str]]:
    """返回 (源文件绝对路径, zip 内相对路径) 列表。"""
    items: list[tuple[Path, str]] = []

    # 必选文件
    roots = {
        "SKILL.md": project_root / "SKILL.md",
        "manifest.yaml": project_root / "manifest.yaml",
        "README.md": project_root / "README.md",
        "LICENSE": project_root / "LICENSE",
        "pyproject.toml": project_root / "pyproject.toml",
    }
    for dest, src in roots.items():
        if src.is_file():
            items.append((src, dest))

    # schemas/
    for p in _walk_files(project_root / "schemas"):
        items.append((p, str(Path("schemas") / p.relative_to(project_root / "schemas"))))

    # src/
    for p in _walk_files(project_root / "src"):
        items.append((p, str(Path("src") / p.relative_to(project_root / "src"))))

    if with_docs:
        docs_root = project_root / "docs"
        if docs_root.is_dir():
            for p in _walk_files(docs_root):
                items.append((p, str(Path("docs") / p.relative_to(docs_root))))

    if with_tests:
        tests_root = project_root / "tests"
        if tests_root.is_dir():
            for p in _walk_files(tests_root):
                items.append((p, str(Path("tests") / p.relative_to(tests_root))))

    # 稳定排序
    items.sort(key=lambda x: x[1])
    return items


def build(
    *,
    output_dir: Path | None = None,
    with_tests: bool = False,
    with_docs: bool = True,
    name: str = "html2md-skill",
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """执行构建，返回 zip 文件绝对路径。"""
    project_root = project_root.resolve()
    version = _read_version(project_root)
    output_dir = (output_dir or (project_root / "dist")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    top_level = f"{name}-{version}"
    zip_name = f"{top_level}.zip"
    zip_path = output_dir / zip_name

    items = _assemble_content_list(
        project_root=project_root,
        with_tests=with_tests,
        with_docs=with_docs,
    )

    # requirements.txt 是衍生产物 —— 写到临时文件后加入 zip
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        req_path = tmp_path / "requirements.txt"
        req_path.write_text(_build_requirements_txt(project_root), encoding="utf-8")
        items.append((req_path, "requirements.txt"))

        # 计算 dist_info（不含 dist_info.json 自身）
        manifest_entries: list[dict[str, Any]] = []
        for src, dest in sorted(items, key=lambda x: x[1]):
            manifest_entries.append(
                {
                    "path": dest,
                    "size": src.stat().st_size,
                    "sha256": _sha256(src),
                }
            )

        dist_info = {
            "name": name,
            "version": version,
            "built_at": _built_at_iso(),
            "includes_tests": with_tests,
            "includes_docs": with_docs,
            "python_requires": ">=3.10",
            "entrypoint": "python -m html2md_skill",
            "skill_manifest": "manifest.yaml",
            "file_count": len(manifest_entries),
            "files": manifest_entries,
        }
        dist_info_path = tmp_path / "dist_info.json"
        dist_info_path.write_text(
            json.dumps(dist_info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        items.append((dist_info_path, "dist_info.json"))

        # 重新按 dest 排序以确保 zip 顺序稳定
        items.sort(key=lambda x: x[1])

        # 确定性 ZIP：固定 mtime 为 1980-01-01
        fixed_time = (1980, 1, 1, 0, 0, 0)

        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(
            zip_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zf:
            for src, dest in items:
                arcname = f"{top_level}/{dest}"
                info = zipfile.ZipInfo(arcname, fixed_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with src.open("rb") as f:
                    zf.writestr(info, f.read())

    return zip_path


# --------------------------- CLI ---------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="html2md_skill.build",
        description="构建 html2md skill 的可安装 zip 分发包",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"输出目录（默认 {DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--with-tests",
        action="store_true",
        help="包含 tests/ 目录（默认不包含）",
    )
    parser.add_argument(
        "--no-docs",
        action="store_true",
        help="不包含 docs/（默认包含）",
    )
    parser.add_argument(
        "--name",
        default="html2md-skill",
        help="包名前缀（默认 html2md-skill）",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help=f"项目根目录（默认 {PROJECT_ROOT}）",
    )
    args = parser.parse_args(argv)

    zip_path = build(
        output_dir=args.output,
        with_tests=args.with_tests,
        with_docs=not args.no_docs,
        name=args.name,
        project_root=args.project_root,
    )
    size_kb = zip_path.stat().st_size / 1024
    print(f"built: {zip_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
