# ch04: Agent Loop Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里（Windows 终端用 `findstr /n` 实测；计数断言用 PowerShell `(Select-String ...).Count`）。
> 行号锚点：全部为 2026-10-29 ch04 落地后的实测值。
> 执行记录：2026-10-29 ch04 落地即验证——`python -m compileall sucode` exit 0；临时脚本 `Temp\sucode_verify\ch04_verify.py`（FakeProvider 驱动）**77 项全绿**（循环 12 / 升档 5 / 分批与执行器 5 / 持久化 3 / HookEngine 5 / 权限 6 / 任务工具 6 / slug 4 / 计划拦截 3 / 事件家族 3 / 重入防护 4 / 恢复轮中断 3 / 思考留存 3 / 系统提示词 1）；C54 启动回归：`--version` 243ms（≤500ms）、`import sucode.providers` 后 anthropic/openai 均未进 `sys.modules`。
> **2026-10-30 修订**：真机动线（② 计划模式 Bash 探索）暴露 `tui/app.py` 遗漏导入 `ToolOutputDelta` / `ToolHeartbeat`——流式输出一到达即 NameError、worker 崩溃（mock 的 Echo 工具不产增量、Sleep<2s 无心跳，且 compileall 查不出运行期 NameError）。已补齐导入，并新增 6 项回归：T22 TUI 事件分支名静态核对（isinstance 引用逐名解析）×2 / T23 真实 Bash 流式 ToolOutputDelta ×2 / T24 慢工具心跳 ×2，全矩阵 **83 项全绿**；tui 行号锚点随 +2 偏移已刷新。
> **2026-10-30 修订二**：真机动线（① 普通提问）暴露 DeepSeek 端点 usage 落点与 OpenAI 规范不同——规范为末尾空 choices 独立 chunk，DeepSeek 实测（deepseek-flash，临时脚本逐 chunk 观测）骑在最后一个内容 chunk 上、无独立空 chunk，原「仅空 choices 分支捕获」永远捕不到。已改为逐 chunk 探测、非空即记（`sucode/providers/openai_provider.py:110-117`，两种形态通吃）；真实链路验证：provider 层 `in=35 out=20`、Agent 层 `本轮 ↑92 ↓27 累计 ↑92 ↓27` + completed（`Temp\sucode_verify\{diag_deepseek_usage,verify_provider_usage,verify_agent_usage_e2e}.py`，不入库）；mock 83 项复验全绿。待办：真实 API 三场景与 TUI 动线真机终验（§4）。
> **2026-10-30 真机证据（升档动线，tiny profile）**：提问 600 字散文——第 1 轮输出恰 64 tokens（length 截断）→「↻ 已升档至 64000，第 1/3 次恢复轮」→ 恢复轮 1003 tokens 写完、正文完整收尾；用量两轮累计（↑5110 ↓1067）与 C59 跨轮累计一致，一次恢复即完成（截图留存）；此动线同时复证了修订二后的 usage 真实显示。
> **2026-10-30 真机终验（§4 全过，ch04 收口）**：工作目录 C:\Users\Su。② /plan slug 全流程通过（slug `jolly-falcon-1030-0008.md` 落盘、只读探索 + 写拦截 + `/exec` 恢复均正确）；③ 权限三值 y 落盘 / a 免问 / n 拒绝回传全部正确；④ 任务板动线通过（`teams\default\tasks.json` 落盘）；⑤ Esc 中断出「已中断」+ 进程杀净、`/new` 后续聊无 400；C60 持久化获真机佐证（`sessions\70b5c0f7f44c\` 两份超长结果全文落盘）。

## 1. 实现完整性

### 生成器接口与事件家族（C55/C56）

- [x] `class Agent` 在 `sucode/agent.py:430`，`async def run(self, text) -> AsyncIterator[AgentEvent]` 在 `:581`（`findstr /n "async def run" sucode\agent.py` → 581；`findstr /n "class Agent:" sucode\agent.py` → 430）。
- [x] `AgentEvent` 联合 **16 类**在 `sucode/agent.py:259-277`（其中 ToolCallStart/ToolCallDelta 复用 providers 定义透传，C32 UX 保留）；`PermissionResponse` 枚举三值在 `:130`（`(Select-String -Path sucode\agent.py -Pattern '^@dataclass').Count` == **17**：14 事件 + LLMResponse + ToolBatch + ExecOutcome，2026-10-29 实测 17）。
- [x] `FinishReason` 五值 Literal 迁入 `sucode/agent.py`（`findstr /n "FinishReason" sucode\agent.py`）；`interrupted` 仅保留枚举值、生成器契约下不产出（N29，脚本 T19 断言硬取消经 CancelledError）。
- [x] providers 事件精简：`StreamEvent` 联合恰 **7** 类（`sucode/providers/base.py:166-174`；脚本 T17 断言 `typing.get_args(StreamEvent)` 长度 7）；MessageStart/UserMessage/RunError/RunFinished/RoundStart/Notice/ToolExecuted/ToolOutputDelta/ToolHeartbeat/PermissionRequest 已从该文件删除——类型定义与活代码引用清零，迁移沿革仅记录在模块 docstring（`sucode/providers/base.py:17`）。
- [x] `StreamCollector` 在 `sucode/agent.py:294`：TextDelta→StreamText / ThinkingDelta→ThinkingText 映射、ToolCallStart/Delta 透传、SignatureDelta 累积（C25）、UsageUpdate 记用量、StreamDone 记 finish_reason、finally `stream.aclose()`（C33）、流尾 finalize 参数 + 逐个 ToolUseEvent（脚本 T2 断言完整参数与透传）。
- [x] `LLMResponse` 在 `sucode/agent.py:282`，含 signature / finish_reason / input_tokens / output_tokens（脚本 T20 断言 thinking+signature 入史、纯对话不留思考）。

### 循环健壮性（C57/C58/C59）

- [x] 常量 `MAX_TOKENS_CEILING=64000` / `MAX_OUTPUT_TOKENS_RECOVERIES=3` / `RESUME_INSTRUCTION` / `UNKNOWN_TOOL_STREAK_LIMIT=3` / `SINGLE_RESULT_CHAR_LIMIT=8000` / `PERSIST_PREVIEW_EDGE=500` 在 `sucode/agent.py:84-89`（`findstr /n "MAX_TOKENS_CEILING SINGLE_RESULT_CHAR_LIMIT" sucode\agent.py`）。
- [x] 升档恢复：首次 `set_max_output_tokens(64000)` 恰一次 + 半截入史 + 续写指令入史 + RetryEvent，3 次后走主循环收束；运行开始 reset（脚本 T7 五项断言：RetryEvent×3 / set 恰一次 / reset==1 / 8 条交替 / 第 4 次 length 直接 completed）。
- [x] 未知工具熔断：整轮全未知 streak+1、任一已知清零、==3 → ErrorEvent + LoopComplete(error)（脚本 T6 双断言：3 轮退出 + 混合场景 5 轮正常完成）。
- [x] 用量上报：UsageEvent 四字段（本轮 + 会话累计），provider 未报不产出（脚本 T1/T2 断言单轮与跨轮累计 13/7）。
- [x] provider 侧：`BaseProvider.set_max_output_tokens / reset_max_output_tokens / _effective_max_tokens` 在 `sucode/providers/base.py:294-303`；anthropic `get_final_message().usage` 在 `anthropic_provider.py:131-139`；openai `include_usage` 在 `openai_provider.py:101`、**逐 chunk 探测捕获**（`:110-117`，2026-10-30 修订二：DeepSeek 的 usage 骑在最后一个内容 chunk 上而非独立空 chunk，仅空 choices 分支捕获不到）、汇总产出在 `:151-152`（`findstr /n "include_usage usage" sucode\providers\openai_provider.py`）。

### 分批与执行（C44 延续 / N25 / N26 / C60）

- [x] `ToolBatch` 在 `sucode/agent.py:345`，`partition_tool_calls` 在 `:352`：相邻安全合并、不安全各自成批、批间原序、禁用判不安全（脚本 T9：5 调用→3 批，形态 [True,False,True]/[2,1,2]；禁用工具判不安全）。
- [x] `StreamingExecutor.submit / collect_results` 在 `sucode/agent.py:388-419`：提交保序 + 异常包装 + CancelledError 穿透（脚本 T10：顺序保持、ValueError 包装进 ExecOutcome）。
- [x] `_execute_tools` 在 `:806`（统一配对入史 + 中断零孤儿 `_fill_interrupted` :845）；`_execute_batch_parallel` 在 `:857`（泵 + 合流队列 + 信号量 4 + 完成哨兵防悬等 + 批收束统一上抛 pump 异常）；`_execute_tool` 在 `:893`（plan 拦截→pre_tool_use 可阻断→权限三值→增量/心跳→整形→post hook 六段完整）。
- [x] `_maybe_persist_or_truncate` 在 `:1038`、`_persist_tool_result` 在 `:1047`：>8000 全文落 `.sucode/sessions/<agent_id>/`、回灌首尾 500 预览 + 保存路径指引、落盘失败退化截断（脚本 T11 三项断言：预览指引 / session 目录 / 全文落盘）。
- [x] `_finalize_interrupted` 在 `:1069`：含**恢复轮合并分支**（末条为续写指令 → 撤回 + 半截并回 + 标记，N29；脚本 T19 三项断言）。

### HookEngine（C61）

- [x] `HOOK_EVENT_NAMES` 八挂点在 `sucode/hooks.py:36`、`HookRejection` :49、`HookContext` :56、`infer_file_path` :71（仅 file_path→path 两字段，N27）、`HookEngine` :80（`(Select-String -Path sucode\hooks.py -Pattern '^class ').Count` == **3**，实测 3）。
- [x] 八挂点全触发且顺序正确：session_start→turn_start→pre_send→post_receive→pre_tool_use→post_tool_use→turn_end→session_end（脚本 T12 断言首现顺序递增）。
- [x] pre_tool_use 可阻断（结果 "Hook rejected: {reason}" 配对回灌）+ notify→HookEvent 上浮；默认空引擎零行为变化（脚本 T12）。
- [x] plan 拦截仍为内置第一道、不经引擎（`sucode/agent.py:521` `_plan_intercept` 在 `_execute_tool` 首段调用）。

### 团队任务板（C62）

- [x] `VALID_STATUSES` 在 `sucode/teams.py:18`、`STATUS_ICONS` ○◐●✕ :21、`Task` :44、`TaskStore` :57（JSON 原子写 + 损坏重建 + 依赖/状态校验）、`TeamManager` :192（`(Select-String -Path sucode\teams.py -Pattern '^class ').Count` == **3**，实测 3）。
- [x] 四工具：`TaskCreateTool`（task_create.py:28）/ `TaskGetTool`（task_get.py:15）/ `TaskListTool`（task_list.py:21）/ `TaskUpdateTool`（task_update.py:30），皆 `category="task"` + `is_concurrency_safe=True`（`findstr /n "category = \"task\"" sucode\tools\task_*.py` 恰 4 处）。
- [x] `Category` 扩展 4 值在 `sucode/tools/base.py:43`（read/write/command/task）；task 类不触发权限确认、计划模式放行——既有 else 分支零改动（脚本 T13/T16 断言）。

### plan slug（C63）

- [x] 24+24 词表与 `make_plan_slug_path` 在 `sucode/chat.py:24-45`；`enter_plan_mode` 无参生成 slug、带参兼容在 `:108-121`；`DEFAULT_PLAN_FILE` 已删除（`findstr /n "DEFAULT_PLAN_FILE" sucode\chat.py` 无命中；脚本 T15 四项断言：目录 / 命名正则 / 父目录 / 带参兼容）。

## 2. 接入完整性（必查，杜绝死代码）

- [x] cli 装配：`TeamManager(work_dir)` + 四任务工具注册（team_name="default"）在 `sucode/cli.py:77-84`，`Agent(session, work_dir=work_dir)` 在 `:86`（`findstr /n "TeamManager TaskCreateTool" sucode\cli.py`）。
- [x] TUI 直连生成器：`async for ev in self._agent.run(text)` 在 `sucode/tui/app.py:456`；`self._worker = self._consume(text)` 发起在 `:303`（`findstr /n "agent.run _consume" sucode\tui\app.py`）。
- [x] 取消链：Esc `request_cancel` + `worker.cancel`（:431-433）双路径；`on_unmount` 取消 worker（:256-257）；`agent.task` 引用清零（`findstr /n "agent.task" sucode\tui\app.py` 仅历史注释 1 处）。
- [x] 权限链：TUI y/n/a → `PermissionResponse` 映射（:400-402）→ `agent.resolve_permission`（:410 → agent.py:471）（`findstr /n "resolve_permission" sucode\tui\app.py sucode\agent.py` 双侧命中）。
- [x] 新事件渲染：RetryEvent（tui/app.py:466）、UsageEvent（:469）、HookEvent（:476）三类系统行；ToolUseEvent 不渲染（:507-508，服务 headless）。
- [x] 工具流式反馈分支：ToolOutputDelta（:509）/ ToolHeartbeat（:511）渲染完好（2026-10-30 修订：补齐二者 agent 导入；ch04_verify T22 分支名静态核对 + T23 真实 Bash 流式 + T24 慢工具心跳三项回归防复发）。
- [x] providers/__init__.py 导出与 7 事件一致（`findstr /n "MessageStart" sucode\providers\__init__.py` 无命中）。
- [x] 退役符号活代码清零：`RunFinished / RoundStart / UserMessage / ToolExecuted / MessageStart / _run_stream` 在 sucode/ 下仅存历史沿革注释（实测 Grep 全仓，命中均为 docstring 内的「原 X 改名 / 替代 Y / 迁移沿革」说明，无任何类型定义或代码引用）。
- [x] 记忆提取接在全部终止点（`findstr /n "_record_run_and_maybe_extract" sucode\agent.py` → 定义 :789 + 各终止点调用，含 CancelledError 路径）。

## 3. 编译与测试

- [x] `python -m compileall sucode` 通过，无语法/导入错误（2026-10-29 实测 exit 0）。
- [x] ch04_verify **83 项全绿**（2026-10-29 首验 77 项全绿；2026-10-30 修订新增 6 项后复验全绿，脚本在 `Temp\sucode_verify\ch04_verify.py` 不入库）：
  - 循环 12：单轮 4 / 多轮工具 6(含用量累计) / max_rounds 3 / 软取消 3 / 硬取消 3 / 熔断 5
  - 升档 5：RetryEvent×3 / 升档恰一次 / reset / 交替 8 条 / 第 4 次收束（+续写指令入史）
  - 分批与执行器 5：5 调用→3 批 / 批形态 / 禁用不安全 / StreamingExecutor 顺序 / 异常包装
  - 持久化 3：预览指引 / session 目录 / 全文落盘
  - HookEngine 5：八挂点全触发 / 顺序 / pre 阻断 / notify→HookEvent / infer_file_path
  - 权限 6：三值恰 3 次 / deny 不落盘 / allow 落盘 / always 不再询问 / deny 结构化回传 / task 类豁免
  - 任务工具 6：创建 / 依赖校验 / 详情 / 图标 / 状态校验 / JSON 重启可载
  - slug 4：目录 / 命名正则 / 父目录 / 带参兼容；计划拦截 3：写拦截 / 未落盘 / 收尾 Notice
  - 事件家族 3：AgentEvent 16 / StreamEvent 7 / 枚举三值；重入 4；恢复轮中断 3；思考留存 3；系统提示词 1
  - 流式与心跳回归 6（2026-10-30 新增）：TUI 事件分支名静态核对 2 / 真实 Bash 流式 ToolOutputDelta 2 / 慢工具心跳 2
- [x] C54 启动回归：`--version` 243ms（≤500ms 上限）；`import sucode.providers` 后 SDK 未加载（`python -c` 实测 False）。

## 4. 端到端验证（真实 API + TUI 人工动线——2026-10-30 真机全过）

- [x] 真实 API ①：普通提问 → StreamText 流式渲染、LoopComplete 终止、UsageEvent 显示真实 token 数（流式与终止 2026-10-29 真机通过；UsageEvent 首验未过——暴露 DeepSeek usage 落点问题，经 2026-10-30 修订二修复；升档动线（tiny profile）复证 tokens 行真实显示与跨轮累计）。
- [x] 真实 API ②：`/plan` → slug 文件路径提示（.sucode/plans/ 下）→ 只读探索 + 写拦截 → 计划落盘 slug 文件 → 收尾 Notice → `/exec` 后写恢复（2026-10-29 首测因 TUI 导入遗漏中断于 Bash 流式；修复后 2026-10-30 重走全过，slug `jolly-falcon-1030-0008.md` 真机落盘）。
- [x] 真实 API ③：写文件 → 权限确认 y 落盘 / a 后不再询问；白名单外命令 → n 拒绝回传（2026-10-30 真机通过）。
- [x] 真实 API ④：TaskCreate → TaskUpdate(in_progress) → TaskList 图标动线，tasks.json 落 `.sucode/teams/default/`（2026-10-30 真机通过，文件已落盘）。
- [x] TUI 动线：长 Bash 中 Esc → 「已中断」+ 进程杀净；`/new` 后再对话无 400（配对不变量真机覆盖）；升档场景（小 max_tokens profile 触发 length）看到「↻」恢复提示（**升档子项 2026-10-30 已验**：tiny profile 截断 → ↻ 升档 64000 → 一次恢复写完，跨轮累计正确；**Esc 中断与 /new 子项同日真机通过**：「已中断」+ 进程杀净、/new 后续聊无 400）。
- [x] 留存证据：升档动线截图（2026-10-30）+ 真机落盘证据（C:\Users\Su\.sucode 树：plans slug / sessions 持久化 / teams tasks.json）已留存。

## 5. 文档

- [x] spec.md / tasks.md / checklist.md 三件套齐全（`docs/ch04/`，2026-10-29）。
- [x] 三件套与代码终态一致（行号锚点为落地实测值；ch03 文档未改动——ch03 承接值「缺省 plan.md」由 ch04 C63 替换，ch03 文档保留历史原貌）。

---

## 承接值（spec 砍掉的具体值，全部以此为准）

- **升档**：`MAX_TOKENS_CEILING = 64000`（每次运行至多 set 一次）；恢复轮上限 **3**；续写指令全文「继续完成刚才未完成的输出，从中断处接着写，不要重复已有内容。」；每次运行开始 reset 回 profile.max_tokens；恢复轮不计 tools_used。
- **熔断**：连续 **3** 轮「整轮全未知工具」→ ErrorEvent + LoopComplete(error)；任一已知工具清零；未知工具单次执行仍回结构化错误。
- **持久化**：`SINGLE_RESULT_CHAR_LIMIT = 8000`；预览首尾各 **500** 字符；路径 `work_dir/.sucode/sessions/<agent_id(12 位)>/<时间戳>-<随机 6 位>.md`；>30000 截断兜底（双保险，safe_execute 先行截断）。
- **用量**：UsageEvent(input/output/total_input/total_output)；provider 未报则不产出；恢复轮用量照常累计。
- **终止原因**：completed / interrupted(仅枚举) / cancelled / error / max_rounds；硬取消不产出终止事件（CancelledError 传播）。
- **沿用既有承接值**：工具轮数上限 **10**（C28）、心跳 **2.0s**（C31）、并发上限 **4**（N16）、权限超时 **60s** 按 DENY、拒绝文案「用户拒绝了本次工具执行，未运行。」、记忆周期 **5**（全部终止原因计数）。
- **任务板**：team_name="default"；store 落 `.sucode/teams/default/tasks.json`；状态 pending/in_progress/completed/blocked ↔ ○◐●✕；id 自增 `T-N`。
- **slug**：`<cwd>/.sucode/plans/<形容词>-<名词>-<MMDD-HHMM>.md`，24+24 词表全小写；每次 /plan 各生成一份；带参自定义路径。
- **启动**：`--version` ≤ 0.5s（实测 243ms）；单协议装配 ≤ 1.5s 沿用 ch03。
