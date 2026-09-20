"""Which wire tag carries `print()` output on this client?

`NexusClient.execute_command` times out against this client even though the Lua
plainly runs (the `Network.LoadGame` that "timed out" did in fact load the
game). `_await_result` only feeds `TAG_COMMAND` (3) payloads to the sentinel
correlator:

    if frame.tag == TAG_COMMAND:
        self._correlator.feed(frame.payload)

Observed live, the client's asynchronous log/print frames arrive with
``tag=-1``. If the nonce sentinels come back on that tag, the correlator never
sees them and every command times out by construction.

This probe sends one wrapped command and dumps **every** frame with its tag, so
the answer is read off the wire instead of inferred.
"""

from __future__ import annotations

import socket
import sys
import time
import uuid
from pathlib import Path

from civsim_harness.nexus.codec import TAG_HANDSHAKE, NexusFrameDecoder, encode_frame
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json, wrap_lua

out: list[str] = []


def emit(line: str) -> None:
    print(line, flush=True)
    out.append(line)


def drain(sock: socket.socket, dec: NexusFrameDecoder, seconds: float) -> list:
    frames = []
    deadline = time.monotonic() + seconds
    sock.settimeout(0.3)
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(16384)
        except TimeoutError:
            continue
        except OSError:
            break
        if not chunk:
            break
        frames.extend(dec.feed(chunk))
    return frames


def main() -> int:
    sock = socket.create_connection(("127.0.0.1", 4318), timeout=5)
    dec = NexusFrameDecoder()

    sock.sendall(encode_frame(TAG_HANDSHAKE, "APP:civsim_harness"))
    drain(sock, dec, 1.0)
    sock.sendall(encode_frame(TAG_HANDSHAKE, "LSQ:"))
    states: dict[str, int] = {}
    for f in drain(sock, dec, 2.0):
        if f.tag == TAG_HANDSHAKE and "\x00" in f.payload:
            toks = f.payload.split("\x00")
            if len(toks) % 2 == 0:
                states = {toks[i + 1]: int(toks[i]) for i in range(0, len(toks), 2)}
    emit(f"states: {len(states)}  has InGame={'InGame' in states} "
         f"has GameCore_Tuner={'GameCore_Tuner' in states}")
    emit(f"state table: {sorted(states.items(), key=lambda kv: kv[1])}")

    target_name = "InGame" if "InGame" in states else ("MainMenu" if "MainMenu" in states else None)
    if target_name is None:
        emit("no usable state to command")
        return 1
    idx = states[target_name]

    nonce = uuid.uuid4().hex
    body = LUA_JSON_PRELUDE + lua_print_json(
        {"probe": '"tag-hunt"', "lua_version": "_VERSION"}
    )
    payload = f"CMD:{idx}:{wrap_lua(nonce, body)}"
    emit(f"\nsending CMD to state {target_name!r} (index {idx}), nonce={nonce}")
    # The command tag the harness uses.
    from civsim_harness.nexus.codec import TAG_COMMAND

    sock.sendall(encode_frame(TAG_COMMAND, payload))

    emit("\n--- every frame received in the next 8s ---")
    hits = []
    for f in drain(sock, dec, 8.0):
        marker = ""
        if nonce in f.payload:
            marker = "   <<<< CARRIES OUR NONCE"
            hits.append(f.tag)
        emit(f"  tag={f.tag!r} payload={f.payload[:160]!r}{marker}")

    emit("")
    if hits:
        emit(f"RESULT: sentinels came back on tag(s) {sorted(set(hits))}; "
             f"TAG_COMMAND is {TAG_COMMAND}")
    else:
        emit("RESULT: no frame carried the nonce at all")

    sock.close()
    Path(__file__).with_name("raw_command_transcript.txt").write_text(
        "\n".join(out), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
