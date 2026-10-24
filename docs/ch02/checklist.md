# sucode · Phase 2 验收清单

> 每项必须实际操作观察后勾选，不接受「应该没问题」。全部勾选 = Phase 2 完成。
> 标注（脚本验证）的条目由自动化脚本完成（mock + 真实 API）；TUI 交互项按文末动线人工确认。

## 工具层
- [x] 注册中心：六件套登记齐全；按名查找正常；未知名报「未知工具」结构化错误
- [x] to_anthropic / to_openai 两种 API 格式各自正确（脚本验证）
- [x] 统一执行保护五路径：正常 / 坏 JSON / 未知名 / 工具抛异常 / 超时 → 均结构化 ToolResult 不崩溃（脚本验证）
- [x] CancelledError 穿透不被吞（脚本验证：取消后无残留任务）
- [x] 输出截断：超 30000 字符结果头尾保留 + 中间注明省略量（脚本验证）

## 六工具行为
- [x] ReadFile：带行号输出；默认 2000 行截断注明；不存在/解码失败可读报错（脚本验证）
- [x] WriteFile：父目录自动创建；写入后报字节数（脚本验证）
- [x] EditFile：唯一匹配替换成功；0 命中报「未出现」；≥2 命中报「匹配 N 处」附行号（脚本验证）
- [x] Bash：`echo 你好` 中文输出无乱码；`exit 7` 退出码 7 正确传递（PowerShell 陷阱验证）；默认 30s 超时并杀进程树（脚本验证）
  - 2026-10-17 增补：真实 API 验证时发现 cmdlet 失败（如 unix 风格 `ls -la`）退出码塌缩为 0（`$LASTEXITCODE` 只对 native 命令生效），已修复为「`$?` 失败且无 native 退出码时强制 exit 1」（t13 27/27 回归通过）
- [x] Glob：按 mtime 降序；上限 500（脚本验证）
- [x] Grep：命中 `file:line:content` 格式；上限 200；跳过 .git/__pycache__；坏正则可读报错（脚本验证）

## 序列化（脚本验证）
- [x] anthropic：assistant 空正文无空 text 块；thinking+signature 仅出现在最后一条含 tool_calls 的 assistant；空参数归一空对象；tool_result 块先于 user 正文
- [x] openai：带工具时 assistant 回传 reasoning_content；user(tool_results) 展开为 N 条 role=tool 消息
- [x] 内部历史 user/assistant 严格交替在工具轮后仍成立（tool_results 挂 user 消息）
  - 2026-10-17 增补：真实 API 验证时发现空正文且无 tool_calls 的 assistant 消息在 DeepSeek 端 400（「content or tool_calls must be set」），openai 序列化已补「(空)」兜底占位（与 anthropic 路径一致）

## 流解析（脚本验证，mock 事件序列）
- [x] Anthropic：content_block_start(tool_use) → ToolCallStart；input_json_delta 碎片拼接；零 delta 空参工具 arguments 归一 `{}`；signature_delta 被捕获
- [x] OpenAI：多工具按 index 正确聚合；首片同带参数时事件不丢；finish_reason=tool_calls 透传

## 编排与中断（脚本验证，mock Provider 全路径）
- [x] 工具轮成功：assistant(tool_calls) → ToolExecuted → user(tool_results) → 二轮正文入史
- [x] 工具失败自愈：is_error 结果回传后二轮继续，模型可读到错误文案
- [x] 一流中断（含已收完整 tool_use）：入史消息不含任何 tool_calls（无孤儿）
- [x] 工具阶段中断：全部工具配对「用户中断」结果入史，轮次收尾，历史无孤儿
- [x] 二流中断：有半截 → 标记入史；一字未出 → 弹掉 assistant(tool_calls)+user(tool_results) 回到干净状态
- [x] 二轮再调用：tool_calls 剥离后入史 + Notice 提示，历史无孤儿
- [x] 配对不变量：上述全部场景后，历史中每个 tool_use 均有紧邻配对 tool_result（逐场景断言）
- [x] Phase 1 原 12 用例全部回归通过（纯对话路径不回归破坏）
  - 原 12 用例 Temp 脚本已随系统清理，2026-10-17 以等价 15 断言脚本重建回归（15/15 通过：完整轮/多轮记忆/thinking 不入史/两种中断/两种出错/热切换/clear/tools=None/纯文本双协议序列化/finish_reason）

## 端到端（必须真实 API，不许 mock）
- [x] deepseek profile：「把当前目录列表写进 list.txt」→ 模型主动调 Bash/WriteFile → 文件真实落盘且内容正确
  - 2026-10-17 deepseek 真机：模型一步 `Get-ChildItem -Force -Name | Set-Content list.txt` 落盘成功（会话层直连驱动，TUI 目视见交互项）
- [x] 「在 sucode 包里搜 serialize_messages 在哪定义」→ 模型调 Grep → 最终回复含正确文件路径
  - 真机回复：sucode/providers/base.py:270 `def serialize_messages(`
- [x] 工具报错自愈：让模型 EditFile 一个不存在的匹配 → is_error 回传后模型自行调整重试
  - 真机：EditFile「未找到匹配」is_error 回传后，二轮模型给出清晰自愈说明（文件存在但无该子串，需先看内容；其二轮 ReadFile 请求被单步设计剥离 + Notice）
- [x] 带工具的多轮：工具轮之后继续纯文本追问，模型记得工具结果（reasoning_content 回传链路真机覆盖）
  - 真机：工具轮后追问「写进了哪个文件」→ 精确回答「list.txt」，请求含工具轮历史未 400

## TUI 交互项（人工动线）
- [ ] 工具行随参数流式出现，执行后变「✓ 名称 · 摘要」；失败红色「✗」
- [ ] 工具执行阶段按 Esc：「已中断」标记出现，追问后模型知道工具被取消
- [ ] 二轮正文 Markdown 渲染正常（代码块高亮）
- [ ] 生成/执行期间输入框仍可用但新消息被拦截提示
- [ ] 双 profile（deepseek/kimi）各真机完成一次工具调用轮

## 交互项建议动线（TUI 内逐项勾选）
1. `sucode` 启动 → 「读一下 pyproject.toml 前 20 行」→ 观察 ReadFile 工具行流式 + ✓ 状态 + 正文复述内容
2. 「把 hello 工具演示.txt 写成内容：你好，工具」→ WriteFile ✓ → 侧窗确认文件真实存在
3. 「搜一下 providers 包里 serialize 出现在哪」→ Grep ✓ → 回复含路径
4. 让模型改一个文件里不存在的字符串 → 观察 ✗ 与模型自愈重试
5. 「运行 python --version」→ Bash ✓ 中文输出无乱码
6. 长命令（如 `python -c "import time; time.sleep(60)"`）执行中按 Esc → 「已中断」→ 追问「刚才工具怎么了」模型知道被取消
7. 「先读 pyproject.toml 再告诉我项目名叫什么」→ 模型在一次提问内自主完成多步（读文件后作答）
   - 修订后：「列出 docs 下的目录 → 读取其中某个 spec.md → 中文总结」→ 观察 **≥2 个 AI 块**、多轮工具自动衔接（验证 Agent 循环）
8. `/profile kimi` → 重复 1-2 任一项（双端工具调用各一轮）
9. 连续 3 轮纯文本对话（验证 Phase 1 路径无回归）

---

# 修订部分验收（单步 → Agent 循环 + 工具执行反馈，T20-T26）

> 承接 spec 修订部分（C27-C33 / N10-N14）中未展开的具体值（上限 10 / 心跳间隔 2s / 提示与错误文本）。

## A. Agent 循环

- [x] 提问「先列出 docs 下有哪些目录，再读取其中一个 spec.md，再总结」→ 界面出现 **≥2 个 AI 块**，每块含对应工具行，**无需二次追问**即完成
- [x] 模型在多轮中再次请求工具时**执行**而非提示（Phase 2 的「连环调用属下一章能力」提示在本期已消失）
- [x] 工具轮数达上限（**10**）后：不再执行新工具，界面提示「已达最大工具轮数（10）」，本轮正文照常入史（正文内容取决于模型输出）
- [x] 循环中任意一轮按 Esc：当前及剩余工具全部配对「[已中断] 用户取消了本次工具执行」结果入史，露出半截内容标 `[已中断]`
- [x] 历史配对校验：跑完后统计入史消息，assistant(tool_calls) 的每个 id 都有唯一配对的 tool_result（零孤儿）

## B. Bash 流式 + 心跳

- [x] 执行 `Get-ChildItem -Force $HOME | Select-Object -First 200`：工具行在命令**结束前**即开始出现输出片段（非等全部结束才一次性显示）
- [x] 执行 `Start-Sleep 5; 'done'`：工具行显示心跳计时，间隔约 **2s**
- [x] 非 Bash 工具执行期间**同样**显示心跳计时（覆盖全工具，N13）
- [x] 工具行始终显示已用秒数（保留一位小数）：运行中每约 2s 刷新一次，执行结束后显示总耗时（`✓ 名称 · 摘要（N.Ns）`）；首心跳前不显示，避免冻结的 0.0s
- [x] Bash 超时（`Start-Sleep 60`，默认 30s）：到点强制终止，回传结构化结果「命令超时（30s）已强制终止」
- [x] Bash 流式执行期间按 Esc：进程树被杀干净（powershell.exe 计数未增加）

## C. 回归与端到端

- [x] Phase 1 流式 / 中断 **12/12** 用例全绿，无回归（2026-10-20 重建等价套件：12/12）
- [x] Phase 2 工具系统 mock 用例全绿，无回归（2026-10-20 重建等价套件：26/26）
  - 覆盖：注册中心 / 双协议 Schema / 执行保护五路径 / 参数校验失败 / disable-enable / CancelledError 穿透 / 输出截断 / 六工具行为 / 文件缓存三态 / anthropic 与 openai 序列化 / anthropic 与 openai 流解析 / 工具轮成功 / 失败自愈 / 四种中断与回滚 / 配对不变量 / Agent 循环 3 轮 / 循环上限 10 / 心跳与收尾心跳
  - 备注：旧清单里的「27/15/11/16/6/15」是当时按脚本文件切分的计数，等价重建后按 A/B/C/D 四段共 26 条断言覆盖同一组行为；端到端真实 API 与 TUI 人工动线仍按本文件对应段落单独执行。
- [x] 真实 API 端到端：一次提问内模型完成 **≥2 轮串行工具调用**（例：「列出 docs 下有哪些 md → 读取其中一个 → 用中文总结」）
- [x] 真实 API：长耗 Bash 命令执行期间，界面可见增量输出与心跳计时

## 验证记录（2026-10-17）

- A：mock 3 工具轮 + 最终回复 → 4 轮；上限测试 → 工具轮止于 10、第 11 轮不再提供工具、Notice 一条；工具阶段中断 → 配对完整含「已中断」。Textual 无头 harness：3 轮事件流 → `#chat` 出现 3 个 `AIMessageView`（每轮一块）。
- B：真实 Bash 流式 → 119 分片、首片于 0.39s 回传；`Start-Sleep 5` → 心跳 2s/4s；非 Bash 慢工具 → 心跳 2s/4s；`Start-Sleep 60` 默认 30s → 超时结构化结果；Esc 取消 → powershell.exe 计数 5→5（无孤儿）。
  - 2026-10-20 增补（耗时显示）：会话层在 `ToolExecuted` 前补发一次「收尾心跳」给出精确总耗时；TUI 结束态显示 `（N.Ns）`，无心跳时用本地计时兜底。mock 校验：running 首心跳前无耗时 → 心跳后 `（2.0s）` → 结束态 `（N.Ns）`；事件序列确认收尾心跳先于 `ToolExecuted`，耗时 0.062s（真实执行期计时）。
- C：真实 API（deepseek/openai 协议）一次提问 → 3 轮、3 次工具执行（Glob/Bash/ReadFile）、tool_use==tool_result、最终中文总结产出。
- Phase 1/2 原有用例脚本为临时文件、已不在仓库，**未原样复跑**；本轮以「六工具经新统一路径（execute_streaming 默认退化）逐个执行通过」+ `sucode --version` 冒烟替代，两项待补跑。

## 验证记录（2026-10-20 补跑回归）

- A（Phase 1，12/12）：完整轮 / 多轮记忆 / thinking 不入史 / 两种中断 / 两种出错 / 热切换 / clear / tools=None / 纯文本双协议序列化 / finish_reason 透传。
- B/C/D（Phase 2，26/26）：见上条覆盖清单。
- **补跑暴露并修复 2 处「设计决策 ↔ 实现」不符**（正是本轮要核对的目标）：
  1. 纯对话路径把 thinking/signature 写入了历史（`_assistant_message` 无条件留存）→ 与「无工具场景 thinking 不入史、节省 token」相悖。改为仅**带工具的会话**留存（`keep_thinking = registry is not None`），spec C25 补「留存范围」一条。
  2. 工具阶段中断的配对结果 `is_error=false`（内容已是「用户取消」却标记为成功）→ 改为 `is_error=true`，spec C24 补明该字段。
- 修复后复跑：Phase 1 12/12、Phase 2 26/26 全绿。

---

# 对齐部分验收（图片3 设计表：设计决策 → Python 实现，T27）

> 与设计表逐行对应；数值与文案承接 spec 对齐部分（C34-C41 / N15）。

## D. 工具层设计决策

- [x] 参数校验：六工具 Schema 由 Pydantic `model_json_schema()` 生成；anthropic 为 `{name, description, input_schema}`、openai 为 `{type:"function", function:{..., parameters}}`，格式各自正确
- [x] 参数校验失败路径：缺必填 / 类型不符 / 坏 JSON / 未知工具 / 禁用工具 → 均返回结构化 `is_error` 文案，程序不崩溃
  - 错误文案：`参数校验失败：path: Field required`、`参数不是合法 JSON：…`、`参数校验失败：path: Input should be a valid string；limit: …`、`未知工具「NoSuchTool」，可用工具：…`、`工具「Bash」已被禁用，当前不可执行`
- [x] 工具分类：`category` 取值正确（ReadFile/Glob/Grep=`read`、WriteFile/EditFile=`write`、Bash=`command`）
- [x] 结果传递：`ToolResult` 为 dataclass 且字段为 `output` / `is_error`
- [x] 注册机制：`disable` 后工具不出现在工具列表且执行被拒；`enable` 后恢复；禁用未知工具报错
- [x] 文件缓存：ReadFile 读时填充；盘上文件被外部改写（mtime 变化）后不吃陈旧内容；WriteFile / EditFile 写后读到新内容
- [x] 异步执行：六工具 `execute` 均为 async；Bash 走 `asyncio.create_subprocess_shell`
- [x] 延迟加载机制：无关提问不暴露、相关提问暴露（人造延迟工具验证）；六工具均 `should_defer=False`，本期行为不变
- [x] 协议适配：`get_all_schemas(protocol)` 两种协议格式正确，`chat.py` 每轮以当前提问作 query

## E. Bash 在 shell 模式下的回归（create_subprocess_shell）

- [x] 中文输出无乱码（`Write-Output 你好`）
- [x] 退出码正确传递（`exit 7` → `[退出码 7]`）
- [x] cmdlet 失败不塌缩为成功（`ls -la` → `[退出码 1]`）
- [x] 超时杀整棵进程树并给结构化报错（`Start-Sleep 5` + `timeout_seconds=2` → `命令超时（2s）已强制终止`）
- [x] CLIXML 噪音剥净且错误正文保留（`ls -la` 输出无 `#< CLIXML` / `</Objs>`，且含 `Get-ChildItem : A parameter cannot be found…`）
- [x] 真实 API：Pydantic Schema 下模型仍能正确发起工具调用并完成一轮（2026-10-20 deepseek 真机：一次提问 3 轮 / 3 工具 Bash+Glob+ReadFile，tool_use==tool_result，中文总结产出）

## 验证记录（2026-10-20）

- D/E：34 项脚本断言全过（临时脚本，不入仓库）。其中 CLIXML 一项为**本轮新发现的真实缺陷**：原正则要求 `#< CLIXML>`，而 PowerShell 5.1 实测输出为 `#< CLIXML` + 换行（末尾无 `>`），导致 CLIXML 从未剥净、XML 噪音直接进入模型上下文；已修正为容忍换行并抽取 `<S S="Error">` 正文。
