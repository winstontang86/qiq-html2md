"""BeautifulSoup Tag 属性读取 helper。

bs4 新版类型标注中 `Tag.get(...)` 可能返回 `str | AttributeValueList | None`，
直接 `.lower()` / `.strip()` 会让 mypy 很难受。这里集中做归一化。
"""

from __future__ import annotations

from bs4 import Tag


def class_str(t: Tag) -> str:
    """把 tag.class 归一为空格连接的字符串。"""
    return str_attr(t, "class")


def str_attr(t: Tag, key: str, default: str = "") -> str:
    """把字符串属性安全取为 str。"""
    raw = t.get(key)
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw
    try:
        return " ".join(str(x) for x in raw)
    except TypeError:
        return str(raw)


def int_attr(t: Tag, key: str, default: int = 1) -> int:
    """把数值属性（如 rowspan/colspan）安全转 int。"""
    raw = t.get(key)
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw))
    except (ValueError, TypeError):
        return default
