# s05 · TodoWrite

> 原章目录：`s05_todo_write/`
> 一句话：**没有计划的 agent 走哪算哪** —— 先列步骤再动手，长任务更不容易漏项。

## 1. 设计动机

给 Agent 一个复杂任务："把所有 Python 文件改成 snake_case 命名，然后跑测试，修好失败。"

Agent 开始干活，改了 3 个文件，跑了个测试，发现 2 个失败，开始修。修着修着，它**忘了最初是"改成 snake_case"**，测试失败把注意力全吸走了。

对话越长越严重：工具结果不断填满上下文，系统提示的影响力被稀释。一个 10 步重构，做完 1-3 步就开始即兴发挥，因为 4-10 步已经被挤出注意力了。

解决方案：**给 Agent 一个计划工具**。先列清单，再做。

## 2. 机制拆解

### 2.1 todo_write 工具：只增加规划能力，不增加执行能力

s05 新增的工具 `todo_write` **不做任何实际工作**——不能读文件、不能跑命令。它的唯一作用是让 Agent 在动手之前先理清思路。

```python
def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    CURRENT_TODOS = todos
    # 打印 + 返回字符串
    return f"Updated {len(CURRENT_TODOS)} tasks"
```

这看起来"没做什么"——但正是它的设计：**只给规划能力，不给执行能力**。规划是模型的活，harness 不应该抢。

### 2.2 三种状态

每个 todo 有 `content`（内容）和 `status`（状态）：

- `pending` — 待办
- `in_progress` — 进行中（同一时刻只有一个）
- `completed` — 已完成

典型流程：先调 `todo_write` 列出所有步骤（全 `pending`）→ 做一个步骤，改成 `in_progress` → 做完改成 `completed` → 看下一个 `pending` → 继续。

### 2.3 Nag reminder：3 轮没调用就提醒

教学版有一个简单机制：连续 3 轮没调 `todo_write` 时，自动注入一条提醒。

```python
if rounds_since_todo >= 3 and messages:
    messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
    rounds_since_todo = 0
```

这是教学机制。CC 源码中没有固定的"3 轮"逻辑，更接近的是 `TodoWriteTool.ts:72-107` 中当 3 个以上 todo 全部完成但没有 verification 项时，追加 verification nudge。

### 2.4 dispatch 机制不变

新工具仍然走 `TOOL_HANDLERS[block.name]` 分发，加一个 handler。**循环骨架不变**。

## 3. 关键改动点（对照 `s05_todo_write/code.py`）

| 组件 | s04 | s05 |
|------|-----|-----|
| 工具数量 | 5 | 6 (+todo_write) |
| 规划能力 | 无 | 带状态的 TODO 列表 + nag reminder |
| SYSTEM 提示 | 通用提示 | 加入 "先计划再执行" 引导 |
| 循环 | 不变 | dispatch 不变，新增 `rounds_since_todo` 计数器和 reminder 注入 |
| 状态存储 | — | `CURRENT_TODOS` 全局变量（教学简化，进程内 / 会话级） |

## 4. TodoWrite vs Task System

仓库里有两套"任务"系统，容易混淆：

| | TodoWrite (s05) | Task System (s12) |
|---|---|---|
| 定位 | 当前任务的执行清单 | 可恢复的任务系统 |
| 存储 | 进程内 / 会话状态 | `.tasks/{id}.json` |
| 依赖 | 无 | `blockedBy` / `blocks` 依赖图 |
| 生命周期 | 当前会话 / 当前任务 | 跨会话保留 |
| 分工 | 不负责任务认领 | `owner` / claim |
| 状态 | pending / in_progress / completed | pending / in_progress / completed |
| 粒度 | Agent 自己的步骤 | 可被认领、追踪、解锁的任务 |

**TodoWrite 是 Agent 给自己列的工作清单**，**Task System 是给团队用的任务图**。CC 中两者并存，通过 `isTodoV2Enabled()` 切换——交互式会话默认启用 V2（Task System），非交互式（SDK）默认 V1（TodoWrite）。

## 5. 试一下

```sh
cd learn-claude-code
python s05_todo_write/code.py
```

**观察重点**：

1. 第一次工具调用是不是 `todo_write`？
2. TODO 列了几步？
3. 执行过程中状态有没有从 `pending` 变成 `in_progress` / `completed`？
4. 模型是否会"忘记"最初的目标？（TodoWrite 的最大价值就是防止这个）

**推荐 prompt**：

- `Refactor s05_todo_write/example/hello.py: add type hints, docstrings, and a main guard` —— 先列 3 步再执行
- `Create a Python package under s05_todo_write/example/demo_pkg with __init__.py, utils.py, and tests/test_utils.py` —— 跨多文件多步任务
- `Review Python files under s05_todo_write/example and fix any style issues` —— 长任务，TodoWrite 防漂移的价值最明显

## 6. 教学版与 CC 的关键差异

| 维度 | 教学版 (s05) | Claude Code |
|------|-------------|------------|
| 状态存储 | `CURRENT_TODOS` 全局变量 | TodoWrite V1 存 AppState（内存）；V2（Task System）存文件 |
| Reminder 机制 | 3 轮未调用就提醒 | 无固定轮数；3 个 todo 全 completed 但缺 verification 项时 nudge |
| `activeForm` 字段 | 无 | CC 用它给 UI spinner 展示"正在做什么" |
| 与 Task System 关系 | 单系统 | 同时存在两套，feature flag + env var 切换 |

教学版把 TodoWrite 简化为进程内的全局字典。CC 的 V1 也是内存 AppState（`TodoWriteTool.ts:65-103`），退出后清空。V2（s12 的 Task System）是文件持久化 + 依赖图 + 并发锁 + ownership + 4 个独立工具（Create/Get/Update/List），层级更高。

**为什么 V1 和 V2 共存**：V1 适合快速、单会话、轻量；V2 适合跨会话、团队协作、有依赖关系。默认按会话类型切换，但用户可用 `CLAUDE_CODE_ENABLE_TASKS` env var 强制启用 V2。

---

下一章：[s06 · Subagent](./06-s06-subagent.md)