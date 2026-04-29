from __future__ import annotations

import json
from pathlib import Path

import pytest

from html2md_skill.core.errors import FatalError
from html2md_skill.infra.fs_sandbox import FsSandbox
from html2md_skill.obs.events import EventBus, new_trace_id


def test_sandbox_write_and_escape(tmp_path: Path) -> None:
    sb = FsSandbox(tmp_path / "out")
    sb.write_text("a/b.txt", "hi")
    assert (tmp_path / "out/a/b.txt").read_text() == "hi"
    with pytest.raises(FatalError):
        sb.resolve("../../etc/passwd")


def test_sandbox_symlink_denied(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    # 制造符号链接子目录
    bad = out / "link"
    bad.symlink_to(tmp_path / "other")
    (tmp_path / "other").mkdir()
    sb = FsSandbox(out)
    with pytest.raises(FatalError):
        sb.write_text("link/x.txt", "nope")


def test_event_bus_core_events(tmp_path: Path) -> None:
    trace = new_trace_id()
    bus = EventBus(trace_id=trace, diag_dir=tmp_path / "_diag")

    bus.emit("skill.started", {"url": "https://example.com"})

    with bus.span("acquire"):
        bus.emit("stage.started", {"stage": "acquire"})
        bus.emit("stage.finished", {"stage": "acquire", "duration_ms": 10})

    bus.emit("skill.finished", {"status": "passed"})

    # events.jsonl 至少 4 行
    lines = (tmp_path / "_diag/events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 4
    first = json.loads(lines[0])
    assert first["name"] == "skill.started"
    assert first["trace_id"] == trace
    assert first["seq"] == 1

    # stage snapshot 落盘
    snap = (tmp_path / "_diag/stages/acquire.json").read_text()
    assert "stage.finished" in snap


def test_event_bus_tail(tmp_path: Path) -> None:
    bus = EventBus(trace_id="t", diag_dir=tmp_path / "_diag")
    for i in range(30):
        bus.emit("skill.progress", {"i": i})
    tail = bus.tail()
    assert len(tail) == 20
    assert tail[-1].payload["i"] == 29
