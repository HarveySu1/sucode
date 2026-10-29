"""搜索工具（Phase 2 tasks T13 → 设计决策对齐修订）：Glob / Grep。

搜索本身纯标准库实现（不引入 ripgrep）；参数校验用 Pydantic 模型（spec C34），
分类均为 read（spec C35）。跳过 .git / __pycache__ / node_modules / .venv 等目录与二进制文件。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from sucode.tools.base import Tool, ToolError

GLOB_MAX_RESULTS = 500  # spec N6
GREP_MAX_HITS = 200

# 递归搜索时跳过的目录名（VCS/缓存/虚拟环境/构建产物）
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".idea", ".vscode", "*.egg-info",
}


def _match_skip_dir(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) if "*" in pat else name == pat
               for pat in SKIP_DIRS)


def _is_binary(file: Path, probe: int = 1024) -> bool:
    """含 NUL 字节视为二进制，跳过（避免误报与乱码）。"""
    try:
        with file.open("rb") as f:
            return b"\0" in (f.read(probe))
    except OSError:
        return True


def _iter_files(root: Path, name_glob: str | None) -> list[Path]:
    """遍历 root 下文本文件：剪枝 SKIP_DIRS，按 glob 过滤文件名。"""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _match_skip_dir(d)]
        for fname in filenames:
            if name_glob and not fnmatch.fnmatch(fname, name_glob):
                continue
            files.append(Path(dirpath) / fname)
    return files


class _GlobParams(BaseModel):
    pattern: str = Field(description="通配模式，如 **/*.py 或 src/**/*.toml")
    path: str = Field(default=".", description="搜索根目录（可选，默认当前目录）")


class Glob(Tool):
    name = "Glob"
    description = (
        "按通配模式查找文件路径（支持 *、**、? 等），结果按修改时间从新到旧排序，"
        f"最多 {GLOB_MAX_RESULTS} 条。适合「找到某个文件在哪」的场景，如 Glob(pattern=\"**/*.py\")。"
        "查找文件内容请改用 Grep。"
    )
    category = "read"
    is_concurrency_safe = True  # 纯读恒安全（spec(docs/ch03) C45 静态属性）
    Params = _GlobParams

    async def execute(self, pattern: str = "", path: str = ".") -> str:
        if not pattern:
            raise ToolError("缺少参数 pattern（通配模式不能为空）")
        root = Path(path or ".").expanduser()
        if not root.is_dir():
            raise ToolError(f"目录不存在：{root}")
        try:
            matches = [p for p in root.glob(pattern) if p.is_file()]
        except (NotADirectoryError, OSError) as exc:
            raise ToolError(f"匹配失败：{exc}") from exc
        if not matches:
            return f"（{root} 下没有匹配 {pattern!r} 的文件）"

        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        truncated = len(matches) > GLOB_MAX_RESULTS
        shown = matches[:GLOB_MAX_RESULTS]
        out = "\n".join(str(p) for p in shown)
        if truncated:
            out += f"\n…[共 {len(matches)} 条，仅显示最新 {GLOB_MAX_RESULTS} 条]…"
        return out


class _GrepParams(BaseModel):
    pattern: str = Field(description="正则表达式（Python re 语法）")
    path: str = Field(default=".", description="搜索根目录（可选，默认当前目录）")
    glob: str = Field(default="", description="只搜索匹配此文件名模式的文件（可选，如 *.py）")


class Grep(Tool):
    name = "Grep"
    description = (
        "在文件内容中按正则表达式搜索，输出「文件:行号: 该行内容」，"
        f"最多 {GREP_MAX_HITS} 处命中；可用 glob 参数只搜特定文件名（如 *.py）。"
        "适合「哪里的代码定义/引用了 X」的场景；找文件路径请改用 Glob。"
    )
    category = "read"
    is_concurrency_safe = True  # 纯读恒安全（spec(docs/ch03) C45 静态属性）
    Params = _GrepParams

    async def execute(self, pattern: str = "", path: str = ".", glob: str = "") -> str:
        if not pattern:
            raise ToolError("缺少参数 pattern（正则不能为空）")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"正则表达式不合法：{exc}") from exc

        root = Path(path or ".").expanduser()
        if not root.is_dir():
            raise ToolError(f"目录不存在：{root}")

        hits: list[str] = []
        scanned = 0
        for file in _iter_files(root, glob or None):
            if _is_binary(file):
                continue
            scanned += 1
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{file}:{lineno}:{line.rstrip()}")
                    if len(hits) >= GREP_MAX_HITS:
                        out = "\n".join(hits)
                        out += (
                            f"\n…[命中已达上限 {GREP_MAX_HITS} 处（已扫描 {scanned} 个文件），"
                            "可缩小 path/glob 范围或用更精确的 pattern]…"
                        )
                        return out
        if not hits:
            return f"（在 {root} 下扫描 {scanned} 个文本文件，没有匹配 {pattern!r} 的内容）"
        return f"{chr(10).join(hits)}\n（共 {len(hits)} 处命中，扫描 {scanned} 个文件）"
