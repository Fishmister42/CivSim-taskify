"""Raw Nexus/FireTuner probe client for the T077 save-path spike.

Deliberately independent of civsim_harness so spike results are evidence about the
GAME, not about our implementation. Parses LSQ the way the wire actually works
(NUL-separated index/name pairs), which differs from the harness's current reading.
"""
from __future__ import annotations

import socket
import struct
import time
import uuid

HOST, PORT = "127.0.0.1", 4318
TAG_HANDSHAKE, TAG_COMMAND = 4, 3


def _frame(tag: int, payload: str) -> bytes:
    body = payload.encode("utf-8") + b"\x00"
    return struct.pack("<Ii", len(body), tag) + body


class Probe:
    def __init__(self, app: str = "civsim-linux-spike") -> None:
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        self.buf = b""
        self.app = app
        self.states: dict[str, int] = {}

    # ---- transport -------------------------------------------------
    def _recv(self, seconds: float) -> None:
        self.sock.settimeout(0.3)
        end = time.time() + seconds
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return
                self.buf += chunk
                end = max(end, time.time() + 0.6)  # extend while data flows
            except socket.timeout:
                continue

    def _frames(self) -> list[tuple[int, str]]:
        out, off = [], 0
        while off + 8 <= len(self.buf):
            ln, tag = struct.unpack_from("<Ii", self.buf, off)
            if ln > len(self.buf) - off - 8:
                break
            payload = self.buf[off + 8: off + 8 + ln]
            out.append((tag, payload.rstrip(b"\x00").decode("utf-8", "replace")))
            off += 8 + ln
        self.buf = self.buf[off:]
        return out

    # ---- protocol --------------------------------------------------
    def handshake(self) -> dict[str, int]:
        self.sock.sendall(_frame(TAG_HANDSHAKE, f"APP:{self.app}"))
        self._recv(1.5)
        self._frames()
        self.sock.sendall(_frame(TAG_HANDSHAKE, "LSQ:"))
        self._recv(2.5)
        states: dict[str, int] = {}
        for tag, payload in self._frames():
            parts = payload.split("\x00")
            for i in range(0, len(parts) - 1, 2):
                idx, name = parts[i].strip(), parts[i + 1].strip()
                if idx.isdigit() and name:
                    states[name] = int(idx)
        self.states = states
        return states

    def exec_lua(self, state_index: int, lua: str, wait: float = 4.0) -> tuple[str, str]:
        """Run lua in a state. Returns (result_between_sentinels, all_raw_output)."""
        nonce = uuid.uuid4().hex[:12]
        wrapped = (
            f'print("---BEGIN:{nonce}---")\n{lua}\nprint("---END:{nonce}---")'
        )
        self.buf = b""
        self.sock.sendall(_frame(TAG_COMMAND, f"CMD:{state_index}:{wrapped}"))
        self._recv(wait)
        raw = "\n".join(p for _, p in self._frames())
        begin, end = f"---BEGIN:{nonce}---", f"---END:{nonce}---"
        if begin in raw and end in raw:
            return raw.split(begin, 1)[1].split(end, 1)[0].strip(), raw
        return "", raw

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
