# ch04: Agent Loop Tasks

> 任务粒度：每个任务可在一次会话内完成、可独立交付。本章为 mewcode ch04 蓝图的 sucode 移植（mewcode 全局替换为 sucode），编号续 ch03（T49 起）；全部任务已于 2026-10-29 落地（2026-10-30 T59/T61 修订：TUI 导入补齐 + 6 项流式/心跳回归，锚点已刷新），逐项标注真实文件 / 行号（落地实测值）。

## T49: docs/ch04 三件套（本章文档）

- 影响文件: `docs/ch04/spec.md`、`docs/ch04/tasks.md`、`docs/ch04/checklist.md`（全部新建；ch03 文档不动）
- 依赖任务: 无（初稿先行，行号锚点随 T61 终态刷新）
- 完成标准: spec 含 C55-C63 / N25-N30 / O24-O31；三件套与代码终态一致；锚点为 2026-10-29 落地实测值。

## T50: AgentEvent 事件家族 + 枚举 + providers 事件精简

- 影响文件: `sucode/agent.py:130-273`、`sucode/providers/base.py:143-173`、`sucode/providers/__init__.py:16-32`
- 依赖任务: 无
- 完成标准: `PermissionResponse(Enum)`（ALLOW/DENY/ALLOW_ALWAYS，agent.py:130）+ 14 个事件 `@dataclass`（ThinkingText/StreamText/ToolUseEvent/ToolOutputDelta/ToolHeartbeat/ToolResultEvent/Notice/PermissionRequest/RetryEvent/UsageEvent/HookEvent/TurnComplete/LoopComplete/ErrorEvent，agent.py:138-257），加复用 providers 的 ToolCallStart/ToolCallDelta 共 **16 类**并入 `AgentEvent` 联合（agent.py:259）；`FinishReason` 五值 Literal 迁入 agent.py；providers/base.py 的 StreamEvent 精简为 7 个 provider 级事件（:166，UsageUpdate 转正式生产 :143），MessageStart/UserMessage/RunError/RunFinished/RoundStart/Notice/ToolExecuted/ToolOutputDelta/ToolHeartbeat/PermissionRequest 从该文件删除；providers/__init__.py 导出同步修剪。

## T51: StreamCollector 与 LLMResponse

- 影响文件: `sucode/agent.py:281-341`
- 依赖任务: T50
- 完成标准: `StreamCollector.consume(stream)` 为 async generator：TextDelta→StreamText、ThinkingDelta→ThinkingText 映射；ToolCallStart/Delta 原样透传；SignatureDelta 只累积（C25）；UsageUpdate 记入轮次用量；StreamDone 记 finish_reason；finally 确定性 `stream.aclose()`（C33）；流尾 finalize 参数并逐个产出 ToolUseEvent。LLMResponse 含 text/thinking/signature/tool_calls/finish_reason/input_tokens/output_tokens（signature 为 sucode 特有）。

## T52: Provider 层 max_tokens 覆写 + usage 生产

- 影响文件: `sucode/providers/base.py:294-303`、`sucode/providers/anthropic_provider.py:94,131-139`、`sucode/providers/openai_provider.py:99-101,110-117,151-152`
- 依赖任务: 无
- 完成标准: `BaseProvider.set_max_output_tokens(n)` / `reset_max_output_tokens()` / `_effective_max_tokens()`（覆写优先，其次 profile.max_tokens）；anthropic 流结束后 `get_final_message().usage` 产出一次 UsageUpdate（:132-138）；openai 传 `stream_options={"include_usage": True}`（:101），**逐 chunk 探测 usage、非空即记**（:110-117，2026-10-30 修订二：DeepSeek 实测 usage 骑在最后一个内容 chunk 上、无独立空 chunk，原仅空 choices 分支捕获不到），StreamDone 前产出一次 UsageUpdate（:151-152）；两家 max_tokens 均取 `_effective_max_tokens()`。真实端点验证：DeepSeek provider 层 in=35 out=20、Agent 层 UsageEvent 本轮+累计正确。

## T53: ToolBatch + partition_tool_calls + StreamingExecutor

- 影响文件: `sucode/agent.py:344-419`
- 依赖任务: T50
- 完成标准: `ToolBatch(concurrent, calls)`（:345）+ `partition_tool_calls(tool_calls, registry)`（:352）——安全 = `tool.is_concurrency_safe and registry.is_enabled(name)`，相邻安全并入同批、不安全各自成批、批间原序，未知/禁用判不安全；`ExecOutcome`（:376，异常包装 from_exception）+ `StreamingExecutor.submit/collect_results`（:388）——提交保序、按序收集、Exception 包装不中断、CancelledError 穿透；ch04 主路径不消费（ch13 复用），临时脚本单测顺序与异常包装。

## T54: Agent 构造器 + run 主循环

- 影响文件: `sucode/agent.py:430-460,581-788`
- 依赖任务: T50-T53
- 完成标准: `Agent.__init__(session, work_dir=None, hook_engine=None)`（:430；agent_id=uuid4[:12]、session_dir=`<work_dir>/.sucode/sessions/<agent_id>`、_active 重入防护、取消信号、权限表、记忆计数、用量累计）；`async def run(self, text) -> AsyncIterator[AgentEvent]`（:581）——PLAN_EXIT_NOTICE 拼接、reset_max_output_tokens、session_start hook；循环内：取消检查点→LoopComplete(cancelled)、turn_start/pre_send hook、StreamCollector 转发、post_receive hook、UsageEvent（C59，未报不产出）、max_tokens 升档恢复（C57：首次 set 64000、半截+续写指令入史、RetryEvent、3 次上限、恢复轮不计 tools_used）、无工具→completed 收束（计划模式附 Notice）、MAX_TOOL_ROUNDS→剥离收束、工具轮分批执行、未知熔断（C58）、turn_end hook+TurnComplete；异常契约（N29）：CancelledError/GeneratorExit→配对收尾（不可 yield）+计数后 re-raise，ProviderError/未预期→finalize+ErrorEvent+LoopComplete(error)；全部终止点经 `_record_run_and_maybe_extract`（:789）计数并周期触发记忆提取（C52 延续）。

## T55: _execute_tool 生成器化 + 并发批合流 + 持久化整形

- 影响文件: `sucode/agent.py:806-1102`
- 依赖任务: T54
- 完成标准: `_execute_tools`（:806）分批执行 + 统一配对入史 + 中断/异常时 `_fill_interrupted`（:845）补齐「[已中断]」零孤儿后 re-raise（flags 置位）；`_execute_batch_parallel`（:857）泵合流——pump 信号量限流（4，N16）、事件合流队列按 id 交错（N25）、finally 投递完成哨兵防悬等、批收束 gather 上抛 pump 异常；`_execute_tool`（:893）async generator：plan 拦截（内置第一道）→ pre_tool_use hook 可阻断（"Hook rejected: {reason}"）→ 权限确认（yield PermissionRequest + await Future，60s 超时按 DENY；ALLOW/DENY/ALLOW_ALWAYS 三分支）→ emit 内部队列 + 心跳轮转执行（事件改 yield，C30/C31 延续）→ `_maybe_persist_or_truncate`（:1038，>8000 落 `_persist_tool_result` :1047；>30000 截断双保险）→ ToolResultEvent → post_tool_use hook；`_finalize_interrupted`（:1069）含恢复轮合并分支（撤回续写指令、半截并回、保持交替，N29）。

## T56: HookEngine 模块与八挂点接线

- 影响文件: `sucode/hooks.py`（全文件）、`sucode/agent.py:551-579` 及 run/_execute_tool 各挂点
- 依赖任务: T54
- 完成标准: `HOOK_EVENT_NAMES` 八挂点（hooks.py:36）、`HookRejection`（:49）、`HookContext`（:56）、`infer_file_path`（:71，仅 file_path→path 两字段，N27）、`HookEngine.register/run_hooks/notify/drain_notifications`（:80，pre_tool_use 首个否决生效、其余只观察）；Agent 侧 `_hook_events`（agent.py:551）drain 转 HookEvent、`_post_tool_hooks`（:555）post 挂点接线；八挂点全部触发且顺序正确（临时脚本断言）；plan 拦截不经引擎（C47 延续）；默认空引擎零行为变化。

## T57: teams.py + 四任务工具 + Category 扩展

- 影响文件: `sucode/teams.py`（全文件）、`sucode/tools/task_create.py`、`task_get.py`、`task_list.py`、`task_update.py`、`sucode/tools/base.py:40-43`
- 依赖任务: 无
- 完成标准: `VALID_STATUSES`（teams.py:18）、`STATUS_ICONS` ○◐●✕（:21）、`Task`（:44）、`TaskStore`（:57，JSON 原子写、损坏重建、create/get/list_tasks/update、id 自增 T-N、依赖引用校验、状态校验）、`TeamManager`（:192，`get_task_store` → `.sucode/teams/<name>/tasks.json`）；四工具 `TaskCreateTool`（task_create.py:28）/ `TaskGetTool`（task_get.py:15）/ `TaskListTool`（task_list.py:21）/ `TaskUpdateTool`（task_update.py:30）皆继承 Tool、`category="task"`、`is_concurrency_safe=True`、构造注入 team_manager + team_name；`Category` 扩展 4 值（tools/base.py:43）——权限确认与计划拦截走既有 else 分支零改动（脚本断言 task 类不触发确认）。

## T58: plan slug 命名

- 影响文件: `sucode/chat.py:22-45,108-121`
- 依赖任务: 无
- 完成标准: `_PLAN_ADJECTIVES`（24 词，:24）+ `_PLAN_NOUNS`（24 词，:29）+ `make_plan_slug_path()`（:36，`<cwd>/.sucode/plans/<形容词-名词>-<MMDD-HHMM>.md`）；`enter_plan_mode()` 无参生成 slug（父目录自动创建）、带参保持自定义路径；`DEFAULT_PLAN_FILE` 删除（ch03 承接值由 C63 替换）。

## T59: TUI async for 接入

- 影响文件: `sucode/tui/app.py:20-24,41-77,240-241,254-257,300-303,390-416,418-433,437-532`
- 依赖任务: T54
- 完成标准: `_run_stream` 队列消费删除，改 `@work async def _consume(text)`（:437）：`async for ev in self._agent.run(text)`（:456）直连；块生命周期惰性建块（ensure_view）+ TurnComplete（finalize+置空）/ LoopComplete（按 reason 收尾，error 配 ErrorEvent 暂存文案）双收尾；RetryEvent（:466）/ UsageEvent（:469）/ HookEvent（:476）渲染为系统行；ToolOutputDelta（:509）/ ToolHeartbeat（:511）流式反馈分支（**2026-10-30 修订**：补齐二者 agent 导入——真机动线曾因遗漏在 Bash 流式输出时 NameError 崩溃，ch04_verify T22 分支名静态核对防复发）；ToolUseEvent 不渲染（服务 headless）；`self._worker` 引用（:241）替代 agent.task——Esc（:431-433）与 on_unmount（:256-257）取消 worker；权限应答映射 PermissionResponse（:400-402）→ `resolve_permission`（:410）；HELP_TEXT 的 /plan 说明更新为 slug。

## T60: cli 装配更新

- 影响文件: `sucode/cli.py:58-87`
- 依赖任务: T57, T59
- 完成标准: `work_dir = Path.cwd()`（:73）；`TeamManager(work_dir)` + 四任务工具注册（team_name="default"，:77-84）；`Agent(session, work_dir=work_dir)`（:86）；C54 启动惰性不回退（parse_args 仍先于重型导入；新模块 hooks/teams 均轻量）——`--version` 实测 243ms、`import sucode.providers` 后无 SDK 进 `sys.modules`。

## T61: 端到端验证

- 影响文件: 无（临时验证脚本 `Temp\sucode_verify\ch04_verify.py`，不入库）
- 依赖任务: T49-T60
- 完成标准: `python -m compileall sucode` exit 0；ch04_verify **83 项全绿**（2026-10-29 首验 77 项：循环 12 / 升档 5 / 分批 3 + 执行器 2 / 持久化 3 / hook 5 / 权限 6 / 任务 6 / slug 4 / 计划拦截 3 / 事件家族 3 / 重入 4 / 恢复轮中断 3 / 思考留存 3 / 系统提示词 1；2026-10-30 修订新增 6 项：TUI 分支名静态核对 2 / 真实 Bash 流式 2 / 慢工具心跳 2）；C54 回归通过。真实 API 三场景 + TUI 动线留 checklist §4 真机终验。

## 进度

- [x] T49 docs/ch04 三件套（本文件 + spec.md + checklist.md）
- [x] T50 AgentEvent 16 类联合 + PermissionResponse + providers 精简 7 事件
- [x] T51 StreamCollector + LLMResponse
- [x] T52 Provider max_tokens 覆写 + usage 生产
- [x] T53 ToolBatch + partition_tool_calls + StreamingExecutor
- [x] T54 Agent 构造器 + run 主循环（升档/熔断/用量/双取消/配对/记忆）
- [x] T55 _execute_tool 生成器化 + 并发批合流 + 持久化整形
- [x] T56 HookEngine 八挂点
- [x] T57 teams.py + 四任务工具 + Category 扩展
- [x] T58 plan slug
- [x] T59 TUI async for 接入
- [x] T60 cli 装配更新
- [x] T61 端到端验证（compileall + ch04_verify 83 项 + C54 回归；真实 API / TUI 动线 2026-10-30 真机终验全过，见 checklist §4——ch04 收口）
