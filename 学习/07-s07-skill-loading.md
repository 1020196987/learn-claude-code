# s07 · Skill Loading

> 原章目录：`s07_skill_loading/`
> 一句话：**用到时再加载，别全塞 prompt 里** —— 通过 tool_result 注入，不塞 system prompt。

## 1. 设计动机

你的项目有一套 React 组件规范、一份 SQL 风格指南、一份 API 设计文档。你希望 Agent 自动遵守这些规范。最直接的想法：全塞进 system prompt。

```python
SYSTEM = (
    f"You are a coding agent. "
    + open("docs/react-style.md").read()       # 2000 行
    + open("docs/sql-style.md").read()         # 1500 行
    + open("docs/api-design.md").read()        # 3000 行
)
```

6500 行 system prompt。Agent 每次调用 LLM 都带着这些文档——不管是在改 CSS 颜色还是修 SQL 查询。**99% 的内容和当前任务无关，白白消耗 token**。

更深的问题：system prompt 是"每次必带"，模型对大段 system prompt 的注意力会被稀释——重要指令和参考资料混在一起。

## 2. 机制拆解

### 2.1 两级加载：目录常驻 + 内容按需

| 层 | 位置 | 时机 | 代价 |
|---|------|------|------|
| 1. 目录 | system prompt | 启动时注入（harness 扫描 `skills/`） | ~100 tokens/skill，每轮都带 |
| 2. 内容 | tool_result | Agent 调用 `load_skill` 时；SKILL.md 可指引后续的 read_file/bash 调用，用于按需访问额外资源 | ~2000 tokens/skill，按需 |

第一级是"告诉模型有什么可用"，第二级是"模型决定要哪个再加载"。**目录在 prompt 里，内容不在**。

### 2.2 skills/ 目录结构

```
skills/
  agent-builder/SKILL.md
  code-review/SKILL.md
  mcp-builder/SKILL.md
  pdf/SKILL.md
```

每个技能一个子目录，包含 `SKILL.md` 文件，YAML frontmatter 记录元数据：

```markdown
---
name: code-review
description: Conduct thorough code reviews following project standards.
---

# Code Review Skill

When conducting a code review:
1. Check naming conventions (snake_case for functions)
2. Verify test coverage
3. Look for security issues
4. ...
```

### 2.3 第一级：启动时注入目录

harness 启动时调用 `_scan_skills()` 扫描 `skills/` 目录，解析每个 `SKILL.md` 的 YAML frontmatter（`name`、`description`），存入 `SKILL_REGISTRY` 字典。`list_skills()` 从注册表生成目录，注入 SYSTEM prompt。

```python
SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    for d in sorted(SKILLS_DIR.iterdir()):
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            SKILL_REGISTRY[meta["name"]] = {"name": meta["name"], "description": meta["description"], "content": raw}

def build_system() -> str:
    catalog = list_skills()  # "- **code-review**: ..."
    return f"You are a coding agent at {WORKDIR}. Skills available:\n{catalog}\nUse load_skill to get full details when needed."
```

Agent 每轮都能看到"我有哪些技能可用"，不花额外 API 调用。

### 2.4 第二级：load_skill 工具按需加载

```python
def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]   # ← 通过 tool_result 注入
```

关键区别：**技能内容不是 system prompt 的一部分，它作为一次工具结果进入当前 messages**。后续调用会随历史一起携带，直到上下文压缩、截断或会话结束。

### 2.5 防路径遍历：通过注册表查找而非文件路径

load_skill 通过 name 查找注册表，**不走文件路径**。这样：

- 不暴露 skills/ 的物理结构
- 防止 `../` 等路径遍历攻击
- 名字规范由 YAML frontmatter 控制，攻击者无法伪造任意文件

## 3. 关键改动点（对照 `s07_skill_loading/code.py`）

| 组件 | s06 | s07 |
|------|-----|-----|
| 工具数量 | 7 | 8 (+load_skill) |
| 知识加载 | 无 | 两级：启动时目录注入 SYSTEM + 运行时 load_skill；SKILL.md 可指引后续资源访问 |
| SYSTEM 提示 | 静态字符串 | 启动时扫描 `skills/` 注入目录 |
| 技能注册表 | 无 | `SKILL_REGISTRY`（启动时填充，防路径遍历） |
| 循环 | 不变 | 不变（skill 工具自动分发） |

## 4. 与 s08 的衔接

按需加载解决了"不该提前带的不要带"。但另一个问题来了：Agent 连续工作 30 分钟后，`messages` 列表塞满了中间过程。旧的 tool_result、过时的文件内容、加载过的技能内容，占着上下文但不产生价值。

**s07 解决"该带的别漏"，s08 解决"该丢的别留"**。两者配合使用：按需加载 → 留下有价值的 → 压缩丢掉无价值的。

## 5. 试一下

```sh
cd learn-claude-code
python s07_skill_loading/code.py
```

**观察重点**：

1. Agent 是否直接从 SYSTEM 里的目录知道有哪些技能？
2. 需要完整规范时是否出现 `[HOOK] load_skill`？
3. 加载后回答是否使用了对应 skill 的说明？
4. 没加载的技能内容是否进入了上下文？

**推荐 prompt**：

- `What skills are available?` —— 验证第一级目录注入生效
- `Load the code-review skill and follow its instructions` —— 第二级加载触发
- `I need to do a code review -- load the relevant skill first` —— 模型主动判断何时加载

## 6. 教学版与 CC 的关键差异

| 维度 | 教学版 (s07) | Claude Code |
|------|-------------|------------|
| 技能来源 | 1 个 `skills/` 目录 | 多来源：user/project/`--add-dir` 目录、legacy commands、bundled skills、plugin skills、MCP skills |
| SKILL.md 字段 | name / description | 9 个字段（when_to_use、allowed-tools、context、model、hooks、paths、user-invocable 等） |
| `context: 'fork'` 技能 | 无 | 支持 fork 模式作为子 Agent 运行 |
| 工具输入 | `name` | `skill` + 可选 `args` |
| 预算控制 | 无 | catalog 预算为上下文窗口的 ~1%（上限 8000 字符） |

### 6.1 技能来源：不是只有一个目录

CC 实际从多个来源加载：

| 来源类型 | 路径 |
|---------|------|
| User skills | `~/.claude/skills/` |
| Project skills | `.claude/skills/` |
| `--add-dir` skills | 命令行额外目录 |
| Legacy commands | `.claude/commands/` |
| Bundled skills | 内置 |
| Plugin skills | 插件系统 |
| MCP skills | 远程 MCP 服务提供 |
| Conditional skills | 带 `paths` frontmatter，按文件路径激活 |

每个来源的技能按规则合并，组成 Agent 可用的完整技能集。

### 6.2 SKILL.md Frontmatter 常见字段

| 字段 | 用途 |
|------|------|
| `name` / `description` | 显示名称和描述 |
| `when_to_use` | 指导模型何时调用 |
| `allowed-tools` | 技能可用工具的自动允许列表 |
| `context` | `inline`（默认）或 `fork`（作为子 Agent 运行） |
| `model` | 模型覆盖（haiku/sonnet/opus/inherit） |
| `hooks` | 技能级别的 hook 配置 |
| `paths` | 条件激活的 glob 模式 |
| `user-invocable` | 用户可以通过 `/name` 调用 |

完整字段列表随版本迭代会变化。

### 6.3 两级加载的精确实现

CC 的 `SkillTool` 返回的 tool_result **展示文本只是 `"Launching skill: {name}"`**，真正的技能内容通过 `newMessages` 注入对话。教学版把两者合并为"通过 tool_result 注入"是一种简化。加载后的 SKILL.md 仍可作为指引，帮助模型后续通过现有 file/bash 工具访问相关资源。

CC 的 `getSkillListingAttachments()` 把技能列表格式化为附件，预算为上下文窗口的 ~1%（上限 8000 字符）。这个上限确保即使技能目录很大也不会撑爆 system prompt。

---

下一章：[s08 · Context Compact](./08-s08-context-compact.md)