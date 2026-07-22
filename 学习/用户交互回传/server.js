const express = require("express");
const cors = require("cors");
const { randomUUID } = require("crypto");

const app = express();

app.use(cors());
app.use(express.json());

/**
 * 保存 SSE 连接。
 * demo 简化：只支持一个全局连接。
 */
let sseResponse = null;

/**
 * 保存等待用户确认的请求。
 * requestId -> resolve
 */
const pendingRequests = new Map();

/**
 * SSE 事件订阅接口。
 */
app.get("/events", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream; charset=utf-8");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");

  sseResponse = res;

  sendEvent({
    type: "connected",
    message: "SSE connected",
  });

  req.on("close", () => {
    sseResponse = null;
  });
});

/**
 * 启动一个模拟 Agent 任务。
 */
app.post("/start", async (req, res) => {
  res.json({ ok: true });

  runAgentDemo();
});

/**
 * 用户回传权限确认结果。
 */
app.post("/respond", (req, res) => {
  const { requestId, decision } = req.body;

  const resolve = pendingRequests.get(requestId);

  if (!resolve) {
    return res.status(404).json({
      error: "request not found",
    });
  }

  pendingRequests.delete(requestId);

  resolve(decision);

  res.json({ ok: true });
});

/**
 * 推送 SSE 事件给前端。
 */
function sendEvent(event) {
  if (!sseResponse) {
    console.log("No SSE client:", event);
    return;
  }

  sseResponse.write(`data: ${JSON.stringify(event)}\n\n`);
}

/**
 * 请求用户确认。
 */
function askPermission({ toolName, args, reason }) {
  const requestId = randomUUID();

  sendEvent({
    type: "permission_request",
    requestId,
    toolName,
    args,
    reason,
  });

  return new Promise((resolve) => {
    pendingRequests.set(requestId, resolve);
  });
}

/**
 * 模拟 Agent Runtime。
 */
async function runAgentDemo() {
  sendEvent({
    type: "message",
    role: "assistant",
    content: "我准备执行一个工具调用。",
  });

  const toolCall = {
    toolName: "bash",
    args: {
      command: "rm test.txt",
    },
  };

  const decision = await askPermission({
    toolName: toolCall.toolName,
    args: toolCall.args,
    reason: "检测到删除文件命令，需要用户确认。",
  });

  if (decision === "allow") {
    sendEvent({
      type: "message",
      role: "tool",
      content: `已模拟执行：${toolCall.args.command}`,
    });

    sendEvent({
      type: "message",
      role: "assistant",
      content: "工具执行完成。",
    });
  } else {
    sendEvent({
      type: "message",
      role: "tool",
      content: "用户拒绝执行工具。",
    });

    sendEvent({
      type: "message",
      role: "assistant",
      content: "我不会继续执行删除操作。",
    });
  }
}

app.listen(3000, () => {
  console.log("Server running at http://localhost:3000");
});
