"""Wire format between the MCP server and the app's bridge, and the client side.

Owned by: workstream "assistant". No Qt here: the MCP server imports it alone.

One JSON object per line, UTF-8, over a TCP connection to 127.0.0.1:

    request  {"id": 1, "token": "...", "method": "get_state", "params": {}}
    reply    {"id": 1, "result": {...}}   or   {"id": 1, "error": "why"}

The app writes :func:`bridge_file` (``{"port", "token", "pid"}``) when its bridge
listens and removes it when the window closes. The token keeps other local users
and web pages (which cannot read the file) from driving the app.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

ENV_FILE = "AIRCONSTRUCT_BRIDGE_FILE"


def bridge_file() -> Path:
    """Where the running app says how to reach it (overridable for tests)."""
    env = os.environ.get(ENV_FILE)
    if env:
        return Path(env)
    return Path.home() / ".airconstruct" / "bridge.json"


def write_bridge_file(port: int, token: str, path: Path | None = None) -> Path:
    path = path or bridge_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}),
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # Windows: the profile folder is already private
        pass
    os.replace(tmp, path)
    return path


def read_bridge_file(path: Path | None = None) -> dict | None:
    path = path or bridge_file()
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or "port" not in info or "token" not in info:
        return None
    return info


def remove_bridge_file(token: str, path: Path | None = None) -> None:
    """Remove the file if it is still ours (another window may have replaced it)."""
    path = path or bridge_file()
    info = read_bridge_file(path)
    if info is not None and info.get("token") == token:
        try:
            path.unlink()
        except OSError:
            pass


class BridgeError(RuntimeError):
    """The app answered with an error (refused or failed command)."""


class NotRunning(ConnectionError):
    """No Air-Construct window with the bridge on could be reached."""


class BridgeClient:
    """Blocking client: one connection, reconnects on the next call after a failure."""

    def __init__(self, path: Path | None = None, timeout: float = 120.0) -> None:
        self.path = path
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._token = ""
        self._next_id = 0

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    def _connect(self) -> None:
        info = read_bridge_file(self.path)
        if info is None:
            raise NotRunning("Air-Construct is not running (or Claude control is off)")
        try:
            sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=5.0)
        except OSError as exc:
            raise NotRunning(f"Air-Construct does not answer: {exc}") from exc
        sock.settimeout(self.timeout)
        self._sock, self._buf, self._token = sock, b"", str(info["token"])

    def connected(self) -> bool:
        try:
            self.call("ping")
        except (NotRunning, BridgeError, OSError):
            return False
        return True

    def call(self, method: str, **params):
        """Send one command and wait for its reply. Raises NotRunning, BridgeError."""
        for attempt in (0, 1):
            if self._sock is None:
                self._connect()
            self._next_id += 1
            msg = {"id": self._next_id, "token": self._token, "method": method,
                   "params": params}
            try:
                assert self._sock is not None
                self._sock.sendall(json.dumps(msg).encode("utf-8") + b"\n")
                reply = self._read_line()
            except OSError as exc:
                self.close()
                if attempt == 0:  # the app was restarted: new port and token
                    continue
                raise NotRunning(f"lost the connection to Air-Construct: {exc}") from exc
            if "error" in reply:
                raise BridgeError(str(reply["error"]))
            return reply.get("result")
        raise NotRunning("Air-Construct does not answer")  # pragma: no cover

    def _read_line(self) -> dict:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(1 << 20)
            if not chunk:
                raise ConnectionResetError("closed by the app")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))
