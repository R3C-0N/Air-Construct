"""The app side of Claude control: a local TCP server that runs bridge commands.

Owned by: workstream "assistant". See :mod:`airconstruct.assistant.protocol` for the
wire format and :mod:`.commands` for what each command does.

The server is a ``QTcpServer`` listening on 127.0.0.1 only, so requests arrive in the
GUI thread like clicks do: no locking, and the window repaints between commands.
Every request carries the token of the bridge file; a wrong token closes the
connection.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from ...assistant import protocol
from .commands import CommandError, Commands

MAX_LINE = 8 << 20  # bytes; a request is a few hundred bytes, case data a few kB
PAUSED = ("the user paused Claude in Air-Construct (pill “Claude” in the top bar): "
          "wait for them to resume, or ask them")


@dataclass
class Activity:
    when: float  # time.time()
    text: str  # French, shown to the user
    ok: bool = True
    error: str = ""


class AssistantBridge(QObject):
    """Owns the server, the bridge file and the pause switch."""

    activity = Signal(object)  # Activity, once per acting command (after it ran)
    clients_changed = Signal(int)
    paused_changed = Signal(bool)
    enabled_changed = Signal(bool)

    def __init__(self, win, bridge_path: Path | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent or win)
        self.win = win
        self.commands = Commands(win)
        self.bridge_path = bridge_path
        self.token = ""
        self.server: QTcpServer | None = None
        self._clients: dict[QTcpSocket, bytearray] = {}
        self._paused = False
        self.history: list[Activity] = []

    # ------------------------------------------------------------------ on / off
    @property
    def enabled(self) -> bool:
        return self.server is not None

    @property
    def port(self) -> int:
        return int(self.server.serverPort()) if self.server is not None else 0

    def start(self) -> bool:
        if self.server is not None:
            return True
        server = QTcpServer(self)
        if not server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0):
            self.win.show_message("Pilotage par Claude indisponible : "
                                  + server.errorString())
            return False
        server.newConnection.connect(self._on_new_connection)
        self.server = server
        self.token = secrets.token_urlsafe(24)
        try:
            protocol.write_bridge_file(self.port, self.token, self.bridge_path)
        except OSError as exc:
            self.win.show_message(f"Pilotage par Claude : fichier de liaison impossible ({exc})")
        self.enabled_changed.emit(True)
        return True

    def stop(self) -> None:
        if self.server is None:
            return
        for sock in list(self._clients):
            sock.abort()
        self._clients.clear()
        self.server.close()
        self.server.deleteLater()
        self.server = None
        protocol.remove_bridge_file(self.token, self.bridge_path)
        self.token = ""
        self.clients_changed.emit(0)
        self.enabled_changed.emit(False)

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        if bool(paused) != self._paused:
            self._paused = bool(paused)
            self.paused_changed.emit(self._paused)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------------ sockets
    def _on_new_connection(self) -> None:
        assert self.server is not None
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._clients[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))
        self.clients_changed.emit(len(self._clients))

    def _on_disconnected(self, sock: QTcpSocket) -> None:
        if self._clients.pop(sock, None) is not None:
            sock.deleteLater()
            self.clients_changed.emit(len(self._clients))

    def _on_ready_read(self, sock: QTcpSocket) -> None:
        buf = self._clients.get(sock)
        if buf is None:
            return
        buf += bytes(sock.readAll().data())
        while b"\n" in buf:
            i = buf.index(b"\n")
            line = bytes(buf[:i])
            del buf[:i + 1]
            reply = self.handle_line(line)
            if reply is None:  # bad token: drop the connection
                sock.abort()
                self._on_disconnected(sock)
                return
            if sock.state() == QTcpSocket.SocketState.ConnectedState:
                sock.write(reply)
                sock.flush()
        if len(buf) > MAX_LINE:
            sock.abort()
            self._on_disconnected(sock)

    # ------------------------------------------------------------------ dispatch
    def handle_line(self, line: bytes) -> bytes | None:
        """One request line -> one reply line (None: refuse the connection)."""
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(msg, dict) or not hmac.compare_digest(
                str(msg.get("token", "")), self.token or "\0"):
            return None
        rid = msg.get("id")
        params = msg.get("params") or {}
        try:
            result = self.dispatch(str(msg.get("method", "")), params)
            reply = {"id": rid, "result": result}
        except CommandError as exc:
            reply = {"id": rid, "error": str(exc)}
        except TypeError as exc:  # wrong or missing parameters
            reply = {"id": rid, "error": f"bad parameters: {exc}"}
        except Exception as exc:  # noqa: BLE001 - report, never crash the window
            traceback.print_exc()
            reply = {"id": rid, "error": f"internal error: {exc!r}"}
        return json.dumps(reply, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"

    def dispatch(self, method: str, params: dict):
        cmd = self.commands.table.get(method)
        if cmd is None:
            raise CommandError(f"unknown command {method!r}")
        if not isinstance(params, dict):
            raise CommandError("params must be an object")
        if cmd.acts and self._paused:
            raise CommandError(PAUSED)
        text = cmd.describe(params)
        if text and cmd.acts:
            self.win.show_message(f"Claude · {text}…", 0)
        try:
            result = cmd.fn(**params)
        except Exception as exc:
            if text:
                self._record(Activity(time.time(), text, ok=False, error=str(exc)))
            raise
        if text:
            self._record(Activity(time.time(), text))
        return result

    def _record(self, act: Activity) -> None:
        self.history.append(act)
        del self.history[:-100]
        if act.ok:
            self.win.show_message(f"Claude · {act.text}")
        else:
            self.win.show_message(f"Claude · {act.text} : refusé ({act.error})")
        self.activity.emit(act)
