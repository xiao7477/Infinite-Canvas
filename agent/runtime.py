"""Codex App Server runtime used by the smart-canvas Agent.

The runtime is deliberately independent from ``main.py``. Canvas-specific tool
registration and environment policy are injected by the integration layer so
upstream route code can remain in the application entrypoint.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException


@dataclass(frozen=True)
class CodexRuntimeDependencies:
    cli_executable: Callable[[], Optional[str]]
    app_server_env: Callable[[str], Dict[str, str]]
    dynamic_tool_specs: Callable[[], List[Dict[str, Any]]]
    is_dynamic_tools_registration_error: Callable[[Exception], bool]
    dynamic_tool_response: Callable[[Dict[str, Any], Optional[bool]], Dict[str, Any]]


class CodexAppServerSession:
    """A single Codex App Server subprocess speaking JSON-RPC over stdio."""

    def __init__(self, project_dir: str, dependencies: CodexRuntimeDependencies):
        self.project_dir = str(project_dir)
        self.dependencies = dependencies
        self.proc = None
        self.thread_id: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._server_requests: Dict[int, Dict[str, Any]] = {}
        self._event_queues: List[asyncio.Queue] = []
        self._stderr_tail: List[str] = []
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.native_tools_enabled = True

    async def start(self, thread_id: Optional[str] = None) -> str:
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                if thread_id and thread_id != (self.thread_id or ""):
                    await self.stop()
                else:
                    return self.thread_id or ""

            cli = self.dependencies.cli_executable()
            if not cli:
                raise HTTPException(
                    status_code=400,
                    detail="未找到 OpenAI Codex CLI。请先安装并登录 Codex 桌面版。",
                )
            env = self.dependencies.app_server_env(self.project_dir)
            self.proc = await asyncio.create_subprocess_exec(
                cli,
                "app-server",
                cwd=self.project_dir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_drain())

            await self._request(
                "initialize",
                {
                    "clientInfo": {"name": "Infinite-Canvas", "title": None, "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=15,
            )
            await self._notify("initialized", {})

            method = "thread/resume" if thread_id else "thread/start"
            thread_params: Dict[str, Any] = (
                {
                    "threadId": thread_id,
                    "model": None,
                    "dynamicTools": self.dependencies.dynamic_tool_specs(),
                }
                if thread_id
                else {
                    "cwd": self.project_dir,
                    "model": None,
                    "dynamicTools": self.dependencies.dynamic_tool_specs(),
                }
            )
            try:
                response = await self._request(method, thread_params, timeout=30)
                self.native_tools_enabled = True
            except HTTPException as exc:
                if not self.dependencies.is_dynamic_tools_registration_error(exc):
                    raise
                self.native_tools_enabled = False
                fallback_params = dict(thread_params)
                fallback_params.pop("dynamicTools", None)
                response = await self._request(method, fallback_params, timeout=30)

            self.thread_id = (
                response.get("thread", {}).get("id")
                or response.get("id")
                or thread_id
                or ""
            )
            return self.thread_id

    async def stop(self) -> None:
        await self.fail_pending_dynamic_tools("会话已停止，工具调用未执行")
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def respond_dynamic_tool_call(self, request_id: Any, result: Dict[str, Any]) -> bool:
        try:
            request_number = int(request_id)
        except (TypeError, ValueError):
            return False
        request = self._server_requests.get(request_number)
        if not request or str(request.get("method") or "") != "item/tool/call":
            return False
        safe_result = {
            "success": bool(result.get("success")),
            "contentItems": result.get("contentItems") if isinstance(result.get("contentItems"), list) else [],
        }
        try:
            await self._respond(request_number, safe_result)
        except Exception:
            return False
        self._server_requests.pop(request_number, None)
        return True

    async def fail_pending_dynamic_tools(self, message: str) -> None:
        pending = [
            request_id
            for request_id, request in self._server_requests.items()
            if str(request.get("method") or "") == "item/tool/call"
        ]
        for request_id in pending:
            response = self.dependencies.dynamic_tool_response(
                {"ok": False, "message": message},
                False,
            )
            await self.respond_dynamic_tool_call(request_id, response)

    async def send_user_message(self, text: str, image_paths=None, timeout: int = 900):
        user_content = [{"type": "text", "text": text, "text_elements": []}]
        for path in image_paths or []:
            if isinstance(path, dict):
                path = path.get("local_path") or path.get("path") or ""
            if path:
                user_content.append({"type": "localImage", "path": path})

        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._event_queues.append(queue)
        try:
            await self._request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": user_content,
                    "cwd": self.project_dir,
                    "summary": "auto",
                    "personality": "friendly",
                    "model": None,
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "workspaceWrite"},
                },
                timeout=180,
            )
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    yield {"method": "turn/timeout", "params": {}}
                    break
                method = message.get("method", "")
                yield message
                if method in ("turn/completed", "fatal", "error"):
                    break
        finally:
            try:
                self._event_queues.remove(queue)
            except ValueError:
                pass

    async def _request(self, method: str, params: dict, timeout: int = 30) -> dict:
        async with self._write_lock:
            message_id = self._next_id
            self._next_id += 1
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[message_id] = future
            message = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(message_id, None)
            detail = f"Codex request timeout: {method}"
            if self._stderr_tail:
                detail += " | stderr: " + " ".join(self._stderr_tail[-3:])[:500]
            raise HTTPException(status_code=504, detail=detail)
        if "error" in response:
            error = response["error"]
            message_text = error.get("message") or json.dumps(error, ensure_ascii=False) if isinstance(error, dict) else str(error)
            raise HTTPException(status_code=502, detail=f"Codex error [{method}]: {message_text}")
        return response.get("result", {})

    async def _notify(self, method: str, params: dict) -> None:
        async with self._write_lock:
            message = {"jsonrpc": "2.0", "method": method, "params": params}
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def _respond(self, request_id: int, result: dict) -> None:
        async with self._write_lock:
            if not self.proc or not self.proc.stdin:
                raise RuntimeError("Codex App Server is not running")
            message = {"jsonrpc": "2.0", "id": request_id, "result": result}
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def _reader_loop(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if "id" in message and "method" in message:
                    try:
                        request_id = int(message["id"])
                        self._server_requests[request_id] = message
                    except (TypeError, ValueError):
                        pass
                    self._broadcast(message)
                elif "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future and not future.done():
                        future.set_result(message)
                else:
                    self._broadcast(message)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._broadcast({"method": "fatal", "params": {"error": str(exc)}})

    def _broadcast(self, message: Dict[str, Any]) -> None:
        for queue in self._event_queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def _stderr_drain(self) -> None:
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr_tail.append(text)
                    self._stderr_tail = self._stderr_tail[-20:]
        except (asyncio.CancelledError, Exception):
            pass


class CodexAppServerRuntime:
    """Owns the App Server session for one canvas/conversation runtime key."""

    def __init__(self, runtime_key: str, project_dir: str, dependencies: CodexRuntimeDependencies):
        self.runtime_key = runtime_key
        self.project_dir = project_dir
        self.dependencies = dependencies
        self.session = CodexAppServerSession(project_dir, dependencies)
        self.thread_id = ""
        self.resume_warning = ""

    async def start(self, thread_id: str = "") -> Dict[str, Any]:
        self.resume_warning = ""
        try:
            self.thread_id = await self.session.start(thread_id=thread_id or None)
        except Exception as exc:
            if not thread_id:
                raise
            message = str(exc)
            try:
                await self.session.stop()
            except Exception:
                pass
            self.session = CodexAppServerSession(self.project_dir, self.dependencies)
            self.thread_id = await self.session.start(thread_id=None)
            if "no rollout found" not in message.lower():
                self.resume_warning = "底层执行线程不可恢复，已新建线程继续。"
        return {"thread_id": self.thread_id, "resume_warning": self.resume_warning}

    async def stop(self) -> None:
        await self.session.stop()

    async def send_user_message(self, text: str, image_paths=None):
        async for event in self.session.send_user_message(text, image_paths):
            yield event

    @property
    def native_tools_enabled(self) -> bool:
        return bool(self.session.native_tools_enabled)

    async def respond_dynamic_tool_call(self, request_id: Any, result: Dict[str, Any]) -> bool:
        return await self.session.respond_dynamic_tool_call(request_id, result)

    async def fail_pending_dynamic_tools(self, message: str) -> None:
        await self.session.fail_pending_dynamic_tools(message)
