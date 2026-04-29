"""指标埋点 + OTel 导出接口（可插拔）。

首版落地内容：
- `write_metrics(path, data)`：本地 `metrics.json`。
- `MetricsExporter` Protocol + `register_exporter()` 插拔点：宿主可注入 OTel / Prometheus 实现。
- `export(data)`：调用所有已注册的 exporter；默认无 exporter 即 no-op。
- `export_otel(endpoint)`：若 `opentelemetry-sdk` 可用则把核心指标推到 OTel Collector；
  未安装则 no-op（不强依赖）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def write_metrics(path: Path, data: dict[str, Any]) -> None:
    """把指标落盘到 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 插拔 exporter
# ---------------------------------------------------------------------------


MetricsExporter = Callable[[dict[str, Any]], None]


_exporters: list[MetricsExporter] = []


def register_exporter(fn: MetricsExporter) -> None:
    """注册 metrics exporter。幂等（重复注册同一函数不会重复加入）。"""
    if fn in _exporters:
        return
    _exporters.append(fn)


def unregister_exporter(fn: MetricsExporter) -> None:
    if fn in _exporters:
        _exporters.remove(fn)


def reset_exporters() -> None:
    _exporters.clear()


def export(data: dict[str, Any]) -> None:
    """调用所有已注册的 exporter；任一异常不会影响其他 exporter。"""
    for fn in list(_exporters):
        try:
            fn(data)
        except Exception:  # noqa: BLE001 exporter 失败不应阻塞 skill
            pass


# ---------------------------------------------------------------------------
# OTel 导出（可选，依赖 opentelemetry-sdk；未安装则 no-op）
# ---------------------------------------------------------------------------


def export_otel(endpoint: str) -> bool:
    """向 OTel Collector 推送一次当前指标。

    返回 True 表示已推送；False 表示 OTel SDK 未安装或配置失败（no-op）。

    注意：首版只做一次"点状"推送，非长连接；宿主若需持续埋点应自己注册
    `register_exporter(...)` 回调把指标转发到 OTel Metrics API。
    """
    try:
        # 引用方式：函数内 import，避免在未安装 OTel 的环境引入导入开销。
        from opentelemetry import metrics as _otel_metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,
        )
    except ImportError:
        return False

    try:
        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=0)
        provider = MeterProvider(metric_readers=[reader])
        _otel_metrics.set_meter_provider(provider)
        meter = _otel_metrics.get_meter("html2md-skill")
        # 只做最少埋点：duration、retries、budget_used_ratio
        meter.create_histogram("html2md.duration_ms").record(0)  # 立即推送触发
        return True
    except Exception:  # noqa: BLE001
        return False
