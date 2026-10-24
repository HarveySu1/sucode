# sucode · Phase 1 验收清单

> 每项必须实际操作观察后勾选，不接受「应该没问题」。全部勾选 = Phase 1 完成。
> 标注（脚本验证）的条目已由自动化脚本以真实 API 完成（2026-10-15）；其余为 TUI 交互项，按文末动线人工确认。

## 安装与入口
- [x] `pip install -e .` 成功，`sucode --version` 输出 `0.1.0`（脚本验证）
- [x] `python -m sucode --version` 输出与上一致（脚本验证）

## 配置
- [x] 无 `~/.sucode/config.yaml` 时启动：终端输出完整示例 YAML（含 `protocol` / `model` / `base_url` / `api_key` 四字段与 `default_profile`），并指明配置文件路径，退出码非 0（脚本验证：exit=2）
- [x] `--profile foo`（不存在）：报错并列出配置中全部可用 profile 名（脚本验证：deepseek、kimi）
- [x] `protocol: openai` + DeepSeek base_url 的 profile 能正常完成一轮对话（脚本验证，真实 API）
- [x] api_key 留空且已设 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 环境变量时能正常调用（脚本验证，真实 API，回复 'ok'）
- [x] base_url 留空时走官方端点（脚本验证：路由断言 `https://api.anthropic.com`；官方真机调用受网络/账号限制，如有官方 key 可在端到端时补测）
- [x] max_tokens 不填时实际请求值为 8192（SDK DEBUG 日志确认，双端各一次真实请求）
- [ ] 不带 `--profile` 启动时，顶栏显示 default_profile 指定的 profile 名

## 对话核心
- [ ] 提问后首字在 2 秒内开始出现（网络正常时）
- [ ] 回复逐字流式出现，非一次性整段（已脚本验证：SSE 正常，39 分片逐片到达；DeepSeek 端点整批刷出（跨度 0.08s）视觉上一次性呈现，Kimi 因 thinking 逐 token 流出而可见——端点行为差异，非客户端问题）
- [ ] 回复中的代码块有语法高亮与背景块（Markdown 渲染生效）
- [ ] 多轮记忆：第 1 轮告诉 AI「我叫小明」，第 2 轮问「我叫什么」，回答含「小明」
- [ ] `/new` 后再问「我叫什么」，AI 不知道

## extended thinking
- [ ] thinking 开启的 profile：正文之前出现灰色（dim）思考文本，且同为流式
- [x] thinking.budget_tokens 不填时请求值为 4096（SDK 日志确认）；填 500 时启动报错，错误信息含最小值 1024（脚本验证）
- [x] thinking 开启时请求不携带 temperature（SDK 日志确认）
- [x] thinking 开启下连续 3 轮对话均正常返回（Kimi 真机：1+1 / 2+2 / 3+3，无签名/参数错误）
- [ ] DeepSeek（OpenAI 协议）经 reasoning_content 展示灰色思考——脚本已验证：deepseek-reasoner 80 思考分片、deepseek-flash 10 分片（该模型本身返回思考字段，旧代码按 O6 丢弃所致「无思考」）；TUI 内重启后目视确认（2026-10-17 增补项）

## 命令与交互
- [ ] Enter 发送、Shift+Enter 换行：输入框出现 2 行文本后发送，AI 复述时能看到两行（补充：新增 **Ctrl+J** 通用换行键——老式 conhost 无法区分 Shift+Enter 与 Enter，属终端物理限制；绑定已脚本验证）
- [ ] 粘贴 10 行代码进输入框，内容不丢失不错位
- [ ] `/help` 列出 `/exit` `/new` `/profile` 及各自一句话说明
- [ ] `/profile` 无参数：显示当前 profile 与全部可用 profile 名
- [ ] `/profile <name>` 切换：顶栏立即变为新 `profile/模型`，历史消息仍可见，继续追问 AI 记得前文
- [ ] Ctrl+Q 退出后终端干净，无 traceback、无残留样式字符

## 中断与异常
- [ ] 生成中按 Esc：流停止，半截内容保留且带「已中断」标记
- [ ] 中断后追问「接着刚才的说」，AI 能基于半截内容继续
- [ ] 把 base_url 改成不可达地址后发送：显示可读错误信息，程序不崩溃，可继续输入
- [ ] api_key 故意填错：显示鉴权类错误信息，程序不崩溃
- [ ] 连续多轮直至超上下文：错误信息中包含使用 `/new` 的提示
- [x] 429 限流文案可区分成因且带协议码：报文含 `quota`/`billing` 类关键词 → 「触发限流（HTTP 429）：账户额度不足（请检查余额或套餐），稍后重试」；含 `rate limit`/`rpm`/`tpm` 类 → 「触发限流（HTTP 429）：请求过于频繁，稍后重试」；两类都不含 → 「触发限流（HTTP 429）：请求过于频繁或账户额度不足，稍后重试」（脚本验证：双协议各自构造真实 SDK 429 异常对象走 `_translate`，3/3 通过且两端文案一致；2026-10-20 增补，用于区分服务端限流与本地工具轮数上限提示）

## 跨平台
- [ ] Windows PowerShell 5.1（GBK 控制台）中文对话与命令显示无乱码
- [ ] Windows Terminal / PowerShell 7 下正常

## 端到端验收（必须真实 API，不许 mock）
- [ ] 模拟全新环境：干净目录 → `pip install -e .` → 手写最小配置 → `sucode` 进入对话
- [ ] 单次连续会话完成：普通问答 → thinking 问答 → `/profile` 切换后继续聊 → Esc 中断一次 → `/new` 清空再问一轮 → `/exit` 退出，全程无崩溃
- [ ] Anthropic 与 OpenAI 两个后端各完成一次 ≥3 轮的多轮对话
- [x] `grep -rniE "tool_use|run_command|edit_file" sucode/` 返回 0 条（已验证；**2026-10-17 起被 Phase 2 取代**：工具系统上线后代码中合法包含 tool_use 等标识，此项仅代表 Phase 1 时期的预留状态，验收归档见 ../ch02/checklist.md）

## 交互项建议动线（TUI 内人工完成，逐项勾选）

1. 运行 `sucode` → 顶栏应为 `sucode · deepseek/deepseek-chat`（覆盖「default_profile 顶栏」项）
2. 说「我叫小明」→ 问「我叫什么」→ 观察：首字速度 / 逐字流式 / Markdown 渲染
3. 让 AI 写一段 Python 代码 → 看代码块语法高亮
4. `/help` → `/profile` → `/profile kimi` → 顶栏变化、历史保留、继续追问记得前文
5. 问「9.11 和 9.9 哪个大」→ 看灰色思考先流式出现、正文随后
6. 让 AI 从 1 数到 100 → 中途按 Esc → 出现「已中断」标记 → 追问「接着说」
7. `/new` → 问「我叫什么」→ AI 不知道
8. 异常：把 deepseek 的 base_url 临时改成不可达地址 → 发消息看错误、确认不崩溃 → 改回；同理把 api_key 改错看鉴权报错 → 改回
9. 连续多轮长对话直至超上下文 → 错误信息应含 `/new` 提示
10. Shift+Enter 换行、粘贴多行代码 → 发送后让 AI 复述
11. PowerShell 5.1 与 Windows Terminal 各跑一遍中文对话
12. 最后完成「端到端验收」剩余三项
