"""Synchronous facade over the MCP stdio client.

Bolt handlers are synchronous; the MCP Python SDK is async. A single background
thread owns an asyncio loop and keeps one stdio session open to the calendar
server for the app's lifetime. `call_tool` is safe to call from any handler
thread and fails loudly — a broken MCP server must be visible, not silently
worked around, because the MCP integration is the point of this app.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from typing import Any


class McpError(RuntimeError):
    pass


class McpCalendarClient:
    def __init__(self, server_script: str, connect_timeout: float = 20.0):
        self._server_script = server_script
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-client-loop", daemon=True
        )
        self._thread.start()
        self._session = None
        try:
            future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
            future.result(timeout=connect_timeout)
        except Exception as exc:  # noqa: BLE001 - surface every startup failure
            raise McpError(f"could not start MCP calendar server: {exc}") from exc

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[self._server_script],
            env=dict(os.environ),
        )
        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 5.0) -> Any:
        if self._session is None:
            raise McpError("MCP session not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        )
        try:
            result = future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise McpError(f"MCP tool '{name}' failed: {exc}") from exc
        if getattr(result, "isError", False):
            detail = _text_of(result) or "unknown tool error"
            raise McpError(f"MCP tool '{name}' returned an error: {detail}")
        return _payload_of(result)

    def list_tools(self, timeout: float = 5.0) -> list[str]:
        future = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
        return [t.name for t in future.result(timeout=timeout).tools]

    def close(self) -> None:
        async def _shutdown():
            try:
                await self._session_ctx.__aexit__(None, None, None)
            finally:
                await self._stdio_ctx.__aexit__(None, None, None)

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=5)
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def _text_of(result: Any) -> str:
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", "") == "text":
            return block.text
    return ""


def _payload_of(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured
    text = _text_of(result)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return None
