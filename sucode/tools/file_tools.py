"""文件工具（Phase 2 tasks T13 → 设计决策对齐修订）：ReadFile / WriteFile / EditFile。

- 参数校验：各工具声明 Pydantic 参数模型，Schema 自动生成（spec C34）
- 工具分类：读类 read / 写类 write（spec C35）
- 文件缓存：ReadFile 读时填充，EditFile / WriteFile 写后失效（spec C38）
- ReadFile 带行号输出（服务后续 EditFile 定位），默认 2000 行截断
- WriteFile 父目录自动创建，UTF-8 写入
- EditFile 原文唯一匹配替换：0 命中 / 多命中都给可指引模型重试的报错（spec C22）
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from sucode.tools.base import FileCache, Tool, ToolError

DEFAULT_READ_LINES = 2000  # 单次读取行数上限（spec N6）

# 进程内共享的文件内容缓存（spec C38）
FILE_CACHE = FileCache()


def _resolve(path: str) -> Path:
    """路径解析：相对当前工作目录；空路径直接报错。"""
    if not path or not str(path).strip():
        raise ToolError("缺少参数 path（文件路径不能为空）")
    return Path(str(path).strip()).expanduser()


class _ReadFileParams(BaseModel):
    path: str = Field(description="文件路径（相对当前目录或绝对路径）")
    offset: int = Field(default=0, description="起始行号（0 基，可选）")
    limit: int = Field(default=DEFAULT_READ_LINES, description="最多读取的行数（可选，默认 2000）")


class ReadFile(Tool):
    name = "ReadFile"
    description = (
        "读取文本文件内容，输出带行号（格式「行号→内容」）。"
        f"默认最多读取 {DEFAULT_READ_LINES} 行，超出截断并注明；"
        "可用 offset（0 基起始行）与 limit（行数）分段读取大文件。"
        "适合查看源码、配置等任何文本文件。"
    )
    category = "read"
    Params = _ReadFileParams

    def is_concurrency_safe(self, arguments: dict) -> bool:
        return True  # 纯读恒安全（spec(docs/ch03) C45）

    async def execute(self, path: str = "", offset: int = 0, limit: int = DEFAULT_READ_LINES) -> str:
        file = _resolve(path)
        if not file.is_file():
            raise ToolError(f"文件不存在：{file}")
        try:
            mtime_ns = file.stat().st_mtime_ns
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc

        text = FILE_CACHE.get(file, mtime_ns)  # 缓存命中则免重复读盘（spec C38）
        if text is None:
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise ToolError(f"不是 UTF-8 文本文件（可能为二进制）：{file}") from None
            FILE_CACHE.put(file, mtime_ns, text)

        lines = text.splitlines()
        total = len(lines)
        start = max(0, int(offset))
        count = max(1, int(limit))
        end = min(total, start + count)
        if start >= total:
            return f"（文件共 {total} 行，offset={start} 已越界，无内容）"

        numbered = [f"{i + 1:>6}→{lines[i]}" for i in range(start, end)]
        out = "\n".join(numbered)
        if end < total:
            out += f"\n…[共 {total} 行，本次显示第 {start + 1}-{end} 行，可用 offset={end} 继续读取]…"
        return out


class _WriteFileParams(BaseModel):
    path: str = Field(description="目标文件路径")
    content: str = Field(description="要写入的完整文本内容")


class WriteFile(Tool):
    name = "WriteFile"
    description = (
        "把文本内容写入文件（整体覆盖写）。父目录不存在会自动创建。"
        "适合新建文件或整文件重写；局部修改请优先用 EditFile。"
    )
    category = "write"
    Params = _WriteFileParams

    async def execute(self, path: str = "", content: str = "") -> str:
        file = _resolve(path)
        data = str(content)
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(data, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        FILE_CACHE.invalidate(file)  # 写后失效（spec C38）
        return f"已写入 {file}（{len(data.encode('utf-8'))} 字节，{data.count(chr(10)) + 1} 行）"


class _EditFileParams(BaseModel):
    path: str = Field(description="目标文件路径")
    old_string: str = Field(description="要被替换的原文（须全文件唯一）")
    new_string: str = Field(description="替换后的新内容")


class EditFile(Tool):
    name = "EditFile"
    description = (
        "精确修改文本文件：把 old_string 原文唯一匹配替换为 new_string。"
        "old_string 必须与文件内容逐字节一致（含缩进/空行）且全文件恰好出现一次；"
        "匹配不到或匹配到多处都会报错，此时请提供更长、更唯一的上下文后重试。"
    )
    category = "write"
    Params = _EditFileParams

    async def execute(self, path: str = "", old_string: str = "", new_string: str = "") -> str:
        file = _resolve(path)
        if not old_string:
            raise ToolError("old_string 不能为空（否则无法定位替换位置）")
        if not file.is_file():
            raise ToolError(f"文件不存在：{file}")

        text = None
        try:
            mtime_ns = file.stat().st_mtime_ns
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc
        text = FILE_CACHE.get(file, mtime_ns)  # 缓存命中则免重复读盘（spec C38）
        if text is None:
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise ToolError(f"不是 UTF-8 文本文件：{file}") from None
            FILE_CACHE.put(file, mtime_ns, text)

        hits = text.count(old_string)
        if hits == 0:
            raise ToolError(
                f"未找到匹配：old_string 在 {file.name} 中未出现。"
                "请检查拼写、缩进与空白后重试（建议先 ReadFile 确认原文）"
            )
        if hits > 1:
            line_numbers = []
            pos = text.find(old_string)
            while pos != -1 and len(line_numbers) < 5:
                line_numbers.append(text[:pos].count("\n") + 1)
                pos = text.find(old_string, pos + 1)
            raise ToolError(
                f"匹配到 {hits} 处（行号示例：{line_numbers}…）："
                "old_string 不唯一。请扩大 old_string 范围、包含更多上下文后重试"
            )

        new_text = text.replace(old_string, new_string, 1)
        try:
            file.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        FILE_CACHE.invalidate(file)  # 写后失效（spec C38）
        return (
            f"已替换 {file}（唯一匹配处，原 {len(old_string)} 字符 → 新 {len(new_string)} 字符，"
            f"文件现共 {new_text.count(chr(10)) + 1} 行）"
        )
