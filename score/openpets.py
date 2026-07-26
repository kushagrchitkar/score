"""Minimal client for OpenPets' local newline-delimited JSON protocol."""

import json
import os
import socket
from pathlib import Path
from typing import Optional


def resolve_openpets_socket(config_home: Optional[Path] = None, uid: Optional[int] = None) -> str:
    """Honor OpenPets' configured socket, falling back to its per-user default."""
    if config_home is None:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_path = Path(config_home) / "openpets" / "config.json"
    try:
        configured = json.loads(config_path.read_text()).get("socketPath")
        if isinstance(configured, str) and configured:
            return configured
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return f"/tmp/openpets-{os.getuid() if uid is None else uid}.sock"


class OpenPetsError(RuntimeError):
    """Raised when the local OpenPets host rejects or cannot receive a command."""


class OpenPetsClient:
    def __init__(self, socket_path: str, timeout: float = 5):
        self.socket_path = socket_path
        self.timeout = timeout

    def _send(self, command: dict) -> dict:
        payload = (json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            connection.sendall(payload)
            chunks = bytearray()
            while not chunks.endswith(b"\n"):
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > 65_536:
                    raise OpenPetsError("OpenPets response exceeded 64 KiB")
        if not chunks:
            raise OpenPetsError("OpenPets returned no response")
        def reject_constant(_value):
            raise ValueError("non-RFC JSON constant")

        try:
            response = json.loads(chunks, parse_constant=reject_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise OpenPetsError("OpenPets returned an invalid response") from exc
        if not isinstance(response, dict):
            raise OpenPetsError("OpenPets returned an invalid response")
        if type(response.get("ok")) is not bool:
            raise OpenPetsError("OpenPets returned an invalid response")
        for field in ("message", "threadId"):
            if response.get(field) is not None and not isinstance(response[field], str):
                raise OpenPetsError("OpenPets returned an invalid response")
        if not response["ok"]:
            raise OpenPetsError(response.get("message") or "OpenPets rejected the command")
        return response

    def notify(self, title: str, thread_id: Optional[str] = None) -> str:
        notification = {"title": title, "status": "message"}
        if thread_id:
            notification["threadId"] = thread_id
        response = self._send({"type": "notify", "notification": notification})
        resolved = response.get("threadId") or thread_id
        if not resolved:
            raise OpenPetsError("OpenPets did not return a thread ID")
        return str(resolved)

    def clear(self, thread_id: str) -> None:
        self._send({"type": "clearMessage", "threadId": thread_id})
