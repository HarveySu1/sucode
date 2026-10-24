# sucode · Phase 2 任务清单

> 执行顺序即列表顺序；依赖关系见各任务。目标目录结构（Phase 1 骨架不变，新增 tools 包）：

```
sucode/
├── spec*.md / tasks*.md / checklist*.md
├── sucode/
│   ├── chat.py               # 扩展：单步工具编排（T17）
│   ├── cli.py                # 扩展：装配 ToolRegistry（T18）
│   ├── providers/
│   │   ├── base.py           # 扩展：Message 模型 + 双协议序列化分叉（T14）
│   │   ├── anthropic_provider.py  # 扩展：tool_use 流解析 + signature 捕获（T15）
│   │   └── openai_provider.py     # 扩展：tool_calls index 聚合（T16）
│   ├── tools/                # ★ 新增包
│   │   ├── __init__.py       # ToolRegistry + create_default_registry（T12）
│   │   ├── base.py           # Tool 抽象 + ToolResult + 统一执行保护（T12）
│   │   ├── file_tools.py     # ReadFile / WriteFile / EditFile（T13）
│   │   ├── search_tools.py   # Glob / Grep（T13）
│   │   └── bash_tool.py      # Bash（T13）
│   └── tui/
│       ├── app.py            # 扩展：工具事件消费（T18）
│       └── widgets.py       # 扩展：工具行渲染（T18）
```

---

## T12 工具层基础
- **内容**：`Tool` 抽象基类——元信息 `name` / `description` / `parameters`（JSON Schema dict）/ `read_only`（权限元信息标记，后续权限确认流程用，本期只定义不拦截）/ `timeout`（执行硬超时）；抽象 `async execute(**kwargs) -> str`（成功返回输出文本，失败抛 `ToolError`）。`ToolResult(ok, output)` dataclass。`ToolRegistry`：register / get（未知名抛 ToolError）/ to_anthropic()（`{name,description,input_schema}`）/ to_openai()（`{type:"function",function:{name,description,parameters}}`）/ `async run(name, arguments_json) -> ToolResult` 统一执行保护——空串参数归一 `{}`；JSON 解析失败 / 未知工具 / 超时（asyncio.wait_for）/ 任何异常 → 全部转 `ToolResult(ok=False, "错误：…")`，**CancelledError 不吞**（Esc 取消穿透）；输出经 `truncate_output` 截断（30000 字符，头尾各半注明）。`create_default_registry()` 装六件套
- **影响文件**：sucode/tools/base.py、sucode/tools/__init__.py（均新增）
- **依赖**：无
- **自测**：注册哑工具后 run 正常/坏 JSON/未知名/抛异常/超时五路径均返回结构化 ToolResult 不崩溃；to_anthropic/to_openai 格式断言

## T13 六工具实现
- **内容**：
  - **ReadFile**(path, offset?, limit?)：带行号输出；默认上限 2000 行，截断注明；文件不存在 / 解码失败报 ToolError
  - **WriteFile**(path, content)：父目录自动创建；UTF-8 写入；成功输出「已写入 path（N 字节）」
  - **EditFile**(path, old_string, new_string)：原文唯一匹配替换；0 命中报「未出现（注意空白/缩进）」；≥2 命中报「匹配 N 处，请提供更长更唯一的上下文」（附命中行号）
  - **Bash**(command, timeout_seconds? 默认 30 上限 300)：Windows `powershell.exe -NoProfile -NonInteractive -EncodedCommand`（脚本前置 `chcp 65001` + `[Console]::OutputEncoding=UTF8`，尾部 `exit $LASTEXITCODE`）；Unix `/bin/bash -c` + `start_new_session=True`；stdin=DEVNULL；UTF-8 errors=replace 解码；输出 = 合并 stdout/stderr + 退出码；超时与取消杀整棵进程树（Windows `taskkill /T /F`，Unix `os.killpg`）；工具级 timeout 设 305s 兜底
  - **Glob**(pattern, path?)：pathlib 递归匹配，mtime 降序，上限 500 条
  - **Grep**(pattern, path?, glob?)：纯 `re` 逐文件扫描，输出 `file:line:content`，上限 200 命中；跳过二进制与 `.git`/`__pycache__`/`node_modules`/`.venv` 等目录；正则编译失败报可读错误
  - 六工具 description 用中文写给模型看；参数 Schema 手写 JSON Schema dict
- **影响文件**：sucode/tools/file_tools.py、search_tools.py、bash_tool.py（均新增）
- **依赖**：T12
- **自测**：临时目录内逐工具真实验证（读/写/改/执行/找/搜 + 三类 EditFile 报错 + Bash 超时与中文输出）

## T14 Message 模型扩展与序列化分叉
- **内容**：`ToolCall(id, name, arguments)`（arguments 为原始 JSON 字符串）与 `ToolResultBlock(tool_call_id, content, is_error)` dataclass；Message 扩展——`thinking_signature: str` 新增、`tool_calls: list[ToolCall]`、`tool_results: list[ToolResultBlock]`；新增事件 `SignatureDelta(signature)`、`ToolExecuted(id, name, ok, preview)`、`Notice(text)` 入 StreamEvent Union；`serialize_messages(protocol, history, has_tools=False)` 双协议分叉：
  - anthropic：assistant → 内容块数组（thinking+signature 仅最后一条含 tool_calls 的 assistant；空正文省略 text 块；tool_use 的 input 解析 arguments，空串归一 `{}`，解析失败归空对象）；user → tool_result 块**先于**正文 text 块
  - openai：assistant → `{role, content, tool_calls:[…], reasoning_content}`（reasoning_content 仅 has_tools 时回传，DeepSeek 硬性要求）；user 带 tool_results → 展开为 N 条 `{role:"tool", tool_call_id, content}`
- **影响文件**：sucode/providers/base.py
- **依赖**：无（可与 T12/T13 并行）
- **自测**：构造含工具轮的历史，断言两协议序列化快照（thinking 仅最后工具轮 / 空 text 省略 / tool_result 先于正文 / reasoning 回传）

## T15 Anthropic Provider 工具解析
- **内容**：`stream(history, tools=None)` 签名扩展，tools 原样入参；流解析新增——`content_block_start` type=tool_use → `ToolCallStart(id, name)`；`input_json_delta.partial_json` → `ToolCallDelta(id, partial_json)`；thinking 块 `signature_delta` → `SignatureDelta(signature)`（Phase 1 丢弃，本期捕获）；`message_delta.stop_reason` 透传（`tool_use` 即工具轮）
- **影响文件**：sucode/providers/anthropic_provider.py
- **依赖**：T14
- **自测**：mock 流（构造 SDK 事件序列）验证 tool_use 聚合与 signature 捕获；有真实 key 时真机工具调用一轮

## T16 OpenAI Provider 工具解析
- **内容**：`stream(history, tools=None)` 签名扩展，tools 原样入参；流解析新增——`delta.tool_calls` 按 index 聚合（provider 内维护 index→{id,name,args,started} 状态，首片带 id+name 发 ToolCallStart，后续参数片发 ToolCallDelta）；`finish_reason=="tool_calls"` 透传
- **影响文件**：sucode/providers/openai_provider.py
- **依赖**：T14
- **自测**：mock 流验证 index 聚合（多工具交错、首片同带参数）；DeepSeek 真机 function calling 一轮

## T17 会话层单步编排
- **内容**：ChatSession 注入 ToolRegistry（可空=纯对话）；`send(text)` 扩展为单步状态机：一流聚合（thinking/text/signature/tool_calls）→ `StreamDone(finish_reason∈{tool_use,tool_calls})` 且有工具时：assistant(含 thinking+signature+tool_calls) 入史 → 串行执行（每工具成功/失败均发 `ToolExecuted`）→ user(tool_results) 入史 → 二轮流 → 最终 assistant 入史；无工具时走 Phase 1 原路径。**配对不变量四路径**（spec C24）：一流中断丢 tool_calls；工具阶段中断全部配对「[已中断] 用户取消了本次工具执行」后收尾；二流中断/错误有半截入史标中断、一字未出弹掉 assistant(tool_calls)+user(tool_results) 整轮回滚；工具失败 is_error 继续二流。二轮又带 tool_calls：剥离入史 + 发 Notice。**逐轮 stream 生命周期**：每轮独立创建，finally 只 aclose 当前轮
- **影响文件**：sucode/chat.py
- **依赖**：T12、T14
- **自测**：mock Provider 全路径回归——普通对话 / 工具轮成功 / 工具失败继续 / 一流中断 / 工具阶段中断 / 二流中断（有半截与一字未出）/ 二轮再调用剥离；历史交替与配对不变量逐条断言

## T18 TUI 工具展示与装配
- **内容**：AIMessageView 扩展——compose 增工具行容器（thinking → 工具行 → Markdown）；`tool_call_started(id,name)` 创建「● 工具名(」行、`tool_call_delta(id,fragment)` 参数流式追加、`tool_executed(id,name,ok,preview)` 更新「✓ 名称 · 摘要 / ✗ 名称 · 摘要」、`notice(text)` 暗淡提示行；工具行 80ms 节流复用现有机制；app.py `_run_stream` 消费循环加四个事件分支；cli.main 装配 `create_default_registry()` 注入 ChatSession
- **影响文件**：sucode/tui/widgets.py、sucode/tui/app.py、sucode/cli.py
- **依赖**：T17
- **自测**：mock 流驱动 TUI 目视：工具行流式出现 → ✓ 状态 → 正文 Markdown；Esc 工具阶段取消显示「已中断」

## T19 端到端验证
- **内容**：mock 回归全量（Phase 1 原 12 用例 + Phase 2 新用例）；真实 API（deepseek profile）：模型主动调工具完成「把当前目录列表写进 list.txt」（断言真实落盘）与「在 sucode 包里搜 serialize_messages 在哪定义」（断言 Grep 命中路径）；工具报错自愈（EditFile 无效匹配 → 模型收到 is_error 后自行调整）；TUI 人工项按同目录 checklist.md 动线执行
- **影响文件**：临时验证脚本（不入仓库）；问题修复视情况
- **依赖**：T18
- **自测**：checklist.md 全部条目可勾选

---

## 修订部分（单步 → Agent 循环 + 工具执行反馈）

## T20 事件类型扩展
- **影响文件**：`sucode/providers/base.py`
- **依赖**：无
- **参考定位**：Phase 2 事件集与联合类型（`base.py` 第 144-172 行，`ToolExecuted` / `Notice` / `StreamEvent`）
- **内容**：新增三个事件——轮次开始（标记新一轮，驱动 TUI 开新块）、工具输出分片、工具心跳（含已用秒数）；并入 `StreamEvent` 联合类型

## T21 工具层可选流式入口
- **影响文件**：`sucode/tools/base.py`、`sucode/tools/__init__.py`
- **依赖**：无
- **参考定位**：`Tool` 抽象与 `execute`（`tools/base.py` 第 74-91 行）；`safe_execute`（第 105-118 行）；`ToolRegistry.run`（`tools/__init__.py` 第 77-85 行）
- **内容**：为 `Tool` 增加**可选**流式执行入口（默认实现退化为 `execute`，非 Bash 工具零改动）；`safe_execute` / `run` 透传 `emit` 回调；超时与取消语义不变

## T22 Bash 增量流式执行
- **影响文件**：`sucode/tools/bash_tool.py`
- **依赖**：T21
- **参考定位**：`execute` 的 `communicate` 读取（`bash_tool.py` 第 130-137 行）；`_kill_tree`（第 58-76 行）；`_powershell_script`（第 44-57 行）
- **内容**：改为增量读取 stdout/stderr，读到片段即经 `emit` 回传；保留 UTF-8 解码、CLIXML 剥离、退出码判定、超时 / 取消杀整树

## T23 会话层 Agent 循环
- **影响文件**：`sucode/chat.py`
- **依赖**：T20
- **参考定位**：`send` 单步编排（`chat.py` 第 105-190 行）；`_RoundAggregator`（第 40-80 行）；中断收尾（第 191-222 行）；二轮 Notice（第 187-189 行）
- **内容**：单步改**多轮循环**；每轮产出轮次开始事件；工具轮计数与上限（10）；到顶后不再提供 / 执行工具并强制收尾；删除二轮 Notice 逻辑；中断 / 出错边界扩展到任意轮

## T24 会话层工具执行反馈编排
- **影响文件**：`sucode/chat.py`
- **依赖**：T21、T23
- **参考定位**：工具串行执行循环（`chat.py` 第 143-160 行）
- **内容**：执行每个工具时并发产出心跳事件（间隔 2s），并排空 Bash 输出分片转成输出事件；执行结束产出 `ToolExecuted`；结果照常配对入史（`CancelledError` 仍穿透并杀树）

## T25 TUI 多块与反馈渲染
- **影响文件**：`sucode/tui/app.py`、`sucode/tui/widgets.py`
- **依赖**：T20
- **参考定位**：`_run_stream` 事件分发（`app.py` 第 229-263 行）；`_new_ai_message`（第 159-165 行）；`ToolLine`（`widgets.py` 第 22-53 行）；`AIMessageView` 工具行入口（第 146-164 行）
- **内容**：收到轮次开始事件即新建 AI 块；`ToolLine` 支持追加输出片段与心跳计时显示；节流与最终整段重渲染规则沿用

## T26 接入主流程 + 端到端验证
- **影响文件**：`sucode/cli.py`（装配校验，必要时微调）；验证脚本临时文件不入库
- **依赖**：T22、T24、T25
- **参考定位**：`cli.py` 注册中心装配；同目录 checklist.md 回归项
- **内容**：装配默认注册中心与流式回调；跑 mock 回归（Phase 1 12/12、Phase 2 各工具用例）+ 真实 API 多轮工具任务 + 长耗 Bash 命令；逐条勾选 checklist

---

## 对齐部分（图片3 设计表：设计决策 → Python 实现）

## T27 工具层设计决策对齐
- **影响文件**：`sucode/tools/base.py`、`sucode/tools/__init__.py`、`sucode/tools/file_tools.py`、`sucode/tools/search_tools.py`、`sucode/tools/bash_tool.py`、`sucode/chat.py`
- **依赖**：T12-T13（工具层原实现）
- **参考定位**：`tools/base.py`（Tool 抽象 / ToolResult / parse_arguments / safe_execute）；`tools/__init__.py`（ToolRegistry / schemas_for / run）；`chat.py`（`_tool_schemas` / `result.ok` 的两处消费）；`anyio`/`pydantic` 已随环境可用（Pydantic 2.11.7）
- **内容**：
  - 参数校验（C34）：六工具各声明 `Params: type[BaseModel]`，`Tool.get_schema()` 由 `model_json_schema()` 生成；`parse_arguments(tool, json)` 走 JSON 解析 + Pydantic 校验，失败转可读 `ToolError`
  - 工具分类（C35）：`read_only: bool` → `category: Literal["read","write","command"]`
  - 结果传递（C36）：`ToolResult` 改 dataclass，字段 `output` + `is_error`（原 `ok`）
  - 注册机制（C37）：新增 `disable` / `enable` / `is_enabled`；禁用工具不进 schema 列表且 `get` 拒绝执行
  - 文件缓存（C38）：新增 `FileCache`（线程安全，路径 + mtime 为键）；ReadFile 读时填充，EditFile / WriteFile 写后失效
  - 异步执行（C39）：Bash 的 `_spawn` 由 `create_subprocess_exec` 改为 `create_subprocess_shell`（Windows 经 shell 调 PowerShell `-EncodedCommand`；Unix 以 `/bin/bash` 为 shell + `start_new_session`）
  - 延迟加载（C40）：新增 `should_defer` 类属性 + `relevance_score()` 词面重合打分；`_select_tools` 对延迟工具按 top_k 择优暴露；本期六工具均 `should_defer=False`
  - 协议适配（C41）：`to_anthropic` / `to_openai` / `schemas_for` 合并为 `get_all_schemas(protocol, query, top_k)`；`chat.py` 每轮以当前提问作 query
  - 缺陷修复（N15）：`_strip_clixml` 原正则要求 `#< CLIXML>`（实测 PowerShell 输出为 `#< CLIXML` + 换行、末尾无 `>`）导致 CLIXML 从未剥净 → 改为容忍换行，并抽出 `<S S="Error">` 正文（`_x000D_`/`_x000A_` 还原为 CR/LF、XML 实体反转义），噪音剥净且错误线索不丢
- **自测**：34 项脚本断言全过——双协议 schema 格式 / 分类取值 / 缺参·坏 JSON·类型不符·未知工具·禁用工具五路径结构化失败 / disable-enable 往返 / FileCache 三态（读填充·mtime 变化不吃陈旧·写后失效）/ EditFile 0 与多命中 / 延迟工具打分暴露与不暴露 / Bash 中文无乱码·exit 7 传码·cmdlet 失败不塌缩·超时杀树·CLIXML 剥净且正文保留

---

## 修订记录
- 2026-10-17：初版。协议要点经官方文档核验定稿——DeepSeek 带 tools 必须回传 reasoning_content（Phase 1「thinking 不入史」修订为仅无工具场景成立）；Anthropic 空 text 块 400 / 空参数归一 / thinking+signature 回传；孤儿 tool_use 配对不变量四路径；PowerShell 退出码塌缩与 GBK 编码陷阱处理。
- 2026-10-17（T18/T19 验证增补）：
  - TUI：`ToolLine.name` 与 DOMNode 只读 `name` 属性冲突导致 worker 崩溃 → 改名 `tool_name`；`notice()` 在组件挂载前收到事件会静默丢弃 → 补 `_pending_notices` 兜底缓存（沿用 Phase 1 双向兜底经验）。
  - Bash：PowerShell cmdlet 失败时 `$LASTEXITCODE` 残留 0（仅 native 命令设置它），退出码塌缩成「成功」→ 判定链改为「`$?` 失败且无 native 退出码时强制 exit 1」（PS 5.1 实测三种路径定稿）；description 增补 PowerShell 语法提示（本期无 system prompt，工具描述是唯一引导通道）。
  - 序列化：空正文且无 tool_calls 的 assistant 消息在 DeepSeek 400（content or tool_calls must be set）→ openai 路径补「(空)」兜底（与 anthropic 路径对齐）。
  - 端到端真机 12/12：E1 落盘 / E2 Grep 定位 base.py:270 / E3 is_error 自愈 / E4 工具轮记忆均通过；单步设计下模型「先读后改」的多步意图被剥离属预期行为（spec C26 + Notice）。
- 2026-10-17（修订：单步 → Agent 循环 + 工具执行反馈，T20-T26）：C26「二轮不执行」被 C27 取代；新增 T20-T26 与 C27-C33 / N10-N14 / L3-L5，移除 O10；补 Bash 增量流式、全工具心跳（2s）、多轮每轮一块。
  - T23：`send()` 由单步重写为循环；硬上限 `MAX_TOOL_ROUNDS=10`；到顶发 `Notice("已达最大工具轮数（10），不再执行工具调用。")` 并强制收尾；删除二轮 Notice 分支。
  - T24：`_run_tool` 用「队列 race」实现——`asyncio.wait({任务, 取分片}, timeout=2s, FIRST_COMPLETED)`，有分片回传 `ToolOutputDelta`、未完成发 `ToolHeartbeat`；`HEARTBEAT_SECONDS=2.0` 对全工具生效（T21 默认退化保证非 Bash 零改动）。
  - T25：`on_submitted` 不再预建块，改由首个 `RoundStart` 建块；`StreamDone` 不在当轮 finalize，统一在下一轮开始或流末尾收尾（否则会截断同轮工具反馈）。
  - 验证：mock 9/9（多轮 / 上限 / 中断配对 / 心跳 / 超时杀树 / Esc 无孤儿）；真实 Bash 流式 119 分片、首片 0.39s；无头 TUI 3 轮 → 3 个 `AIMessageView`；真实 API 一次提问 3 轮 3 工具（Glob/Bash/ReadFile）收尾通过。
- 2026-10-20（修订：工具行耗时显示，C31）：
  - `chat.py _run_tool`：`ToolExecuted` 前补发一次收尾心跳（`elapsed = 实际执行期耗时`），让 TUI 结束态拿到精确总耗时，无需扩展事件结构。
  - `tui/widgets.py ToolLine`：新增 `_started_at` / `_got_heartbeat`；`finish()` 优先用末次心跳值、无心跳时本地兜底；`_elapsed_suffix()` 统一输出 `（N.Ns）`；运行中在首次心跳后追加耗时（首心跳前不显示，避免冻结的 0.0s）。
- 2026-10-20（对齐：图片3 设计表 → Python 实现，T27）：
  - 依赖变更：引入 Pydantic（2.11.7）做参数校验与 Schema 生成，N7 由「依赖零增加」改为「新增 Pydantic，搜索仍用纯 re」。
  - 全量口径变更：`ToolResult.ok` → `is_error`；`read_only` → `category`；`schemas_for` → `get_all_schemas(protocol, query, top_k)`；`parse_arguments(name, …)` → `parse_arguments(tool, …)`。相关消费点（`chat.py` 两处、`tools/__init__.py` 的 `run`）一并更新。
  - 新增能力：注册中心 `disable`/`enable`；`FileCache`（读填充 / 写失效）；`should_defer` + `relevance_score` 延迟加载机制（六工具均不延迟）。
  - Bash 子进程改 `create_subprocess_shell`；**实测同时暴露并修复** CLIXML 剥离正则失配（原要 `#< CLIXML>`，实际为 `#< CLIXML\r\n`）——改为容忍换行 + 抽取错误正文，避免模型收到 XML 噪音或丢自愈线索。
  - 验证：34 项脚本断言全过（含双协议 schema、五类失败路径结构化、disable/enable、FileCache 三态、延迟打分、Bash 中文/退出码/超时/CLIXML）。
