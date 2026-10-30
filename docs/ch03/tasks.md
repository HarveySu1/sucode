# ch03: Agent 编排器与执行策略 Tasks

> **【已归档 · 勿作现行依据】** 本章任务对应的实现在 2026-10-30 被 ch04 重构：`Agent.start()`/`asyncio.Queue` 通道、`RunFinished` 收尾、`DEFAULT_PLAN_FILE` 均已退役。本文保留 ch03 当时原貌，**任务完成标准中的文件:行号锚点全部失效**；现行任务见 [ch04/tasks.md](../ch04/tasks.md)。
>
> 任务粒度: 每个任务可在一次会话内完成，可独立交付。完成标准精确到文件:行号（2026-10-24 落地后实测锚点）。
> T28-T43 代码已入库（2026-10-20/21）；T29 静态判定重构与 T44-T47 工具披露已于 2026-10-24 落地（mock 验证通过）；T48 启动路径惰性化 2026-10-29 落地（实测 −3.9s）。

## T28: Provider 层系统提示词传参
- 影响文件: `sucode/providers/base.py`、`sucode/providers/anthropic_provider.py`、`sucode/providers/openai_provider.py`
- 依赖任务: 无
- 完成标准:
 - `sucode/providers/base.py:393-399` `stream(self, history, tools=None, system="")` 签名带 `system`（空串不携带）；
 - Anthropic 端走顶层 `system=` 参数，OpenAI 兼容端在 messages 首位插入 `{"role": "system", ...}`；
 - system 不进 `Message` 历史（N22 零污染）。

## T29: 静态并发安全判定（2026-10-24 重构：动态入参判定 → 静态类属性）
- 影响文件: `sucode/tools/base.py`、`sucode/tools/file_tools.py`、`sucode/tools/search_tools.py`、`sucode/tools/bash_tool.py`、`sucode/agent.py`
- 依赖任务: 无（在库代码重构）
- 完成标准:
 - `sucode/tools/base.py:130` `Tool` 类属性区改为 `is_concurrency_safe: bool = False` 类属性，删除带 `arguments` 入参的判定方法；
 - `sucode/tools/file_tools.py:47` ReadFile 改类属性 `is_concurrency_safe = True`；WriteFile / EditFile 不覆写；
 - `sucode/tools/search_tools.py:68` Glob / `:107` Grep 改类属性 `is_concurrency_safe = True`；
 - `sucode/tools/bash_tool.py` 删除动态判定方法（Bash 恒不安全、走串行）；`READONLY_BASH_COMMANDS` / `BASH_META_CHARS` / `is_readonly_command`（60-82）保留，转供 C46 两处用途；
 - `sucode/agent.py:430-435` `_is_safe_call` 简化为 `registry.find` + 类属性读取（未知工具 → False），不再 `parse_arguments` 预校验。

## T30: 事件类型扩展
- 影响文件: `sucode/providers/base.py`
- 依赖任务: 无
- 完成标准: `sucode/providers/base.py:217-273` 定义 `FinishReason`（completed/interrupted/cancelled/error/max_rounds）、`UserMessage`（221）、`RunError`（228）、`RunFinished`（235），并全部并入 `StreamEvent` 联合（255-273）。

## T31: Agent 编排器与事件通道
- 影响文件: `sucode/agent.py`（新建）、`sucode/chat.py`（剥离循环）
- 依赖任务: T30
- 完成标准:
 - `sucode/agent.py:170-310` `Agent` 类：`start(text)`（292-306）建后台任务并返回 `asyncio.Queue`；`_run`（313-）跑 ReAct 循环，历史读写经 ChatSession；
 - 通道开头压 `UserMessage`、结尾由 `_finish`（384-392）压 `RunFinished`（唯一结束信号）；Provider 错误/未预期异常 → `RunError` + `RunFinished(error)` 不跨任务抛；`CancelledError`/`GeneratorExit` → 中断收尾（`_finalize_interrupted` 624-637）→ `RunFinished(interrupted)` 后 re-raise；
 - `sucode/chat.py:21-` `ChatSession` 退化为容器：history / profile / switch_profile / clear（`send()` 已删除）。

## T32: 分批执行器
- 影响文件: `sucode/agent.py`
- 依赖任务: T29、T31
- 完成标准:
 - `sucode/agent.py:418-431` `_plan_batches`：相邻安全调用合并为一批，不安全各自成批，批间保持原顺序；
 - `sucode/agent.py:475-489` `_run_parallel_batch`：`asyncio.gather` + `asyncio.Semaphore(CONCURRENCY_LIMIT)`（上限 4，`:73`）；
 - `sucode/agent.py:433-473` `_execute_tools`：批完成统一配对入史；中断/未预期异常时已完成保留真实结果、被取消与未开始批整批配对「[已中断]」（零孤儿）后 re-raise。

## T33: 拦截位机制
- 影响文件: `sucode/agent.py`
- 依赖任务: T31
- 完成标准:
 - `sucode/agent.py:112-114` 定义 `PreToolHook`（返回 ToolResult 即否决）/ `PostToolHook`（只观察）类型；
 - `sucode/agent.py:181-182` 构造注入钩子列表，`_plan_intercept` 为首个内置前置钩子，默认直通；
 - `sucode/agent.py:507-518` 前置否决结果直接配对入史 + `ToolExecuted` 照常产出；`:589-591` 后置钩子观察（含否决结果）。

## T34: plan-only 模式
- 影响文件: `sucode/chat.py`、`sucode/agent.py`
- 依赖任务: T28、T33
- 完成标准:
 - `sucode/chat.py:17` `DEFAULT_PLAN_FILE = "plan.md"`；`:72-95` 模式状态（plan_mode / plan_file / enter_plan_mode / exit_plan_mode / take_plan_exit_pending）；
 - `sucode/agent.py:249-254` `_system_prompt` 按模式装配（`NORMAL_SYSTEM_PROMPT` `:76-79` / `PLAN_SYSTEM_PROMPT_TEMPLATE` `:80-89`）；
 - `sucode/agent.py:256-282` `_plan_intercept`：写类目标 ≠ 计划文件 → `PLAN_WRITE_INTERCEPTED`（`:91-94`）；命令类非白名单 → `PLAN_COMMAND_INTERCEPTED`（`:95-97`）；计划文件精确匹配放行（N20）；
 - `sucode/agent.py:284-288` `_notify_plan_finished` 收尾压 Notice（`PLAN_DONE_NOTICE` `:98`）；
 - `sucode/agent.py:300-302` `/exec` 后首次请求拼接 `PLAN_EXIT_NOTICE`（`:101`，一次生效）。

## T35: 取消信号
- 影响文件: `sucode/agent.py`
- 依赖任务: T31
- 完成标准:
 - `sucode/agent.py:184` `self._cancel_event = asyncio.Event()`；`:194-200` `request_cancel()` 对外触发；
 - `_run` 每轮开始前检查信号 → 命中干净退出（`RunFinished(cancelled)`）；任务取消路径不变（`RunFinished(interrupted)`）；`:303` 每次 start 清空信号（跨运行不复用）。

## T36: TUI 接入事件通道（接入主流程）
- 影响文件: `sucode/tui/app.py`、`sucode/cli.py`
- 依赖任务: T31、T32、T33、T34、T35
- 完成标准:
 - `sucode/cli.py:49-62` `boot()` 装配 `ChatSession(provider, create_default_registry())` + `Agent(session)` + TUI；
 - `sucode/tui/app.py:274-292` `on_submitted`：`session.send` 改 `agent.start`；`:424-` `_run_stream` 队列消费循环，`RunFinished` 驱动收尾与 `_streaming` 复位；
 - `sucode/tui/app.py:294-330` 命令路由 `/plan [文件]` / `/exec`，`:249-252` 顶栏随模式显示；Esc = `request_cancel()` + `task.cancel()` 双触发（`action_interrupt` `:405-`）。

## T37: 端到端验证（ch03 主体）
- 影响文件: 临时验证脚本（不入仓库）、`docs/ch03/checklist.md`
- 依赖任务: T28-T36
- 完成标准: mock 回归全量（Phase 1 12 + Phase 2 26 + 本章新增）+ 真实 API 三场景（多读类并发 / 计划模式全流程 / Esc 后 /new 无 400）；checklist 对应区逐条勾选。

## T38: 权限请求事件与 Future 应答机制
- 影响文件: `sucode/providers/base.py`、`sucode/agent.py`
- 依赖任务: T30
- 完成标准:
 - `sucode/providers/base.py:242-252` `PermissionRequest(request_id, tool_name, summary, future)` 并入 `StreamEvent`（`:272`）；
 - `sucode/agent.py:186-187` 待应答表 `_pending_permissions` + 会话授权集 `_permission_granted`；
 - `sucode/agent.py:204-208` `resolve_permission(request_id, verdict)`（已失效请求静默忽略，N23）；
 - `sucode/agent.py:228-245` `_request_permission`：建 Future、压事件、`asyncio.wait_for(timeout=PERMISSION_TIMEOUT)`（`:104` 60s）、超时按 deny、finally 清表。

## T39: 写类/命令类执行前权限确认
- 影响文件: `sucode/agent.py`
- 依赖任务: T38
- 完成标准:
 - `sucode/agent.py:211-217` `_needs_permission`：write 恒确认；command 且非白名单确认（复用 `is_readonly_command`，C46）；read 不确认；
 - `sucode/agent.py:220-226` `_permission_summary` 单行摘要（命令类给命令、写类给路径）；
 - `sucode/agent.py:520-546` `_run_tool` 权限块（前置钩子之后、执行之前）：已授权/计划模式跳过；allow 照常执行；always 记授权集；deny → `PERMISSION_DENIED`（`:105`）配对入史 + `ToolExecuted(ok=False)` + 后置钩子观察。

## T40: TUI 权限确认应答
- 影响文件: `sucode/tui/app.py`
- 依赖任务: T38、T39
- 完成标准:
 - `sucode/tui/app.py:229` `_pending_permission` 待确认态；`:458-487` `_run_stream` 收到 `PermissionRequest` → 醒目消息（工具名 + 摘要 + y/n/a 指引），同 id `ToolExecuted` 到达清理待确认态；
 - `sucode/tui/app.py:277-279` 待确认态下输入框单行路由（不受生成中拦截影响）；`:379-397` `_resolve_permission`：y/yes→allow、n/no→deny、a/always→always，无效输入提示用法且不进消息流；
 - `sucode/tui/app.py:405-421` Esc 清理待确认态（Future 悬空无害，N23）。

## T41: 记忆提取触发器
- 影响文件: `sucode/agent.py`
- 依赖任务: T31
- 完成标准:
 - `sucode/agent.py:189-190` `_run_count` + `_memory_tasks` 任务集；
 - `sucode/agent.py:384-392` `_finish` 统一出口计数（全部终止原因均计），`% EXTRACT_EVERY_N_RUNS == 0` 触发；
 - `sucode/agent.py:394-401` `_schedule_memory_extraction`：历史快照传参、空历史不触发、`create_task` + 任务集跟踪防 GC。

## T42: 记忆提取实现
- 影响文件: `sucode/memory.py`（新建）
- 依赖任务: T41
- 完成标准:
 - `sucode/memory.py:16-17` `MEMORY_FILE = "project_memory.md"` / `EXTRACT_EVERY_N_RUNS = 5`；`:20-24` `MEMORY_SYSTEM_PROMPT`（只输出条目、无内容输出「（无）」）；
 - `sucode/memory.py:27-50` `extract_memory`：`provider.stream(history, None, MEMORY_SYSTEM_PROMPT)` 收集 TextDelta → 追加写 cwd 下记忆文件（`## YYYY-MM-DD HH:MM` 段落）；空输出/「（无）」不写；异常静默吞掉、CancelledError 穿透。

## T43: 接入主流程与端到端验证（C51/C52）
- 影响文件: 临时验证脚本（不入仓库）、`docs/ch03/checklist.md`
- 依赖任务: T38-T42
- 完成标准: mock 全路径（三值应答/超时/计划模式不确认/always 会话授权/记忆周期触发/不阻塞/失败静默）+ 真实 API 三场景（允许落盘/拒绝未落盘/连续 5 轮生成 project_memory.md）；checklist 对应区逐条勾选。

## T44: deferred 工具披露协议（2026-10-24 新增）
- 影响文件: `sucode/tools/__init__.py`、`sucode/tools/base.py`
- 依赖任务: T29
- 完成标准:
 - `sucode/tools/__init__.py:32` `ToolRegistry.__init__` 增 `self._discovered: set[str]`；
 - 动态管理区后新增 `mark_discovered(name)`（79-83）/ `is_discovered(name)`（85-86）/ `get_deferred_tool_names()`（88-96，should_defer 且未 discovered 且未 disabled 的工具名）；
 - 新增 `search_deferred(query, max_results=DEFAULT_DEFERRED_TOP_K)`（107-119，按名中 +10 / 描述中 +5 / 分词 +3/+1 打分倒序裁剪）与 `find_deferred_by_names(names)`（98-105，仅返回 deferred 工具）；
 - `get_all_schemas(protocol)`（123-147）去掉 `query`/`top_k` 形参，跳过未披露 deferred；`_select_tools` 已删除；
 - `sucode/tools/base.py:99` `relevance_score` 评分规则替换为名中 +10 / 描述中 +5 / 分词 +3/+1（供 `search_deferred` 使用）。

## T45: ToolSearchTool（2026-10-24 新增）
- 影响文件: `sucode/tools/tool_search.py`（新建）
- 依赖任务: T44
- 完成标准:
 - `Params(query: str, max_results: int = DEFAULT_DEFERRED_TOP_K)`；`name="ToolSearch"`、`category="read"`、`should_defer=False`（自身从不延迟）；持有 `registry` 与 `protocol`；
 - `execute`：`select:Name1,Name2` 前缀走 `find_deferred_by_names`，否则走 `search_deferred`；命中后逐个 `registry.mark_discovered` 并回传 `Found N tool(s)...` + JSON 序列化 schema；未命中回 `No matching deferred tools for "<q>". Available: <names>`。

## T46: 工具披露接入主流程（2026-10-24 新增）
- 影响文件: `sucode/cli.py`、`sucode/agent.py`
- 依赖任务: T44、T45
- 完成标准:
 - `sucode/cli.py:62-64` 装配区注册 `ToolSearchTool(registry, protocol=profile.protocol)`；
 - `sucode/agent.py:253-263` `_system_prompt`：存在未披露 deferred 时追加一行「可用 ToolSearch 检索的延迟工具：<names>」（`_deferred_hint` `:265-278`，N22 不入史）；
 - `sucode/agent.py:633-641` `_tool_schemas` 去 `query` 形参；`_consume_round`（`:614-631`）同步去 query 传参。

## T47: 端到端验证（T29 重构 + C53）
- 影响文件: 临时验证脚本（不入仓库）、`docs/ch03/checklist.md`
- 依赖任务: T29、T44、T45、T46
- 完成标准:
 - `python -m compileall sucode` 通过；mock 回归不破坏（Phase 1 12 + Phase 2 26 + ch03 已有用例）；
 - mock 披露流转全路径：deferred 不在初始 schema → `select:`/关键词命中 → `mark_discovered` → 下一轮 schema 出现；ToolSearch 为 read 类（不触发权限、不被计划模式拦截）；
 - mock 并发矩阵重验（静态判定下 Bash 全串行、ReadFile/Glob/Grep 并发）；
 - 真实 API 回归：六件套 schema 列表行为不变、权限/计划/记忆不回归；checklist 逐条勾选。

## T48: 启动路径惰性化（2026-10-29 新增）
- 影响文件: `sucode/cli.py`、`sucode/providers/__init__.py`
- 依赖任务: 无（在库代码重构）
- 完成标准:
 - `sucode/cli.py:55` `args = build_parser().parse_args(argv)` 位于全部 `from sucode...` 重型导入之前——`--version` / `--help` 在解析期退出，不再加载 Provider SDK 与 TUI 栈（此前实测全量导入约 4.1s）；
 - `sucode/providers/__init__.py` 删除 `AnthropicProvider` / `OpenAIProvider` 顶层 `from ..._provider import ...`；`_PROVIDERS`（`:33-37`）改为 `protocol → (模块名, 类名)`；
 - `get_provider`（`:40-49`）经 `importlib.import_module` 按 protocol 首次使用时导入；未知 protocol 报错文案不变；`__all__` 移除两个实现类（无外部消费者，`grep` 复核）；
 - 验收（2026-10-29 实测）：`python -m sucode --version` 4.1s → **113-119ms**；openai profile 完整装配导入 **1257ms**（anthropic profile 3398ms，其中 SDK 自身 2649ms 不可再降）；`import sucode.providers` 后 `anthropic`/`openai` 均不在 `sys.modules`；
 - 回归：69 项审计断言（49 + 19 + 1）全绿，`compileall` exit 0。

---

## 修订记录
- 2026-10-20：初版。事件通道取队列广播；分批按需求方伪代码（相邻安全合并、不安全各自成批）；计划模式收尾为提示 + 手动切换（无审批 UI）；headless CLI 入口本章不做。
- 2026-10-21：C47 修订（/exec 后首次请求拼接模式切换提示，一次生效）；新增 C51/C52（权限确认 + 记忆提取），T38-T43。
- 2026-10-24：对齐 mewcode ch03 写法重构——C45 并发判定由动态入参判定改为静态类属性（T29 重构，Bash 全串行；白名单收窄为 C46 两处用途）；新增 C53 工具披露（T44-T47：discovered 集 + search/select 双查询 + ToolSearchTool，替换原按提问打分自动暴露）；文档形态统一为行号锚点 + 可执行验证命令。
- 2026-10-29：新增 C54 / T48 启动路径惰性化——`parse_args` 前置于重型导入、Provider 按 protocol 惰性导入；`--version` 冷启动 4.1s → 0.12s，openai 装配 4.1s → 1.26s。
