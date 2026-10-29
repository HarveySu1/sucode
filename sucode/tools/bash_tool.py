"""Bash 工具（Phase 2 tasks T13 → 设计决策对齐修订）：跨平台命令执行。

Windows 陷阱处理（spec N9，经文档核验）：
- powershell.exe -Command 进程退出码塌缩为 0/1 → 用 -EncodedCommand 承载脚本，
  尾部显式 exit $LASTEXITCODE 传递真实退出码
- 中文控制台默认 GBK → 脚本前置 chcp 65001 + [Console]::OutputEncoding=UTF8，
  Python 侧 UTF-8 + errors=replace 解码
- 超时/取消必须杀整棵进程树（taskkill /T /F），asyncio 的 proc.kill() 只杀主进程

子进程拉起（spec C39）：统一用 asyncio.create_subprocess_shell（异步非阻塞）；
Windows 经 shell 调 powershell.exe -EncodedCommand，Unix 以 /bin/bash 为 shell 并
start_new_session 独立进程组，超时/取消用 killpg 杀整树。
stdin 固定 DEVNULL：交互式命令不会挂住（会立即失败或按 EOF 处理）。

Phase 2 修订（spec(docs/ch02) C30/N11）：execute_streaming 增量读取 stdout/stderr，
读到片段即经 emit 回传（实时进度），超时/取消仍杀整棵树；execute 退化为不回调的流式执行。
"""

from __future__ import annotations

import asyncio
import base64
import html
import os
import re
import signal
import sys

from pydantic import BaseModel, Field

from sucode.tools.base import Emit, Tool, ToolError

# PowerShell -NonInteractive 下自身错误流被序列化为 CLIXML（`#< CLIXML\r\n<Objs…</Objs>`），
# 对模型是 XML 噪音；外部程序的 stderr 不受影响。注意实际首行是「#< CLIXML」后跟换行，
# 末尾并无「>」——匹配时必须容忍换行，否则永远剥不干净（实测 PS 5.1）。
_CLIXML_RE = re.compile(r"#< CLIXML[\s\S]*?(?:</Objs>|$)")
_CLIXML_ERROR_RE = re.compile(r'<S S="Error">([\s\S]*?)</S>')
_CLIXML_ESCAPES = (("_x000D_", "\r"), ("_x000A_", "\n"))


def _strip_clixml(text: str) -> str:
    """剥离 CLIXML 包装，但保留其中的错误正文（丢正文会让模型失去自愈线索）。"""
    if "#< CLIXML" not in text:
        return text.strip()
    blocks = _CLIXML_ERROR_RE.findall(text)
    if not blocks:
        return _CLIXML_RE.sub("", text).strip()
    restored = []
    for block in blocks:
        line = html.unescape(block)
        for esc, ch in _CLIXML_ESCAPES:
            line = line.replace(esc, ch)
        restored.append(line.strip())
    return "\n".join(restored).strip()


DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0

# 只读命令白名单（spec(docs/ch03) C46 修订）：服务计划模式命令拦截与权限确认范围，
# 不参与并发判定（C45 改静态属性后 Bash 一律串行）。
# 命令首部命中（单 token 命令或 git 双 token 子命令）且不含元字符才判只读。
READONLY_BASH_COMMANDS = (
    "ls", "dir", "Get-ChildItem", "cat", "type", "Get-Content",
    "pwd", "Get-Location", "tree", "where",
    "git status", "git diff", "git log", "git show", "git branch",
)
# 出现即拦截的 shell 元字符（管道/顺序分隔/重定向/后台）
BASH_META_CHARS = "|;><&"


def is_readonly_command(command: str) -> bool:
    """命令是否属于只读白名单：首部命令命中且不含 shell 元字符（C46）。

    大小写不敏感（PowerShell 命令名大小写无关）；空命令不算只读；
    元字符出现在任何位置（含引号内）都判不命中——组合命令默认不命中（spec L6）。
    """
    text = str(command or "").strip()
    if not text:
        return False
    if any(ch in text for ch in BASH_META_CHARS):
        return False
    tokens = [t.lower() for t in text.split()]
    for entry in READONLY_BASH_COMMANDS:
        entry_tokens = entry.lower().split()
        if tokens[: len(entry_tokens)] == entry_tokens:
            return True
    return False


def _powershell_script(command: str) -> str:
    """包装用户命令：UTF-8 环境前置 + 真实退出码显式传递 + 抑制干扰首选项。

    退出码判定链（经 PS 5.1 实测）：
    - 成功：$?=True → exit $LASTEXITCODE（chcp 保证其非 null）
    - native 命令非零退出：$?=False 且 $LASTEXITCODE=真实码 → 原样透传
    - cmdlet 失败（如 unix 风格 `ls -la`）：$?=False 且 $LASTEXITCODE 残留 0 → 强制 exit 1，
      防止报错被塌缩成「成功」拖垮模型自愈
    """
    return (
        "chcp 65001 >$null\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$WarningPreference = 'SilentlyContinue'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        f"{command}\n"
        "if (-not $? -and $LASTEXITCODE -eq 0) { exit 1 }\n"
        "exit $LASTEXITCODE\n"
    )


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """杀整棵进程树；失败静默（进程可能已退出）。"""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            # taskkill 也是外部命令，同步开销小；用 create_subprocess 避免阻塞事件循环
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 清理路径不二次失败
        pass


class _BashParams(BaseModel):
    command: str = Field(description="要执行的命令")
    timeout_seconds: int = Field(
        default=0,
        description=f"超时秒数（可选，默认 {DEFAULT_TIMEOUT:.0f}，最大 {MAX_TIMEOUT:.0f}）",
    )


class Bash(Tool):
    name = "Bash"
    description = (
        f"在系统 shell 中执行命令（Windows 为 PowerShell，必须用 PowerShell 语法，"
        f"如列目录用 Get-ChildItem / dir，不要用 ls -la 等 unix 语法；Unix 为 bash），"
        f"返回合并的 stdout/stderr 与退出码。timeout_seconds 默认 {DEFAULT_TIMEOUT:.0f}s、"
        f"上限 {MAX_TIMEOUT:.0f}s。适合运行程序、查看目录、git 操作等；"
        "交互式命令不支持（stdin 已关闭）。"
    )
    category = "command"
    Params = _BashParams
    timeout = MAX_TIMEOUT + 5.0  # execute 硬超时兜底：比最大可调超时再宽 5s

    async def execute(self, command: str = "", timeout_seconds: int = 0) -> str:
        """非流式执行：等价于不回调 emit 的流式执行（保留统一接口的抽象方法实现）。"""
        return await self.execute_streaming(None, command=command, timeout_seconds=timeout_seconds)

    async def execute_streaming(
        self, emit: Emit | None, command: str = "", timeout_seconds: int = 0
    ) -> str:
        """增量执行（spec(docs/ch02) C30/N11）：读到 stdout/stderr 片段即经 emit 回传。

        保留 Phase 2 全部 Windows 陷阱处理（UTF-8 解码、CLIXML 剥离、退出码判定、
        超时/取消杀整棵树）；仅把「等全部结束才返回」换成「边读边回传」。
        """
        if not command or not str(command).strip():
            raise ToolError("缺少参数 command（要执行的命令不能为空）")
        timeout = float(timeout_seconds) if timeout_seconds else DEFAULT_TIMEOUT
        if timeout <= 0 or timeout > MAX_TIMEOUT:
            raise ToolError(f"timeout_seconds 应为 1-{MAX_TIMEOUT:.0f} 之间的整数，当前为 {timeout_seconds}")

        proc = await self._spawn(command)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        async def pump(stream, sink: list[str]) -> None:
            """持续读一个管道直到 EOF；每个片段解码后入 sink 并回传 emit。"""
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                sink.append(text)
                if emit is not None:
                    emit(text)

        readers = [
            asyncio.create_task(pump(proc.stdout, stdout_parts)),
            asyncio.create_task(pump(proc.stderr, stderr_parts)),
        ]

        async def wait_and_drain() -> None:
            await proc.wait()
            await asyncio.gather(*readers)  # 进程结束后仍读完管道余量到 EOF

        try:
            await asyncio.wait_for(wait_and_drain(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            raise ToolError(f"命令超时（{timeout:.0f}s）已强制终止：{command[:100]}") from None
        except asyncio.CancelledError:
            await _kill_tree(proc)  # Esc 取消也要杀干净，避免残留孤儿进程
            raise
        finally:
            for r in readers:
                r.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        stdout = "".join(stdout_parts)
        stderr = _strip_clixml("".join(stderr_parts))
        code = proc.returncode if proc.returncode is not None else -1

        parts: list[str] = []
        if stdout.strip():
            parts.append(stdout.rstrip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.rstrip()}")
        parts.append(f"[退出码 {code}]")
        return "\n".join(parts)

    @staticmethod
    async def _spawn(command: str) -> asyncio.subprocess.Process:
        """按平台拉起子进程（spec C39：统一 create_subprocess_shell）。

        - Windows：经 shell 调 powershell.exe -NoProfile -NonInteractive -EncodedCommand
          （base64 无特殊字符，shell 层转义安全）
        - Unix：以 /bin/bash 为 shell + start_new_session 独立进程组，便于 killpg 杀整树
        """
        if sys.platform == "win32":
            encoded = base64.b64encode(
                _powershell_script(command).encode("utf-16-le")
            ).decode("ascii")
            return await asyncio.create_subprocess_shell(
                f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        return await asyncio.create_subprocess_shell(
            command,
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # 独立进程组 → killpg 可杀整树
        )
