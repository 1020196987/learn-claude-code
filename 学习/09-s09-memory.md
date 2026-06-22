# s09 · Memory

> 原章目录：`s09_memory/`
> 一句话：**记住该记的，忘掉该忘的** —— 三个子系统：筛选、提取、整理。跨压缩、跨会话。

## 1. 设计动机

s08 的 autoCompact 会把当前目标、剩余工作、用户约束写进摘要，但**细节会丢失**："用 tab 缩进不要用空格"可能被简化成"用户有代码风格偏好"。而且**新开一个会话，连摘要也没了**。

LLM 没有持久状态，所有信息都在上下文窗口里。上下文满了要压缩，压缩就有损。需要一层**不参与压缩、跨会话保留**的存储。

更准确地说：需要的不只是"存储"，而是三个子系统：

- **筛选**（加载）：哪些记忆和当前任务相关？
- **提取**（写入）：什么时候该把信息沉淀为记忆？
- **整理**（维护）：记忆文件多了怎么办？

## 2. 机制拆解

### 2.1 四类记忆

| 类型 | 回答什么 | 示例 |
|------|---------|------|
| `user` | 你是谁 | "用 tab 不用空格" |
| `feedback` | 怎么做事 | "别 mock 数据库" |
| `project` | 正在发生什么 | "auth 重写是合规驱动" |
| `reference` | 东西在哪找 | "pipeline bug 在 Linear INGEST" |

### 2.2 存储：Markdown 文件 + 索引

每个记忆是一个 `.md` 文件，YAML frontmatter 记录元数据：

```markdown
---
name: user-preference-tabs
description: User prefers tabs for indentation
type: user
---

User prefers using tabs, not spaces, for indentation.
**Why:** Consistency with existing codebase conventions.
**How to apply:** Always use tabs when writing or editing files.
```

`MEMORY.md` 是索引，一行一个链接：

```markdown
- [user-preference-tabs](user-preference-tabs.md) — User prefers tabs for indentation
```

写入新记忆时自动重建索引。索引常驻 SYSTEM prompt（可被 prompt cache 缓存），文件内容按需注入。

### 2.3 加载：两条路径

**路径一：索引常驻 SYSTEM**。`build_system()` 在每次用户请求开始时读取 `MEMORY.md`，把记忆清单注入。

**路径二：相关记忆按需注入**。每次用户请求开始时，`load_memories()` 把最近对话和记忆目录（name + description）一起发给 LLM 做一次轻量 side-query，选出相关的文件名，再读文件内容临时注入到当前 user turn。最多 5 条，控制开销。

```python
def select_relevant_memories(messages, max_items=5):
    files = list_memory_files()
    catalog = "\n".join(f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files))
    response = client.messages.create(model=MODEL,
        messages=[{"role": "user", "content":
            f"Select relevant memory indices. Return JSON array.\n\n"
            f"Recent conversation:\n{recent}\n\nMemory catalog:\n{catalog}"}],
        max_tokens=200)
    indices = json.loads(re.search(r'\[.*?\]', extract_text(response.content)).group())
    return [files[i]["filename"] for i in indices if 0 <= i < len(files)]
```

如果 side-query 失败（API 错误、JSON 解析失败），**降级到关键词匹配 name + description**。

### 2.4 写入：每轮结束后提取

用户不会每次都说"记住这个"。偏好通常散落在正常对话中："用 tab 比空格好"、"以后都用单引号"。

`extract_memories()` 在每轮结束时运行，**条件是模型停止且没有 tool_use**（说明对话告一段落）：

```python
if response.stop_reason != "tool_use":
    extract_memories(pre_compress)    # 从压缩前快照提取新记忆
    consolidate_memories()            # 检查是否需要整理
    return
```

提取前先检查已有记忆，避免重复。提取 prompt 要求 LLM 返回 `{name, type, description, body}` 的 JSON 数组，只有确实有新信息时才写文件。

### 2.5 整理：低频合并去重

记忆文件会积累。`consolidate_memories()` 在文件数达到阈值（默认 10）时触发，让 LLM 去重、合并矛盾、淘汰过时记忆。

CC 把这个过程叫 **Dream**，实际有**四层门控**：

1. **时间门控**：距上次合并 ≥ 24 小时
2. **扫描节流**：避免频繁扫描文件系统
3. **会话门控**：自上次合并以来修改了 ≥ 5 个会话 transcript
4. **锁门控**：没有其他进程正在合并（`.consolidate-lock` 文件）

教学版简化为文件数阈值。

### 2.6 关键设计：索引 vs 内容 的分离

```
SYSTEM prompt   →  MEMORY.md（常驻，cache 友好）
       ↓
当前 user turn  →  选中的记忆文件内容（按需注入，不破坏 cache）
       ↓
messages[] 末尾 →  assistant 看到完整内容
```

为什么这样分离？

- **SYSTEM prompt 频繁出现**，每次调用都带。索引小（~1 行 / 记忆），可以全部放进去让模型看到。
- **记忆内容只在相关时需要**，全塞会撑爆 SYSTEM。side-query 选完再注入到 user turn，避免占位。
- **不破坏 prompt cache**：索引是 SYSTEM 的一部分，cache 命中；记忆内容只影响 user turn 头部，不影响 cache 命中。

## 3. 关键改动点（对照 `s09_memory/code.py`）

| 组件 | s08 | s09 |
|------|-----|-----|
| 记忆能力 | 无（压缩后偏好随摘要退化） | 存储 + 加载 + 提取 + 整理 |
| 新函数 | — | `write_memory_file`, `select_relevant_memories`, `load_memories`, `extract_memories`, `consolidate_memories` |
| 存储 | — | `.memory/MEMORY.md` 索引 + `.memory/*.md` 文件 |
| 工具 | 9 (s08) | 教学版精简到 6 个基础工具，专注于记忆机制 |
| 循环 | 每轮只做压缩 | 每轮注入记忆 + 压缩 + 每轮结束后提取 + 定期整理 |

## 4. User Memory vs Session Memory

| | User Memory | Session Memory |
|---|---|---|
| 持久性 | 跨会话 | 单会话 |
| 存储 | `memory/` 下多个 .md 文件 | `session-memory/<id>/memory.md` |
| 加载到 | system prompt | compact 摘要 |
| 用途 | 跨会话的知识积累 | 跨 compact 的上下文连续性 |

**sessionMemoryCompact**（s08 中提到的机制）正是使用了 Session Memory：autoCompact 前先读 session memory 文件，如果内容足够（≥ 10K token、≥ 5 条文本消息、≤ 40K token），就用它做摘要，不调 LLM。

## 5. 试一下

```sh
cd learn-claude-code
python s09_memory/code.py
```

**观察重点**：

1. 每轮结束后是否出现 `[Memory: extracted N new memories]`？
2. `.memory/` 目录下是否生成了 `.md` 文件？
3. `MEMORY.md` 索引是否更新？
4. 新一轮对话时 Agent 是否自动加载了之前的记忆？

**推荐 prompt**（分多轮输入，观察记忆的累积和加载）：

- `I prefer using tabs for indentation, not spaces. Remember that.`
- `Create a Python file called test.py` —— 观察 Agent 是否用了 tab
- `What did I tell you about my preferences?` —— 观察 Agent 是否记得
- `I also prefer single quotes over double quotes for strings.` —— 累积多条偏好

## 6. 教学版与 CC 的关键差异

### 6.1 记忆选择：LLM 选，不是 embedding

CC 用 **Sonnet 本身来选**（`findRelevantMemories.ts`），不是 embedding 向量相似度：

1. `memoryScan.ts` 扫描 `.memory/` 下所有 `.md` 文件（排除 MEMORY.md），最多 200 个，按 mtime 降序
2. 把 `name` + `description` 列成清单
3. 发给 Sonnet side-query："根据名称和描述选出真正有用的记忆（最多 5 个）。不确定就不要选。"
4. Sonnet 返回 `{ selected_memories: ["file1.md", ...] }`
5. 选中文件读取完整内容（每文件 ≤ 200 行 / 4096 字节），注入上下文。单 session 总预算 60KB

每轮用户 turn 开始时，`query.ts:301-304` 启动 memory prefetch（**异步**）；工具执行后 `1592-1614` 非阻塞收集结果，**不卡主流程**。

### 6.2 提取时机：stop hook，不是 autoCompact 后

触发位置（`stopHooks.ts:141-155`）：在 `handleStopHooks()` 中，**fire-and-forget** 触发提取和 Dream。教学版把提取放在 `stop_reason != "tool_use"` 分支里，方向一致。

CC 的提取通过 **forked agent 执行**（`extractMemories.ts:371-427`）：受限权限、`skipTranscript: true`、`maxTurns: 5`。还有重叠保护：如果主 Agent 已经写入了记忆文件，跳过提取。

### 6.3 Dream：四层门控

不是"空闲时触发"或"数量够了就合并"，而是四层门控（`autoDream.ts`，默认值 `63-66`，门控逻辑 `130-190`）：

1. **时间门控**：距上次合并 ≥ 24 小时
2. **扫描节流**：避免频繁扫描文件系统
3. **会话门控**：自上次合并以来修改了 ≥ 5 个会话 transcript
4. **锁门控**：没有其他进程正在合并（`.consolidate-lock` 文件）

合并本身通过 forked agent 执行（`224-233`）：定位 → 收集近期信号 → 合并写文件 → 剪枝更新索引。**锁文件 mtime 就是 lastConsolidatedAt**。崩溃恢复：1 小时后锁自动过期。

### 6.4 真实实现比教学版复杂的地方

- **Feature flags**：记忆相关功能有多层 feature gate 控制
- **Team memory**：团队共享记忆，`loadMemoryPrompt()` 有专门路径（教学版未涉及）
- **KAIROS**：时机感知的记忆提取策略，`loadMemoryPrompt()` 中 daily-log 模式
- **Prompt cache**：记忆注入需要考虑 prompt cache 的 TTL，避免每次都重写 system prompt 的大段内容
- **文件锁**：多进程并发时的锁机制
- **Memory prefetch**：异步预取，不阻塞主流程

### 6.5 教学版的简化是刻意的

- LLM side-query → LLM side-query + 关键词降级：教学版保留了 LLM 选择，加了降级路径
- 记忆 JSON → Markdown + frontmatter：教学版与 CC 一致
- stop hook 触发 → `stop_reason != "tool_use"` 分支：方向一致
- 四层门控 → 文件数阈值：教学版没有 transcript 系统和多会话概念
- forked agent + 受限权限 → 直接调用：教学版没有子进程隔离

---

下一章：[s10 · System Prompt](./10-s10-system-prompt.md)