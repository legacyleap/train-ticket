"""Minimal MCP client (Streamable HTTP, JSON-RPC) for the LegacyLeap Meta-Cognitive Model server.

Stdlib only. The API key comes from ``LEGACYLEAP_MCP_KEY`` (or ``deploy/mcp.env``), never from code.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://dev-mcp-server.legacyleap.ai/mcp/"


def api_key() -> str:
    key = os.environ.get("LEGACYLEAP_MCP_KEY", "")
    if not key:
        env = Path(__file__).resolve().parents[1] / "deploy" / "mcp.env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("LEGACYLEAP_MCP_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"')
    if not key:
        msg = "LEGACYLEAP_MCP_KEY is not set (env var or deploy/mcp.env)"
        raise RuntimeError(msg)
    return key


class MCMClient:
    def __init__(self, url: str = DEFAULT_URL, key: str | None = None, timeout: int = 180) -> None:
        self.url = os.environ.get("LEGACYLEAP_MCP_URL", url)
        self.key = key or api_key()
        self.timeout = timeout
        self.session: str | None = None
        self.server: dict[str, Any] = {}
        self._id = 0

    # --- transport
    def _post(self, obj: dict[str, Any]) -> tuple[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "X-API-Key": self.key}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(obj).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            return r.headers.get("Content-Type", ""), r.read().decode()

    @staticmethod
    def _messages(ct: str, raw: str) -> list[dict[str, Any]]:
        if "event-stream" in ct:
            out = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        out.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
            return out
        return [json.loads(raw)] if raw.strip() else []

    def connect(self) -> dict[str, Any]:
        self._id += 1
        ct, raw = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "vouch", "version": "0.1"}}})
        for m in self._messages(ct, raw):
            if m.get("id") == self._id and "result" in m:
                self.server = m["result"].get("serverInfo", {})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except urllib.error.HTTPError:
            pass
        return self.server

    def call(self, tool: str, **args: Any) -> Any:
        """Call a tool; JSON text content is decoded, anything else returned as text."""
        if not self.session:
            self.connect()
        self._id += 1
        ct, raw = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                              "params": {"name": tool, "arguments": args}})
        for m in self._messages(ct, raw):
            if m.get("id") != self._id:
                continue
            if "error" in m:
                msg = f"{tool}: {m['error']}"
                raise RuntimeError(msg)
            res = m.get("result", {})
            texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
            txt = "\n".join(texts)
            if res.get("isError"):
                msg = f"{tool}: {txt[:300]}"
                raise RuntimeError(msg)
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return txt
        msg = f"{tool}: no response"
        raise RuntimeError(msg)

    def projects(self) -> list[dict[str, Any]]:
        if not self.session:
            self.connect()
        self._id += 1
        ct, raw = self._post({"jsonrpc": "2.0", "id": self._id, "method": "resources/read",
                              "params": {"uri": "mcm://projects"}})
        for m in self._messages(ct, raw):
            if m.get("id") == self._id and "result" in m:
                for c in m["result"].get("contents", []):
                    try:
                        return json.loads(c.get("text", "[]"))
                    except json.JSONDecodeError:
                        return []
        return []
