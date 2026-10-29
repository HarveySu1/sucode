"""团队任务工具：TaskGet（spec(docs/ch04) C62，T57）。按 id 查看单个任务详情。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sucode.teams import STATUS_ICONS, TaskStore, TeamManager
from sucode.tools.base import Tool


class _TaskGetParams(BaseModel):
    task_id: str = Field(description="任务 id（如 T-1）")


class TaskGetTool(Tool):
    name = "TaskGet"
    description = "按 id 查看共享任务板上单个任务的详情（标题/描述/状态/依赖关系/时间戳）。"
    category = "task"
    is_concurrency_safe = True
    Params = _TaskGetParams

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    def _store(self) -> TaskStore:
        return self._team_manager.get_task_store(self._team_name)

    async def execute(self, task_id: str = "") -> str:
        task = self._store().get(task_id)
        lines = [
            f"任务 {task.id} {STATUS_ICONS.get(task.status, '○')} [{task.status}] {task.title}",
            f"描述：{task.description or '（无）'}",
            f"阻塞：{', '.join(task.blocks) if task.blocks else '（无）'}",
            f"被阻塞于：{', '.join(task.blocked_by) if task.blocked_by else '（无）'}",
            f"创建时间：{task.created_at or '（未知）'}",
            f"更新时间：{task.updated_at or '（未知）'}",
        ]
        return "\n".join(lines)
