"""会话层（tasks.md T6 → Phase 2 T17 → Phase 3 T31 瘦身 + T34 模式状态）：

Phase 3（spec(docs/ch03) C42）把 Agent 循环迁出到独立编排器（sucode/agent.py），
本模块退化为状态容器：内存对话历史 + 当前 Provider 实例（profile 热切换）+
工具注册中心引用 + 计划模式状态（C47）。循环编排、工具执行、事件产出全部归
Agent；历史读写经本对象方法进行（append / pop / history 快照），编排器不触碰内部结构。

配对不变量等循环期约束由编排器负责维护（延续 ch02 C24/C29）。
"""

from __future__ import annotations

from sucode.config import ProfileConfig
from sucode.providers import get_provider
from sucode.providers.base import BaseProvider, Message
from sucode.tools import ToolRegistry

DEFAULT_PLAN_FILE = "plan.md"  # 计划模式默认计划文件（cwd 下，checklist 承接值）


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
        """/plan [文件]：进入计划模式（唯一放行写目标为计划文件，缺省 plan.md）。"""
        self._plan_mode = True
        self._plan_file = plan_file.strip() or DEFAULT_PLAN_FILE
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
