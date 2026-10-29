# ch03: Agent 编排器与执行策略 Spec

> 2026-10-20 初版；2026-10-21 修订新增 C51/C52；2026-10-24 重构：C45 并发判定由「按入参动态判定」改为静态类属性（对齐 mewcode），新增 C53 工具披露（deferred + ToolSearch），C46 白名单用途随之收窄。编号沿用跨章连续（C/N/O/T 续 ch02），不随本章重起。2026-10-29 新增 C54（启动路径惰性化）。

## 1. 背景

Phase 2 落地了工具系统与 ReAct 循环，但循环长在会话对象里、以异步生成器直接喂给唯一消费者（TUI），工具一律串行执行，也没有「模式」概念。ch03 把循环升格为独立的**编排器**：过程事件经通道广播（为后续 headless CLI 铺路）、同轮工具按并发安全性分批执行、新增只规划不执行的计划模式、外部取消信号可传播到循环、工具执行前后留拦截位、Provider 层支持最小系统提示词；并在执行策略上补齐三块——风险操作权限确认（C51）、周期性项目记忆提取（C52）、deferred 工具披露协议（C53，供后续 MCP/Skill 等大批量工具挂载）。

## 2. 目标

交付 `agent.Agent` 编排器与 `asyncio.Queue` 事件通道：ReAct 循环在后台任务运行，事件按序压入通道，`RunFinished` 为唯一结束信号；同轮工具调用按静态 `is_concurrency_safe` 类属性切批，安全批信号量限流并发、不安全调用各自成批串行；计划模式经内置前置钩子拦截写类与非白名单命令；Esc（任务取消）与取消信号（轮间检查点）双中断路径共用配对不变量；写类与白名单外命令执行前经 `PermissionRequest` 事件 + 同进程 Future 应答确认；每 5 次运行后台提取项目记忆追加写入 `project_memory.md`；deferred 工具经 `ToolSearch` 按需披露、命中即 `mark_discovered` 进入后续 schema 列表。

## 3. 功能需求

- C42: Agent 编排器独立：ReAct 循环从会话对象迁出为独立编排器（`sucode/agent.py`）；`ChatSession` 退化为历史 + profile + 模式状态的容器，循环、工具执行、事件产出全部归编排器。
- C43: 事件通道：编排器以后台任务运行，全过程事件按序压入 `asyncio.Queue`——用户消息、轮次开始、思考、正文、工具调用开始/参数、增量输出、心跳、完成、通知、错误、权限请求、终止；**RunFinished 是唯一结束信号**，后台任务内一切异常（含 Provider 错误）转错误事件收尾，不跨任务抛异常。通道支持多消费者形态，本期实际消费者仅 TUI。
- C44: 工具分批执行：同轮工具调用序列按并发安全性切批——相邻的安全调用合并为一批并发执行（信号量限流），不安全调用各自成批串行，批与批按原顺序；每批等全部完成后统一配对入史。
- C45: 并发安全判定（**2026-10-24 修订为静态属性**）：工具以**类属性** `is_concurrency_safe: bool = False` 声明能否并发，判定不读入参；纯读工具（ReadFile/Glob/Grep）置 `True`，写类与命令类不覆写恒 `False`（Bash 即使只读命令也串行）；注册中心查不到的未知工具视为不安全（`ToolRegistry.find` 返回 `None`）。
- C46: 只读白名单（**2026-10-24 收窄用途**）：`bash_tool.is_readonly_command` 白名单服务两处——计划模式命令拦截（C47）与权限确认范围（C51）；**不再参与并发判定**（C45 改静态后）。判定按命令首 token（含 git 双 token 子命令）+ 元字符排除，组合管道命令默认不命中。
- C47: plan-only 模式：会话级开关；进入后装配计划模式系统提示（只探索不修改、唯一可写目标为计划文件、四步工作流）；写类与非白名单命令被前置拦截并回传结构化提示让模型改道；计划产出写入计划文件（放行按文件身份精确匹配，不防御白名单命令的间接副作用）；收尾以 Notice 提示用户手动 /exec，**不做审批 UI**。/exec 后首次用户请求前拼接一次模式切换提示（随该轮消息入史，一次生效）。
- C48: 取消信号传播：外部取消信号（UI 为首个触发源）经 `asyncio.Event` 传入编排器，**轮次边界检查点**检测到后干净退出（温和：当前轮跑完才停）；即时中断路径（任务取消异常穿透）保留；两路径共用同一套中断收尾与配对不变量（延续 ch02 C24/C29）。
- C49: 拦截位：工具执行前后留通用钩子——前置钩子可否决（返回结构化结果替代执行，直接配对入史），后置钩子只观察；默认无钩子直通；计划模式拦截即第一个内置前置钩子。
- C50: 最小 system prompt：Provider 层支持会话级系统提示词传参（`stream(..., system="")`），双协议各自组装；系统提示词**不进对话历史**；普通模式一条静态最小文案、计划模式专用文案；deferred 工具名单（C53）也在此拼装；不做复杂组装。
- C51: 风险操作权限确认：普通模式下写类与非白名单命令类工具执行前，经通道发出 `PermissionRequest` 事件（request_id + 工具名 + 参数摘要 + 应答 Future）；应答三值 allow（照常执行）/ deny（结构化错误回传模型，工具不执行）/ always（**本会话内该工具名不再询问**，`/new` 不清除，重启失效）；读类与白名单命令不确认；计划模式保持自动拦截不走确认；等待应答超时（60s）按拒绝；应答入口对已失效请求静默忽略，等待期被取消时 Future 悬空无害。
- C52: 项目记忆提取：每 5 次编排运行（**全部终止原因均计数**）后，以后台任务对会话历史快照做一次 LLM 总结提取（项目规则/工程约定/技术约束/用户偏好），追加写入 cwd 的 `project_memory.md`（时间戳段落，**追加式不去重不合并**，治理留后续章节）；fire-and-forget——不阻塞主流程、失败静默、CancelledError 穿透；空历史不触发、空输出不写文件；提取结果**不注入系统提示词**。
- C53: 工具披露（**2026-10-24 新增**）：`Tool.should_defer` 类属性声明延迟披露；Registry 以 `mark_discovered` / `is_discovered` 跟踪披露状态（运行期状态，不持久化）；`get_all_schemas(protocol)` 跳过 disabled 与**未披露**的 deferred 工具（不再按提问打分自动暴露）；两种披露查询——`find_deferred_by_names`（对应 `select:Name1,Name2` 精确选）与 `search_deferred`（name/描述打分检索）；`ToolSearchTool`（`category="read"`、自身 `should_defer=False`）命中后逐个 `mark_discovered` 并回传 schema JSON，未命中回退列出全部可披露工具名；未披露 deferred 工具名清单拼进系统提示词，供模型知道可检索什么。当前六件套均不 defer，机制供后续章节工具挂载。
- C54: 启动路径惰性化（**2026-10-29 新增，回归修正**）：启动期只付出本次实际使用协议的依赖成本——参数解析先于一切重型导入（`--version` / `--help` 在解析期退出，不加载 Provider SDK 与 TUI 栈）；Provider 实现模块由工厂按 `protocol` 首次使用时导入，单协议会话不加载另一协议 SDK（保持 `N3 其余层不动` 的扩展面）。验收口径：`--version` 冷启动 ≤ 0.5s、单协议（openai）装配导入 ≤ 1.5s。

## 4. 非功能要求

- N16: 并发上限：安全批内并发度受信号量约束（上限 4，见 checklist），防无限并发拖垮系统。
- N17: 通道语义：单次运行内事件先进先出、不丢不乱；跨运行不复用通道（每次 start 新建队列，取消信号同）。
- N18: 配对不变量在分批下延续：批内全部完成或被取消后统一配对；未开始的批整批配对中断结果；任何路径零孤儿（延续 ch02 C24/C29）。
- N19: 异常事件化：后台任务内 Provider 错误、未预期异常一律转错误事件 + 终止事件；取消异常走中断收尾后发终止事件（原因=interrupted）后 re-raise（任务取消契约）。
- N20: 拦截例外的唯一性：计划文件是计划模式下唯一放行的写目标；只读白名单是唯一放行的命令集；两者之外一律拦截。
- N21: 并发下的反馈保持：多工具同时产出增量输出与心跳时以调用 id 区分（通道内交错、界面各自独立更新）；耗时统计不因并发失真（接受界面滚动跳跃）。
- N22: 系统提示词零历史污染：带与不带系统提示词的对话历史条目数一致；profile 热切换 / 清空上下文不受影响；deferred 名单（C53）同属系统提示词组装，不入史。
- N23: 权限请求的应答经 Future 同进程传递：请求在等待期被取消（Esc/应用退出）时 Future 悬空无害；应答入口对已失效请求（超时已拒/已取消）静默忽略。
- N24: 启动开销按需：`--version` / `--help` 与正常启动的依赖导入必须收敛到本次实际使用的协议；不得因工厂注册而全量导入两侧 SDK；新增后端仍只改 Provider 层一处注册表（N3）。

## 5. 设计概要

- 核心数据结构：`Agent`（编排器：队列通道/分批/钩子/权限/记忆计数）、`ChatSession`（历史 + profile + 计划模式状态容器）、`ToolRegistry`（工具容器 + disabled 集 + discovered 集）、`Tool` ABC（`category` / `is_concurrency_safe` / `should_defer` 类属性）、`PreToolHook` / `PostToolHook` 钩子类型、通道事件 dataclass 集（`UserMessage`/`RoundStart`/`TextDelta`/`ToolCallStart`/`ToolCallDelta`/`ToolExecuted`/`ToolOutputDelta`/`ToolHeartbeat`/`Notice`/`RunError`/`RunFinished`/`PermissionRequest`，集中在 `sucode/providers/base.py`）、`memory.extract_memory` 提取协程。
- 主流程（一次用户提问 → 终止）:
 1. TUI `on_submitted` → `Agent.start(text)`：用户消息入史（/exec 后首次拼接模式切换提示）→ 新建队列 + 后台任务；
 2. 每轮 `_consume_round`：装配系统提示词（普通/计划 + deferred 名单）→ `registry.get_all_schemas(protocol)` 取 schema → 透传流事件进通道；
 3. 轮内 ToolCall 齐 → `_plan_batches` 按静态 `is_concurrency_safe` 切批；
 4. 批内单工具 `_run_tool`：前置钩子（计划拦截可否决）→ 权限确认块（C51）→ `registry.run` 统一执行（超时/异常转结构化结果）→ 后置钩子观察；
 5. 收尾 `_finish`：运行计数 → 达周期触发记忆提取（fire-and-forget）→ 压 `RunFinished`（唯一结束信号）。
- 调用链:
 - 装配：`cli.boot` 先 `parse_args`（`--version` / `--help` 在此退出，不加载重型依赖）→ `load_config` → `get_provider(profile)`（按 protocol 惰性导入实现模块）→ `ChatSession(provider, create_default_registry())` → 注册 `ToolSearchTool` → `Agent(session)` → TUI；
 - 权限应答：TUI `_resolve_permission(y/n/a)` → `Agent.resolve_permission(request_id, verdict)` → Future 置结果 → 等待中的 `_request_permission` 解阻塞；
 - 取消：Esc → `Agent.task.cancel()`（即时）+ `Agent.request_cancel()`（轮间检查点）双触发；
 - 披露：`ToolSearchTool.execute` → `registry.search_deferred` / `find_deferred_by_names` → `mark_discovered` → 下一轮 `get_all_schemas` 出现该工具。
- 与其他模块的交互:
 - 被依赖：`sucode/tui/app.py`（消费通道、应答权限、/plan /exec、Esc）、`sucode/cli.py`（装配）；
 - 依赖：`sucode/providers/base.py`（事件与 `stream(system=)`）、`sucode/tools/`（Registry 与六件套 + ToolSearch）、`sucode/chat.py`（状态容器）、`sucode/memory.py`（提取协程）；
 - 工具层不反向依赖 agent/tui（底层约束延续 ch02）。

## 6. Out of Scope

- O16: headless CLI 消费入口（一次性命令行提问）——通道能力就绪，入口留后续章节。
- O17: 权限规则引擎与持久化授权（细粒度策略、跨会话记忆授权、审批流不做；本期为交互式确认 + 会话级 always）。
- O18: 复杂系统提示词组装（环境信息注入、项目上下文、动态拼装）——记忆提取结果亦不注入（见 O22）。
- O19: 计划审批交互 UI（批准/继续计划的选择组件）；仅通知提示 + 手动切换。
- O20: 事件多播（fan-out 到多队列）、跨会话通道复用、多会话并行。
- O21: 写类工具的依赖感知调度（纯顺序保证，不做依赖图）。
- O22: 记忆的注入与治理（提取结果不进系统提示词；不去重、不合并、不检索；文件治理留后续章节）。
- O23: 披露状态持久化与治理（discovered 集仅运行期，重启重新披露）；deferred 工具的运行时增删（装配期一次性注册，运行期只读）。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。
