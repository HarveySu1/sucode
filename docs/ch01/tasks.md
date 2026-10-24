# sucode · Phase 1 任务清单

> 执行顺序即列表顺序；依赖关系见各任务。每个任务可在一个专注会话内完成，完成自测后再进入下一个。
> 目标目录结构：

```
sucode/
├── pyproject.toml
├── spec.md / tasks.md / checklist.md
├── sucode/
│   ├── __init__.py          # 版本号
│   ├── __main__.py          # python -m sucode
│   ├── cli.py               # 入口：参数解析 → 配置 → 启动 TUI
│   ├── config.py            # YAML 配置加载与校验
│   ├── chat.py              # 会话：历史、上下文组装、profile 切换
│   ├── providers/
│   │   ├── __init__.py      # 工厂：按 protocol 返回 Provider 实例
│   │   ├── base.py          # 抽象接口 + 统一流事件定义
│   │   ├── anthropic_provider.py
│   │   └── openai_provider.py
│   └── tui/
│       ├── __init__.py
│       └── app.py           # Textual 应用（布局/输入/命令/中断/渲染）
```

---

## T1 项目脚手架
- **内容**：pyproject.toml（依赖：textual / anthropic / openai / pyyaml；requires-python >= 3.10；入口 `sucode = sucode.cli:main`）；包骨架；cli.py 先用 argparse 支持 `--version` 与 `--profile`（占位不生效）
- **影响文件**：pyproject.toml、sucode/__init__.py、sucode/__main__.py、sucode/cli.py
- **依赖**：无
- **参考定位**：setuptools 的 `[project.scripts]` entry-points；argparse 标准库
- **自测**：`pip install -e .` 成功，`sucode --version` 输出 0.1.0，`python -m sucode --version` 一致

## T2 配置模块
- **内容**：加载 `~/.sucode/config.yaml`；结构为顶层 `default_profile` + `profiles` 映射，每个 profile 含 protocol / model / base_url / api_key 四核心字段与可选 max_tokens、thinking(enabled + budget_tokens)；校验：protocol 仅限 anthropic|openai、必填字段缺失、thinking 预算下限；缺文件/字段非法时打印完整示例模板与修复指引后以非 0 退出；api_key 留空回退环境变量；base_url 留空走官方端点
- **影响文件**：sucode/config.py（pyproject 无需改）
- **依赖**：T1
- **参考定位**：`yaml.safe_load`；环境变量名 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`；thinking 预算下限 1024（Anthropic API 硬性约束）、max_tokens 必须大于预算
- **自测**：无配置文件启动 → 看到含四字段的示例模板；写入两个 profile 的合法配置 → 解析成功

## T3 Provider 抽象层
- **内容**：定义抽象接口（输入完整对话历史，返回流事件迭代器）与统一事件集（思考片段 / 正文片段 / 结束 / 异常），事件用 dataclass 表达；提供工厂函数按 profile 的 protocol 返回对应实现；未知 protocol 抛统一异常
- **影响文件**：sucode/providers/__init__.py、sucode/providers/base.py
- **依赖**：无（可与 T2 并行）
- **参考定位**：`abc.ABC` 或 `typing.Protocol`；`dataclasses`；异常统一从 base 导出
- **自测**：工厂对 anthropic/openai 两个 protocol 返回不同实现类；未注册的 protocol 抛异常

## T4 Anthropic Provider
- **内容**：基于官方 SDK 实现抽象接口；流式调用并透出思考与正文两类片段事件；thinking 开启时的约束处理（不携带 temperature；max_tokens 缺省 8192 且须大于思考预算，预算缺省 4096）；多轮历史组装时不回传 assistant 的思考块（非工具场景合法且省 token）；SDK/HTTP 异常转译为统一异常
- **影响文件**：sucode/providers/anthropic_provider.py
- **依赖**：T3
- **参考定位**：anthropic SDK `client.messages.stream(...)` 上下文管理器；遍历流事件，`content_block_delta` 事件的 `delta.type == "thinking_delta"` → 思考片段、`"text_delta"` → 正文片段；思考开关参数 `thinking={"type": "enabled", "budget_tokens": N}`；SDK 默认自带连接重试
- **自测**：写一个临时脚本直连真机，跑通「一轮含 thinking 的流式输出」，终端能交替看到思考与正文

## T5 OpenAI Provider
- **内容**：基于官方 SDK 实现同一抽象接口；流式透出正文片段事件（不产出思考事件）；支持自定义 base_url 指向任意 OpenAI 兼容端点；鉴权/网络异常转译为统一异常
- **影响文件**：sucode/providers/openai_provider.py
- **依赖**：T3
- **参考定位**：openai SDK `client.chat.completions.create(stream=True)`；`chunk.choices` 可能为空、`delta.content` 可能为 None（跳过）；客户端构造参数 `base_url=` / `api_key=`
- **自测**：临时脚本直连 OpenAI 或 DeepSeek 端点，流式打印逐字输出

## T6 会话管理
- **内容**：内存中的对话历史（用户/AI 交替）；组装上下文调用 Provider 并收集流事件；AI 回复（含中断半截，带标记）入史；清空命令语义；profile 热切换 = 仅更换 Provider 实例，历史保留
- **影响文件**：sucode/chat.py
- **依赖**：T3
- **参考定位**：中断消息以「AI 角色 + 截断标记」入史，后续轮次照常参与上下文
- **自测**：用 mock Provider 验证：两轮后历史长度为 4；切换 profile 后历史不变；清空后长度为 0

## T7 TUI 骨架
- **内容**：Textual 全屏应用，三段布局：顶栏（`sucode · {profile}/{model}`）、滚动对话区、底部多行输入框；Ctrl+Q 退出；子类化 TextArea 实现 Enter 发送 / Shift+Enter 换行；发送后立即清空输入框
- **影响文件**：sucode/tui/__init__.py、sucode/tui/app.py
- **依赖**：T1
- **参考定位**：Textual `App.compose` + CSS 布局（`#header / #chat / #input` 三容器）；`textual.widgets.TextArea` 的 `BINDINGS` 覆盖 enter 键；换行动作为 `insert("\n")`；退出绑定 `ctrl+q`
- **自测**：`sucode` 启动看到三段布局；输入多行文本回车后（此时可仅打印到对话区）Ctrl+Q 干净退出、无终端残留

## T8 流式渲染
- **内容**：对话区分角色展示：用户消息一种样式、AI 思考暗淡灰色、AI 正文 Markdown 增量渲染（节流刷新而非每个 chunk 都重绘）、中断标记与错误信息样式；流结束后最终整段重渲染一次保证 Markdown 完整
- **影响文件**：sucode/tui/app.py（过大可拆 sucode/tui/widgets.py）
- **依赖**：T7
- **参考定位**：`textual.widgets.Markdown` 的 `append()` 做增量追加；节流参考 80ms 一次（`set_interval` 或时间戳判断）；结束后 `Markdown.update(全文)` 收尾；暗淡样式用 Textual style `dim`
- **自测**：mock Provider 逐字吐出「思考 + 含代码块的正文」，观察：思考灰、正文逐字出现、最终代码块高亮、无闪烁

## T9 命令、中断与错误处理
- **内容**：发送前拦截 `/` 开头输入：`/exit`、`/new`、`/help`（列出全部命令）、`/profile`（无参显示当前与可用列表，带参切换并更新顶栏）；Esc 中断生成（取消后台任务，半截入史并标记）；网络/鉴权错误显示后保留半截；上下文超限错误提示使用 `/new`；生成期间输入框保持可用
- **影响文件**：sucode/tui/app.py、sucode/chat.py
- **依赖**：T6、T8
- **参考定位**：Textual worker 机制（`run_worker` / `@work`，worker.cancel() 中断流式循环）；命令解析在消息发送路径最前端；顶栏更新用 reactive 属性
- **自测**：mock 场景下依次执行四个命令；Esc 中断 mock 长输出，半截带标记显示且后续轮次能引用

## T10 接入主流程
- **内容**：cli.main 串联全链路：解析 `--profile`（缺省读配置 default_profile）→ T2 加载与校验 → 工厂创建 Provider → 构建会话 → 启动 TUI；启动期一切失败（无配置/未知 profile/非法 protocol）统一为人类可读输出 + 非 0 退出码；Windows 控制台 UTF-8 兜底处理
- **影响文件**：sucode/cli.py、sucode/tui/app.py
- **依赖**：T2、T4、T5、T6、T9
- **参考定位**：argparse `--profile` 默认值延迟到配置加载后决定；`sys.stdout.reconfigure(encoding="utf-8")` 处理 GBK 控制台；`--version` 输出保持 T1 行为
- **自测**：分别制造「无配置 / 未知 profile / 非法 protocol」三种启动失败，均输出指引且退出码非 0；正常配置下 `sucode` 与 `sucode --profile xxx` 均进入对话

## T11 端到端验证
- **内容**：按 checklist.md 全量执行：双后端真实 API 对话、thinking 展示、多轮记忆、profile 热切换、Esc 中断、各错误场景、PowerShell 中文显示；发现的问题当场修复并复验
- **影响文件**：视问题而定（预期为小修）
- **依赖**：T10
- **参考定位**：checklist.md 即执行脚本；grep 验证项见清单末尾
- **自测**：checklist 全部条目可勾选

---

## 修订记录
- 2026-10-17：T5 后增补——OpenAI Provider 读取 reasoning_content 产出思考事件（spec C6/O6 同步修订）；新增 Ctrl+J 通用换行键（老式 conhost 无法区分 Shift+Enter，属终端物理限制）；输入框光标改下划线样式；SDK 客户端 timeout 收敛为 120s（为流式中退出进程的最坏挂起兜底）。
- 2026-10-17（档二对齐课程设计表）：Provider/会话层全面 async 化（async generator + AsyncAnthropic/AsyncOpenAI + async with 自动关流 + Textual 异步 worker，Esc 中断在 await 点即时生效，退出不再有线程挂起风险）；自定义异常扩至 4 种（ProviderError + Auth/RateLimit/ContextOverflow 子类）；事件类型扩至 7 个（MessageStart/UsageUpdate/ToolCallStart/ToolCallDelta 为 Phase 2 预留）；Message 扩至 5 字段（thinking/tool_calls/tool_call_id 为 Phase 2 预留）；新增 serialize_messages 双协议序列化分发。mock 回归 10/10、双后端真机验证通过。
