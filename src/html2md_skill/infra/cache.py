"""两级缓存：HTTP 响应缓存 + 抽取结果指纹缓存。

布局
----
<cache_dir>/
  http/
    <sha(url)>.json      # {etag, last_modified, final_url, status, headers, content_b64, ts}
  extract/
    <sha(key)>.json      # Extract 产出的 dict 快照

路径
----
默认 `<XDG_CACHE_HOME|~/.cache>/html2md-skill`；可通过环境变量 `HTML2MD_SKILL_CACHE_DIR` 覆盖。

设计
----
- HTTP 缓存只是"条件请求辅助"，不强制用；`get_http(url)` 返回 `HttpCacheEntry | None`，
  调用方决定是否带条件头。
- 抽取级缓存 key 由 `make_extract_key(url, strategy, adapter_version)` 稳定计算。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _default_cache_dir() -> Path:
    env = os.environ.get("HTML2MD_SKILL_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "html2md-skill").resolve()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# HTTP 缓存
# ---------------------------------------------------------------------------


@dataclass
class HttpCacheEntry:
    url: str
    final_url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    stored_at: float = 0.0

    def conditional_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.etag:
            h["If-None-Match"] = self.etag
        if self.last_modified:
            h["If-Modified-Since"] = self.last_modified
        return h


def _http_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / "http" / f"{_sha(url)}.json"


def get_http(url: str, cache_dir: Path | None = None) -> HttpCacheEntry | None:
    cache_dir = cache_dir or _default_cache_dir()
    p = _http_path(cache_dir, url)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return HttpCacheEntry(
            url=raw["url"],
            final_url=raw["final_url"],
            status=int(raw["status"]),
            headers=dict(raw.get("headers", {})),
            content=base64.b64decode(raw.get("content_b64", "")),
            etag=raw.get("etag"),
            last_modified=raw.get("last_modified"),
            stored_at=float(raw.get("stored_at", 0.0)),
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def put_http(entry: HttpCacheEntry, cache_dir: Path | None = None) -> None:
    cache_dir = cache_dir or _default_cache_dir()
    p = _http_path(cache_dir, entry.url)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": entry.url,
        "final_url": entry.final_url,
        "status": entry.status,
        "headers": entry.headers,
        "content_b64": base64.b64encode(entry.content).decode("ascii"),
        "etag": entry.etag,
        "last_modified": entry.last_modified,
        "stored_at": entry.stored_at or time.time(),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def invalidate_http(url: str, cache_dir: Path | None = None) -> None:
    p = _http_path(cache_dir or _default_cache_dir(), url)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 抽取结果缓存
# ---------------------------------------------------------------------------


def make_extract_key(
    url: str,
    *,
    render_mode: str,
    adapter_version: str = "v1",
    extractor_profile: str = "adapter",
) -> str:
    raw = f"{url}|rm={render_mode}|ad={adapter_version}|prof={extractor_profile}"
    return _sha(raw)


def _extract_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / "extract" / f"{key}.json"


def get_extract(key: str, cache_dir: Path | None = None) -> dict[str, Any] | None:
    cache_dir = cache_dir or _default_cache_dir()
    p = _extract_path(cache_dir, key)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def put_extract(key: str, payload: dict[str, Any], cache_dir: Path | None = None) -> None:
    cache_dir = cache_dir or _default_cache_dir()
    p = _extract_path(cache_dir, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def invalidate_extract(key: str, cache_dir: Path | None = None) -> None:
    p = _extract_path(cache_dir or _default_cache_dir(), key)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 全局控制开关
# ---------------------------------------------------------------------------


_DISABLED = False


def set_enabled(enabled: bool) -> None:
    global _DISABLED
    _DISABLED = not enabled


def enabled() -> bool:
    return not _DISABLED


def cache_dir() -> Path:
    return _default_cache_dir()
