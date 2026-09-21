"""The foundation, end to end (T013-T015).

Phase 2's task list assigns tests to the registry, health derivation, and view
references, but not to the three pieces every user story will import on its
first line: the bind resolver, the application factory, and the CLI. Four
parallel contributors about to build on those deserve to know they work, so
this suite covers them at the seams they will actually touch.
"""

from __future__ import annotations

import pytest

from civsim_web.app import create_app
from civsim_web.cli import build_parser, main
from civsim_web.net.bind import (
    WildcardBindRefused,
    is_bindable_address,
    resolve_bind_addresses,
)


@pytest.fixture
def support():
    from web_support import fixtures

    return fixtures


# --------------------------------------------------------------------------
# T013 -- bind resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "*", ""])
def test_a_wildcard_bind_is_refused(address):
    """FR-029 is answered by the socket, not by an assumption about the network."""
    with pytest.raises(WildcardBindRefused) as excinfo:
        resolve_bind_addresses([address])
    assert "FR-029" in str(excinfo.value)


@pytest.mark.parametrize("address", ["8.8.8.8", "203.0.113.5", "169.254.10.1"])
def test_non_private_and_link_local_addresses_are_refused(address):
    """Only RFC1918 plus loopback. Link-local looks like success and is not."""
    assert not is_bindable_address(address)
    with pytest.raises(WildcardBindRefused):
        resolve_bind_addresses([address])


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.42", "10.0.0.7", "172.16.5.1"])
def test_private_and_loopback_addresses_are_accepted(address):
    assert is_bindable_address(address)
    assert resolve_bind_addresses([address]) == (address,)


def test_detected_addresses_always_include_loopback_last():
    """Loopback last so the first entry is the address FR-028 hands to a device.

    Note what this does *not* prove: `all(is_bindable_address(a))` checks the
    output of a function that already filtered with `is_bindable_address`, so it
    holds whether or not the filter is there. The test below is the one that
    bites (T072).
    """
    detected = resolve_bind_addresses()
    assert detected[-1] == "127.0.0.1"
    assert all(is_bindable_address(a) for a in detected)


def test_auto_detection_drops_a_public_or_link_local_host_address(monkeypatch):
    """FR-029 on the path that actually runs by default (T072).

    The explicit-config path is well covered above -- a wildcard is refused, and
    so is `8.8.8.8`. But `--bind` is usually unset, and then the addresses come
    from `detect_private_addresses` reading this host's own interfaces. Nothing
    tested that path with anything to reject: on an ordinary RFC1918-only
    machine the filter has nothing to do, so deleting it would leave every
    assertion in this file passing while a host that *does* hold a routable
    address -- a cloud VM, a machine on a public /24, a container with a
    link-local -- started binding it. FR-029 is one line ("MUST NOT be reachable
    from outside the local network") and that is the shape of its failure.

    So the host is stubbed to hold exactly the addresses that must be dropped,
    plus one that must survive.
    """
    from civsim_web.net import bind

    monkeypatch.setattr(bind, "_primary_outbound_address", lambda: "203.0.113.5")
    monkeypatch.setattr(bind.socket, "gethostname", lambda: "stubbed-host")
    monkeypatch.setattr(
        bind.socket,
        "gethostbyname_ex",
        lambda _name: ("stubbed-host", [], ["8.8.8.8", "169.254.10.1", "10.2.0.2"]),
    )
    monkeypatch.setattr(
        bind.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, "", ("100.64.0.1", 0))],
    )

    detected = bind.detect_private_addresses()

    assert "10.2.0.2" in detected, "a genuine RFC1918 address must still be detected"
    for routable in ("203.0.113.5", "8.8.8.8", "169.254.10.1", "100.64.0.1"):
        assert routable not in detected, (
            f"{routable} reached the bind list; FR-029 forbids this interface "
            f"being reachable from outside the local network"
        )
    assert detected[-1] == "127.0.0.1"


# --------------------------------------------------------------------------
# T014 -- the application and /healthz
# --------------------------------------------------------------------------


def test_healthz_reports_the_store_and_the_registry(support):
    client = support.make_client()
    body = client.get("/healthz", headers={"accept": "application/json"}).json()

    assert body["ok"] is True
    assert body["store"]["ok"] is True
    assert body["service"] == "civsim_web"
    assert body["bind_addresses"] == ["127.0.0.1"]
    # Invariant V10's half that this route can carry: which registry version
    # the *running process* loaded, which is more reliable than the file on
    # disk next to it.
    assert body["panel_registry"]["version"] == "1"
    assert body["panel_registry"]["content_hash"]


def test_healthz_reports_an_unreachable_store_without_failing(support):
    """A check that raises on failure is not a health check.

    200 with `store.ok = false` keeps "the store is down" distinguishable from
    "this endpoint is broken" -- a distinction the error table in
    contracts/web-read-api.md depends on.
    """
    store = support.make_store()
    store.set_health(ok=False, detail="connection refused")
    client = support.make_client(store)

    response = client.get("/healthz", headers={"accept": "application/json"})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["store"]["detail"] == "connection refused"


def test_creating_the_app_with_a_wildcard_bind_refuses_before_a_socket_exists(support):
    with pytest.raises(WildcardBindRefused):
        create_app(store=support.make_store(), bind_addresses=["0.0.0.0"])


def test_an_invalid_registry_aborts_app_creation(support, tmp_path):
    """A registry that fails to load must not be served partially validated."""
    from civsim_web.registry.loader import PanelRegistryLoadError

    broken = tmp_path / "panels"
    broken.mkdir()
    (broken / "VERSION").write_text("1\n", encoding="utf-8")
    # Only one of the four required files.
    (broken / "live.yaml").write_text("[]\n", encoding="utf-8")

    with pytest.raises(PanelRegistryLoadError):
        create_app(
            store=support.make_store(),
            bind_addresses=["127.0.0.1"],
            panels_dir=broken,
        )


def test_static_is_mounted(support):
    """US1's `poll.js` and `style.css` need somewhere to be served from."""
    app = support.make_app()
    assert any(getattr(route, "path", None) == "/static" for route in app.routes)


# --------------------------------------------------------------------------
# T015 -- the CLI
# --------------------------------------------------------------------------


def test_doctor_reports_all_three_preflight_lines(capsys):
    exit_code = main(["doctor"])
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert "store             : ok" in captured
    assert "panel registry    : ok" in captured
    assert "bind address(es)  :" in captured
    assert "no wildcard" in captured


def test_doctor_fails_loudly_on_a_refused_bind(capsys):
    exit_code = main(["--bind", "0.0.0.0", "doctor"])
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "FAILED" in captured
    assert "preflight FAILED" in captured


def test_doctor_fails_on_an_unloadable_registry(tmp_path, capsys):
    (tmp_path / "VERSION").write_text("1\n", encoding="utf-8")
    exit_code = main(["--panels", str(tmp_path), "doctor"])
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "panel registry    : FAILED" in captured


def test_the_store_is_configuration_not_code():
    """quickstart Scenario 6: swapping stores must be a value, not an edit."""
    from civsim_web.cli import resolve_store

    default = resolve_store("fake")
    assert default.ping().ok is True

    by_reference = resolve_store("civsim_web.store_client.fake:FakeMatchStore")
    assert by_reference.ping().ok is True

    with pytest.raises(ValueError):
        resolve_store("not-a-module-reference")


def test_the_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
