import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { BridgeClient } from "../bridge-client.mjs";

test("bridge client preserves the full adapter delivery lifecycle", async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-chat-channel-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const database = path.join(directory, "chat.sqlite3");
  const bootstrap = new BridgeClient({
    database,
    bridgeId: "bootstrap",
    room: "test",
    endpoint: "claude.test",
  });
  const initialized = await bootstrap.run(
    ["init", "--room", "test"],
    { pinBridge: false },
  );
  const bridgeId = initialized.bridge.id;
  const claude = new BridgeClient({
    database,
    bridgeId,
    room: "test",
    endpoint: "claude.test",
  });
  const codex = new BridgeClient({
    database,
    bridgeId,
    room: "test",
    endpoint: "codex.test",
    system: "codex",
  });
  await claude.join();
  await codex.join();
  await claude.acquireAdapter("owner-test", "claude.channel", "test-session", 30);
  const unicodeText = "long task — café हिंदी 😀";
  const sent = await codex.send({
    recipients: ["claude.test"],
    text: unicodeText,
  });
  const received = await claude.receive({ wait: 0, visibility: 30 });
  assert.equal(received.message.text, unicodeText);
  const id = sent.message.id;
  const receipt = received.delivery.receipt;
  await claude.mark(id, receipt, "injected", {
    adapter: "claude.channel",
    hostRef: "test-session",
  });
  await claude.renew(id, receipt, 60);
  await claude.acknowledge(id, receipt);
  await claude.mark(id, receipt, "acted");
  await claude.send({
    recipients: ["codex.test"],
    text: "still working",
    replyTo: id,
    key: `test-progress-${id}`,
    kind: "progress",
  });
  const inProgress = await claude.run([
    "delivery", "--room", "test", "--as", "claude.test", "--id", String(id),
  ]);
  assert.equal(inProgress.delivery.state, "acted");
  assert.equal(inProgress.delivery.replied_at, null);
  await claude.send({
    recipients: ["codex.test"],
    text: "clarification answered",
    replyTo: id,
    key: `test-reply-${id}`,
    kind: "reply",
  });
  const detail = await claude.run([
    "delivery", "--room", "test", "--as", "claude.test", "--id", String(id),
  ]);
  assert.equal(detail.delivery.state, "replied");
  assert.deepEqual(
    detail.delivery.events.map(event => event.state),
    ["queued", "claimed", "injected", "observed", "acted", "replied"],
  );
  const released = await claude.releaseAdapter("owner-test");
  assert.equal(released.requeued, 0);
});

test("a blocked receive can be aborted without waiting for its poll timeout", async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-chat-abort-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const database = path.join(directory, "chat.sqlite3");
  const bootstrap = new BridgeClient({
    database,
    bridgeId: "bootstrap",
    room: "test",
    endpoint: "claude.abort",
  });
  const initialized = await bootstrap.run(
    ["init", "--room", "test"],
    { pinBridge: false },
  );
  const client = new BridgeClient({
    database,
    bridgeId: initialized.bridge.id,
    room: "test",
    endpoint: "claude.abort",
  });
  await client.join();

  const controller = new AbortController();
  const started = Date.now();
  const pending = client.receive({
    wait: 30,
    visibility: 30,
    signal: controller.signal,
  });
  setTimeout(() => controller.abort(), 150);
  await assert.rejects(pending, error => error?.name === "AbortError");
  assert.ok(Date.now() - started < 5_000);
});
