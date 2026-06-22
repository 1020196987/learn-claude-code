# s04 · Hooks

> 原章目录：`s04_hooks/`
> 一句话：**挂在循环上，不写进循环里** —— hook 在工具执行前后注入扩展逻辑。

## 1. 设计动机

s03 的 Agent 有权限检查了。但每次加一个新检查（比如"记录每次 bash 调用"、"操作后自动 git add"），都要修改 `agent_loop` 函数：

```python
def agent_loop(messages):
    while True:
        for block in response.content:
            output = execute(block)
            log_to_file(block)        # 加一行
            check_permission(block)    # 加一行
            notify_slack(block)        # 又加一行
            auto_git_add(block)        # 再加一行
            # ... 很快循环就认不出来了
```

你想扩展的是 Agent 的**行为**，但改的却是**循环本身**。循环应该是一个稳定的核心，扩展应该挂在外面。

## 2. 机制拆解

### 2.1 四个事件覆盖一个完整的 agent cycle

| 事件 | 触发时机 | 典型用途 |
|------|---------|---------|
| `UserPromptSubmit` | 用户输入提交后、进入 LLM 前 | 输入验证、注入上下文 |
| `PreToolUse` | 工具执行前 | 权限检查、日志记录 |
| `PostToolUse` | 工具执行后 | 副作用（自动 git add 等）、输出检查 |
| `Stop` | 循环即将退出时 | 收尾清理、强制续跑 |

扩展通过 `register_hook()` 添加，循环只调用 `trigger_hooks()`。

### 2.2 Hook 注册表：一个字典 + 一个触发器

```python
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:   # 返回值 ≠ None → hook 说"停"
            return result
    return None
```

**返回值约定**：

- `None` = 放行，继续执行
- 非 None = 拦截（或在 `Stop` 时强制续跑）

这就把"扩展行为"的接口固化了：写一个函数、注册到对应事件、决定要不要返回阻拦。

### 2.3 s04 把权限检查搬到 hook 上

s03 的 `check_permission()` 写在循环里。s04 把同样逻辑包装成 PreToolUse hook：

```python
def permission_hook(block):
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                return "Permission denied by deny list"
    # ... 闸门 2、3 类似逻辑
    return None

register_hook("PreToolUse", permission_hook)
```

s03 → s04 改动的不是权限逻辑，而是"逻辑的位置"——从循环内硬编码变成循环外注册。**这是关注点分离**。

### 2.4 Stop hook 可以强制续跑

教学版 Stop hook 用于打印收尾统计。但如果 hook 返回非 None 值，**循环不会退出**，而是把 hook 返回值作为新的 user 消息注入：

```python
if response.stop_reason != "tool_use":
    force = trigger_hooks("Stop", messages)
    if force:
        messages.append({"role": "user", "content": force})
        continue   # ← 不退出，继续下一轮
    return
```

这是"模型说做完了，但 hook 不同意"的机制。典型用途：审计未通过、缺关键步骤、必须再检查一遍。

## 3. 关键改动点（对照 `s04_hooks/code.py`）

| 组件 | s03 | s04 |
|------|-----|-----|
| 扩展方式 | `check_permission()` 硬编码在循环里 | `HOOKS` 注册表 + `trigger_hooks()` |
| 新函数 | — | `register_hook`, `trigger_hooks` |
| hook 回调 | — | `context_inject_hook`, `permission_hook`, `log_hook`, `large_output_hook`, `summary_hook` |
| 循环 | 直接调用 `check_permission()` | 调用 `trigger_hooks("PreToolUse", ...)` |
| 退出控制 | 无 | `trigger_hooks("Stop", ...)` 可阻止退出 |
| 输入拦截 | 无 | `trigger_hooks("UserPromptSubmit", ...)` 可注入上下文 |

循环里**只改了一处**：s03 直接调用 `check_permission(block)`，s04 改为 `trigger_hooks("PreToolUse", block)`。

## 4. 试一下

```sh
cd learn-claude-code
python s04_hooks/code.py
```

**观察重点**：

1. 每次工具执行前，是否出现了 `[HOOK]` 日志？
2. 权限被拒时，是 hook 拦截的还是循环里硬编码的？
3. PostToolUse 钩子能否拿到工具输出？（如 `large_output_hook`）

**推荐 prompt**：

- `Read the file README.md` —— 应该直接通过，观察 hook 日志
- `Create a file called test.txt` —— 通过后观察 PostToolUse 是否触发
- `Delete all temporary files in /tmp` —— bash + `rm` 触发权限 hook

## 5. 教学版与 CC 的关键差异

| 维度 | 教学版 (s04) | Claude Code |
|------|-------------|------------|
| Hook 事件数 | 4 个（核心 cycle） | 27 个（含 SessionStart、PreCompact、TaskCreated、TeammateIdle 等） |
| HookResult 字段 | 简单的 "非 None = 阻止" | 14 个字段（message、blockingError、updatedInput、permissionBehavior、preventContinuation 等） |
| Hook 优先级 | 顺序执行 | 多源合并（user settings > project settings > plugin 等） |
| Hook allow 与 deny 规则的不变式 | 无 | **hook 返回 allow 时仍要检查 deny/ask 规则**（最关键的安全设计） |
| Stop hook 防无限循环 | 无 | `stopHookActive` 状态字段防 stop hook 反复触发 |

**关键不变式**（`toolHooks.ts:325-331`）：CC 的权限系统最重要的安全设计是 **hook 返回 allow 时，仍然要检查 settings.json 的 deny/ask 规则**。即使用户的 hook 脚本说"允许"，如果在 settings.json 中禁用了这个工具，操作仍然会被阻止。教学版没有这个层次。

**stopHookActive 防永循环**：CC 的 Stop hooks 有防无限循环机制（`query.ts:212,1300`）。当 stop hooks 产生 blockingError 时，循环带 `stopHookActive: true` 重入下一轮。后续迭代中 stop hooks 看到这个标志就不会再次触发。这防止了一个永不停机的 bug：模型自纠后 stop hook 再次报错 → 模型再自纠 → stop hook 再报错...

**hook_stopped_continuation**：PostToolUse hooks 返回 `preventContinuation: true` 时，会产生一个 `hook_stopped_continuation` 附件（`toolHooks.ts:117-130`）。query.ts 检测到后设置 `shouldPreventContinuation = true`，循环退出。这是 "hook 优雅地让 Agent 停机" 的机制，不是崩溃，是完成。

---

下一章：[s05 · TodoWrite](./05-s05-todo-write.md)