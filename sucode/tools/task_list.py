"""团队任务工具：TaskList（spec(docs/ch04) C62，T57）。

列出共享任务板上的任务；状态按 ○(pending) ◐(in_progress) ●(completed) ✕(blocked) 渲染。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sucode.teams import VALID_STATUSES, TaskStore, TeamManager, render_status
from sucode.tools.base import Tool, ToolError


class _TaskListParams(BaseModel):
    status: str | None = Field(
        default=None,
        description=f"按状态过滤（可选，取值：{'/'.join(VALID_STATUSES)}；缺省列出全部）",
    )


class TaskListTool(Tool):
    name = "TaskList"
    description = (
        f"列出共享任务板上的全部任务，状态以图标渲染："
        f"○ pending / ◐ in_progress / ● completed / ✕ blocked。"
        f"可用 status 参数只看某一状态（取值：{'、'.join(VALID_STATUSES)}）。"
    )
    category = "task"
    is_concurrency_safe = True
    Params = _TaskListParams

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    def _store(self) -> TaskStore:
        return self._team_manager.get_task_store(self._team_name)

    async def execute(self, status: str | None = None) -> str:
        if status is not None and status not in VALID_STATUSES:
            raise ToolError(
                f"非法状态 {status!r}，合法取值：{'、'.join(VALID_STATUSES)}（缺省列出全部）"
            )
        tasks = self._store().list_tasks(status)
        if not tasks:
            scope = f"（状态 {status}）" if status else ""
            return f"任务板上没有任务{scope}。"
        lines = []
        for task in tasks:
            line = f"{render_status(task.status)} {task.id} {task.title}"
            if task.blocked_by:
                line += f" ←被阻塞于 {','.join(task.blocked_by)}"
            if task.blocks:
                line += f" →阻塞 {','.join(task.blocks)}"
            lines.append(line)
        return "\n".join(lines)
