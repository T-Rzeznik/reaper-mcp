"""Synchronous TCP client for the in-Reaper bridge."""

from __future__ import annotations

import json
import os
import socket
import uuid
from typing import Any


class BridgeError(RuntimeError):
    """Raised when the bridge returns ok=false or the call fails."""


class BridgeClient:
    def __init__(self, host: str | None = None, port: int | None = None, timeout: float = 30.0):
        self.host = host or os.environ.get("REAPER_MCP_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("REAPER_MCP_PORT", "8765"))
        self.timeout = timeout

    def call(self, method: str, **params: Any) -> Any:
        req = {"id": uuid.uuid4().hex, "method": method, "params": params}
        payload = (json.dumps(req) + "\n").encode("utf-8")

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(payload)
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise BridgeError("bridge closed connection before responding")
                    buf += chunk
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            raise BridgeError(
                f"could not reach Reaper bridge at {self.host}:{self.port} — "
                f"is the bridge script running inside Reaper? ({e})"
            ) from e

        line, _, _ = buf.partition(b"\n")
        resp = json.loads(line)
        if not resp.get("ok"):
            err = resp.get("error", "unknown error")
            raise BridgeError(err)
        return resp.get("result")
