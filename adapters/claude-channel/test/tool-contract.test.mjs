import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import {
  getDefaultEnvironment,
  StdioClientTransport,
} from "@modelcontextprotocol/sdk/client/stdio.js";

import { BridgeClient } from "../bridge-client.mjs";


const HERE = path.dirname(fileURLToPath(import.meta.url));
const ADAPTER = path.resolve(HERE, "..", "index.mjs");

function resultPayload(result) {
  const item = result.content?.find(value => value.type === "text");
  assert.ok(item?.text, "tool result did not contain JSON text");
  return JSON.parse(item.text);
}

test("Claude MCP send uses explicit retry keys without collapsing intentional sends", async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-chat-tools-"));
  let clientClosed = false;
  const database = path.join(directory, "chat.sqlite3");
  const bootstrap = new BridgeClient({
    database,
    bridgeId: "bootstrap",
    room: "test",
    endpoint: "claude.tools",
  });
  const initialized = await bootstrap.run(
    ["init", "--room", "test"],
    { pinBridge: false },
  );
  const target = new BridgeClient({
    database,
    bridgeId: initialized.bridge.id,
    room: "test",
    endpoint: "target.tools",
    system: "test",
  });
  await target.join();

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [ADAPTER],
    cwd: path.resolve(HERE, "..", "..", ".."),
    env: {
      ...getDefaultEnvironment(),
      AGENT_CHAT_DB: database,
      AGENT_CHAT_BRIDGE: initialized.bridge.id,
      AGENT_CHAT_ROOM: "test",
      AGENT_CHAT_ENDPOINT: "claude.tools",
      AGENT_CHAT_SYSTEM: "claude",
      AGENT_CHAT_PYTHON: bootstrap.python,
    },
    stderr: "pipe",
  });
  const client = new Client(
    { name: "agent-chat-contract-test", version: "1.0.0" },
    { capabilities: {} },
  );
  t.after(async () => {
    if (!clientClosed) await client.close();
    await rm(directory, { recursive: true, force: true });
  });
  await client.connect(transport);

  const tools = await client.listTools();
  const sendTool = tools.tools.find(item => item.name === "agent_chat_send");
  assert.deepEqual(sendTool.inputSchema.required, ["to", "text", "request_key"]);

  const argumentsValue = {
    to: "target.tools",
    text: "same words may be one retry or two intended messages",
    request_key: "conversation-one",
  };
  const first = resultPayload(await client.callTool({
    name: "agent_chat_send",
    arguments: argumentsValue,
  }));
  const retry = resultPayload(await client.callTool({
    name: "agent_chat_send",
    arguments: argumentsValue,
  }));
  assert.equal(first.duplicate, false);
  assert.equal(retry.duplicate, true);
  assert.equal(retry.message.id, first.message.id);

  await assert.rejects(
    client.callTool({
      name: "agent_chat_send",
      arguments: { ...argumentsValue, text: "conflicting retry" },
    }),
    /dedupe key was already used/,
  );

  const intentional = resultPayload(await client.callTool({
    name: "agent_chat_send",
    arguments: { ...argumentsValue, request_key: "conversation-two" },
  }));
  assert.equal(intentional.duplicate, false);
  assert.notEqual(intentional.message.id, first.message.id);

  const received = [
    await target.receive({ wait: 0, visibility: 30 }),
    await target.receive({ wait: 0, visibility: 30 }),
  ];
  assert.deepEqual(
    received.map(item => item.message.id),
    [first.message.id, intentional.message.id],
  );
  for (const item of received) {
    await target.acknowledge(item.message.id, item.delivery.receipt);
  }
  assert.equal((await target.receive({ wait: 0, visibility: 30 })).status, "empty");
  await client.close();
  clientClosed = true;
});
