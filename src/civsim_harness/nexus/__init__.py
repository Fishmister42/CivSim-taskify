"""Firaxis Nexus wire protocol: framing, handshake, contexts, heartbeat.

See contracts/nexus-protocol.md for the normative wire contract.

- :mod:`civsim_harness.nexus.codec` -- frame/parse the wire format (T029).
- :mod:`civsim_harness.nexus.sentinels` -- per-request nonce wrapping and
  correlated result extraction (T032, T033).
- :mod:`civsim_harness.nexus.client` -- connection, handshake, and
  request/response discipline (T031, T032, T159).
- :mod:`civsim_harness.nexus.heartbeat` -- the periodic hang-detection probe
  (T034).
"""
