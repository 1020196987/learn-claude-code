# s01 · Agent Loop

> 原章目录：`s01_agent_loop/`
> 一句话：**One loop & Bash is all you need** —— 一个工具 + 一个循环 = 一个 Agent。

## 1. 设计动机

当你向大模型提出一个问题（比如"读取目录下有哪些文件并执行 XXX.py"），模型能输出 bash 命令，但**输出完了就停了**。它不会自己跑，也不会看到结果后继续推理。

要让它"自己跑并继续推理"，需要做的就一件事：**把"模型输出 → 执行 → 把结果喂回去 → 再调模型"这条链自动化**。这就是 agent loop。

```
User --> messages[] --> LLM --> response
                                      |
                            stop_reason == "tool_use"?
                           /                          \
                         yes                           no
                          |                             |
                    execute tools                    return text
                    append results
                    loop back -----------------> messages[]
```

这是最小循环。**每个 AI Agent 都需要这个循环**。模型决定何时调用工具、何时停止；代码只是执行模型的要求。

## 2. 机制拆解

### 2.1 循环的两条路径

| 信号 | 含义 | 循环动作 |
|------|------|---------|
| `stop_reason == "tool_use"` | 模型举手说"我要用工具" | 执行 → 结果喂回去 → 继续 |
| `stop_reason != "tool_use"` | 模型说"我做完了" | 退出循环 |

教学版用 `stop_reason` 作为继续/退出的判据。生产版的 Claude Code **不直接信任 `stop_reason`**，因为流式响应里 `stop_reason` 可能还没更新但内容里已经有 `tool_use` block 了。CC 用 `needsFollowUp` 标志：接收到流式消息时检测到 `tool_use` block 就设为 true；query loop 靠 `needsFollowUp` 决定是否继续。教学版为概念清晰，采用 `stop_reason` 判断。

### 2.2 循环只做 5 件事

1. 把用户问题放进 `messages[]`
2. 把 `messages[]` 和工具定义发给 LLM
3. 追加模型回答到 `messages[]`，没调工具就退出
4. 执行模型要求的工具（只有一个：`run_bash`），收集结果
5. 把工具结果追加为新的 user 消息，回到第 2 步

整套不到 30 行。这就是最小可运行的 agent harness 内核。

### 2.3 关键设计：把"中间人"删掉

原来的工作流：模型给 bash → 你复制 → 跑 → 复制输出 → 贴回对话框 → 模型继续。

agent loop 做的就是这个循环的自动化。**它没加任何"智能"**，只是把模型和环境之间的"胶水代码"写出来了。

## 3. 循环始终不变

这是整个仓库最重要的设计哲学：**从 s01 到 s20，agent loop 本身一直不变**。变的是循环周围的机制（工具、权限、hooks、记忆、团队……）。

教学版的 `agent_loop(messages)` 30 行代码，在 s20 综合版里仍然是核心骨架。CC 1729 行的 `query.ts` 核心也是这 30 行——所有多出来的字段（State 上的 10 个字段、20+ 种退出/继续路径、流式执行）都是**保护机制**，不是核心逻辑。

> 启示：先理解核心循环，后面的一切自然展开。

## 4. 关键改动点（对照 `s01_agent_loop/code.py`）

| 组件 | 数量/内容 |
|------|---------|
| 工具 | 只有 `bash` 一个 |
| 循环骨架 | `while True` + `messages.append` + `stop_reason` 判断 |
| 工具执行 | 硬编码 `run_bash(block.input["command"])` |
| 消息追加 | assistant 完整 + user 一条 `tool_result` 列表 |
| 路径校验 | 无（教学 demo，直接执行模型给的命令） |

下一章（s02）只改"工具执行"那一行：硬编码的 `run_bash()` 变成 `TOOL_HANDLERS[block.name]()` 查表分发。**循环本身不动**。

## 5. 试一下

```sh
cd learn-claude-code
python s01_agent_loop/code.py
```

**观察重点**：

1. 模型什么时候调用工具（循环继续），什么时候不调用（循环结束）？
2. 多轮对话时 `messages` 列表如何增长？
3. 同一个 bash 命令执行多次会得到不同结果吗？比如 `ls` 之后的目录结构变了，模型能看到吗？

**推荐 prompt**：

- `Create a file called hello.py that prints "Hello, World!"` —— 模型调 `bash` 执行 `echo` 或 `cat`，看到结果后告诉用户完成。
- `What is the current git branch?` —— 一次工具调用就能回答，看循环退出的速度。
- `List all Python files, then read the first one` —— 多次工具调用，看模型如何规划。

> **安全提示**：s01 没有权限检查，模型生成的 bash 命令会直接执行。建议在临时目录里跑，避免影响项目文件。真正的权限闸门是 s03 的内容。

## 6. 教学版与 CC 的关键差异

| 维度 | 教学版 (s01) | Claude Code (query.ts) |
|------|-------------|---------------------|
| 循环继续判据 | `stop_reason == "tool_use"` | `needsFollowUp` 标志（流式中更可靠） |
| State 字段 | 仅 `messages` | 10 个字段（含 toolUseContext、autoCompactTracking、stopHookActive、turnCount 等） |
| 退出路径数 | 1 条（不调工具） | 20+ 条（max turns、blocking limit、prompt too long、model error、hook stop 等） |
| 工具执行 | 同步串行 | `StreamingToolExecutor` 流式并发 |
| 流式支持 | 无 | 完整流式（工具可在模型还在生成时启动） |

**一句话总结**：30 行循环 = 1729 行的核心。所有复杂字段和退出路径都是保护机制。先理解核心循环，后面展开自然。

---

下一章：[s02 · Tool Use](./02-s02-tool-use.md)