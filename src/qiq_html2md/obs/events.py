"""事件总线 + JSONL 落盘 + trace/span + Stage 快照 sink。

职责
----
- 为任务生成 trace_id（ULID）；Stage 进入时生成 span_id。
- 结构化事件流，写入 `<output_dir>/_diag/events.jsonl`。
- `stage.finished` 事件自动镜像到 `<output_dir>/_diag/stages/<stage>.json`。
- 保留最后 N 条事件（供 SkillResponse.events_tail）。
- 订阅/推送接口。

首版核心事件（6 种）：
  skill.started / skill.finished
  stage.started / stage.finished
  quality.scored / retry.planned

非核心事件自由使用，推荐在 payload 中携带 level。
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import ulid

from html2md_skill.core.types import Event
from html2md_skill.infra.fs_sandbox import FsSandbox

EVENT_TAIL_SIZE = 20
EventHandler = Callable[[Event], None]


def _now_iso() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class EventBus:
    """任务级事件总线。

    一个 skill 任务一个 EventBus 实例（通过 pipeline 持有）。
    """

    def __init__(self, trace_id: str, diag_dir: Path) -> None:
        self.trace_id = trace_id
        self._sandbox = FsSandbox(diag_dir)
        self._events_path = self._sandbox.resolve("events.jsonl")
        self._stages_dir = self._sandbox.mkdirp("stages")
        self._seq = 0
        self._current_span: str | None = None
        self._current_stage: str = "orchestrator"
        self._subscribers: list[EventHandler] = []
        self._tail: collections.deque[Event] = collections.deque(maxlen=EVENT_TAIL_SIZE)

    # ------- 订阅 -------

    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    # ------- 发送事件 -------

    def emit(self, name: str, payload: dict[str, Any] | None = None, *, stage: str | None = None) -> Event:
        self._seq += 1
        evt = Event(
            ts=_now_iso(),
            trace_id=self.trace_id,
            span_id=self._current_span,
            stage=stage or self._current_stage,
            seq=self._seq,
            name=name,
            payload=payload or {},
        )
        # 1. 落盘
        self._sandbox.append_line("events.jsonl", json.dumps(evt.model_dump(), ensure_ascii=False))
        # 2. 订阅者
        for h in self._subscribers:
            try:
                h(evt)
            except Exception:  # noqa: BLE001 订阅者异常不影响主流程
                pass
        # 3. 尾部缓冲
        self._tail.append(evt)
        # 4. Stage 快照 sink
        if name == "stage.finished":
            stage_name = evt.payload.get("stage") or evt.stage
            snapshot_path = self._stages_dir / f"{stage_name}.json"
            snapshot_path.write_text(
                json.dumps(evt.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return evt

    # ------- Stage 计时 span -------

    @contextmanager
    def span(self, stage: str) -> Iterator[str]:
        prev_span = self._current_span
        prev_stage = self._current_stage
        span_id = str(ulid.ULID())[:12]
        self._current_span = span_id
        self._current_stage = stage
        try:
            yield span_id
        finally:
            self._current_span = prev_span
            self._current_stage = prev_stage

    # ------- 导出 -------

    def tail(self) -> list[Event]:
        return list(self._tail)

    @property
    def events_path(self) -> Path:
        return self._events_path


def new_trace_id() -> str:
    return str(ulid.ULID())


# ---------------------------------------------------------------------------
# TRACE.md 自动生成
# ---------------------------------------------------------------------------


def write_trace_md(bus: EventBus, path: Path | None = None) -> Path:
    """将事件流生成人类可读的时间轴 Markdown。

    读取完整 `events.jsonl`，输出到 `_diag/TRACE.md`。
    """
    events_path = bus.events_path
    out_path = path or events_path.parent / "TRACE.md"

    lines: list[str] = [f"# Trace {bus.trace_id}", ""]
    if not events_path.is_file():
        lines.append("_(no events)_")
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out_path

    lines.append("## Timeline")
    lines.append("")
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = evt.get("ts", "")
        hms = ts[11:23] if len(ts) >= 23 else ts
        name = evt.get("name", "")
        stage = evt.get("stage", "")
        payload = evt.get("payload") or {}
        hint = _format_payload_hint(name, payload)
        lines.append(f"- `{hms}`  `{name:<18}` `{stage:<14}` {hint}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def _format_payload_hint(name: str, payload: dict[str, Any]) -> str:
    if name == "skill.started":
        return f"url={payload.get('url')} render={payload.get('render_mode')}"
    if name == "skill.finished":
        parts = [f"status={payload.get('status')}", f"retries={payload.get('retries')}"]
        err = payload.get("error")
        if err:
            parts.append(f"error={str(err)[:60]}")
        return " ".join(parts)
    if name == "stage.started":
        return ""
    if name == "stage.finished":
        dur = payload.get("duration_ms")
        err = payload.get("error")
        if err:
            return f"ERROR dur={dur}ms reason={payload.get('reason')}"
        return f"dur={dur}ms"
    if name == "quality.scored":
        return f"score={payload.get('final_score')} passed={payload.get('passed')}"
    if name == "retry.planned":
        return f"reason={payload.get('reason')} target={payload.get('target_stage')}"
    if name == "warning.raised":
        return f"code={payload.get('code')}"
    short = {k: v for k, v in payload.items() if k in ("code", "url", "final_url", "detail")}
    if short:
        return str(short)
    return ""
