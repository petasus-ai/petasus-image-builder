"""A client for the QEMU guest agent's socket."""
from __future__ import annotations

import json
import os
import socket
import time


class AgentError(Exception):
    pass


class Agent:
    def __init__(self, path: str):
        self.path = path

    def command(self, name: str, arguments: dict | None = None, timeout: float = 15.0):
        """One command on a fresh connection. A sync exchange first drops whatever an earlier client left."""
        deadline = time.monotonic() + timeout
        token = int.from_bytes(os.urandom(4), "big")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            try:
                sock.connect(self.path)
                sock.sendall(json.dumps({"execute": "guest-sync", "arguments": {"id": token}}).encode() + b"\n")
                self._read(sock, deadline, lambda reply: reply.get("return") == token)
                request = {"execute": name}
                if arguments:
                    request["arguments"] = arguments
                sock.sendall(json.dumps(request).encode() + b"\n")
                reply = self._read(sock, deadline, lambda reply: "return" in reply or "error" in reply)
            except OSError as err:
                raise AgentError(f"{name}: {err}") from None
        if "error" in reply:
            raise AgentError(f"{name}: {reply['error'].get('desc', reply['error'])}")
        return reply["return"]

    @staticmethod
    def _read(sock, deadline: float, accept):
        buffer = b""
        while time.monotonic() < deadline:
            chunk = sock.recv(65536)
            if not chunk:
                raise OSError("the agent closed the connection")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    reply = json.loads(line.strip(b"\xff \r"))
                except ValueError:
                    continue
                if isinstance(reply, dict) and accept(reply):
                    return reply
        raise OSError("no reply from the agent")

    def ping(self) -> bool:
        try:
            self.command("guest-ping", timeout=5)
            return True
        except AgentError:
            return False
