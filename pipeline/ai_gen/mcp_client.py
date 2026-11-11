"""Thin async wrapper over the official `mcp` Python SDK (v2.x) client.

Transports: streamable HTTP (default, Artlist's hosted server) and stdio (a local
bridge or the test fake server). For tests, an in-process `MCPServer` can be passed
as `target`, which exercises the same `mcp.Client` code path without a subprocess.

The auth token comes from the env var named in config (default ARTLIST_MCP_TOKEN).
It is only placed in the HTTP header (or the stdio child's env when configured), and
it is scrubbed from any exception text this module raises.
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client


class MCPCallError(RuntimeError):
    """A tool call failed (transport error or the server returned isError)."""


class ToolBackend(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...


def _redact(text: str, secret: str | None) -> str:
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text


def result_to_dict(result: Any) -> dict[str, Any]:
    """Turn a CallToolResult into a plain dict.

    Prefers structured content; else parses JSON from text blocks; else {"text": ...}.
    """
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if set(structured) == {"result"}:
            inner = structured["result"]
            if isinstance(inner, dict):
                return inner
            if isinstance(inner, str):  # servers that return a JSON string from a `-> str` tool
                try:
                    parsed = json.loads(inner)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    return parsed
                return {"text": inner}
        return structured
    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None:
            # Embedded resource / resource link: surface its URI as a url.
            res = getattr(block, "resource", None)
            uri = getattr(res, "uri", None) or getattr(block, "uri", None)
            if uri:
                return {"url": str(uri)}
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            texts.append(text)
            continue
        if isinstance(parsed, dict):
            return parsed
        texts.append(text)
    return {"text": "\n".join(texts)}


class ArtlistMCP:
    """`async with ArtlistMCP(cfg) as mcp: await mcp.call_tool(...)`."""

    def __init__(self, cfg: dict, *, target: Any = None):
        self.cfg = cfg
        self.server_cfg = cfg.get("server", {})
        self._target = target
        self._client: Client | None = None
        self._http: httpx2.AsyncClient | None = None
        self._token = os.environ.get(self.server_cfg.get("token_env", "ARTLIST_MCP_TOKEN")) or None

    def describe(self) -> str:
        """Safe, token-free description of the connection target."""
        if self._target is not None:
            return f"in-process server {type(self._target).__name__}"
        s = self.server_cfg
        if s.get("transport") == "stdio":
            return f"stdio: {s.get('command')} {' '.join(map(str, s.get('args', [])))}"
        return f"streamable HTTP: {s.get('url')} (token: {'set' if self._token else 'NOT set'})"

    def _build_target(self) -> Any:
        if self._target is not None:
            return self._target
        s = self.server_cfg
        transport = s.get("transport", "streamable_http")
        if transport == "stdio":
            if not s.get("command"):
                raise MCPCallError("stdio transport needs [server].command")
            env = {k: str(v) for k, v in (s.get("env") or {}).items()}
            for name in s.get("env_passthrough") or []:
                if name in os.environ:
                    env[name] = os.environ[name]
            if s.get("pass_token_env") and self._token:
                env[s.get("token_env", "ARTLIST_MCP_TOKEN")] = self._token
            return StdioServerParameters(command=s["command"], args=[str(a) for a in s.get("args", [])],
                                         env=env or None)
        if transport in ("streamable_http", "http"):
            headers = {}
            if self._token:
                scheme = s.get("auth_scheme", "Bearer")
                headers[s.get("auth_header", "Authorization")] = f"{scheme} {self._token}".strip()
            timeout = float(s.get("timeout_s", 120))
            self._http = httpx2.AsyncClient(headers=headers,
                                            timeout=httpx2.Timeout(30.0, read=max(timeout, 30.0)))
            return streamable_http_client(s["url"], http_client=self._http)
        raise MCPCallError(f"unknown transport {transport!r}")

    async def __aenter__(self) -> "ArtlistMCP":
        try:
            client = Client(self._build_target(), read_timeout_seconds=float(self.server_cfg.get("timeout_s", 120)))
            await client.__aenter__()
        except BaseException as e:  # noqa: BLE001 - re-raise with the token scrubbed
            if self._http is not None:
                await self._http.aclose()
            if isinstance(e, MCPCallError):
                raise
            raise MCPCallError(_redact(f"could not connect ({self.describe()}): {e!r}", self._token)) from None
        self._client = client
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self._client is not None:
                await self._client.__aexit__(*exc)
        finally:
            if self._http is not None:
                await self._http.aclose()

    async def list_tools(self) -> list[dict[str, Any]]:
        assert self._client is not None, "use `async with`"
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            res = await self._client.list_tools(cursor=cursor)
            for t in res.tools:
                tools.append({
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": getattr(t, "input_schema", None),
                })
            cursor = getattr(res, "next_cursor", None)
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None, "use `async with`"
        try:
            result = await self._client.call_tool(name, arguments)
        except Exception as e:  # noqa: BLE001
            raise MCPCallError(_redact(f"tool {name!r} failed: {e!r}", self._token)) from None
        data = result_to_dict(result)
        if getattr(result, "is_error", False):
            raise MCPCallError(_redact(f"tool {name!r} returned an error: {json.dumps(data)[:500]}", self._token))
        return data
