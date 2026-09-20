"""R5 Windows spike: what the tuner actually puts on the wire, frame by frame.

`NexusClient.connect()` fails against this client with

    Nexus LSQ response payload has an odd number of NUL-separated fields
    payload: "Civ6\\0Sid Meier's Civilization 6\\0C:\\...\\Binaries\\Debug"

which is not a malformed LSQ response -- it is the **server's own greeting**,
being read as though it were the reply to `LSQ:`. This probe records the exact
frame sequence so the fix is written against observed bytes rather than a
second guess.

Uses the harness's *codec* (framing is not in question) but not its *client*
(the client's read discipline is exactly what is under test).
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

from civsim_harness.nexus.codec import NexusFrameDecoder, encode_frame

TAG_HANDSHAKE = 1
TAG_COMMAND = 2


def drain(sock: socket.socket, decoder: NexusFrameDecoder, *, seconds: float = 1.5) -> list:
    frames = []
    deadline = time.monotonic() + seconds
    sock.settimeout(0.3)
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(8192)
        except TimeoutError:
            continue
        except OSError:
            break
        if not chunk:
            break
        frames.extend(decoder.feed(chunk))
    return frames


def show(label: str, frames: list, out: list[str]) -> None:
    line = f"--- {label}: {len(frames)} frame(s) ---"
    print(line); out.append(line)
    for i, f in enumerate(frames):
        line = f"  [{i}] tag={f.tag} payload={f.payload!r}"
        print(line); out.append(line)


def main() -> int:
    out: list[str] = []
    from civsim_harness.nexus.codec import TAG_COMMAND as TC, TAG_HANDSHAKE as TH

    sock = socket.create_connection(("127.0.0.1", 4318), timeout=5)
    decoder = NexusFrameDecoder()

    show("immediately after connect, before we send anything", drain(sock, decoder), out)

    sock.sendall(encode_frame(TH, "APP:civsim_harness"))
    show("after APP:", drain(sock, decoder), out)

    sock.sendall(encode_frame(TH, "LSQ:"))
    show("after LSQ:", drain(sock, decoder, seconds=2.0), out)

    sock.close()
    Path(__file__).with_name("raw_protocol_transcript.txt").write_text(
        "\n".join(out), encoding="utf-8"
    )
    print("\nwrote raw_protocol_transcript.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
