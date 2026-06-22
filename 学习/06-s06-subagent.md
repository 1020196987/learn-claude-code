# s06 · Subagent

> 原章目录：`s06_subagent/`
> 一句话：**大任务拆小，每个小任务干净的上下文** —— 子 Agent 用独立 `messages[]`，不污染主对话。

## 1. 设计动机

Agent 在修一个 bug。它读了 30 个文件来追踪调用链，中间聊了 60 轮。`messages` 列表涨到 120 条，其中大部分是"追踪调用链"的中间过程，**和"修 bug"这个最终目标无关**。

这些中间过程占着上下文位置，让 Agent 越来越"健忘"，它记不住最初的问题是什么了。

换个角度：你修 bug 的时候，会**"开一个新终端"**来追踪调用链。追踪完了，终端关掉，结果写进笔记，回到原来的终端继续修 bug。Agent 也需要这个能力：

```
开一个独立的子进程 → 给它一个独立的消息列表 → 让它专心做一件事
         ↓
它做完了 → 只把结论文本回传 → 主对话上下文保持干净
```

## 2. 机制拆解

### 2.1 spawn_subagent：独立 messages[] + 30 轮安全限制

子 Agent 跑自己的 `while True` 循环，有自己的 `messages`、`tools`、`system`：

```python
def spawn_subagent(description: str) -> str:
    sub_tools = [bash, read_file, write_file, edit_file, glob]  # 不含 task（防递归）
    messages = [{"role": "user", "content": description}]  # 全新 messages[]

    for _ in range(30):  # safety limit
        response = client.messages.create(...)
        # ... 同样的循环
        if response.stop_reason != "tool_use":
            break

    return extract_text(messages[-1]["content"])  # 只回传结论
```

三个关键设计：

| 决策 | 选择 | 原因 |
|------|------|------|
| 上下文隔离 | 全新 `messages[]` | 子 Agent 的中间过程不污染主 Agent 的上下文 |
| 只回传结论 | `extract_text(last_message)` | 不回传整个 messages 列表 |
| 禁止递归 | 子 Agent 无 task 工具 | 防止子 Agent 再 spawn 新的子 Agent |
| 安全策略不跳过 | 子 Agent 工具调用也走 PreToolUse hook | 上下文隔离不代表权限隔离 |

### 2.2 子 Agent 的工具是受限的

子 Agent 有 bash、read、write、edit、glob 五个工具，**但没有 `task` 工具**。这是教学版对"禁止递归 spawn"的简化实现——直接砍掉 spawn 能力。CC 的实现更精细（见后文）。

### 2.3 子 Agent 有独立的 system prompt

```python
SUB_SYSTEM = "你是子 Agent。直接完成任务，不要再委派给其他子 Agent。"
```

明确告诉子 Agent："别再叫别人了，自己干。"这是 prompt 工程在多 agent 场景下的典型应用——每个 Agent 有明确的角色边界。

### 2.4 主 Agent 看到的：只是 spawn 的工具调用 + 返回的字符串

主 Agent 的 messages 列表里：

```
[user] 帮我找一下这个项目的测试框架
[assistant] [调用 task 工具，描述 "找测试框架"]
[user] [tool_result: "项目用 pytest，在 tests/ 目录下..."]
[assistant] 好的，这个项目用 pytest...
```

主 Agent 不知道也不关心子 Agent 内部怎么干。它只看到："我问子 Agent，子 Agent 回了字符串。"

## 3. 关键改动点（对照 `s06_subagent/code.py`）

| 组件 | s05 | s06 |
|------|-----|-----|
| 工具数量 | 6 | 7 (+task) |
| 新函数 | — | `spawn_subagent`（独立 messages[] + 30 轮安全限制） |
| 上下文隔离 | 全部在主对话中 | 子 Agent 用全新的 messages[] |
| 循环 | 不变 | dispatch 不变，子 Agent 有独立 SUB_SYSTEM 和 hook 保护的循环 |

循环骨架依然零改动。新增的是一个 tool handler（`spawn_subagent`），handler 内部跑自己的子循环。

## 4. 试一下

```sh
cd learn-claude-code
python s06_subagent/code.py
```

**观察重点**：

1. 是否出现 `[Subagent spawned]` / `[Subagent done]` 日志？
2. 子 Agent 的工具调用是否以 `[sub] ...` 输出？（看主 Agent 是否能区分主/子活动）
3. 主 Agent 最后是否只继续处理子 Agent 返回的摘要？
4. 主 Agent 的 messages 列表长度增长了多少？和直接做相比？

**推荐 prompt**：

- `Use a subtask to find what testing framework this project uses` —— 子 Agent 去读文件，主 Agent 只收结论
- `Delegate: read all .py files in agents/ and summarize what each one does` —— 多个子任务并行（教学版串行）
- `Use a task to create s06_subagent/example/string_tools.py with a slugify(text: str) function, then verify it from the parent agent` —— 验证子 Agent 的产出

## 5. 教学版与 CC 的关键差异

CC 实际有**三种**子 Agent 执行模式，教学版只展示了第一种：

| 模式 | 触发条件 | 上下文 |
|------|---------|--------|
| **Normal Subagent** | 指定了 `subagent_type`（normal path） | 全新 messages[]，只有 prompt |
| **Fork Subagent** | 没指定 `subagent_type`，fork gate 开启 | 通过 `buildForkedMessages()` 构造 cache-friendly 前缀，**共享 prompt cache** |
| **General-Purpose** | 没指定 `subagent_type`，fork gate 关闭 | 同 Normal |

### 5.1 Fork 模式：为了共享 Prompt Cache

Fork 模式（`forkSubagent.ts:60-71`）是教学版没有的核心概念。它不创建全新上下文，而是通过 `buildForkedMessages()`（`forkSubagent.ts:107-168`）构造 cache-friendly 消息前缀，**保留父 assistant message 并生成 placeholder tool results**。

目的不是隔离，而是让 Anthropic API 的 prompt cache 命中：父子 Agent 的 system prompt、tools、messages 前缀完全一致，API 端不需要重算。

缓存命中的五个关键组件（`forkedAgent.ts:57-68`）：system prompt、tools、model、messages 前缀、thinking config，必须字节级一致。

### 5.2 Context Isolation 的精确粒度

`createSubagentContext()`（`forkedAgent.ts:345-462`）创建子 Agent 的 `ToolUseContext`：

| 字段 | 行为 |
|------|------|
| `abortController` | 新的 child controller，父 abort 向下传播 |
| `setAppState` | 默认 no-op；但 sync agent 通过 `shareSetAppState` 共享 |
| `readFileState` | **从父克隆**（避免重复读相同文件） |
| `queryTracking` | 新 chainId，`depth = parentDepth + 1` |

子 Agent **不是完全隔离的**：文件读取状态是共享的。UI 和通知的隔离程度取决于执行路径（sync/async/fork/teammate 各不同）。

### 5.3 递归 Fork 防护

教学版用"子 Agent 不给 task 工具"表达递归保护。真实实现更精细：

- `isInForkChild()`（`forkSubagent.ts:78-89`）检查对话历史中是否有 `FORK_BOILERPLATE_TAG`，有就拒绝。
- `constants/tools.ts:36-46` 中 `Agent` 工具默认在所有 agent 的禁用集合里，`USER_TYPE === 'ant'` 时例外。
- `forkSubagent.ts:73-89` 针对 fork child 有专门的递归保护。
- `agentToolUtils.ts:100-110` 在 teammate 场景下有特殊放行。

不是简单的"禁止新的子 Agent"，而是基于调用路径的精细防护。

### 5.4 Permission Bubbling

Fork Agent 的 `permissionMode: 'bubble'`（`forkSubagent.ts:67`）意味着**子 Agent 的权限弹窗冒泡到父终端**，用户在主终端里审批子 Agent 的操作。这是 s15 团队机制的早期雏形。

### 5.5 Async vs Sync

教学版只展示了同步子 Agent（父等着子跑完）。CC 还支持异步路径（`AgentTool.tsx:686-764`）：`run_in_background: true` 时异步启动，返回 `{ status: 'async_launched' }` 立即给父 Agent，子 Agent 完成后通过通知机制告知父 Agent。实际触发条件不止 `run_in_background`，还有 auto-background、assistant force async、coordinator/proactive 等路径。

---

下一章：[s07 · Skill Loading](./07-s07-skill-loading.md)