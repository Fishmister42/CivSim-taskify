"""LAN bind-address resolution (T013, research R8; FR-028, FR-029).

This service must be reachable from other devices on the operator's local
network *and* must not be reachable from outside it. Those are two separate
requirements and only one of them is satisfied by a firewall someone else
configured, so the bind itself is made explicit here:

- **Never a wildcard.** ``0.0.0.0`` binds every interface the host has,
  including ones the operator has not thought about -- a VPN tunnel, a hotspot,
  a bridged container network. Refusing the wildcard means FR-029 is answered
  by the socket rather than by an assumption about the network's shape.
- **Private ranges only.** Only RFC1918 IPv4 addresses (plus loopback) are
  offered as bind targets, so a publicly-routable address on the host cannot
  become a bind target by accident.
- **Logged at startup.** ``app.py`` logs every resolved address, so the
  operator verifies reachability by reading the log rather than by trying it.

Note this is deliberately the *opposite* posture from 002's operator surface,
which binds narrower than the LAN because it must never be reachable from
another device at all. This feature binds as wide as the LAN and no wider;
that the two differ is intentional, not an inconsistency (plan.md Constraints).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Sequence

__all__ = [
    "DEFAULT_PORT",
    "RFC1918_NETWORKS",
    "WILDCARD_ADDRESSES",
    "WildcardBindRefused",
    "detect_private_addresses",
    "is_bindable_address",
    "resolve_bind_addresses",
]

#: quickstart.md's address examples use this port.
DEFAULT_PORT = 8420

#: Every spelling of "bind everything". Refused, all of them.
WILDCARD_ADDRESSES: frozenset[str] = frozenset({"0.0.0.0", "::", "*", ""})


class WildcardBindRefused(ValueError):
    """A wildcard bind was requested. This is a refusal, not a warning.

    FR-029 is not satisfiable by binding every interface and hoping none of
    them is public, so this raises rather than degrading to a narrower bind
    the operator did not ask for -- a silent substitution would leave them
    believing they had configured something they had not.
    """


#: RFC1918, spelled out. Deliberately **not** ``IPv4Address.is_private``:
#: Python treats every reserved block as "private", including the TEST-NET
#: documentation ranges (``192.0.2.0/24``, ``198.51.100.0/24``,
#: ``203.0.113.0/24``), ``198.18.0.0/15``, and ``240.0.0.0/4``. None of those
#: is a LAN, and accepting one would widen FR-028's "the user's local network"
#: to blocks the operator never meant. CGNAT (``100.64.0.0/10``, used by some
#: mesh VPNs) is likewise excluded: T013 names RFC1918, and a VPN-reachable
#: bind is exactly the "beyond the local network" FR-029 forbids.
RFC1918_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def is_bindable_address(address: str) -> bool:
    """True for an RFC1918 IPv4 address or IPv4 loopback; False otherwise.

    Link-local (``169.254.0.0/16``) is excluded despite being non-routable:
    it is an autoconfiguration fallback that appears when DHCP failed, and
    binding to it produces a service the operator's other devices cannot
    reach while looking, in a log line, exactly like success.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not isinstance(parsed, ipaddress.IPv4Address):
        return False
    if parsed.is_loopback:
        return True
    return any(parsed in network for network in RFC1918_NETWORKS)


def _primary_outbound_address() -> str | None:
    """The address the host would use to reach the LAN, without sending anything.

    ``connect`` on a UDP socket only sets the peer for subsequent sends; no
    packet leaves the machine. It is the portable way to learn which interface
    the routing table prefers, which is the one an operator's other devices
    will actually reach.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routable-looking, never routed
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def detect_private_addresses() -> tuple[str, ...]:
    """Every private IPv4 address this host has, loopback last.

    Loopback is placed last so the first entry is the LAN address an operator
    hands to another device -- the common case for FR-028.
    """
    found: list[str] = []

    primary = _primary_outbound_address()
    if primary and is_bindable_address(primary) and primary not in found:
        found.append(primary)

    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        addresses = []
    for address in addresses:
        if is_bindable_address(address) and address not in found:
            found.append(address)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if is_bindable_address(address) and address not in found:
                found.append(address)
    except OSError:
        pass

    loopback = "127.0.0.1"
    non_loopback = [a for a in found if not ipaddress.ip_address(a).is_loopback]
    return (*non_loopback, loopback)


def resolve_bind_addresses(explicit: Sequence[str] | None = None) -> tuple[str, ...]:
    """The addresses to bind, refusing a wildcard and anything non-private.

    ``explicit`` (from configuration) is validated, not merely trusted: an
    operator who asks for ``0.0.0.0`` gets a refusal naming FR-029, and one who
    asks for a public address gets a refusal naming it. With no explicit
    request, every detected private address is returned.
    """
    if explicit:
        resolved: list[str] = []
        for address in explicit:
            candidate = address.strip()
            if candidate in WILDCARD_ADDRESSES:
                raise WildcardBindRefused(
                    f"refusing wildcard bind address {address!r}: this interface must "
                    f"not be reachable from outside the local network (FR-029). Name "
                    f"the private address to bind, or leave it unset to bind every "
                    f"detected private address"
                )
            if not is_bindable_address(candidate):
                raise WildcardBindRefused(
                    f"refusing bind address {address!r}: only RFC1918 private IPv4 "
                    f"addresses and IPv4 loopback may be bound (FR-028, FR-029)"
                )
            if candidate not in resolved:
                resolved.append(candidate)
        return tuple(resolved)

    detected = detect_private_addresses()
    if not detected:
        # Cannot happen in practice -- loopback is always appended -- but the
        # empty case is named rather than left to produce a confusing bind.
        raise WildcardBindRefused(
            "no private IPv4 address detected on this host, and a wildcard bind is "
            "refused (FR-029)"
        )
    return detected
