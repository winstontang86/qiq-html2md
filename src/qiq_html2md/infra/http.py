"""HTTP 客户端 + SSRF 护栏。

职责
----
- 封装 httpx；支持 http/https，限制重定向与响应大小。
- SSRF 护栏：禁止 localhost、私有网段、链路本地地址。
- 也支持 file:// 方案（仅限本地 fixtures / 测试）——默认关闭，通过 `allow_file_scheme=True` 开启。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from qiq_html2md.core.errors import FatalError, RetryableError
from qiq_html2md.infra import cache as cache_mod

MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50MB
MAX_REDIRECTS = 5

# SSRF 黑名单网段
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


@dataclass
class HttpResponse:
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    encoding: str | None
    from_cache: bool = False

    @property
    def text(self) -> str:
        enc = self.encoding or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def _assert_safe_host(host: str) -> None:
    """SSRF 护栏：解析 host，任一 A/AAAA 命中黑名单即拒绝。"""
    # 允许通过环境变量临时放行（测试用）
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FatalError("dns_failed", host=host) from e
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                raise FatalError("ssrf_denied", host=host, ip=addr)


def _check_url(url: str, *, allow_file_scheme: bool = False) -> str:
    """校验 URL，返回规范化的 scheme。"""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        if not allow_file_scheme:
            raise FatalError("scheme_denied", scheme=scheme)
        return scheme
    if scheme not in ("http", "https"):
        raise FatalError("scheme_denied", scheme=scheme)
    if not parsed.hostname:
        raise FatalError("url_invalid", url=url)
    _assert_safe_host(parsed.hostname)
    return scheme


def get(
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    allow_file_scheme: bool = False,
    max_bytes: int = MAX_RESPONSE_BYTES,
    use_cache: bool = True,
) -> HttpResponse:
    """执行 GET。失败按性质抛 FatalError 或 RetryableError。

    当 `use_cache=True` 且全局缓存开启时：
    - 先查本地缓存；若命中，带 If-None-Match / If-Modified-Since。
    - 服务器 304 → 返回缓存内容，`from_cache=True`。
    - 其他 2xx → 写入缓存并返回新响应。
    """
    scheme = _check_url(url, allow_file_scheme=allow_file_scheme)

    if scheme == "file":
        return _get_file(url)

    default_headers = {
        "User-Agent": "qiq-html2md/0.1 (+https://github.com/laotang/qiq-html2md)",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en,zh;q=0.8",
    }
    if headers:
        default_headers.update(headers)

    vary_key = cache_mod.vary_key_from_headers(default_headers)
    cache_entry = None
    if use_cache and cache_mod.enabled():
        cache_entry = cache_mod.get_http(url, vary_key=vary_key)
        if cache_entry is not None:
            if cache_entry.is_fresh():
                return HttpResponse(
                    final_url=cache_entry.final_url,
                    status_code=200,
                    headers=cache_entry.headers,
                    content=cache_entry.content,
                    encoding=cache_entry.headers.get("content-encoding") or "utf-8",
                    from_cache=True,
                )
            default_headers.update(cache_entry.conditional_headers())

    try:
        with httpx.Client(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=timeout,
            headers=default_headers,
        ) as client:
            resp = client.get(url)
    except httpx.TimeoutException as e:
        raise RetryableError("http_timeout", url=url) from e
    except httpx.HTTPError as e:
        raise RetryableError("http_error", url=url, detail=str(e)) from e

    # 304 → 直接用缓存
    if resp.status_code == 304 and cache_entry is not None:
        return HttpResponse(
            final_url=cache_entry.final_url,
            status_code=200,
            headers=cache_entry.headers,
            content=cache_entry.content,
            encoding=cache_entry.headers.get("content-encoding") or "utf-8",
            from_cache=True,
        )

    # 状态码检查
    if resp.status_code >= 500:
        raise RetryableError("http_5xx", status=resp.status_code, url=url)
    if resp.status_code >= 400:
        raise FatalError("http_4xx", status=resp.status_code, url=url)

    # 大小限制
    if len(resp.content) > max_bytes:
        raise FatalError("response_too_large", size=len(resp.content), url=url)

    result = HttpResponse(
        final_url=str(resp.url),
        status_code=resp.status_code,
        headers=dict(resp.headers),
        content=resp.content,
        encoding=resp.encoding,
    )

    # 写缓存
    if use_cache and cache_mod.enabled() and resp.status_code == 200:
        should_store, expires_at = cache_mod.response_cache_policy(result.headers, now=time.time())
        if should_store:
            cache_mod.put_http(
                cache_mod.HttpCacheEntry(
                    url=url,
                    final_url=result.final_url,
                    status=result.status_code,
                    headers=result.headers,
                    content=result.content,
                    etag=result.headers.get("etag"),
                    last_modified=result.headers.get("last-modified"),
                    stored_at=time.time(),
                    expires_at=expires_at,
                    vary_key=vary_key,
                )
            )

    return result


def _get_file(url: str) -> HttpResponse:
    """读取本地 file:// —— 测试与离线 fixture 专用。"""
    parsed = urlparse(url)
    # file://host/path —— 忽略 host
    path = Path(parsed.path)
    if not path.is_file():
        raise FatalError("file_not_found", path=str(path))
    data = path.read_bytes()
    return HttpResponse(
        final_url=url,
        status_code=200,
        headers={},
        content=data,
        encoding="utf-8",
    )
