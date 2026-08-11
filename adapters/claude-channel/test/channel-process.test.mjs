import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { BridgeClient } from "../bridge-client.mjs";


const HERE = path.dirname(fileURLToPath(import.meta.url));
const ADAPTER = path.resolve(HERE, "..", "index.mjs");

async function waitFor(check, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("timed out waiting for channel process state");
}

test("channel EOF releases its lease and unfinished delivery", async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-chat-eof-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const database = path.join(directory, "chat.sqlite3");
  const bootstrap = new BridgeClient({
    database,
    bridgeId: "bootstrap",
    room: "test",
    endpoint: "source.test",
    system: "test",
  });
  const initialized = await bootstrap.run(
    ["init", "--room", "test"],
    { pinBridge: false },
  );
  const bridgeId = initialized.bridge.id;
  const source = new BridgeClient({
    database,
    bridgeId,
    room: "test",
    endpoint: "source.test",
    system: "test",
  });
  await source.join();

  const child = spawn(process.execPath, [ADAPTER], {
    env: {
      ...process.env,
      AGENT_CHAT_DB: database,
      AGENT_CHAT_BRIDGE: bridgeId,
      AGENT_CHAT_ROOM: "test",
      AGENT_CHAT_ENDPOINT: "claude.eof",
      AGENT_CHAT_SYSTEM: "claude",
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  const stderr = [];
  child.stderr.on("data", chunk => stderr.push(chunk));
  child.stdout.resume();
  t.after(() => {
    if (child.exitCode === null) child.kill();
  });

  await waitFor(async () => {
    if (child.exitCode !== null) {
      throw new Error(`channel exited early: ${Buffer.concat(stderr).toString("utf8")}`);
    }
    const members = await source.run(["members", "--room", "test"]);
    return members.members.find(
      member => member.endpoint === "claude.eof" && member.adapter_online,
    );
  });

  const sent = await source.send({
    recipients: ["claude.eof"],
    text: "unfinished before host EOF",
  });
  await waitFor(async () => {
    const detail = await source.run([
      "delivery", "--room", "test", "--as", "claude.eof",
      "--id", String(sent.message.id),
    ]);
    return ["claimed", "injected"].includes(detail.delivery.state) && detail;
  });

  child.stdin.end();
  const exitCode = await Promise.race([
    new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", resolve);
    }),
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error("channel did not exit after stdin EOF")), 10_000);
    }),
  ]);
  assert.equal(exitCode, 0, Buffer.concat(stderr).toString("utf8"));

  const recovered = await waitFor(async () => {
    const members = await source.run(["members", "--room", "test"]);
    const member = members.members.find(item => item.endpoint === "claude.eof");
    const detail = await source.run([
      "delivery", "--room", "test", "--as", "claude.eof",
      "--id", String(sent.message.id),
    ]);
    return !member.adapter_online && detail.delivery.state === "queued" && detail;
  });
  assert.equal(recovered.delivery.replied_at, null);
});
