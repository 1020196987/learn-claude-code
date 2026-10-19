# Opik 学习笔记

## 1. Opik 是什么

Opik 是一个面向 LLM 应用的可观测性与评测平台，可用于记录、调试和分析：

- 模型输入与输出
- Agent 多步骤执行过程
- 工具调用
- Token 用量、耗时和成本
- 错误信息
- Prompt 和模型版本
- 离线、在线评测结果

它可以使用 Opik Cloud，也可以自行部署。本项目使用 Opik Cloud，并通过 Python SDK 接入。

## 2. 当前示例的接入方式

示例文件：`s01_agent_loop/code.py`。

### 2.1 安装与配置

```bash
source .venv/bin/activate
uv pip install -r requirements.txt
opik configure
```

选择 Opik Cloud 后，需要配置 Opik API Key 和 Workspace。Opik 凭据用于上传 Trace，`ANTHROPIC_API_KEY` 则用于调用模型，两者用途不同。

### 2.2 包装 Anthropic 客户端

```python
from anthropic import Anthropic
from opik.integrations.anthropic import track_anthropic

client = track_anthropic(
    Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL")),
    project_name=OPIK_PROJECT_NAME,
)
```

`track_anthropic` 会自动记录（拦截）每次 `client.messages.create()` 模型调用，包括模型的输入、输出、Token 用量、耗时和成本，并将这次调用展示为当前 Trace 中的一个执行步骤。在 Opik 中，这种“模型调用步骤”称为 `llm` Span。

### 2.3 记录整个 Agent Loop

```python
@track(
    name="agent_loop",
    project_name=OPIK_PROJECT_NAME,
    capture_input=False,
    capture_output=False,
)
def agent_loop(messages: list):
    ...
```

`agent_loop()` 每执行一次，就会形成一条处理本轮问题的 Trace。

这里关闭自动输入、输出采集，原因是：

- `messages` 包含完整对话历史，自动采集会重复上传大量内容。
- 对话或工具输出可能包含不需要上传的信息。
- 当前 `agent_loop()` 没有返回最终答案，而是修改传入的 `messages` 列表，函数返回值为 `None`。

因此只手动记录本轮问题和最终答案：

```python
# 仅记录当前问题，避免每轮重复上传完整会话历史。
opik_context.update_current_trace(
    input={"query": latest_message},
    metadata={"model": MODEL},
    tags=["s01", "agent-loop"],
)

# 函数本身返回 None，因此手动把真正的回答写入 Trace。
opik_context.update_current_trace(
    output={"answer": final_text},
)
```

### 2.4 记录 Bash 工具调用

```python
@track(type="tool", project_name=OPIK_PROJECT_NAME)
def run_bash(command: str) -> str:
    ...
```

每次调用 `run_bash()`，Opik 都会创建一个 `tool` 类型的 Span，记录命令和执行结果。

### 2.5 退出前刷新数据

```python
# Opik 默认批量上传，进程退出前确保最后一批数据已经发送。
flush_tracker()
```

Opik SDK 默认采用批量上报。命令行脚本或短生命周期程序应在退出前调用 `flush_tracker()`，避免最后一批 Trace 尚未上传。

## 3. Trace、Span 和 Thread

### 3.1 Trace

Trace 表示处理一次请求的完整执行路径。在当前示例中，一次用户提问对应一次 `agent_loop()` 调用，也对应一条 Trace。

```text
用户问题 1 → Trace 1
用户问题 2 → Trace 2
用户问题 3 → Trace 3
```

### 3.2 Span

Span 是 Trace 中的一个具体执行步骤。Opik 支持以下 Span 类型：

| 类型 | 用途 |
| --- | --- |
| `general` | Agent 编排、普通函数或入口操作 |
| `llm` | 模型调用 |
| `tool` | 工具、检索、API 或数据库调用 |
| `guardrail` | 安全检查或结果验证 |

当前示例没有手动调用 `span()`，Span 由集成和装饰器自动创建：

```python
client.messages.create(...)  # track_anthropic 自动创建 LLM Span
run_bash(...)                # @track(type="tool") 自动创建 Tool Span
```

### 3.3 根 Span

根 Span 是一条 Trace 中没有父 Span 的最上层 Span。当前示例中，`@track(name="agent_loop")` 创建根 Span，内部的模型和 Bash 调用成为它的子 Span：

```text
Trace：agent_loop
└── 根 Span：agent_loop
    ├── LLM Span
    ├── Tool Span
    └── LLM Span
```

Trace 是整条调用链的容器，根 Span 是容器中最外层的实际操作，因此界面上会看到两个同名的 `agent_loop`。

一条 Trace 技术上可以有多个没有父级的顶层 Span，但对于一次完整请求，通常使用一个逻辑根 Span 更清晰。意外出现多个根 Span，可能意味着父子追踪上下文没有正确传播。

### 3.4 Thread

Thread 用于把同一次多轮对话产生的多条 Trace 关联起来：

```text
Thread：一次完整会话
├── Trace 1：第一轮问题
├── Trace 2：第二轮问题
└── Trace 3：第三轮问题
```

如果不同 Trace 使用相同的 `thread_id`，Opik 就会把它们归入同一个 Thread：

```python
opik_context.update_current_trace(
    thread_id=session_id,
)
```

`thread_id` 只负责 Opik 中的会话归类，不负责保存模型上下文。当前示例真正的对话记忆仍由 `history` 列表维护。

## 4. 当前 Agent Loop 的执行结构

一次需要执行三个 Bash 命令的问题，可能产生以下调用树：

```text
agent_loop Trace
└── agent_loop 根 Span
    ├── anthropic_messages_create（第一次模型调用）
    ├── run_bash（工具调用 1）
    ├── run_bash（工具调用 2）
    ├── run_bash（工具调用 3）
    └── anthropic_messages_create（第二次模型调用）
```

对应的执行过程：

1. 第一次调用模型，模型根据问题决定是否调用工具。
2. 模型返回一个或多个 `tool_use` 内容块。
3. 程序逐个执行 `run_bash()`。
4. 程序将执行结果包装为 `tool_result` 并追加到 `messages`。
5. Agent Loop 再次调用模型。
6. 模型根据原问题和工具结果生成最终回答。

因此，一轮用户问题只产生一条 Trace，但其中可以有多个 LLM Span 和 Tool Span。

## 5. Opik 界面字段

### 5.1 `count 1`

列表表头中的：

```text
Input   count 1
Output  count 1
Errors  count 0
```

表示当前列表或筛选范围内：

- 1 条记录包含 Input。
- 1 条记录包含 Output。
- 0 条记录包含错误。

它不是对话编号、字符数或 Token 数。

### 5.2 耗时与 Token

Trace 上的耗时表示整条执行链路的持续时间。LLM Span 上的数字通常表示本次模型调用的 Token 用量。

例如：

```text
第一次模型调用：354 Token
第二次模型调用：4387 Token
Trace 总量：4741 Token
```

工具调用本身不消耗模型 Token，因此 `run_bash` 通常只有耗时，没有 Token 数。

### 5.3 `signature`

`signature` 属于 `thinking` 内容块：

```yaml
- type: thinking
  thinking: 模型的思考内容
  signature: adcaa9ab...
```

在 Anthropic 消息协议中，它是模型服务生成的校验信息，用于在 Thinking Block 被传回接口时验证其来源和完整性。

它的产生路径是：

```text
模型服务生成 thinking 和 signature
→ Anthropic SDK 接收
→ track_anthropic 记录
→ Opik 展示
```

它不是 Opik 生成的，也不应由业务代码解析或修改。当前使用 MiniMax 的 Anthropic 兼容接口，具体签名实现由该接口提供方负责。

### 5.4 工具调用 `id`

每个 `tool_use` 都有唯一 ID：

```yaml
- type: tool_use
  id: call_function_xxx_1
  name: bash
  input:
    command: find . -name "*.py"
```

工具执行完成后，程序通过相同 ID 返回结果：

```python
results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": output,
})
```

对应关系为：

```text
tool_use.id == tool_result.tool_use_id
```

模型借此判断每份工具结果对应哪一次工具请求。一次响应中存在多个工具调用时，每个调用都有不同 ID。

### 5.5 `caller`

`caller` 表示工具调用由谁发起。较新的 Anthropic 协议可以区分：

```yaml
# 模型直接调用工具。
caller:
  type: direct
```

以及：

```yaml
# Anthropic 服务端代码执行工具间接调用客户端工具。
caller:
  type: code_execution_20260120
  tool_id: srvtoolu_xxx
```

当前界面显示：

```yaml
caller: null
```

表示上游 Anthropic 兼容接口没有返回明确的 caller 元数据，或者 SDK 将缺省字段表示为 `None`，Opik 序列化后显示为 `null`。它不表示工具调用失败，也不表示 Trace 父子关系丢失。

当前代码没有使用服务端代码执行工具，因此从流程上看，这些 Bash 请求仍属于模型直接产生的工具调用。

## 6. Tags 和 Metadata

当前代码为 Trace 添加：

```python
tags=["s01", "agent-loop"]
metadata={"model": MODEL}
```

两者区别：

- `tags`：简单字符串标签，适合分类和快速筛选。
- `metadata`：键值结构，适合保存模型、版本、环境等详细信息。

标签不影响模型行为或程序执行，只用于 Opik 中的查询和组织。例如：

```text
tags contains "s01"
tags contains "agent-loop"
```

## 7. 追踪上下文与异步任务

Opik 依靠当前追踪上下文确定新 Span 的父级，核心信息可以理解为：

```text
trace_id
当前 span_id
```

同步调用时，上下文会沿调用栈自然传播：

```text
agent_loop
→ client.messages.create
→ run_bash
```

在线程、进程、任务队列或服务边界中，上下文可能无法自动传播，例如：

- `ThreadPoolExecutor` 或其他线程池
- 后台异步任务在父 Span 结束后才执行
- Celery、Kafka 等跨进程任务
- HTTP 调用到另一个微服务
- 第三方 SDK 在内部线程或独立回调中运行

上下文丢失后，原本的子 Span 可能变成另一个顶层 Span，甚至形成独立 Trace。跨服务场景需要显式传播 Trace 信息。

当前 `s01_agent_loop/code.py` 使用同步调用，模型调用和工具调用均位于同一调用栈中，一般不会遇到异步上下文丢失问题。

## 8. 数据安全注意事项

Opik 会记录模型输入、输出和工具结果，接入时应注意：

- 不要记录 API Key、密码或访问令牌。
- 避免上传包含个人信息或业务敏感数据的完整历史。
- 谨慎记录 Bash 输出，命令可能读取配置文件或环境变量。
- 对生产环境数据设置适当的采样、脱敏和保存策略。
- API Key 只放在本地配置或环境变量中，不提交到 Git。

## 9. 核心概念速查

| 概念 | 含义 | 当前示例 |
| --- | --- | --- |
| Project | Trace 的项目级分组 | `learn-claude-code-s01` |
| Thread | 一次多轮会话 | 当前尚未设置 `thread_id` |
| Trace | 处理一轮用户问题的完整链路 | 一次 `agent_loop()` |
| 根 Span | Trace 中最外层操作 | `agent_loop` |
| LLM Span | 一次模型调用 | `anthropic_messages_create` |
| Tool Span | 一次工具调用 | `run_bash` |
| Tag | 用于分类和筛选的字符串 | `s01`、`agent-loop` |
| Metadata | 结构化附加信息 | `model` |
| `tool_use.id` | 工具请求唯一标识 | `call_function_...` |
| `tool_result.tool_use_id` | 与工具请求配对 | 等于对应 `tool_use.id` |
| `signature` | Thinking Block 的校验信息 | 上游模型服务生成 |
| `caller` | 工具调用来源 | 当前兼容接口返回 `null` |
