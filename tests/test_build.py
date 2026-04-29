"""构建工具测试。"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from qiq_html2md import build as build_mod


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def clean_dist(tmp_path: Path) -> Path:
    """隔离 dist 输出目录，避免污染仓库 dist/。"""
    return tmp_path / "dist"


def test_build_produces_zip(clean_dist: Path) -> None:
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=False, with_docs=True)
    assert zip_path.is_file()
    assert zip_path.name.startswith("qiq-html2md-")
    assert zip_path.suffix == ".zip"
    assert zip_path.stat().st_size > 10_000


def test_zip_contains_required_files(clean_dist: Path) -> None:
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=False, with_docs=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    top = None
    for n in names:
        top = n.split("/", 1)[0]
        break
    assert top is not None

    required = [
        f"{top}/SKILL.md",
        f"{top}/manifest.yaml",
        f"{top}/requirements.txt",
        f"{top}/dist_info.json",
        f"{top}/schemas/request.schema.json",
        f"{top}/schemas/response.schema.json",
        # build.py 故意把 src/qiq_html2md/ 平铺到顶层 qiq_html2md/，
        # 便于宿主直接 `python -m qiq_html2md`（不需要 PYTHONPATH=src）
        f"{top}/qiq_html2md/__main__.py",
        f"{top}/qiq_html2md/core/pipeline.py",
        f"{top}/qiq_html2md/stages/acquire.py",
    ]
    for rq in required:
        assert rq in names, f"missing: {rq}"


def test_zip_excludes_caches_and_tests_by_default(clean_dist: Path) -> None:
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=False, with_docs=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)
    assert not any("/tests/" in n for n in names)
    assert not any("/.venv/" in n for n in names)


def test_with_tests_includes_tests(clean_dist: Path) -> None:
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=True, with_docs=False)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any(n.endswith("tests/test_mvp_smoke.py") for n in names)
    assert not any("/docs/" in n for n in names)


def test_dist_info_contents(clean_dist: Path) -> None:
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=False, with_docs=False)
    with zipfile.ZipFile(zip_path) as zf:
        top = zf.namelist()[0].split("/", 1)[0]
        data = json.loads(zf.read(f"{top}/dist_info.json"))

    assert data["name"] == "qiq-html2md"
    assert data["version"] == build_mod._read_version()
    assert data["entrypoint"] == "python -m qiq_html2md"
    assert data["skill_manifest"] == "manifest.yaml"
    assert data["file_count"] >= 20
    assert data["python_requires"].startswith(">=")
    # 每个 files 项含 path / size / sha256
    for item in data["files"]:
        assert "path" in item
        assert isinstance(item["size"], int) and item["size"] >= 0
        assert len(item["sha256"]) == 64


def test_sha256_matches_actual_bytes(clean_dist: Path) -> None:
    """dist_info.json 中的 sha256 必须与 zip 内实际文件匹配。"""
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=False, with_docs=False)
    with zipfile.ZipFile(zip_path) as zf:
        top = zf.namelist()[0].split("/", 1)[0]
        data = json.loads(zf.read(f"{top}/dist_info.json"))
        for item in data["files"]:
            if item["path"] == "dist_info.json":
                continue
            body = zf.read(f"{top}/{item['path']}")
            assert hashlib.sha256(body).hexdigest() == item["sha256"], item["path"]


def test_reproducible_with_source_date_epoch(
    clean_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """固定 SOURCE_DATE_EPOCH → 两次构建 zip 字节级一致。"""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    out_a = build_mod.build(output_dir=clean_dist / "a", with_tests=False, with_docs=False)
    out_b = build_mod.build(output_dir=clean_dist / "b", with_tests=False, with_docs=False)
    assert _sha(out_a) == _sha(out_b)


def test_build_accepts_explicit_project_root(clean_dist: Path) -> None:
    zip_path = build_mod.build(
        output_dir=clean_dist,
        with_tests=False,
        with_docs=False,
        project_root=build_mod.PROJECT_ROOT,
    )
    assert zip_path.is_file()


def test_requirements_txt_has_base_deps(clean_dist: Path) -> None:
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=False, with_docs=False)
    with zipfile.ZipFile(zip_path) as zf:
        top = zf.namelist()[0].split("/", 1)[0]
        req = zf.read(f"{top}/requirements.txt").decode()
    assert "httpx" in req
    assert "pydantic" in req
    # v0.3.0：playwright 已提升为硬依赖，直接出现在 deps 列表中（不再注释）
    assert "playwright" in req
    # 仍应提醒用户下载 chromium 二进制
    assert "playwright install chromium" in req


def test_package_is_self_contained_and_runnable(
    clean_dist: Path, tmp_path: Path
) -> None:
    """解压 zip 到空目录，直接就能跑 smoke（无需 PYTHONPATH）。"""
    zip_path = build_mod.build(output_dir=clean_dist, with_tests=False, with_docs=False)

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(install_dir)
    top = next(install_dir.iterdir())  # <name>-<version>/

    # 关键点：qiq_html2md/ 顶层布局（build.py 会剥掉 src/ 前缀）
    assert (top / "qiq_html2md" / "__main__.py").is_file()

    import subprocess
    import sys

    fixture = Path(__file__).parent / "fixtures" / "arxiv_sample.html"
    out_dir = tmp_path / "out"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(top) + os.pathsep + env.get("PYTHONPATH", "")
    env["QIQ_HTML2MD_CACHE_DIR"] = str(tmp_path / "cache")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "qiq_html2md",
            "--skip-deps-check",
            "--allow-file-scheme",
            "--url",
            f"file://{fixture}",
            "--output-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert (out_dir / "article.md").is_file()
    resp = json.loads(result.stdout)
    assert resp["status"] == "passed"
