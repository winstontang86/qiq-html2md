"""文件系统沙盒。

所有 skill 内部的写操作必须经过本模块，强制：
- 所有写入必须位于 `output_dir` 内。
- 禁止 `..` 穿越与符号链接逃逸。
"""

from __future__ import annotations

import os
from pathlib import Path

from html2md_skill.core.errors import FatalError


class FsSandbox:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, relpath: str | Path) -> Path:
        """解析相对路径，校验仍在 root 内。"""
        p = (self._root / Path(relpath)).resolve()
        try:
            p.relative_to(self._root)
        except ValueError as e:
            raise FatalError("path_escape", path=str(p), root=str(self._root)) from e
        # 进一步防符号链接逃逸：任一祖先是 symlink 则拒绝（root 自己除外）
        cur: Path | None = p
        while cur is not None and cur != self._root:
            if cur.is_symlink():
                raise FatalError("symlink_denied", path=str(cur))
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
        return p

    def mkdirp(self, relpath: str | Path) -> Path:
        p = self.resolve(relpath)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_text(self, relpath: str | Path, content: str, encoding: str = "utf-8") -> Path:
        p = self.resolve(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return p

    def write_bytes(self, relpath: str | Path, data: bytes) -> Path:
        p = self.resolve(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def append_line(self, relpath: str | Path, line: str) -> Path:
        p = self.resolve(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            if not line.endswith(os.linesep) and not line.endswith("\n"):
                f.write("\n")
        return p
