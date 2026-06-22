# s02 · Tool Use

> 原章目录：`s02_tool_use/`
> 一句话：**加一个工具，只加一个 handler** —— 循环不用动，新工具注册进 dispatch map 就行。

## 1. 设计动机

s01 的 Agent 只有一个 bash 工具。读文件要 `cat path/to/file`、写文件要 `echo "..." > file.py`、改文件要 `sed -i`。**模型想的是"读这个文件"，却要拼出 shell 命令**，多了一层翻译，浪费 token，还容易拼错。

为什么不直接给模型一个 `read_file` 工具？

- 模型可以直接说"读 README.md"，不再拼路径
- 工具可以自带安全校验（限制在工作区内）
- dispatch 表里加一行就扩展一个能力，循环不用动

## 2. 机制拆解

### 2.1 TOOL_HANDLERS 字典：循环里只改一行

s01 循环里"执行工具"那一步原本是：

```python
output = run_bash(block.input["command"])
```

s02 改成查表分发：

```python
handler = TOOL_HANDLERS[block.name]   # 查表
output = handler(**block.input)        # 调用
```

**循环本身没有任何其他改动**。这就是"加工具 = 加一行映射"的设计——dispatch 机制让循环与具体工具解耦。

### 2.2 工具的"双胞胎"：定义 + 实现

每个工具都由两部分组成：

| 部分 | 位置 | 作用 |
|------|------|------|
| **定义**（schema） | `TOOLS` 数组 | 告诉 LLM "我能做什么"，包含名称、描述、输入参数 schema |
| **实现**（handler） | `TOOL_HANDLERS` 字典 | 当 LLM 调工具时，实际执行的函数 |

加一个工具 = 在 `TOOLS` 数组加一条 + 在 `TOOL_HANDLERS` 字典加一行映射。两条必须配套。

### 2.3 s02 的 5 个工具

| 工具 | 输入 | 实现要点 |
|------|------|---------|
| `bash` | `command: string` | 同步执行 shell 命令，捕获 stdout/stderr |
| `read_file` | `path: string, limit?: number` | 读文件内容，可选限制行数 |
| `write_file` | `path: string, content: string` | 写入文件，覆盖 |
| `edit_file` | `path: string, old_text: string, new_text: string` | 单次替换；找不到旧文本返回错误 |
| `glob` | `pattern: string` | 用 `glob.glob` 找文件 |

注意：file 类工具都经过 `safe_path()` 校验，限制在工作区内。bash 还没限制——这是 s03 的内容。

### 2.4 多工具调用：模型一次返回多个 tool_use

模型经常一次返回多个 tool_use："读一下 a.py 和 b.py，然后列出所有 .py 文件"。**教学版按 `response.content` 原始顺序逐个执行**——不并发、不分批。

生产版 CC 用 `partitionToolCalls()` 把工具调用按连续块分批，batch 内并发安全的工具并行执行，batch 间严格顺序。"并发安全"不是简单的"只读 vs 写"，而是按具体输入判断：

| 工具 | isReadOnly | isConcurrencySafe |
|------|-----------|-------------------|
| FileRead | true | true |
| Glob | true | true |
| Bash `ls` | true | **true** ← 关键差异 |
| Bash `rm` | false | false |
| TaskCreate | false | **true** ← 改状态但可并发（每次写不同文件） |

CC 的 Bash 工具的 `isConcurrencySafe` 等于 `isReadOnly`——只读命令可并发，写命令不可。教学版按顺序执行，跳过并发优化。

### 2.5 验证管线

教学版用 JSON Schema 校验参数（`TOOLS` 数组里的 `input_schema`），不调用工具自身的 `validateInput()`。CC 多了 5 步验证：

1. Zod schema 验证（参数类型/结构）
2. 工具级 `validateInput()`（参数值验证，如路径是否在工作区）
3. PreToolUse hooks（可改输入/阻止执行——s04 详细讲）
4. 权限检查（s03 的核心）
5. 执行 `tool.call()`

教学版把 1-3 步都简化掉，只保留了基本的 schema 校验。

## 3. 关键改动点（对照 `s02_tool_use/code.py`）

| 组件 | s01 | s02 |
|------|-----|-----|
| 工具数量 | 1 (bash) | 5 (+read, write, edit, glob) |
| 工具执行 | 硬编码 `run_bash()` | `TOOL_HANDLERS[block.name]` 查表 |
| 路径安全 | 无 | `safe_path()` 校验（仅 file 类） |
| 循环 | `while True` + `stop_reason` | **与 s01 完全一致** |

循环零改动。**这就是 dispatch 模式的威力**。

## 4. 试一下

```sh
cd learn-claude-code
python s02_tool_use/code.py
```

**观察重点**：

1. 模型什么时候只调一个工具，什么时候一次调多个？
2. 多个工具调用的顺序是否和模型描述的顺序一致？
3. 文件路径在工作区外时会发生什么？（注意：s02 只在 file 类工具有保护，bash 仍然畅通无阻）

**推荐 prompt**：

- `Read the file README.md and tell me what this project is about` —— 单工具调用
- `Create a file called test.py that prints "hello", then read it back` —— 多工具、跨工具类型
- `Find all Python files in this directory` —— 触发 `glob`
- `Read both README.md and requirements.txt, then create a summary file` —— 三个工具链式调用

## 5. 教学版与 CC 的关键差异

| 维度 | 教学版 (s02) | Claude Code |
|------|-------------|------------|
| 工具定义 | `TOOLS` 数组 + `TOOL_HANDLERS` 字典（分离） | 每个工具是 `buildTool()` 创建的对象，包含 schema/验证/权限/执行 |
| 工具并发 | 按原始顺序串行 | `partitionToolCalls()` 按连续块分批，并发安全工具并行 |
| 验证 | JSON Schema | Zod schema + 工具 `validateInput()` + PreToolUse hook |
| 流式执行 | 无 | `StreamingToolExecutor` 工具可在模型还在生成时启动 |
| 结果持久化 | 全部塞 messages | `maxResultSizeChars` 阈值，超大结果落盘 |

教学版的分离方式（定义数组 + handler 字典）对教学更清晰——一眼看出"加一个工具 = 两条定义"。CC 把验证、权限、执行都封装在工具对象里，运行时通过 `getAllBaseTools()` 汇总。

---

下一章：[s03 · Permission](./03-s03-permission.md)