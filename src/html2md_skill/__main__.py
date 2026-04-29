"""CLI 入口：python -m html2md_skill

用法：
  echo '{"url":"https://...","output_dir":"./output"}' | python -m html2md_skill -
  python -m html2md_skill request.json
  python -m html2md_skill --url https://... --output-dir ./output
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from html2md_skill.core.pipeline import run
from html2md_skill.core.types import SkillRequest


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

    payload: dict[str, Any] = {"url": args.url}
    if args.output_dir:
        payload["output_dir"] = args.output_dir
    if args.debug:
        payload["debug"] = args.debug
    return SkillRequest(**payload)


def main() -> int:
    parser = argparse.ArgumentParser(prog="html2md-skill", description="HTML → Markdown skill")
    parser.add_argument("request_file", nargs="?", help="JSON 请求文件；'-' 表示从 stdin 读取")
    parser.add_argument("--url", help="直接给 URL（不使用 JSON 文件）")
    parser.add_argument("--output-dir", default=None, help="输出目录")
    parser.add_argument("--debug", choices=["lite", "full"], default=None)
    parser.add_argument(
        "--allow-file-scheme",
        action="store_true",
        help="允许 file:// 协议（仅测试/离线 fixture）",
    )
    args = parser.parse_args()

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
