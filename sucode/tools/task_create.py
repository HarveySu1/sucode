"""团队任务工具：TaskCreate（spec(docs/ch04) C62，T57）。

在共享任务板上创建任务；持久化与依赖校验见 sucode/teams.py TaskStore。
category="task"（不触发权限确认、计划模式放行），is_concurrency_safe=True。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sucode.teams import TaskStore, TeamManager, render_status
from sucode.tools.base import Tool


class _TaskCreateParams(BaseModel):
    title: str = Field(description="任务标题（简短一句话）")
    description: str = Field(default="", description="任务补充说明（可选）")
    blocks: list[str] = Field(
        default_factory=list,
        description="本任务阻塞的其他任务 id 列表（可选，如 [\"T-1\"]）",
    )
    blocked_by: list[str] = Field(
        default_factory=list,
        description="阻塞本任务的其他任务 id 列表（可选）",
    )


class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = (
        "在团队共享任务板上创建一个新任务（初始状态 pending）。"
        "可用 blocks / blocked_by 声明与其他任务的依赖关系（引用已存在任务的 id，如 T-1）。"
        "适合把多步骤工作拆成可跟踪、可查询的任务清单。"
    )
    category = "task"
    is_concurrency_safe = True
    Params = _TaskCreateParams

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    def _store(self) -> TaskStore:
        return self._team_manager.get_task_store(self._team_name)

    async def execute(
        self,
        title: str = "",
        description: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> str:
        task = self._store().create(
            title,
            description,
            blocks=list(blocks or []),
            blocked_by=list(blocked_by or []),
        )
        deps = ""
        if task.blocks or task.blocked_by:
            deps = f"（依赖：阻塞 {task.blocks or '无'}，被阻塞于 {task.blocked_by or '无'}）"
        return f"已创建任务 {task.id}：{task.title} {render_status(task.status)}{deps}"
