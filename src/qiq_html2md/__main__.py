"""CLI 入口：python -m qiq_html2md。

支持两类输入：
- JSON 请求文件 / stdin：`python -m qiq_html2md request.json` 或 `... -`
- 命令行参数：`python -m qiq_html2md --url ... --output-dir ...`

另支持独立子功能：
- `--check-deps`：仅运行依赖预检并打印报告，退出码 0=全齐，1=缺失。
- 默认行为：正常运行前做预检，缺失直接退出码 2（**strict 已是默认**）。
- `--skip-deps-check`：跳过启动时预检（仅适合 CI 明确已装好的场景）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from qiq_html2md.core.pipeline import run
from qiq_html2md.core.types import SkillRequest
from qiq_html2md.infra.preflight import check_runtime_deps, format_install_hints


def _load_request(args: argparse.Namespace) -> SkillRequest:
    if args.request_file:
        if args.request_file == "-":
            data = json.load(sys.stdin)
        else:
            with open(args.request_file, encoding="utf-8") as f:
                data = json.load(f)
        return SkillRequest(**data)

    if not args.url:
        raise SystemExit("error: 必须提供 --url 或 request 文件")

    payload: dict[str, Any] = {
        "url": args.url,
        "output_dir": args.output_dir,
        "timeout_seconds": args.timeout_seconds,
        "render_mode": args.render_mode,
        "table_mode": args.table_mode,
        "formula_mode": args.formula_mode,
        "image_mode": args.image_mode,
        "quality_check": not args.no_quality_check,
        "max_retry": args.max_retry,
        "include_references": not args.no_references,
        "include_metadata": not args.no_metadata,
        "debug": args.debug,
        "preserve_intermediate": args.preserve_intermediate,
        "idempotency_key": args.idempotency_key,
    }
    # 去掉 None，保留 False/0 等显式值
    payload = {k: v for k, v in payload.items() if v is not None}
    return SkillRequest(**payload)


def _run_preflight(*, quiet: bool = False) -> int:
    """运行依赖预检。

    返回值：0=全齐；2=缺失（强制退出码）。
    quiet=True 时全齐不打印；缺失场景永远打印。
    """
    report = check_runtime_deps()
    if report.all_ok:
        if not quiet:
            print(format_install_hints(report), file=sys.stderr)
        return 0
    # 有缺失 —— v0.3.0 起 strict 是默认，直接返回 2
    print(format_install_hints(report), file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="qiq-html2md", description="HTML → Markdown skill")
    parser.add_argument("request_file", nargs="?", help="JSON 请求文件；'-' 表示从 stdin 读取")
    parser.add_argument("--url", help="直接给 URL（不使用 JSON 文件）")
    parser.add_argument("--output-dir", default=None, help="输出目录")
    parser.add_argument("--timeout-seconds", type=int, default=None, help="总超时秒数，1-600")
    parser.add_argument("--render-mode", choices=["auto", "static", "browser"], default=None)
    parser.add_argument("--table-mode", choices=["auto", "markdown", "html", "image"], default=None)
    parser.add_argument("--formula-mode", choices=["auto", "latex", "mathml", "image"], default=None)
    parser.add_argument("--image-mode", choices=["download", "link", "both"], default=None)
    parser.add_argument("--no-quality-check", action="store_true", help="跳过质量评分与重试")
    parser.add_argument("--max-retry", type=int, default=None, help="最大局部重试次数，0-5")
    parser.add_argument("--no-references", action="store_true", help="不输出参考文献")
    parser.add_argument("--no-metadata", action="store_true", help="不写 metadata.json")
    parser.add_argument("--debug", choices=["lite", "full"], default=None)
    parser.add_argument("--preserve-intermediate", action="store_true", help="保留中间产物到 _diag/intermediate/")
    parser.add_argument("--idempotency-key", default=None, help="幂等键；输出到 output_dir/<key>/")
    parser.add_argument(
        "--allow-file-scheme",
        action="store_true",
        help="允许 file:// 协议（仅测试/离线 fixture）",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="仅运行依赖预检并打印报告，不执行转换任务",
    )
    parser.add_argument(
        "--skip-deps-check",
        action="store_true",
        help="跳过启动时的依赖预检（仅适合 CI 明确已装好的场景）",
    )
    args = parser.parse_args()

    # 子功能：仅预检
    if args.check_deps:
        code = _run_preflight()
        # --check-deps 统一退出语义：0=全齐，1=缺失
        return 0 if code == 0 else 1

    # 正常流程：默认 strict 预检
    if not args.skip_deps_check:
        pre_code = _run_preflight(quiet=True)
        if pre_code != 0:
            return 2

    req = _load_request(args)
    resp = run(req, allow_file_scheme=args.allow_file_scheme)

    print(json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))
    if resp.status == "passed":
        return 0
    if resp.status == "degraded":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
