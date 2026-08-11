import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ADAPTER_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(ADAPTER_DIR, "..", "..");

function required(value, name) {
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function parseObject(text, label) {
  const value = text.trim();
  if (!value) throw new Error(`${label} returned no JSON`);
  try {
    return JSON.parse(value);
  } catch (error) {
    throw new Error(`${label} returned invalid JSON: ${error.message}`);
  }
}

function defaultPython(environment = process.env) {
  if (environment.AGENT_CHAT_PYTHON) return environment.AGENT_CHAT_PYTHON;
  if (process.platform !== "win32") return "python3";
  for (const directory of (environment.PATH || "").split(path.delimiter)) {
    if (!directory) continue;
    const candidate = path.join(directory, "python.exe");
    if (existsSync(candidate)) return candidate;
  }
  const pyenvRoot = environment.PYENV_ROOT || environment.PYENV;
  if (pyenvRoot) {
    const versionFile = path.join(pyenvRoot, "version");
    if (existsSync(versionFile)) {
      const version = readFileSync(versionFile, "utf8").trim().split(/\s+/)[0];
      const candidate = path.join(pyenvRoot, "versions", version, "python.exe");
      if (existsSync(candidate)) return candidate;
    }
  }
  return "python.exe";
}

export class BridgeClient {
  constructor(options = {}) {
    this.database = required(options.database, "database");
    this.bridgeId = required(options.bridgeId, "bridgeId");
    this.room = required(options.room, "room");
    this.endpoint = required(options.endpoint, "endpoint");
    this.system = options.system || "claude";
    this.python = options.python || defaultPython();
    this.repositoryRoot = options.repositoryRoot || REPOSITORY_ROOT;
  }

  async run(
    commandArgs,
    { input, acceptedExitCodes = [0], pinBridge = true, signal } = {},
  ) {
    const args = [
      "-m",
      "agent_chat",
      "--db",
      this.database,
    ];
    if (pinBridge) args.push("--expect-bridge", required(this.bridgeId, "bridgeId"));
    args.push(...commandArgs);
    const existingPythonPath = process.env.PYTHONPATH;
    const pythonPath = path.join(this.repositoryRoot, "src") + (
      existingPythonPath ? path.delimiter + existingPythonPath : ""
    );
    const child = spawn(this.python, args, {
      cwd: this.repositoryRoot,
      env: { ...process.env, PYTHONPATH: pythonPath },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
      signal,
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", chunk => stdout.push(chunk));
    child.stderr.on("data", chunk => stderr.push(chunk));
    if (input !== undefined) child.stdin.end(input, "utf8");
    else child.stdin.end();
    const exitCode = await new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", resolve);
    });
    const out = Buffer.concat(stdout).toString("utf8");
    const err = Buffer.concat(stderr).toString("utf8");
    if (!acceptedExitCodes.includes(exitCode)) {
      let detail = err.trim() || out.trim() || `exit ${exitCode}`;
      try {
        detail = parseObject(detail, "agent-chat").error || detail;
      } catch {
        // Preserve the original diagnostic when it is not JSON.
      }
      throw new Error(`agent-chat failed: ${detail}`);
    }
    return parseObject(out || err, "agent-chat");
  }

  join() {
    return this.run([
      "join", "--room", this.room, "--endpoint", this.endpoint,
      "--system", this.system, "--label", "Claude Code Channel",
    ]);
  }

  receive({ wait = 30, visibility = 120, signal } = {}) {
    return this.run([
      "receive", "--room", this.room, "--as", this.endpoint,
      "--wait", String(wait), "--visibility", String(visibility),
    ], { acceptedExitCodes: [0, 3], signal });
  }

  mark(messageId, receipt, state, { adapter, hostRef } = {}) {
    const args = [
      "mark", "--room", this.room, "--as", this.endpoint,
      "--id", String(messageId), "--receipt", receipt, "--state", state,
    ];
    if (adapter) args.push("--adapter", adapter);
    if (hostRef) args.push("--host-ref", hostRef);
    return this.run(args);
  }

  acknowledge(messageId, receipt) {
    return this.run([
      "ack", "--room", this.room, "--as", this.endpoint,
      "--id", String(messageId), "--receipt", receipt,
    ]);
  }

  renew(messageId, receipt, visibility = 120) {
    return this.run([
      "renew", "--room", this.room, "--as", this.endpoint,
      "--id", String(messageId), "--receipt", receipt,
      "--visibility", String(visibility),
    ]);
  }

  release(messageId, receipt) {
    return this.run([
      "release", "--room", this.room, "--as", this.endpoint,
      "--id", String(messageId), "--receipt", receipt,
    ]);
  }

  acquireAdapter(owner, adapter, hostRef, ttl = 30) {
    const args = [
      "adapter-acquire", "--room", this.room, "--as", this.endpoint,
      "--owner", owner, "--adapter", adapter, "--ttl", String(ttl),
    ];
    if (hostRef) args.push("--host-ref", hostRef);
    return this.run(args);
  }

  renewAdapter(owner, ttl = 30) {
    return this.run([
      "adapter-renew", "--room", this.room, "--as", this.endpoint,
      "--owner", owner, "--ttl", String(ttl),
    ]);
  }

  releaseAdapter(owner) {
    return this.run([
      "adapter-release", "--room", this.room, "--as", this.endpoint,
      "--owner", owner,
    ]);
  }

  send({ recipients, text, replyTo, key, kind }) {
    const args = ["send", "--room", this.room, "--from", this.endpoint];
    for (const recipient of recipients) args.push("--to", recipient);
    if (replyTo !== undefined && replyTo !== null) {
      args.push("--reply-to", String(replyTo));
    }
    if (key) args.push("--key", key);
    if (kind) args.push("--kind", kind);
    args.push("--stdin");
    return this.run(args, { input: text });
  }
}

export function configFromEnvironment(environment = process.env) {
  return {
    database: required(environment.AGENT_CHAT_DB, "AGENT_CHAT_DB"),
    bridgeId: required(environment.AGENT_CHAT_BRIDGE, "AGENT_CHAT_BRIDGE"),
    room: required(environment.AGENT_CHAT_ROOM, "AGENT_CHAT_ROOM"),
    endpoint: required(environment.AGENT_CHAT_ENDPOINT, "AGENT_CHAT_ENDPOINT"),
    system: environment.AGENT_CHAT_SYSTEM || "claude",
    python: environment.AGENT_CHAT_PYTHON,
  };
}
