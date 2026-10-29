"""会话层（tasks.md T6 → Phase 2 T17 → Phase 3 T31 瘦身 + T34 模式状态）：

Phase 3（spec(docs/ch03) C42）把 Agent 循环迁出到独立编排器（sucode/agent.py），
本模块退化为状态容器：内存对话历史 + 当前 Provider 实例（profile 热切换）+
工具注册中心引用 + 计划模式状态（C47）。循环编排、工具执行、事件产出全部归
Agent；历史读写经本对象方法进行（append / pop / history 快照），编排器不触碰内部结构。

配对不变量等循环期约束由编排器负责维护（延续 ch02 C24/C29）。
"""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

from sucode.config import ProfileConfig
from sucode.providers import get_provider
from sucode.providers.base import BaseProvider, Message
from sucode.tools import ToolRegistry

# 计划模式 slug 词表（spec(docs/ch04) C63，N28）：24 形容词 + 24 名词，
# 拼出人眼可读的 plan 文件名，避免 .sucode/plans/ 下多份历史计划无法区分。
_PLAN_ADJECTIVES = (
    "amber", "brave", "calm", "dawn", "eager", "flint", "gentle", "harbor",
    "ivory", "jolly", "keen", "lunar", "mellow", "nimble", "opal", "placid",
    "quiet", "ruby", "swift", "tidy", "urban", "vivid", "warm", "zesty",
)
_PLAN_NOUNS = (
    "atlas", "beacon", "compass", "delta", "ember", "falcon", "garnet", "harvest",
    "island", "jungle", "kite", "lantern", "meadow", "nectar", "orbit", "prism",
    "quarry", "ridge", "sparrow", "temple", "umbra", "vessel", "willow", "zephyr",
)


def make_plan_slug_path() -> Path:
    """生成一份新的计划文件路径：<cwd>/.sucode/plans/<形容词-名词>-<MMDD-HHMM>.md。

    只生成路径与父目录，不写文件；每次进入计划模式各生成一份（时间戳区分）。
    """
    slug = (
        f"{random.choice(_PLAN_ADJECTIVES)}-{random.choice(_PLAN_NOUNS)}"
        f"-{datetime.now().strftime('%m%d-%H%M')}"
    )
    return Path.cwd() / ".sucode" / "plans" / f"{slug}.md"


class ChatSession:
    """单次运行内的会话状态：Provider 实例、可选工具注册中心与内存对话历史。"""

    def __init__(self, provider: BaseProvider, registry: ToolRegistry | None = None) -> None:
        self._provider = provider
        self._registry = registry
        self._history: list[Message] = []
        self._plan_mode = False  # 计划模式开关（C47）
        self._plan_file = ""  # 计划模式下唯一放行的写目标（N20 例外唯一性）
        self._plan_exit_pending = False  # /exec 后待拼接切换提示（一次生效，C47 修订）

    @property
    def provider(self) -> BaseProvider:
        """当前 Provider（Agent 每轮流经此取流）。"""
        return self._provider

    @property
    def registry(self) -> ToolRegistry | None:
        """工具注册中心；None 表示纯对话会话（回归路径）。"""
        return self._registry

    @property
    def profile(self) -> ProfileConfig:
        """当前 profile（顶栏显示用）。"""
        return self._provider.profile

    @property
    def history(self) -> list[Message]:
        """历史只读快照。"""
        return list(self._history)

    def append(self, message: Message) -> None:
        """历史追加（Agent 循环写入的唯一入口）。"""
        self._history.append(message)

    def pop(self) -> Message:
        """历史回滚（中断收尾：首轮回滚用户消息 / 非首轮回滚配对消息对）。"""
        return self._history.pop()

    def switch_profile(self, profile: ProfileConfig) -> None:
        """热切换 profile：仅换 Provider 实例，历史保留（spec C10）。"""
        self._provider = get_provider(profile)

    def clear(self) -> None:
        """/new：清空上下文，重新开始。"""
        self._history.clear()
        self._plan_exit_pending = False  # 清空后不再携带切换提示（防幽灵拼接）

    # ---- 计划模式状态（spec(docs/ch03) C47，T34）----

    @property
    def plan_mode(self) -> bool:
        """是否处于计划模式（只探索不修改）。"""
        return self._plan_mode

    @property
    def plan_file(self) -> str:
        """计划文件路径；仅在计划模式下有意义。"""
        return self._plan_file

    def enter_plan_mode(self, plan_file: str = "") -> None:
        """/plan [文件]：进入计划模式（唯一放行写目标为计划文件）。

        无参时惰性生成可读 slug 文件（spec(docs/ch04) C63：.sucode/plans/ 下
        形容词-名词-时间戳命名，父目录自动创建）；带参按用户指定路径（当次会话生效）。
        """
        self._plan_mode = True
        if plan_file.strip():
            self._plan_file = plan_file.strip()
        else:
            path = make_plan_slug_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._plan_file = str(path)
        self._plan_exit_pending = False  # 重新进入计划模式，旧退出标志作废

    def exit_plan_mode(self) -> None:
        """/exec：退出计划模式（恢复普通最小系统提示词，写类工具恢复可执行）。"""
        self._plan_mode = False
        self._plan_file = ""
        # 退出后的首次用户请求携带一次模式切换提示（C47 修订）：
        # /exec 是 UI 命令不产生历史消息，模型无从得知模式已切换，会凭旧上下文拒绝写操作
        self._plan_exit_pending = True

    def take_plan_exit_pending(self) -> bool:
        """读取并清除「刚退出计划模式」标志（编排器消费：首次请求前拼接切换提示）。"""
        pending = self._plan_exit_pending
        self._plan_exit_pending = False
        return pending
