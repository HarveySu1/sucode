# sucode · Phase 3 任务清单

> 执行顺序即列表顺序；依赖关系见各任务。目标目录结构（Phase 2 骨架不变，新增 agent.py）：

```
sucode/
├── sucode/
│   ├── agent.py             # ★ 新增：Agent 编排器 + 事件通道 + 分批 + 拦截位 + 取消（T31-T35）
│   ├── chat.py              # 瘦身：循环迁出，保留历史/profile/模式状态（T31/T34）
│   ├── cli.py               # 扩展：装配 Agent（T36）
│   ├── providers/
│   │   └── base.py          # 扩展：system 传参（T28）+ 通道事件类型（T30）
│   ├── tools/
│   │   ├── base.py          # 扩展：按入参的并发安全判定（T29）
│   │   └── bash_tool.py     # 扩展：只读白名单（T29）
│   └── tui/
│       └── app.py           # 扩展：通道消费 + /plan /exec + 取消双路径（T36）
```

---

## T28 Provider 层系统提示词传参
- **影响文件**：sucode/providers/base.py、anthropic_provider.py、openai_provider.py
- **依赖**：无（可与 T29/T30 并行）
- **参考定位**：base.py `stream()` 签名（第 342-349 行）；`serialize_messages` 双协议分叉（第 320-332 行）；两家 provider 的请求组装处（messages 列表构造）
- **内容**：`stream()` 加 `system: str = ""` 参数（空串不携带）；Anthropic 端走顶层 `system=` 参数，OpenAI 兼容端在 messages 首位插入 `{"role": "system", ...}`；system 不进 `Message` 历史（零污染，N22）
- **自测**：mock 两家 SDK 客户端断言请求体——带 system 时各协议字段位置正确；不带时无 system 痕迹；历史序列化输出与 system 无关

## T29 工具层并发安全判定 + 只读白名单
- **影响文件**：sucode/tools/base.py、file_tools.py、search_tools.py、bash_tool.py
- **依赖**：无（可与 T28/T30 并行）
- **参考定位**：tools/base.py `Tool` 类（第 111-131 行，`category` 之后加判定方法）；bash_tool.py 类头部（白名单常量 + 判定逻辑）
- **内容**：`Tool` 基类新增 `is_concurrency_safe(self, arguments: dict) -> bool`，默认 `False`；ReadFile / Glob / Grep 覆写恒 `True`；WriteFile / EditFile 不覆写（默认不安全）；Bash 覆写——首 token 命中只读白名单**且**不含 shell 元字符（`|` `;` `>` `<` `&`）才安全；白名单具体命令集与元字符清单见 checklist 承接
- **自测**：判定矩阵——六工具 × 代表入参；Bash 白名单内外、含元字符组合、空命令；判定只读入参不产生副作用

## T30 事件类型扩展
- **影响文件**：sucode/providers/base.py
- **依赖**：无（可与 T28/T29 并行）
- **参考定位**：事件定义区与联合类型（第 106-222 行；`RoundStart` 第 186 行、`StreamEvent` 第 209 行——编排层事件沿用定义于此的先例）
- **内容**：新增三事件并入 `StreamEvent` 联合——用户消息事件（本轮提问文本，供未来非 TUI 消费者）、编排终止事件（终止原因枚举：正常完成 / 用户中断 / 取消信号 / 错误 / 达轮数上限）、编排错误事件（可读错误文案）
- **自测**：纯数据类，断言并入 T31 的事件序列测试覆盖

## T31 Agent 编排器与事件通道
- **影响文件**：新建 sucode/agent.py；sucode/chat.py（剥离循环）
- **依赖**：T30
- **参考定位**：chat.py `send()`（第 137-192 行，循环主体整体迁移）、`_RoundAggregator`（第 55-97 行）、`_execute_tools`（第 194-220 行，T32 改造）、`_run_tool`（第 222-267 行）、`_consume_round`（第 287-302 行）、`_finalize_interrupted`（第 304-317 行）、模块常量 `MAX_TOOL_ROUNDS` / `HEARTBEAT_SECONDS`（第 51-52 行，随迁）
- **内容**：`Agent` 类——`start(text)` 创建后台任务并返回事件通道（`asyncio.Queue`）；后台任务内跑 ReAct 循环（迁移 send 逻辑，历史读写经 ChatSession）；事件按序压入队列，开头压用户消息事件、结尾压终止事件；`ProviderError` / 未预期异常 → 错误事件 + 终止事件（原因=错误），**不跨任务抛**；`CancelledError` / `GeneratorExit` → 沿用中断收尾（配对规则不变）→ 终止事件（原因=用户中断）；`ChatSession.send()` 删除，保留 history / profile / `switch_profile` / `clear`（T34 再加模式状态）
- **自测**：mock Provider 三路径事件序列断言——正常对话（用户消息 → 轮开始 → 正文 → 终止[正常]）、工具循环（≥2 轮）、错误路径（错误事件 + 终止[错误]且任务正常结束无泄漏）；后台任务计数归零

## T32 分批执行器
- **影响文件**：sucode/agent.py
- **依赖**：T29、T31
- **参考定位**：chat.py `_execute_tools`（第 194-220 行，串行循环改分批）；`_run_tool`（第 222-267 行，单工具「队列 race」执行抽为批内可复用单元）；切批算法按需求方伪代码——遍历调用序列，经注册中心查工具做安全判定，安全且上一批也是安全批则并入，否则新开批
- **内容**：切批函数（未知工具判不安全，走串行路径在执行时返回结构化错误）；安全批 `asyncio.gather` + 信号量限并发（上限值见 checklist）；不安全调用各自成批串行；批与批按原顺序；批完成统一配对入史；中断覆盖——未开始的批整批配对「已中断」结果，进行中批取消后同样配对；并发批内多工具的增量输出 / 心跳照常按 id 产出
- **自测**：切批矩阵（安全+安全+不安全+安全 → 三批，前两个合并）；3 个各 sleep 1s 的安全工具总耗时 < 2.5s（并发生效）；不安全批之间完成时间不重叠（串行）；8 个安全工具同时在跑 ≤ 上限；三场景中断后历史零孤儿

## T33 拦截位机制
- **影响文件**：sucode/agent.py
- **依赖**：T31
- **参考定位**：T32 的单工具执行入口（执行前调前置、拿到结果后调后置）
- **内容**：前置钩子——async 签名，入参为工具调用，返回结构化结果即否决（该结果直接配对入史，工具不执行），返回空放行；后置钩子——入参为工具调用 + 执行结果，只观察；`Agent` 构造注入钩子列表，默认空列表直通
- **自测**：前置否决 → 工具未执行、结果入史且模型可读；前置放行 → 正常执行；后置收到 (调用, 结果)；无钩子时与直通行为逐事件一致

## T34 plan-only 模式
- **影响文件**：sucode/chat.py（模式状态）、sucode/agent.py（提示词装配 + 拦截钩子）
- **依赖**：T28、T33
- **参考定位**：ChatSession 状态字段区（第 103-107 行，加模式状态）；plan 系统提示词全文与拦截提示文案见 checklist 承接；拦截规则复用 T29 白名单
- **内容**：ChatSession 增模式状态（普通 / 计划 + 计划文件路径）；进入计划模式 → 每轮装配计划专用系统提示词；计划拦截实现为内置前置钩子——写类工具目标 ≠ 计划文件 → 拦截并回传结构化提示；命令类非白名单 → 拦截；目标为计划文件 → 放行（N20 例外唯一性）；循环收尾（模型产出最终回复）时压通知事件提示手动切换；退出恢复普通最小提示词
- **自测**：计划模式下 WriteFile 目标非计划文件 → 拦截文案入史；目标为计划文件 → 放行落盘；Bash 白名单外 → 拦截；模式切换后系统提示词变化（mock 断言请求体）；收尾通知事件出现

## T35 取消信号
- **影响文件**：sucode/agent.py
- **依赖**：T31
- **参考定位**：chat.py 异常收尾（第 184-192 行，`CancelledError` / `GeneratorExit` 路径保留）；迁移后循环的轮次顶部（检查点位置）
- **内容**：取消信号（`asyncio.Event` 包装）+ 对外触发方法；后台任务每轮开始前检查信号 → 命干净退出（终止事件，原因=取消信号）；任务取消路径不变（`CancelledError` → 中断收尾 → 终止事件，原因=用户中断）；两路径共用同一套收尾逻辑
- **自测**：工具轮间隙触发信号 → 下一轮开始前退出、历史配对完整；任务取消 → 即时中断、半截标已中断；两路径终止事件原因各自正确

## T36 TUI 接入事件通道（接入主流程）
- **影响文件**：sucode/tui/app.py、sucode/cli.py
- **依赖**：T31、T32、T33、T34、T35
- **参考定位**：app.py `_run_stream`（第 233-285 行，async for 迭代 send() 改为启动 Agent + 队列消费循环，终止事件收尾）；`on_submitted`（第 172-188 行，`session.send` 改 `agent.start`）；`action_interrupt`（第 226-230 行，改取消信号 + 任务取消双触发）；`_handle_command`（第 190-223 行，新增 `/plan [文件]` 与 `/exec`，帮助文本同步）；顶栏 `_topbar_text`（第 149-151 行，显示当前模式）；cli.py `boot()`（第 49-61 行，构造 Agent 与 TUI 装配）
- **内容**：通道消费 worker——队列取事件逐个分发（用户消息事件跳过：TUI 已自行渲染）；终止事件驱动收尾与 `_streaming` 复位；错误事件走错误展示；命令路由 `/plan`（可带计划文件路径，缺省用默认名）与 `/exec`；Esc = 触发取消信号 + 取消后台任务
- **自测**：mock 驱动无头 TUI——正常多轮、工具行、错误、两条取消路径界面表现；Phase 1/2 交互动线不回归

## T37 端到端验证
- **影响文件**：临时验证脚本（不入仓库）；docs/ch03/checklist.md
- **依赖**：T28-T36 全部
- **参考定位**：ch02 checklist 回归与真实 API 先例（Phase 1 12 用例 + Phase 2 26 用例重建脚本）
- **内容**：mock 回归全量（Phase 1 12 + Phase 2 26 + 本章新增：通道事件序列 / 切批 / 并发 / 拦截 / 计划模式 / 双取消路径）；真实 API 三场景——①诱导同轮多读类并发 ②计划模式全流程（拦截 → 改道 → 计划落盘 → 收尾提示 → /exec 恢复）③多轮工具执行中 Esc 后 /new 再对话无 400；checklist 逐条勾选

## T38 权限请求事件与 Future 响应机制
- **影响文件**：sucode/providers/base.py、sucode/agent.py
- **依赖**：T30（事件定义区先例）
- **参考定位**：base.py 事件定义区（RunFinished 之后、StreamEvent 联合之前，T30 先例）；agent.py `Agent.__init__`（T35 取消信号旁）
- **内容**：base.py 新增权限请求事件（request_id / tool_name / summary / future）并入 StreamEvent 联合；Agent 持待应答权限表与对外应答方法（按 id 找 Future 置结果，已失效请求静默忽略）；请求协程——创建 Future、压事件、带超时等待（超时值见 checklist）、超时按拒绝、finally 清表
- **自测**：mock——allow / deny / always 三应答路径；超时按拒绝；请求期间任务取消后无悬挂影响

## T39 写类/命令类执行前权限确认
- **影响文件**：sucode/agent.py
- **依赖**：T38
- **参考定位**：agent.py `_run_tool`（前置钩子循环之后、工具后台任务创建之前）；判定复用 C46 只读白名单（bash_tool.is_readonly_command）；拒绝文案见 checklist 承接
- **内容**：风险判定方法——category == write 恒确认；category == command 且不在只读白名单确认；read 与白名单命令不确认；`_run_tool` 插权限块——需要确认且工具未在会话授权集 → 发请求；deny → 结构化拒绝结果配对入史 + ToolExecuted(ok=False) + 后置钩子观察拒绝结果；always → 会话授权集记录后放行；allow → 照常执行；计划模式跳过确认（自动拦截行为不回归）
- **自测**：写类弹 / 读类不弹 / 白名单命令不弹 / 白名单外命令弹；deny 后工具未执行且拒绝结果入史；always 后同工具第二次调用不再弹

## T40 TUI 权限确认应答
- **影响文件**：sucode/tui/app.py
- **依赖**：T38、T39
- **参考定位**：app.py `on_submitted`（提交路由最前）、`_run_stream` 事件分发（PermissionRequest 分支 + ToolExecuted 清待确认态）、`action_interrupt`（清待确认态）、CSS 区
- **内容**：收到权限请求 → 醒目系统消息（工具名 + 摘要 + y/n/a 指引）并进入待确认态；待确认态下输入框单行路由（y/yes → 允许、n/no → 拒绝、a/always → 总是允许；其他输入提示用法，不进消息流）；生成中的新消息拦截对确认输入放行；同 id 工具行到达或 Esc 中断时清理待确认态
- **自测**：mock 驱动无头 TUI——请求消息出现 / 应答允许后工具执行 / 应答拒绝后工具行 ✗ / 无效应答提示 / Esc 清理

## T41 记忆提取触发器
- **影响文件**：sucode/memory.py（新建）、sucode/agent.py
- **依赖**：T31
- **参考定位**：agent.py 全部 RunFinished 产出点（六处，统一收口）；触发周期与记忆文件名见 checklist 承接
- **内容**：新建 memory.py（提取协程 + 周期常量 + 记忆提示词常量）；Agent 终止事件统一出口内做运行计数（全部终止原因均计），达周期即以 create_task 触发提取（传历史快照；任务集跟踪、完成自动丢弃）；空历史不触发
- **自测**：mock——恰好第 N 次运行触发（completed 与 error 均计数）；提取任务进行中可立即开始下一次 start（不阻塞）；空历史不触发

## T42 记忆提取实现
- **影响文件**：sucode/memory.py
- **依赖**：T41
- **参考定位**：provider.stream 接口（history + tools=None + system=记忆提示词，T28 传参能力）
- **内容**：历史快照 → LLM 流式总结（收集正文片段）→ 追加写入工作目录记忆文件（带时间戳段落，追加式不去重）；空输出不写文件；任何异常静默吞掉（fire-and-forget 语义），CancelledError 穿透不吞
- **自测**：mock provider 返回记忆文本 → 文件出现且含时间戳与文本；provider 抛错 → 静默无影响；空输出不写文件

## T43 接入主流程与端到端验证
- **影响文件**：临时验证脚本（不入仓库）；docs/ch03/checklist.md
- **依赖**：T38-T42 全部
- **参考定位**：T37 验证先例（mock 全路径 + 真实 API 场景）
- **内容**：mock 全路径回归（权限三值 + 超时 + 计划模式不确认 + always 会话授权 + 记忆周期触发 + 不阻塞 + 提取失败静默）；真实 API 三场景——①普通模式诱导写文件 → 确认请求出现 → 脚本应答允许 → 真实落盘；②应答拒绝 → 模型收到结构化错误且文件未落盘；③连续 5 轮对话后工作目录出现 project_memory.md；checklist 逐条勾选

---

## 修订记录
- 2026:10:20：初版。关键决策经需求方澄清定稿——事件通道取队列广播形态；分批算法采用需求方伪代码（相邻安全合并、不安全各自成批、并发上限防拖垮）；Bash 归属由白名单动态判定（非按分类一刀切）；计划模式收尾为提示 + 手动切换（无审批 UI）；只读白名单在并发判定与计划拦截间共享；headless CLI 入口本章不做。
- 2026:10:21：C47 修订（T37 真机发现）：`/exec` 是 UI 命令不产生历史消息，模型凭旧上下文会误判仍在计划模式而口头拒绝写操作（deepseek 真机复现）。修复：ChatSession 记「刚退出计划模式」一次性标志，Agent.start 消费后在该轮用户消息前拼接 `[模式切换] 已退出计划模式…` 提示（随该轮消息入史，一次生效；`/plan` 重新进入与 `/new` 清空均使标志作废）。影响文件：sucode/chat.py、sucode/agent.py。
- 2026:10:21：修订新增 C51/C52（权限确认交互 + 项目记忆提取），T38-T43。关键决策经需求方澄清：确认范围 = 写类恒确认 + 命令类白名单外确认（读类与白名单命令不确认，与 C46 白名单共享）；应答三值 allow/deny/always（always = 本会话内该工具不再询问，/new 不清除）；计划模式保持自动拦截不走确认；记忆按 RunFinished 计数（全部终止原因均计）每 5 次触发，fire-and-forget，追加写入工作目录 project_memory.md，不注入系统提示词（O22）。
