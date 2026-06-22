# s03 · Permission

> 原章目录：`s03_permission/`
> 一句话：**先划边界，再给自由** —— 执行前做权限判断，决定哪些操作需要审批。

## 1. 设计动机

s02 的 Agent 有 5 个工具。file tools 受 `safe_path` 保护，但 bash 不受限制。让模型"清理一下项目"，它可能执行 `rm -rf /`。

**安全不能靠信任模型，要靠代码**——在工具执行之前做判断。

注意 s02 已经有了 file 工具的 `safe_path` 校验，但那只是"沙箱化"，不是"权限判断"。权限判断要回答的是：这个操作是否需要用户批准？

## 2. 机制拆解

### 2.1 三道闸门

s02 的循环保留，唯一的变动是在工具执行前插入 `check_permission()`。三道闸门顺序固定：

| 闸门 | 作用 | 命中后 |
|------|------|--------|
| 1. 拒绝列表 | 永远禁止的操作（`rm -rf /`、`sudo`） | 直接拒绝，不执行 |
| 2. 规则匹配 | 取决于上下文的操作（写工作区外、`rm` 文件） | 交给闸门 3 |
| 3. 用户审批 | 闸门 2 命中后，暂停等用户确认 | 用户决定允许或拒绝 |

三道都没命中 → 直接执行。大部分日常操作走这条路（读文件、列目录、写工作区内文件……）。

### 2.2 闸门 1：硬拒绝表

```python
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]

def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None
```

教学示意：简单字符串匹配不是可靠安全机制，命令变体和 shell 展开可能绕过。CC 的拒绝逻辑要复杂得多（见后文）。

### 2.3 闸门 2：规则匹配

```python
PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file"],
        "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
        "message": "Writing outside workspace",
    },
    {
        "tools": ["bash"],
        "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
        "message": "Potentially destructive command",
    },
]
```

每条规则指定**适用工具 + 检查条件 + 原因**。命中后交给闸门 3，让用户决定。

### 2.4 闸门 3：用户审批

```python
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n⚠  {reason}")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"
```

教学版用 CLI 询问。CC 在 TUI/IDE 中弹对话框，可以显示队友名字、颜色、操作预览。

### 2.5 拒绝即 tool_result，不抛异常

注意拒绝后的处理：权限被拒不是抛异常，而是把"Permission denied"作为 `tool_result` 喂回给模型。这样**模型能看到自己被拒了，能调整策略**（换工具、改路径、问用户）。

## 3. 关键改动点（对照 `s03_permission/code.py`）

| 组件 | s02 | s03 |
|------|-----|-----|
| 安全模型 | 无（信任模型） | 三道闸门权限管线 |
| 新函数 | — | `check_deny_list`, `check_rules`, `ask_user`, `check_permission` |
| 循环 | 直接执行所有工具 | 执行前插入 `check_permission()` |
| 拒绝处理 | 无 | 拒绝 → 工具结果为 "Permission denied" |

循环只多了**一行判断**：`if not check_permission(block): continue`（带拒绝消息的 tool_result）。

## 4. 试一下

```sh
cd learn-claude-code
python s03_permission/code.py
```

**观察重点**：

1. 哪些操作直接通过？
2. 哪些需要你确认？
3. 哪些被直接拒绝？
4. 被拒绝后模型会不会调整策略？（比如先尝试 read_file 看看 `/etc/` 是不是真的存在）

**推荐 prompt**：

- `Create a file called test.txt in the current directory` —— 应该直接通过
- `Delete all temporary files in /tmp` —— bash + `rm` 会触发闸门 2
- `What files are in the current directory?` —— 只读操作，全部通过
- `Try to write a file to /etc/something` —— 写工作区外，触发闸门 2

## 5. 教学版与 CC 的关键差异

CC 的权限系统远比三道闸门复杂：

| 维度 | 教学版 (s03) | Claude Code |
|------|-------------|------------|
| 决策类型 | 3 种（deny/ask/allow） | 4 种（deny/ask/allow/passthrough） |
| 规则来源 | 1 个本地 DENY_LIST | 8 个来源（user/project/local settings、policy、CLI args、session 等） |
| 验证阶段 | 1 个 check_permission | 多阶段：Zod → validateInput → backfill → PreToolUse hook → hasPermissionsToUseToolInner |
| 自动审批 | 无 | `YoloClassifier` 用分类器 LLM 自动判断 |
| 权限冒泡 | 无 | 子 agent 权限弹窗冒泡到父终端 |
| isDestructive | 无（教学版没有 UI） | 存在但**纯 UI 展示用**，不参与权限决策 |

CC 的拒绝列表不是单一文件，而是从 8 个来源合并：`~/.claude/settings.json`、`.claude/settings.json`、`settings.local.json`、feature flags、企业 policy、CLI args、内联命令、会话内临时授权。每条规则格式为 `{ toolName, ruleBehavior, ruleContent }`，多个来源按优先级合并。

CC 还有 `bypassPermissions` 模式（自动模式），配合 `YoloClassifier`（`yoloClassifier.ts:1012`）减少弹窗。分类器把工具调用 + 对话上下文发给一个分类器 LLM 判断是否安全。先尝试 `acceptEdits` 模式模拟，如果允许就直接批准；再查安全工具白名单；最后才调分类器。连续拒绝太多次 → 回退人工审批。

**关键设计差异**：CC 的 `isDestructive`（`Tool.ts:405-406`）**纯粹是 UI 展示用的**——在工具列表里显示 `[destructive]` 标签。它**不参与权限决策**。默认所有工具返回 `false`。只有 ExitWorktree 和 MCP 工具覆写了它。

---

下一章：[s04 · Hooks](./04-s04-hooks.md)