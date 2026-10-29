"""团队任务工具：TaskUpdate（spec(docs/ch04) C62，T57）。

更新任务字段；状态取值校验 VALID_STATUSES，依赖引用须指向已存在任务。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sucode.teams import VALID_STATUSES, TaskStore, TeamManager, render_status
from sucode.tools.base import Tool


class _TaskUpdateParams(BaseModel):
    task_id: str = Field(description="要更新的任务 id（如 T-1）")
    title: str | None = Field(default=None, description="新标题（可选）")
    description: str | None = Field(default=None, description="新描述（可选）")
    status: str | None = Field(
        default=None,
        description=f"新状态（可选，取值：{'/'.join(VALID_STATUSES)}）",
    )
    blocks: list[str] | None = Field(
        default=None, description="替换阻塞关系（可选，引用已存在任务 id）"
    )
    blocked_by: list[str] | None = Field(
        default=None, description="替换被阻塞关系（可选，引用已存在任务 id）"
    )


class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = (
        "更新共享任务板上的任务：可改标题、描述、状态与依赖关系（只传要改的字段）。"
        f"状态合法取值：{'、'.join(VALID_STATUSES)}。"
        "完成一个任务后把 status 改为 completed；开工时改为 in_progress。"
    )
    category = "task"
    is_concurrency_safe = True
    Params = _TaskUpdateParams

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    def _store(self) -> TaskStore:
        return self._team_manager.get_task_store(self._team_name)

    async def execute(
        self,
        task_id: str = "",
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> str:
        task = self._store().update(
            task_id,
            title=title,
            description=description,
            status=status,
            blocks=list(blocks) if blocks is not None else None,
            blocked_by=list(blocked_by) if blocked_by is not None else None,
        )
        changed = []
        if title is not None:
            changed.append("标题")
        if description is not None:
            changed.append("描述")
        if status is not None:
            changed.append(f"状态→{task.status}")
        if blocks is not None:
            changed.append("阻塞关系")
        if blocked_by is not None:
            changed.append("被阻塞关系")
        summary = "、".join(changed) or "（未改动任何字段）"
        return f"已更新任务 {task.id}（{summary}）：{render_status(task.status)} {task.title}"
