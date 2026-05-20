"""MCP Client — dynamic tool discovery via Model Context Protocol.

The escape hatch: instead of writing integrations for git, github, databases, etc.,
connect to MCP servers and auto-import their tools.

Ref:
- opencode: mcp/index.ts — StdioClientTransport, tool conversion to AI SDK format
- kimi-cli: KimiToolset.load_mcp_tools() with background loading
- Design doc: connect_mcp_server() → auto-map to JSON Schema tools
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from opencollab.application.tool_runtime import ToolRuntime
from opencollab.adapters.tools.base import Tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistent MCP connection — handles lifecycle for all tools from one server
# ---------------------------------------------------------------------------

class MCPConnection:
    """Persistent connection to an MCP server via stdio transport.

    Manages the full lifecycle: start process → initialize handshake →
    tool discovery → tool execution → shutdown.

    All MCPTool instances from the same server share one MCPConnection.
    """

    def __init__(self, command: str, args: list[str]):
        self._command = command
        self._args = args
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._initialized = False
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        """Start process and perform MCP initialize handshake if needed."""
        async with self._lock:
            if self._initialized and self._process and self._process.returncode is None:
                return

            # (Re)start process
            if self._process and self._process.returncode is not None:
                self._initialized = False

            self._process = await asyncio.create_subprocess_exec(
                self._command, *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._request_id = 0

            # MCP initialize handshake
            init_resp = await self._jsonrpc_call_raw("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "opencollab", "version": "0.1.0"},
            })
            logger.debug(f"MCP init response: {init_resp}")

            # Send initialized notification (no response expected)
            await self._send_notification("notifications/initialized")

            self._initialized = True

    async def call(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and return the response."""
        await self.ensure_ready()
        return await self._jsonrpc_call_raw(method, params)

    async def list_tools(self) -> list[dict]:
        """Discover available tools from the server."""
        resp = await self.call("tools/list", {})
        if "result" in resp and "tools" in resp["result"]:
            return resp["result"]["tools"]
        return []

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Execute a tool on the server."""
        return await self.call("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        """Terminate the server process."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
        self._initialized = False

    # ---- Internal JSON-RPC transport ----

    async def _jsonrpc_call_raw(self, method: str, params: dict) -> dict:
        """Send JSON-RPC request and read response (Content-Length framed)."""
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        await self._send_message(request)
        return await self._read_message()

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response)."""
        notif: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            notif["params"] = params
        await self._send_message(notif)

    async def _send_message(self, message: dict) -> None:
        msg = json.dumps(message)
        frame = f"Content-Length: {len(msg)}\r\n\r\n{msg}"
        self._process.stdin.write(frame.encode())
        await self._process.stdin.drain()

    async def _read_message(self) -> dict:
        """Read a Content-Length framed JSON-RPC message.

        Ref: openclaw parseContentLengthHeader — validate before parse.
        """
        header = await asyncio.wait_for(self._process.stdout.readline(), timeout=30)
        if not header:
            raise ConnectionError("MCP server closed stdout")
        await self._process.stdout.readline()  # empty line separator

        # Validate Content-Length header (ref: openclaw — return error, not crash)
        header_str = header.decode().strip()
        if ":" not in header_str:
            raise ValueError(f"Malformed MCP header (no colon): {header_str!r}")
        try:
            length = int(header_str.split(":", 1)[1].strip())
        except ValueError:
            raise ValueError(f"Invalid Content-Length value: {header_str!r}")
        if length < 0:
            raise ValueError(f"Negative Content-Length: {length}")

        body = await asyncio.wait_for(self._process.stdout.readexactly(length), timeout=30)
        return json.loads(body.decode())


# ---------------------------------------------------------------------------
# MCPTool — a single tool backed by a shared MCPConnection
# ---------------------------------------------------------------------------

class MCPTool(Tool):
    """A tool backed by a remote MCP server.

    Multiple MCPTool instances from the same server share one MCPConnection,
    which maintains the process and MCP handshake state.
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        connection: MCPConnection,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._conn = connection

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        try:
            resp = await self._conn.call_tool(self.name, params)
            if "result" in resp:
                result = resp["result"]
                if isinstance(result, dict) and "content" in result:
                    parts = []
                    for c in result["content"]:
                        if c.get("type") == "text":
                            parts.append(c["text"])
                    return "\n".join(parts) if parts else json.dumps(result)
                return json.dumps(result)
            elif "error" in resp:
                return f"MCP error: {resp['error']}"
            return json.dumps(resp)
        except Exception as e:
            return f"MCP tool execution error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Public API — load tools from an MCP server
# ---------------------------------------------------------------------------

async def load_mcp_tools(command: str, args: list[str]) -> tuple[list[MCPTool], MCPConnection]:
    """Connect to an MCP server and discover its tools.

    Returns (tools, connection). The caller is responsible for calling
    connection.close() when done.

    Usage:
        tools, conn = await load_mcp_tools("npx", ["-y", "@modelcontextprotocol/server-github"])
        agent = Agent(..., tools=[*basic_tools, *tools])
        # ... use agent ...
        await conn.close()
    """
    conn = MCPConnection(command, args)

    try:
        await conn.ensure_ready()
        tool_defs = await conn.list_tools()

        tools: list[MCPTool] = []
        for td in tool_defs:
            tools.append(MCPTool(
                name=td["name"],
                description=td.get("description", ""),
                parameters=td.get("inputSchema", {"type": "object", "properties": {}}),
                connection=conn,
            ))
            logger.info(f"Loaded MCP tool: {td['name']}")

        return tools, conn

    except asyncio.TimeoutError:
        logger.error(f"MCP server {command} timed out during initialization")
        await conn.close()
        return [], conn
    except Exception as e:
        logger.error(f"Failed to load MCP tools from {command}: {e}")
        await conn.close()
        return [], conn
