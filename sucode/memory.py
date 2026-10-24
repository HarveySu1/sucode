"""项目记忆提取（spec(docs/ch03) C52，2026-10-21 修订新增）。

每 EXTRACT_EVERY_N_RUNS 次编排运行后由 Agent 以 fire-and-forget 任务调用：
对会话历史快照做一次 LLM 总结提取，追加写入工作目录下 MEMORY_FILE。
- 不阻塞主流程；任何失败静默吞掉（提取失败不影响对话，CancelledError 穿透不吞）
- 追加式不去重不合并（L11）；提取结果不注入系统提示词（O22，组装留后续章节）
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sucode.providers.base import BaseProvider, Message, TextDelta

MEMORY_FILE = "project_memory.md"  # 工作目录下的项目记忆文件（checklist 承接值）
EXTRACT_EVERY_N_RUNS = 5  # 提取周期：每 N 次编排运行（全部终止原因均计数，checklist 承接值）

# 提取提示词（checklist 承接值）：只输出确定可复用的条目，无内容输出「（无）」
MEMORY_SYSTEM_PROMPT = (
    "你是项目记忆提取器。请从下面的对话历史中提炼这个项目的持久有效信息："
    "项目规则、工程约定、技术约束、用户偏好。只输出确定且可复用的条目，"
    "每条一行、以「- 」开头；没有值得记录的内容就只输出「（无）」。不要输出其他解释。"
)


async def extract_memory(provider: BaseProvider, history: list[Message]) -> None:
    """一次提取：历史快照 → LLM 流式总结 → 追加写入记忆文件。

    任何异常静默吞掉（fire-and-forget 语义）；空输出不写文件。
    """
    try:
        parts: list[str] = []
        stream = provider.stream(list(history), None, MEMORY_SYSTEM_PROMPT)
        try:
            async for ev in stream:
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
        finally:
            await stream.aclose()
        text = "".join(parts).strip()
        if not text or text == "（无）":
            # 空输出 / 模型按提示词约定表态「（无）」（无值得记录的内容）：不写文件
            return
        path = Path.cwd() / MEMORY_FILE
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {stamp}\n{text}\n")
    except Exception:  # noqa: BLE001 fire-and-forget：提取失败静默，不影响主流程
        pass
