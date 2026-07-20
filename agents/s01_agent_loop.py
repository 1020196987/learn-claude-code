#!/usr/bin/env python3
# Harness: the loop -- the model's first connection to the real world.
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.
"""

import os
import subprocess
from pathlib import Path

try:
    import readline

    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# Load .env next to this script (not cwd-dependent) so the file is found
# regardless of where the user runs python from.
load_dotenv(Path(__file__).parent / ".env", override=True)

# if os.getenv("ANTHROPIC_BASE_URL"):
#     os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    auth_token=os.getenv("ANTHROPIC_AUTH_TOKEN"),
)
MODEL = os.environ["MODEL_ID"]

# 系统提示词
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# 工具声明列表，把“可用工具”描述给模型，这个结构不是随便自定义的，它是 Anthropic Claude Messages API 的 Tool Use / Function Calling 工具定义格式
# | 字段 | 作用 |
# |---|---|
# | `name` | 工具名，模型调用工具时会用这个名字 |
# | `description` | 工具说明，帮助模型判断什么时候用这个工具 |
# | `input_schema` | 工具入参格式，告诉模型调用工具时应该传什么参数 |

# Anthropic 官方文档也说明：如果要让 Claude 调用你自己定义的函数，需要传一个带 input_schema 的 tool；Claude 会返回 tool_use，然后由你的应用程序执行这个调用。可参考：https://platform.claude.com/docs/zh-CN/agents-and-tools/tool-use/overview
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",  # 表示工具输入必须是一个对象。
            "properties": {
                "command": {"type": "string"}
            },  # 表示对象里允许/定义了一个字段 command，类型是字符串。
            "required": ["command"],  # 表示 command 是必填参数。
        },
    }
]

# 所以模型调用上面这个工具时，应该生成类似：
# {
#   "type": "tool_use",
#   "name": "bash",
#   "input": {
#     "command": "pwd"
#   }
# }


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})
        # If the model didn't call a tool, we're done
        if response.stop_reason != "tool_use":
            return
        # 模型如果想执行命令，会生成类似这样的 tool call：
        # {
        #     "type": "tool_use",
        #     "name": "bash",
        #     "input": {
        #         "command": "ls -la"
        #     }
        # }
        # Execute each tool call, collect results
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m$ {block.input['command']}\033[0m")
                # 当前这里为了演示，写死一个调用工具，如果有多个工具，更通用的写法：用工具分发表
                output = run_bash(block.input["command"])
                print(output[:200])
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
