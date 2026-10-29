"""命令行入口（tasks.md T10）：参数解析 → 配置 → Provider → 会话 → TUI。

重型依赖（textual / 两家 SDK）延迟到 boot() 内导入，
保证 `sucode --version` 无需加载整个 TUI 栈。
"""

from __future__ import annotations

import argparse
import sys

from sucode import __version__
from sucode.config import ConfigError


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="sucode",
        description="sucode —— 终端 AI 编程助手",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="使用配置文件中的指定 profile（缺省用 default_profile）",
    )
    return parser


def _force_utf8_stdio() -> None:
    """Windows 控制台兜底：错误信息与示例模板统一按 UTF-8 输出。

    交互式终端在 PEP 528（WindowsConsoleIO）下本就是 UTF-8；此处覆盖
    重定向到 GBK 管道等场景。失败静默跳过，不影响主流程。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 已关闭/非常规流，跳过
            pass


def boot(argv: list[str] | None = None):
    """解析参数并构建应用实例；配置类失败以 ConfigError 抛出。"""
    from sucode.agent import Agent
    from sucode.chat import ChatSession
    from sucode.config import load_config
    from sucode.providers import get_provider
    from sucode.tools import create_default_registry
    from sucode.tools.tool_search import ToolSearchTool
    from sucode.tui.app import SucodeApp

    args = build_parser().parse_args(argv)
    config = load_config()
    profile = config.get(args.profile)
    tool_registry = create_default_registry()
    tool_registry.register(ToolSearchTool(tool_registry, protocol=profile.protocol))
    session = ChatSession(get_provider(profile), tool_registry)
    agent = Agent(session)  # T36 装配：TUI 只消费 Agent 事件通道
    return SucodeApp(session, agent, config)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口，控制台脚本包装器会以返回值作为退出码。"""
    _force_utf8_stdio()
    try:
        app = boot(argv)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    app.run()
    return 0
