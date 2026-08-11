"""Small asynchronous JSONL client for the Codex app-server protocol."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Legacy Codex app threads can return their complete persisted history as one
# JSONL response during ``thread/resume`` even when an initial page is
# requested.  Keep a hard transport ceiling, but make it large enough to
# reattach real long-running interactive threads (the Windows app commonly
# accumulates more than 32 MiB before compaction).
MAX_JSONL_BYTES = 128 * 1024 * 1024
MESSAGE_QUEUE_SIZE = 256


class AppServerError(RuntimeError):
    """A transport or JSON-RPC error from Codex app-server."""


class AppServerClient:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        request_timeout: float = 60,
    ) -> None:
        if not argv:
            raise ValueError("app-server command cannot be empty")
        self.argv = list(argv)
        self.cwd = cwd
        self.request_timeout = request_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=MESSAGE_QUEUE_SIZE
        )
        self.server_requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=MESSAGE_QUEUE_SIZE
        )
        self.closed = asyncio.Event()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: deque[str] = deque(maxlen=40)

    async def start(self) -> None:
        if self.process is not None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=str(self.cwd) if self.cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_JSONL_BYTES + 1,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def initialize(self, *, experimental: bool = False) -> dict[str, Any]:
        capabilities: dict[str, Any] = {}
        if experimental:
            capabilities["experimentalApi"] = True
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "cross_agent_bridge",
                    "title": "Cross-Agent Bridge",
                    "version": "0.3.0-dev.0",
                },
                "capabilities": capabilities,
            },
        )
        await self.notify("initialized", {})
        return result

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process is None:
            raise AppServerError("app-server is not started")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        await self._write({"method": method, "id": request_id, "params": params})
        try:
            message = await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise AppServerError(f"app-server request timed out: {method}") from exc
        if "error" in message:
            error = message["error"]
            detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise AppServerError(f"{method} failed: {detail}")
        result = message.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def respond(
        self,
        request_id: str | int,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        await self._write(message)

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerError(self._closed_message())
        # Keep the JSONL transport ASCII-only. JSON escapes preserve every Unicode
        # code point and avoid a Windows app-server stdin boundary that otherwise
        # decodes raw UTF-8 bytes through the active ANSI code page.
        encoded = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
        async with self._write_lock:
            process.stdin.write((encoded + "\n").encode("utf-8"))
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AppServerError(self._closed_message()) from exc

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                try:
                    line = await self.process.stdout.readline()
                except ValueError as exc:
                    raise AppServerError(
                        f"app-server JSONL frame exceeds {MAX_JSONL_BYTES} bytes"
                    ) from exc
                if not line:
                    break
                if len(line) > MAX_JSONL_BYTES:
                    raise AppServerError(f"app-server JSONL frame exceeds {MAX_JSONL_BYTES} bytes")
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise AppServerError("app-server emitted invalid JSONL") from exc
                if not isinstance(message, dict):
                    raise AppServerError("app-server emitted a non-object JSON value")
                if "id" in message and ("result" in message or "error" in message):
                    response_id = message["id"]
                    if isinstance(response_id, bool) or not isinstance(response_id, int):
                        raise AppServerError("app-server response id must be an integer")
                    future = self._pending.pop(response_id, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif "id" in message and "method" in message:
                    request_id = message["id"]
                    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                        raise AppServerError("app-server request id must be a string or integer")
                    await self.server_requests.put(message)
                else:
                    await self.notifications.put(message)
        except Exception as exc:
            self._fail_pending(exc)
        finally:
            self._fail_pending(AppServerError(self._closed_message()))
            self.closed.set()

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _closed_message(self) -> str:
        detail = " | ".join(item for item in self._stderr if item)
        return "app-server closed" + (f": {detail}" if detail else "")

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self.process = None
