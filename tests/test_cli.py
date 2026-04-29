"""CLI 单元测试（直接调用 main/_load_request，便于 coverage 统计）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from html2md_skill import __main__ as cli
from html2md_skill.core.types import SkillRequest


class DummyResponse:
    def __init__(self, status: str = "passed") -> None:
        self.status = status

    def model_dump(self) -> dict:
        return {"status": self.status}


def test_load_request_from_json_file(tmp_path: Path) -> None:
    req_file = tmp_path / "request.json"
    req_file.write_text(json.dumps({"url": "https://example.com/a"}), encoding="utf-8")
    args = cli.argparse.Namespace(
        request_file=str(req_file),
        url=None,
        output_dir=None,
        timeout_seconds=None,
        render_mode=None,
        table_mode=None,
        formula_mode=None,
        image_mode=None,
        no_quality_check=False,
        max_retry=None,
        no_references=False,
        no_metadata=False,
        debug=None,
        preserve_intermediate=False,
        idempotency_key=None,
    )
    req = cli._load_request(args)
    assert isinstance(req, SkillRequest)
    assert req.url == "https://example.com/a"


def test_load_request_from_cli_args() -> None:
    args = cli.argparse.Namespace(
        request_file=None,
        url="https://example.com/a",
        output_dir="/tmp/out",
        timeout_seconds=10,
        render_mode="static",
        table_mode="html",
        formula_mode="mathml",
        image_mode="both",
        no_quality_check=True,
        max_retry=0,
        no_references=True,
        no_metadata=True,
        debug="full",
        preserve_intermediate=True,
        idempotency_key="abc-123",
    )
    req = cli._load_request(args)
    assert req.output_dir == "/tmp/out"
    assert req.timeout_seconds == 10
    assert req.render_mode == "static"
    assert req.table_mode == "html"
    assert req.formula_mode == "mathml"
    assert req.image_mode == "both"
    assert req.quality_check is False
    assert req.max_retry == 0
    assert req.include_references is False
    assert req.include_metadata is False
    assert req.debug == "full"
    assert req.preserve_intermediate is True
    assert req.idempotency_key == "abc-123"


def test_load_request_requires_url() -> None:
    args = cli.argparse.Namespace(request_file=None, url=None)
    with pytest.raises(SystemExit):
        cli._load_request(args)


def test_main_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(req: SkillRequest, *, allow_file_scheme: bool = False) -> DummyResponse:
        assert req.url == "https://example.com/a"
        assert allow_file_scheme is False
        return DummyResponse("passed")

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["html2md-skill", "--url", "https://example.com/a"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert '"status": "passed"' in out


def test_main_degraded_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: DummyResponse("degraded"))
    monkeypatch.setattr(sys, "argv", ["html2md-skill", "--url", "https://example.com/a"])
    assert cli.main() == 2


def test_main_failed_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: DummyResponse("failed"))
    monkeypatch.setattr(sys, "argv", ["html2md-skill", "--url", "https://example.com/a"])
    assert cli.main() == 1
