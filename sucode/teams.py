"""团队共享任务板（spec(docs/ch04) C62，T57）。

TaskStore / TeamManager 与四个任务工具（TaskCreate / TaskGet / TaskList / TaskUpdate，
见 sucode/tools/task_*.py）组成跨智能体共享任务板：任务以 JSON 文件持久化（原子写），
支持 blocks / blocked_by 依赖关系。定位与单进程待办不同——面向后续多智能体协作共享，
本章单智能体即可用；任务以「共享任务板」为单位，按团队名隔离存储。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from sucode.tools.base import ToolError

VALID_STATUSES = ("pending", "in_progress", "completed", "blocked")

# TaskList 渲染用的状态图标（T11 承接值）
STATUS_ICONS = {"pending": "○", "in_progress": "◐", "completed": "●", "blocked": "✕"}


def render_status(status: str) -> str:
    """状态 → 图标（未知状态兜底 pending 图标）。"""
    return STATUS_ICONS.get(status, "○")


def _now() -> str:
    """时间戳（created_at / updated_at 用）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _id_num(task_id: str) -> int:
    """任务 id 的数字部分（T-2 < T-10 排序用；非标准形态兜底 0）。"""
    tail = task_id[2:] if task_id.startswith("T-") else task_id
    try:
        return int(tail)
    except ValueError:
        return 0


@dataclass
class Task:
    """单个共享任务：标题/描述/状态 + blocks（我阻塞谁）/ blocked_by（我被谁阻塞）。"""

    id: str
    title: str
    description: str = ""
    status: str = "pending"
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class TaskStore:
    """JSON 文件持久化的任务仓库（原子写：临时文件 + replace）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._tasks: dict[str, Task] = {}
        self._next_id = 1
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        """启动时读入任务文件；损坏的存储按空板重建（下次 save 覆盖），不拖垮工具。"""
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._next_id = int(raw.get("next_id", 1))
            for item in raw.get("tasks", []):
                task = Task(
                    id=str(item["id"]),
                    title=str(item.get("title", "")),
                    description=str(item.get("description", "")),
                    status=str(item.get("status", "pending")),
                    blocks=[str(x) for x in item.get("blocks", [])],
                    blocked_by=[str(x) for x in item.get("blocked_by", [])],
                    created_at=str(item.get("created_at", "")),
                    updated_at=str(item.get("updated_at", "")),
                )
                self._tasks[task.id] = task
        except (OSError, ValueError, TypeError, KeyError):
            # json.JSONDecodeError 是 ValueError 子类；坏文件按空板重启
            self._tasks = {}
            self._next_id = 1

    def _save(self) -> None:
        payload = {
            "next_id": self._next_id,
            "tasks": [asdict(t) for t in self._sorted()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # 原子落盘，避免半截文件

    def _sorted(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: _id_num(t.id))

    def _check_refs(self, task_id: str, refs: list[str]) -> None:
        """依赖引用校验：引用的任务必须存在，且不能引用自身。"""
        for ref in refs:
            if ref == task_id:
                raise ToolError(f"任务不能依赖自身：{ref}")
            if ref not in self._tasks:
                raise ToolError(f"依赖任务不存在：{ref}（先创建该任务，或用 TaskList 核对编号）")

    # ---- 读写接口 ----

    def create(
        self,
        title: str,
        description: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> Task:
        title = (title or "").strip()
        if not title:
            raise ToolError("任务标题不能为空")
        task_id = f"T-{self._next_id}"
        blocks = list(blocks or [])
        blocked_by = list(blocked_by or [])
        self._check_refs(task_id, blocks)
        self._check_refs(task_id, blocked_by)
        now = _now()
        task = Task(
            id=task_id,
            title=title,
            description=(description or "").strip(),
            status="pending",
            blocks=blocks,
            blocked_by=blocked_by,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task_id] = task
        self._next_id += 1
        self._save()
        return task

    def get(self, task_id: str) -> Task:
        task = self._tasks.get((task_id or "").strip())
        if task is None:
            raise ToolError(f"任务不存在：{task_id}（用 TaskList 查看现有任务）")
        return task

    def list_tasks(self, status: str | None = None) -> list[Task]:
        tasks = self._sorted()
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> Task:
        task = self.get(task_id)
        if status is not None and status not in VALID_STATUSES:
            raise ToolError(f"非法状态 {status!r}，合法取值：{'、'.join(VALID_STATUSES)}")
        if blocks is not None:
            self._check_refs(task.id, list(blocks))
        if blocked_by is not None:
            self._check_refs(task.id, list(blocked_by))
        if title is not None:
            cleaned = title.strip()
            if not cleaned:
                raise ToolError("任务标题不能为空")
            task.title = cleaned
        if description is not None:
            task.description = (description or "").strip()
        if status is not None:
            task.status = status
        if blocks is not None:
            task.blocks = list(blocks)
        if blocked_by is not None:
            task.blocked_by = list(blocked_by)
        task.updated_at = _now()
        self._save()
        return task


class TeamManager:
    """按团队名提供 TaskStore：进程内缓存 + 每团队一个 JSON 文件。"""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = Path(work_dir)
        self._stores: dict[str, TaskStore] = {}

    def get_task_store(self, team_name: str) -> TaskStore:
        """取（或首建）指定团队的任务仓库：落 <work_dir>/.sucode/teams/<name>/tasks.json。"""
        name = (team_name or "").strip() or "default"
        if name not in self._stores:
            self._stores[name] = TaskStore(
                self._work_dir / ".sucode" / "teams" / name / "tasks.json"
            )
        return self._stores[name]
