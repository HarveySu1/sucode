# ch03: Agent 编排器与执行策略 Checklist

> **【已归档 · 勿作现行依据】** 本章验收结论在 2026-10-30 被 ch04 覆盖：编排器从 `asyncio.Queue` 通道改为异步生成器，`RunFinished` 唯一结束信号退役，`StreamEvent` 精简为 7 类，plan 文件改为 slug 命名。下列勾选反映 ch03 当时的验收事实，**其中的行号锚点与命令断言已不适用于当前代码**；现行验收见 [ch04/checklist.md](../ch04/checklist.md)。
>
> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里（Windows 终端用 `findstr /n` 实测；计数断言用 PowerShell `(Select-String ...).Count`）。
> 行号锚点：全部为 2026-10-24 T44-T46 落地后的实测值。
> 执行记录：2026-10-20 mock 层 236 pass / 0 fail + 真实 API 三场景 14 pass / 0 fail（脚本在 Temp\sucode_verify\ 不入库）；C51/C52 mock 层已过。2026-10-24 T44-T46 落地 + T47 重建验证：t47_verify 40 项（披露流转 17 / 接入链路 9 / 静态判定与分批计时 9 / 权限矩阵重验 5）+ t43_verify 回归 42 项（权限全路径 17 / 记忆提取 12 / TUI 动线 13）全绿，compileall 通过。待办：真实 API ④⑤⑥ 与 TUI 动线 1-5 终验。2026-10-29 T48 启动惰性化落地：`--version` 4.1s → 0.12s、openai 装配 4.1s → 1.26s，69 项回归全绿。

## 1. 实现完整性

### 编排器与通道（C42/C43/C48）
- [x] `Agent` 类在 `sucode/agent.py:174`：`start()` 返回 `asyncio.Queue`、后台任务跑 ReAct 循环（`findstr /n "class Agent" sucode\agent.py`）。
- [x] `RunFinished` 唯一结束信号 + 五值原因枚举 `FinishReason` 在 `sucode/providers/base.py:217,235`（`findstr /n "FinishReason" sucode\providers\base.py`）。
- [x] 通道事件 `UserMessage` / `RoundStart` / `TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolExecuted` / `ToolOutputDelta` / `ToolHeartbeat` / `Notice` / `RunError` / `RunFinished` / `PermissionRequest` 全部并入 `StreamEvent`（`sucode/providers/base.py:255-273`，`(Select-String -Path sucode\providers\base.py -Pattern '^class ').Count` ≥ 15）。
- [x] 取消信号 `request_cancel()` 在 `sucode/agent.py:198-204`，轮间检查点退出原因 = cancelled；每次 start 清空信号（`:327`）。
- [x] 中断收尾 `_finalize_interrupted` 在 `sucode/agent.py:643-656`：半截正文带 `[已中断]` 标记（`INTERRUPTED_MARKER` `:73`）入史。

### 分批执行（C44/C45/N16）
- [x] `Tool` 基类以**类属性**声明 `is_concurrency_safe: bool = False`（`sucode/tools/base.py:130`；原带 `arguments` 的判定方法已删除）（`findstr /n "is_concurrency_safe" sucode\tools\base.py`，命中为类属性赋值行、无 `def is_concurrency_safe`）。
- [x] ReadFile（`sucode/tools/file_tools.py:47`）/ Glob（`sucode/tools/search_tools.py:68`）/ Grep（`:107`）类属性 `is_concurrency_safe = True`；WriteFile / EditFile / Bash 不覆写（`findstr /n "is_concurrency_safe" sucode\tools\file_tools.py sucode\tools\search_tools.py sucode\tools\bash_tool.py` 恰 3 处类属性赋值，bash_tool.py 无命中）。
- [x] `_is_safe_call` 在 `sucode/agent.py:430-435` 只做 `registry.find` + 类属性读取（未知工具 → False），无 `parse_arguments` 预校验（`findstr /n "parse_arguments" sucode\agent.py` 命中不含 430-435 行段）。
- [x] 切批 `_plan_batches` 在 `sucode/agent.py:437-450`：相邻安全合并、不安全各自成批、批间原序。
- [x] 安全批 `asyncio.gather` + `Semaphore(CONCURRENCY_LIMIT)`（`sucode/agent.py:494-508`，上限 **4** 在 `:77`）。
- [x] 中断零孤儿：`_execute_tools`（`:452-492`）取消/未预期异常时未开始批整批配对 `[已中断]` 结果（`INTERRUPTED_TOOL_RESULT` `:74`）。

### 计划模式与拦截位（C47/C49/C50）
- [x] `PreToolHook` / `PostToolHook` 类型在 `sucode/agent.py:112-114`；`_plan_intercept` 为首个内置前置钩子（`:181`）。
- [x] 计划拦截 `_plan_intercept` 在 `:280-313`：写类目标 ≠ 计划文件拦截、命令类非白名单拦截、计划文件精确匹配放行。
- [x] 模式状态在 `sucode/chat.py:72-95`；`DEFAULT_PLAN_FILE = "plan.md"` 在 `:17`。
- [x] 系统提示词装配 `_system_prompt` 在 `sucode/agent.py:253-263`；`stream(..., system="")` 传参在 `sucode/providers/base.py:393-399`；system 不进历史（mock 断言带与不带历史条目数一致）。
- [x] `/exec` 后首次请求拼接 `PLAN_EXIT_NOTICE`（`sucode/agent.py:105,325-327`，一次生效）。

### 权限确认（C51）
- [x] `PermissionRequest(request_id, tool_name, summary, future)` 在 `sucode/providers/base.py:242-252`。
- [x] `PERMISSION_TIMEOUT = 60.0` 与 `PERMISSION_DENIED = "用户拒绝了本次工具执行，未运行。"` 在 `sucode/agent.py:108-109`。
- [x] `_needs_permission`（`:215-221`）：write 恒确认；command 非白名单确认（复用 `is_readonly_command`）；read 不确认。
- [x] `_request_permission`（`:232-249`）：Future + 事件 + 60s 超时按 deny + finally 清表；`resolve_permission`（`:208-212`）对已失效请求静默忽略。
- [x] `_run_tool` 权限块（`:510-570`）：三值应答分支齐全——allow 执行 / always 记 `_permission_granted` / deny 配对 `PERMISSION_DENIED` + `ToolExecuted(ok=False)` + 后置钩子观察。

### 记忆提取（C52）
- [x] `sucode/memory.py:16-17` `MEMORY_FILE = "project_memory.md"` / `EXTRACT_EVERY_N_RUNS = 5`；提取提示词在 `:20-24`。
- [x] `extract_memory` 在 `sucode/memory.py:27-50`：历史快照流式总结 → 追加写 cwd（`## YYYY-MM-DD HH:MM` 段落）；空输出/「（无）」不写；异常静默、CancelledError 穿透。
- [x] 触发器在 `sucode/agent.py:409-433`：`_finish` 统一计数（全部终止原因均计）→ 达 5 触发；空历史不触发；任务集跟踪防 GC。

### 工具披露（C53，T44-T46 新增）
- [x] `ToolRegistry.__init__`（`sucode/tools/__init__.py:29-32`）含 `self._discovered: set[str]`（`findstr /n "_discovered" sucode\tools\__init__.py`）。
- [x] Registry 公开方法含 `register` / `get` / `find` / `disable` / `enable` / `is_enabled` / `names` / `mark_discovered` / `is_discovered` / `get_deferred_tool_names` / `search_deferred` / `find_deferred_by_names` / `get_all_schemas` / `run` 共 14 个（`(Select-String -Path sucode\tools\__init__.py -Pattern 'def (register|get|find|disable|enable|is_enabled|names|mark_discovered|is_discovered|get_deferred_tool_names|search_deferred|find_deferred_by_names|get_all_schemas|run)\b').Count` == 14，实测 14）。
- [x] `get_all_schemas(protocol)` 签名无 `query`/`top_k`（`sucode/tools/__init__.py:123`），跳过未披露 deferred；`_select_tools` 已删除（`findstr /n "_select_tools" sucode\tools\__init__.py` 无命中）。
- [x] `search_deferred` 打分规则：名中 +10 / 描述中 +5 / 分词 +3/+1（`sucode/tools/base.py:99` `relevance_score`；被 `sucode/tools/__init__.py:107-119` 调用）。
- [x] `ToolSearchTool` 在 `sucode/tools/tool_search.py`：`category="read"`、`should_defer=False`；`select:` 前缀走 `find_deferred_by_names`、否则 `search_deferred`；命中逐个 `mark_discovered` 并回 `Found N tool(s):` + schema JSON；未命中回 `No matching deferred tools for "<q>". Available: <names>`（`findstr /n "select:|mark_discovered|No matching deferred tools" sucode\tools\tool_search.py`）。

## 2. 接入完整性（必查，杜绝死代码）

- [x] `create_default_registry` 在 `sucode/tools/__init__.py:166-174` 注册六件套，并被 `sucode/cli.py:55,62` 消费（`findstr /n "create_default_registry" sucode\tools\__init__.py sucode\cli.py`）。
- [x] `ToolSearchTool` 在 `sucode/cli.py:62-64` 装配区注册（消费 `profile.protocol`）（`findstr /n "ToolSearchTool" sucode\cli.py`）。
- [x] `Agent` 在 `sucode/cli.py:51,62` 装配；TUI 只经 `agent.start` 发起编排（`sucode/tui/app.py:292`，`findstr /n "agent.start" sucode\tui\app.py`）。
- [x] `get_all_schemas` 接入点在 `sucode/agent.py:633-641`（`_tool_schemas`，每轮 `_consume_round` `:614` 消费）；`registry.run` 执行入口在 `:572`。
- [x] `_tool_schemas` 与 `_consume_round` 已无 `query` 传参（`findstr /n "query" sucode\agent.py` 仅命中 `:276` 提示词文案，不含 614-641 行段）。
- [x] 未披露 deferred 名单拼进系统提示词（`_system_prompt` `sucode/agent.py:253-263` 追加，`_deferred_hint` `:265-278`；`findstr /n "get_deferred_tool_names" sucode\agent.py`）。
- [x] 权限应答链：TUI `_resolve_permission`（`sucode/tui/app.py:379-397`）→ `Agent.resolve_permission`（`findstr /n "resolve_permission" sucode\tui\app.py sucode\agent.py` 双侧命中）。
- [x] `/plan` `/exec` 命令路由在 `sucode/tui/app.py:308-330`，顶栏模式显示在 `:249-252`。
- [x] 记忆触发接在 `_finish` 统一出口（`sucode/agent.py:409-433`，`findstr /n "_schedule_memory_extraction" sucode\agent.py`）。
- [x] 计划拦截与权限范围共用 `is_readonly_command`（`findstr /n "is_readonly_command" sucode\agent.py sucode\tools\bash_tool.py`，agent.py 命中 2 处：`_plan_intercept` 与 `_needs_permission`）。

## 3. 编译与测试

- [x] `python -m compileall sucode` 通过，无 SyntaxError（2026-10-24 实测 exit 0）。
- [x] mock 回归不破坏：Phase 1 原 12 用例 + Phase 2 原 26 用例 + ch03 已有用例（通道/切批/并发/拦截/计划/双取消/权限/记忆）全绿（2026-10-24 t43_verify 42 项 + t47_verify 40 项全绿；Phase 1/2 独立脚本已不在 Temp，其核心路径——纯文本对话、工具配对入史、历史条目数——由 t43 T4b 与 t47 B4/C6 断言覆盖）。
- [x] mock 披露流转：deferred 桩工具不在初始 schema → `select:名` 与关键词两种查询命中 → `mark_discovered` → 下一轮 schema 出现；未命中回退列出可用名（2026-10-24 t47_verify 17 项全绿）。
- [x] mock 静态判定矩阵：3 × SleepSafe(1s) 并发总耗时 < 2.5s 且峰值并发 3；2 × UnsafeSleep(0.5s) 串行 ≥ 1.0s 不重叠且峰值 1；同轮 8 个读类峰值 ≤ 4（两波 ≥ 0.6s）；混合批序批间保序；Bash 白名单命令（git status）也判不安全。
- [x] mock 权限矩阵重验（静态判定后不回归）：写类弹 / 读类不弹 / 白名单命令不弹 / 白名单外命令弹（拒绝后模型收到拒绝说明）；ToolSearch（read 类）不弹。

## 4. 端到端验证（真实 API + TUI 人工动线）

- [x] 真实 API ①：提问诱导同轮多读类（如「并行读取 pyproject.toml 和 TRAE.md 后对比」）→ 双工具行 + 回答概括两文件（本地读毫秒级，并发语义由上方 mock 矩阵严格验证）。
- [x] 真实 API ②：计划模式全流程——只读探索 → 写类被拦截并改道 → 计划真实落盘 plan.md → 收尾提示 → `/exec` 后写操作恢复。
- [x] 真实 API ③：多轮工具执行中 Esc → `/new` 清空 → 再对话无 400（配对不变量真机覆盖）。
- [x] 真实 API ④：普通模式诱导写文件 → 确认请求出现 → 应答允许 → 真实落盘；应答拒绝 → 模型收到结构化错误且文件未落盘。
- [x] 真实 API ⑤：连续 5 轮对话后 cwd 出现 `project_memory.md`（含时间戳段落）。
- [x] 真实 API ⑥（披露回归）：六件套场景 schema 列表与 ch03 重构前一致（无 deferred 工具时模型可用工具集不变）。
- [x] TUI 动线 1：`/plan` → 给任务 → 模型只用只读工具探索、计划落盘、收尾出现切换提示；顶栏随模式更新。
- [x] TUI 动线 2：计划模式下要求「直接改文件」→ 工具行拦截提示 → 不落盘任何非 plan.md 文件。
- [x] TUI 动线 3：普通模式让模型写文件 → 醒目确认消息 → `y` 落盘；再写 → `a` → 第三次不再询问。
- [x] TUI 动线 4：白名单外命令（如 `Get-Date`）→ 确认出现 → `n` → 工具行 ✗ 且模型收到拒绝说明；60s 无应答自动拒绝。
- [x] TUI 动线 5：生成中 Esc → 「已中断」；长 Bash（`Start-Sleep 10`）2 个心跳后 Esc → 进程树杀净（powershell.exe 计数不增）。

## 5. 文档

- [x] spec.md / tasks.md / checklist.md 三件套齐全（`docs/ch03/`，2026-10-24 重写为 mewcode 形制）。
- [x] 三件套与代码终态一致（2026-10-24 T44-T46 落地后已复核并刷新行号锚点）。

## 6. 启动路径（C54，2026-10-29 新增）

- [x] `boot()` 中 `args = build_parser().parse_args(argv)` 位于首个 `from sucode...` 重型导入之前（`findstr /n "parse_args" sucode\cli.py` → 55 行 < 57 行）。
- [x] `providers/__init__.py` 无 `anthropic_provider` / `openai_provider` 顶层导入（`findstr /n "_provider import" sucode\providers\__init__.py` 无命中）。
- [x] `_PROVIDERS` 为 `protocol → (模块名, 类名)` 且 `get_provider` 经 `importlib.import_module` 惰性导入（`findstr /n "import_module" sucode\providers\__init__.py`）。
- [x] `import sucode.providers` 后两侧 SDK 均未加载（`python -c "import sys,sucode.providers;print('anthropic' in sys.modules,'openai' in sys.modules)"` → `False False`）。
- [x] `--version` 冷启动 ≤ 0.5s（实测 113 / 119 ms，此前 4.1s）。
- [x] openai profile 完整装配导入 ≤ 1.5s（实测 1257ms）；anthropic profile 3398ms（其中 `import anthropic` 自身 2649ms，不可再降）。
- [x] 未知 protocol 仍抛 `ProviderError`（实测「未注册的 protocol「bogus」，已注册：anthropic、openai」）。
- [x] 回归：69 项审计断言（49 + 19 + 1）全绿，`python -m compileall sucode` exit 0。

---

## 承接值（spec 砍掉的具体值，全部以此为准）

- 并发上限 **4**（信号量，`sucode/agent.py:77`）；工具轮数硬上限 **10**（`sucode/agent.py:75`）、心跳 **2.0s**（`sucode/tools/bash_tool.py`，沿用 ch02）。
- 计划文件默认 **plan.md**（cwd 下，`sucode/chat.py:17`）；`/plan <路径>` 可改，当次会话内生效。
- 普通/计划模式系统提示词全文：`sucode/agent.py:80-104`；计划拦截文案 `:96-103`；收尾 Notice `:105`；模式切换提示 `:105`。
- Bash 只读白名单命令集与元字符 `|;><&`：`sucode/tools/bash_tool.py:60-82`（服务计划拦截与权限确认两处，不参与并发判定）。
- 权限：超时 **60 秒**按拒绝；拒绝文案「用户拒绝了本次工具执行，未运行。」；应答词表 `y`/`yes`→allow、`n`/`no`→deny、`a`/`always`→always（本会话级，`/new` 不清除，重启失效）；TUI 提示含 y/n/a 指引与 60s 说明。
- 记忆：周期 **5** 次运行（全部终止原因计数）；文件 **project_memory.md**（cwd，追加式 `## YYYY-MM-DD HH:MM` 段落）；空输出/「（无）」不写；失败静默；不注入系统提示词。
- 披露：`search_deferred` 打分 名中 +10 / 描述中 +5 / 分词 +3/+1，默认 `max_results = DEFAULT_DEFERRED_TOP_K = 3`（`sucode/tools/base.py:35`）；未命中回退文案 `No matching deferred tools for "<q>". Available: <names>`；discovered 集仅运行期（O23）。
- 启动时延（C54）：`--version` ≤ **0.5s**（实测 0.12s）；单协议（openai）装配导入 ≤ **1.5s**（实测 1.26s）；未使用协议 SDK 不得进入 `sys.modules`。
