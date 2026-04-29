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
默认 `<XDG_CACHE_HOME|~/.cache>/qiq-html2md`；可通过环境变量 `QIQ_HTML2MD_CACHE_DIR` 覆盖。

设计
----
- HTTP 缓存只是"条件请求辅助"，不强制用；`get_http(url)` 返回 `HttpCacheEntry | None`，
  调用方决定是否带条件头。
- 抽取级缓存 key 由 `make_extract_key(url, strategy, adapter_version)` 稳定计算。
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _default_cache_dir() -> Path:
    env = os.environ.get("QIQ_HTML2MD_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "qiq-html2md").resolve()


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
    expires_at: float | None = None
    vary_key: str | None = None

    def is_fresh(self, now: float | None = None) -> bool:
        now = now or time.time()
        return self.expires_at is not None and now < self.expires_at

    def conditional_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.etag:
            h["If-None-Match"] = self.etag
        if self.last_modified:
            h["If-Modified-Since"] = self.last_modified
        return h


def _http_key(url: str, vary_key: str | None = None) -> str:
    return _sha(f"{url}|vary={vary_key or ''}")


def _http_path(cache_dir: Path, url: str, vary_key: str | None = None) -> Path:
    return cache_dir / "http" / f"{_http_key(url, vary_key)}.json"


def get_http(
    url: str,
    cache_dir: Path | None = None,
    *,
    vary_key: str | None = None,
) -> HttpCacheEntry | None:
    cache_dir = cache_dir or _default_cache_dir()
    p = _http_path(cache_dir, url, vary_key)
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
            expires_at=raw.get("expires_at"),
            vary_key=raw.get("vary_key"),
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def put_http(entry: HttpCacheEntry, cache_dir: Path | None = None) -> None:
    cache_dir = cache_dir or _default_cache_dir()
    p = _http_path(cache_dir, entry.url, entry.vary_key)
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
        "expires_at": entry.expires_at,
        "vary_key": entry.vary_key,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def invalidate_http(url: str, cache_dir: Path | None = None, *, vary_key: str | None = None) -> None:
    p = _http_path(cache_dir or _default_cache_dir(), url, vary_key)
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
    include_references: bool = True,
    clean_rules: list[str] | None = None,
    flags: dict[str, Any] | None = None,
) -> str:
    payload = {
        "url": url,
        "render_mode": render_mode,
        "adapter_version": adapter_version,
        "extractor_profile": extractor_profile,
        "include_references": include_references,
        "clean_rules": clean_rules or [],
        "flags": flags or {},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
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


def vary_key_from_headers(headers: dict[str, str]) -> str:
    """计算简化版 Vary key：纳入会影响 HTML 内容的常见请求头。"""
    keys = ("accept", "accept-language", "user-agent")
    normalized = {k: headers.get(k, headers.get(k.title(), "")) for k in keys}
    return _sha(json.dumps(normalized, sort_keys=True))


def response_cache_policy(headers: dict[str, str], *, now: float | None = None) -> tuple[bool, float | None]:
    """解析最小缓存策略，返回 (should_store, expires_at)。

    支持：Cache-Control: no-store / max-age=N，以及 Expires。
    """
    now = now or time.time()
    cc = headers.get("cache-control", headers.get("Cache-Control", "")).lower()
    if "no-store" in cc:
        return False, None
    for part in cc.split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                seconds = int(part.split("=", 1)[1])
                return True, now + max(0, seconds)
            except ValueError:
                break
    exp = headers.get("expires", headers.get("Expires"))
    if exp:
        try:
            dt = email.utils.parsedate_to_datetime(exp)
            return True, dt.timestamp()
        except (TypeError, ValueError, IndexError):
            pass
    # 默认保守：条件请求缓存，不视为 fresh；下次会带 ETag/Last-Modified revalidate。
    return True, None
