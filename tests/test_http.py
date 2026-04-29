from __future__ import annotations

from pathlib import Path

import pytest

from qiq_html2md.core.errors import FatalError
from qiq_html2md.infra import http


def test_ssrf_denied_localhost() -> None:
    with pytest.raises(FatalError) as exc:
        http.get("http://localhost/")
    assert exc.value.payload["host"] == "localhost"


def test_ssrf_denied_loopback_literal() -> None:
    with pytest.raises(FatalError):
        http.get("http://127.0.0.1/")


def test_ssrf_denied_private_range() -> None:
    with pytest.raises(FatalError):
        http.get("http://192.168.1.1/")


def test_scheme_denied_ftp() -> None:
    with pytest.raises(FatalError):
        http.get("ftp://example.com/")


def test_file_scheme_default_denied() -> None:
    with pytest.raises(FatalError):
        http.get("file:///tmp/x.html")


def test_file_scheme_allowed(tmp_path: Path) -> None:
    p = tmp_path / "a.html"
    p.write_text("<html><body>hi</body></html>")
    resp = http.get(f"file://{p}", allow_file_scheme=True)
    assert resp.status_code == 200
    assert "hi" in resp.text
