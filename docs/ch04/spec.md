# ch04: Agent Loop Spec

> 2026-10-29 初版。对齐 mewcode ch04（Agent Loop）蓝图，mewcode 全局替换为 sucode；编号沿用跨章连续（C/N/O/T 续 ch03：C55 起 / N25 起 / O24 起 / T49 起），不随本章重起。
> 架构决策（2026-10-29 用户拍板）：D1 对外接口对齐 mewcode 异步生成器——ch03 C42/C43 的 asyncio.Queue 通道退役；D2 四项能力全纳入（循环健壮性包 / 超长结果持久化 / HookEngine 八挂点 / 团队任务四工具）；D3 计划模式仅引入 slug 命名，提醒机制保持 system 全量承载，命令名保持 /plan 与 /exec。
> 2026-10-30 修订：TUI 补齐 ToolOutputDelta / ToolHeartbeat 导入（行为规格不变，真机动线暴露的导入遗漏）；行号锚点随刷新。
> 2026-10-30 修订二（C59）：openai 端 usage 捕获改为**逐 chunk 探测、非空即记**——DeepSeek 实测 usage 骑在最后一个内容 chunk 上（无独立空 choices chunk），与 OpenAI 规范形态不同，原「仅空 choices 分支捕获」在 DeepSeek 端点失效；两种形态现均通吃。

## 1. 背景

ch03 交付了以 asyncio.Queue 事件通道广播的 ReAct 编排器：后台任务跑循环、RunFinished 为唯一结束信号、TUI 从队列消费。mewcode 的对应章（ch04: Agent Loop）以「`async for event in agent.run(conversation)` 异步生成器 + AgentEvent dataclass 家族」暴露同一过程，并明确不引入显式队列。本章按此蓝图把 sucode 的对外接口重构为生成器形态，同时移植其循环健壮性包（max_tokens 升档恢复 / 未知工具熔断 / token 用量上报）、超长工具结果持久化、HookEngine 八挂点、团队共享任务板四工具与 plan 文件 slug 命名。重构期间 ch02/ch03 既有硬约束全部延续：C25 thinking 签名、C28 工具轮数上限、C29 中断收尾、C33 流关闭、C47 计划模式、C48 双取消、C51 权限确认、C52 记忆提取、C53 工具披露。

## 2. 目标

交付 `agent.Agent.run(text)` 异步生成器与 AgentEvent 事件家族：TUI 用 `async for` 直连消费，块生命周期改为惰性建块 + TurnComplete/LoopComplete 双收尾；StreamCollector 把 provider 流折叠为轮次结构（含 anthropic 思考签名）；finish_reason=max_tokens/length 时一次性升档 64000 并注入续写指令，最多 3 次恢复轮（RetryEvent）；连续 3 轮仅请求未知工具即熔断终止；每轮上报 token 用量（本轮 + 会话累计）；工具结果超 8000 字符持久化到 `.sucode/sessions/<agent_id>/` 并回灌预览；HookEngine 提供 session_start / turn_start / pre_send / post_receive / pre_tool_use / post_tool_use / turn_end / session_end 八挂点（pre_tool_use 可阻断，通知经 HookEvent 上浮）；TaskCreate / TaskGet / TaskList / TaskUpdate 四工具经 TeamManager/TaskStore 落 JSON 任务板（blocks/blocked_by 依赖关系）；`/plan` 无参时惰性生成 `.sucode/plans/<形容词-名词>-<MMDD-HHMM>.md` 可读 slug。

## 3. 功能需求

- C55: 生成器事件接口：`Agent.run(text)` 为 `async def ... -> AsyncIterator[AgentEvent]`（`sucode/agent.py:581`），调用方 `async for` 消费，生成器自然终止即结束；AgentEvent 为 16 个事件类的类型联合（`sucode/agent.py:259`，其中 ToolCallStart / ToolCallDelta 复用 providers 定义做参数流式透传，ch02 C32 的 UX 保留）：ThinkingText / StreamText / ToolCallStart / ToolCallDelta / ToolUseEvent / ToolOutputDelta / ToolHeartbeat / ToolResultEvent / Notice / PermissionRequest / RetryEvent / UsageEvent / HookEvent / TurnComplete / LoopComplete / ErrorEvent；`LoopComplete(reason)` 为唯一自然终止信号；**ch03 C42/C43 修订**——asyncio.Queue 通道与 RunFinished / RunError / RoundStart / UserMessage / ToolExecuted / MessageStart 等编排事件退役，`providers/base.py` 的 StreamEvent 精简为 7 个 provider 级事件；`PermissionResponse` 枚举（ALLOW / DENY / ALLOW_ALWAYS，agent.py:130）替代字符串应答（N30）；运行中重复 run 抛 RuntimeError（重入防护，替代 ch03 的后台任务存活检查）。
- C56: StreamCollector 流折叠（`sucode/agent.py:294`）：TextDelta / ThinkingDelta 映射为 StreamText / ThinkingText；ToolCallStart / ToolCallDelta 原样透传（参数流式）；SignatureDelta 只累积不外发；UsageUpdate 记入轮次用量；StreamDone 记 finish_reason；流尾把参数碎片拼进 ToolCall 并逐个产出 ToolUseEvent（对齐 mewcode ToolCallComplete → ToolUseEvent）；LLMResponse 含 text / thinking / signature / tool_calls / finish_reason / input_tokens / output_tokens（signature 为 sucode 特有，服务 C25）；底层流的确定性 aclose 由 collector 负责（C33 延续）。
- C57: max_tokens 升档恢复：finish_reason ∈ {"max_tokens", "length"} 且恢复次数 <3 时——首次调 `provider.set_max_output_tokens(64000)`（每次运行至多一次升档），assistant 半截入史 + 注入续写指令（RESUME_INSTRUCTION，随该轮 user 消息入史）+ 产出 RetryEvent，continue 进入恢复轮；3 次后不再升档、走主循环逻辑收束；每次运行开始 `reset_max_output_tokens()` 回到 profile 配置；恢复轮不计 tools_used（升档只发生在纯文本轮）；恢复轮被中断时按 N29 的合并规则收尾。
- C58: 未知工具熔断：某轮全部工具调用均为未知/禁用工具（registry.find 为 None）则 streak+1，任一已知工具清零；streak==3 → ErrorEvent + LoopComplete(error) 终止（幻觉死循环防护；未知工具的单次执行仍返回结构化错误供模型自愈，熔断只针对连续整轮全未知）。
- C59: token 用量上报：两家 provider 每流结束产出一次 UsageUpdate（anthropic 经 `get_final_message().usage`；openai 经 `stream_options={"include_usage": True}`，**逐 chunk 探测 usage、非空即记**——兼容 OpenAI 规范的末尾空 choices 独立 chunk 与 DeepSeek 的「骑在最后内容 chunk 上」两种形态，2026-10-30 修订二）；Agent 每轮累计会话总量并产出 UsageEvent（本轮值 + 累计值，四字段）；provider 未报用量则不产出（零噪音）。
- C60: 超长工具结果持久化：结果长度 > SINGLE_RESULT_CHAR_LIMIT（8000）时全文持久化到 `work_dir/.sucode/sessions/<agent_id>/<时间戳>-<随机>.md`，回灌改预览（首尾各 500 字符 + 「…（完整输出 N 字符已保存：<路径>）」）；落盘失败退化为截断（不阻塞主流程）；> MAX_OUTPUT_CHARS（30000）的头尾截断兜底保留（safe_execute 已先行截断，双保险）；agent_id = uuid4().hex[:12]，session 目录首次持久化时创建。
- C61: HookEngine 八挂点（`sucode/hooks.py`）：session_start / turn_start / pre_send / post_receive / pre_tool_use / post_tool_use / turn_end / session_end（HookContext 带 event / tool_name / args / file_path / result / round / text）；**仅 pre_tool_use 可阻断**——钩子返回 HookRejection 即否决该次调用（结果回灌 "Hook rejected: {reason}"，配对入史），其余挂点只观察；`engine.notify` 排队界面消息、Agent 每挂点执行后 drain 转 HookEvent 上浮；默认空引擎零行为变化；plan 拦截仍为内置第一道、不经引擎（C47 延续）；hook 配置文件加载不做（O28）。
- C62: 团队共享任务板：Task（id / title / description / status / blocks / blocked_by / created_at / updated_at）、TaskStore（JSON 原子落盘 `<work_dir>/.sucode/teams/<name>/tasks.json`，临时文件 + replace；损坏文件按空板重建）、TeamManager.get_task_store；四工具 TaskCreate / TaskGet / TaskList / TaskUpdate（`sucode/tools/task_*.py`）继承 Tool、`is_concurrency_safe=True`、`category="task"`（Category 扩展为 4 值——不触发权限确认、计划模式放行，既有 `_needs_permission` / `_plan_intercept` 的 else 分支零改动）；状态校验 VALID_STATUSES（pending / in_progress / completed / blocked）、依赖引用须存在且不得自引用；TaskList 按 ○◐●✕ 渲染；cli 默认注册（team_name="default"）。
- C63: plan slug 命名：`/plan` 无参时 `enter_plan_mode` 惰性生成 `<cwd>/.sucode/plans/<形容词>-<名词>-<MMDD-HHMM>.md`（24 形容词 + 24 名词词表，父目录自动创建）；带参保持自定义路径（当次会话生效）；提醒机制维持 PLAN_SYSTEM_PROMPT 全量随 system 每轮携带（不引入 build_plan_mode_reminder 周期注入与 /do 命令）；`/plan` 与 `/exec` 命令名不变。ch03 承接值「缺省 plan.md」由本条替换。

## 4. 非功能需求

- N25: 并发批合流：安全批每工具一个 pump 任务（`Semaphore(CONCURRENCY_LIMIT)=4` 限流，N16 延续），事件经合流队列按到达序转发；多工具增量输出/心跳按调用 id 交错（N21 延续）；pump 无论正常/异常/取消都投递完成哨兵，主循环不悬等；批间按 partition 原序执行；pump 内异常在批收束时统一上抛（保持 ch03「异常中断整轮」语义）。
- N26: StreamingExecutor 顺序保证：`submit` 保序 + `collect_results` 按提交顺序收集；任何 Exception 包装为 ExecOutcome(is_error) 不中断整批；CancelledError 穿透（配对交上层 N18）；本章主路径不消费本类，供 ch13 SubAgent / Teams 复用。
- N27: hook 路径推断精简：`infer_file_path` 仅取 file_path → path 两字段（对齐 mewcode N4），只供 hook 匹配，不做全参数扫描。
- N28: slug 可读性：词表全小写英文、时间戳区分同日多次计划；**每次进入计划模式各生成一份**（区别于 mewcode 的 Agent 级 `_plan_path_cache` 单例——sucode 计划状态在 ChatSession，per-entry 生成同样满足「人眼可区分多份历史 plan」目标）。
- N29: 生成器取消契约：温和路径（request_cancel 轮间检查点）→ `LoopComplete("cancelled")`；硬路径（消费者 cancel / aclose）→ CancelledError / GeneratorExit 沿生成器传播，配对收尾在 except 内完成且**不可 yield**——硬取消不产出终止事件，消费者以异常感知中断；工具阶段中断时未完成调用统一配对「[已中断]」（N18 延续；`flags["tools_interrupted"]` 置位防止上层二次回滚制造孤儿）；恢复轮被中断时撤回续写指令、半截并回上一条 assistant（保持 user/assistant 交替与截断标记）；`FinishReason.interrupted` 仅保留枚举值、生成器契约下不产出。
- N30: 权限应答枚举化：PermissionResponse 三值经 `resolve_permission` 回填 Future；对已失效请求（超时已拒 / 已取消）静默忽略（N23 延续）；等待超时 60s 按 DENY；ALLOW_ALWAYS 记会话级授权集（/new 不清除，重启失效，C51 延续）。

## 5. 设计概要

- 核心数据结构（`sucode/agent.py`）：AgentEvent 联合（:259）、LLMResponse（:282）、StreamCollector（:294）、ToolBatch（:345）+ `partition_tool_calls`（:352）、ExecOutcome（:376）+ StreamingExecutor（:388）、Agent（:430；字段：_session / _work_dir / _hooks / _agent_id / _session_dir / _active / _cancel_event / _pending_permissions / _permission_granted / _run_count / _memory_tasks / _total_input / _total_output）。
- 主流程（一次 run）：
 1. 重入防护 → PLAN_EXIT_NOTICE 拼接（C47 修订延续）→ 用户消息入史 → `reset_max_output_tokens` → session_start hook；
 2. 循环：取消检查点（cancelled 收束）→ turn_start / pre_send hook → StreamCollector 消费 provider 流（C56 边收边转发）→ post_receive hook → UsageEvent（C59）→ max_tokens 升档恢复（C57）→ 无工具调用即 completed 收束（计划模式附收尾 Notice）→ MAX_TOOL_ROUNDS 拒绝收束（C28 延续）→ assistant(tool_calls) 入史 → partition 切批执行（串行 / 并发批）→ 结果统一配对入史 → 未知工具熔断（C58）→ turn_end hook + TurnComplete；
 3. 异常契约（N29 / N19 修订）：CancelledError / GeneratorExit 配对后 re-raise；ProviderError / 未预期异常 → finalize + ErrorEvent + LoopComplete(error)；
 4. 全部终止点计数并周期触发记忆提取（C52 延续，含硬取消路径）。
- 调用链：
 - TUI：`on_submitted` → `self._worker = self._consume(text)`（`sucode/tui/app.py:303`）→ `async for ev in self._agent.run(text)`（:456）→ isinstance 分支渲染（惰性建块 + Turn/LoopComplete 双收尾；Retry / Usage / HookEvent 系统行）；
 - Esc：`request_cancel`（轮间）+ `worker.cancel`（即时）双触发（:431-433，N29）；
 - 权限：PermissionRequest 事件（TUI 挂起应答态）→ y/n/a 映射 PermissionResponse（:400-402）→ `agent.resolve_permission`（:410 → agent.py:471）；
 - 装配：`cli.boot` → `TeamManager(work_dir)` + 四任务工具注册（`sucode/cli.py:77-84`）→ `Agent(session, work_dir)`（:86）→ SucodeApp。
- 与其他模块的交互：
 - 依赖：sucode.chat（会话容器 + slug）、sucode.hooks（引擎）、sucode.teams（任务板）、sucode.providers（7 事件流 + max_tokens 覆写 + usage 生产）、sucode.tools（Registry + 六件套 + ToolSearch + 任务四工具）、sucode.memory（提取协程）；
 - 被依赖：sucode/tui/app.py（async for 消费）、sucode/cli.py（装配）；
 - hooks / teams 不反向依赖 agent（底层约束延续 ch02）。

## 6. Out of Scope

- O24: auto_compact 压缩、CompactNotification 与 Layer1/2 结果预算（压缩需求出现前不做；工具层 30000 截断已兜底）。
- O25: 环境上下文注入与长记忆注入系统提示词（ch03 O18/O22 延续；记忆提取结果仍不注入）。
- O26: SubAgent / Fork / 递归智能体（ch13；StreamingExecutor 只留机制与单测）。
- O27: Worktree 隔离（ch14；plan 文件直接落 cwd 下 .sucode/plans）。
- O28: hook 配置文件加载与治理（引擎机制先行，运行期 register 面已具备）。
- O29: build_plan_mode_reminder 周期注入与 /do 命令（system 全量承载已覆盖提醒需求）。
- O30: 权限规则引擎持久化授权（ch03 O17 延续；本期仍交互确认 + 会话级 always）。
- O31: Agent 当工具递归调用与 coordinator_mode / team_name / _team_manager / context_window / instructions_content / memory_manager 等 mewcode 多智能体字段（ch13 前不引入，杜绝死代码）；事件多播 fan-out（ch03 O20 延续）。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。
