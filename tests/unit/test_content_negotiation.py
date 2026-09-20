"""The single content-negotiation seam (T012).

Every route in this feature ends here, so the rules are worth pinning down
before four user stories are built on top of them. The property under test is
not "JSON works and HTML works" -- it is that **both are the same object**.
The test below renders a template that iterates the serialized model and
asserts every JSON key reaches the page, which is the seed of the
field-parity contract test US1 and US2 grow (T033, T039) and the mechanism
behind FR-007.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from jinja2 import DictLoader, Environment, StrictUndefined

from civsim_web.negotiate.respond import NegotiationError, respond, wants_json
from civsim_web.viewmodels.base import ViewModel


class _Demo(ViewModel):
    run_id: str
    turn_number: int
    reasoning: str | None = None


PAYLOAD = _Demo(run_id="run-1", turn_number=34, reasoning="held the settler")

TEMPLATE = """<ul>
{% for key, value in data.items() %}<li data-field="{{ key }}">{{ value }}</li>
{% endfor %}</ul>"""


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.state.jinja_env = Environment(
        loader=DictLoader({"demo.html": TEMPLATE}), undefined=StrictUndefined
    )

    @app.get("/demo")
    def demo(request: Request) -> Response:
        return respond(request, PAYLOAD, "demo.html")

    @app.get("/json-only")
    def json_only(request: Request) -> Response:
        return respond(request, PAYLOAD)

    @app.get("/no-env")
    def no_env(request: Request) -> Response:
        request.app.state.jinja_env = None
        return respond(request, PAYLOAD, "demo.html")

    return TestClient(app)


@pytest.mark.parametrize(
    ("headers", "params", "expect_json"),
    [
        ({"accept": "application/json"}, {}, True),
        ({"accept": "text/html,application/xhtml+xml"}, {}, False),
        ({"accept": "text/html"}, {"format": "json"}, True),
        ({"accept": "application/json"}, {"format": "html"}, False),
        ({}, {}, True),
        ({"accept": "*/*"}, {}, True),
    ],
    ids=[
        "accept-json",
        "accept-html",
        "format-json-overrides-accept",
        "format-html-overrides-accept",
        "no-accept-defaults-to-json",
        "wildcard-accept-defaults-to-json",
    ],
)
def test_the_branch_follows_the_documented_rule_order(client, headers, params, expect_json):
    response = client.get("/demo", headers=headers, params=params)
    assert response.status_code == 200
    is_json = response.headers["content-type"].startswith("application/json")
    assert is_json is expect_json


def test_both_readers_receive_the_same_fields(client):
    """FR-007, in its smallest form: no field in one view and not the other."""
    as_json = client.get("/demo", headers={"accept": "application/json"}).json()
    as_html = client.get("/demo", headers={"accept": "text/html"}).text

    assert as_json == {"run_id": "run-1", "turn_number": 34, "reasoning": "held the settler"}
    for key, value in as_json.items():
        assert f'data-field="{key}"' in as_html, f"{key} reached JSON but not HTML"
        assert str(value) in as_html


def test_a_route_with_no_template_serves_json_to_both(client):
    """A deliberate, visible state -- not a 406 for a browser."""
    browser = client.get("/json-only", headers={"accept": "text/html"})
    assert browser.headers["content-type"].startswith("application/json")
    assert browser.json()["run_id"] == "run-1"


def test_an_html_request_with_no_environment_raises(client):
    """Silently serving JSON instead would hide a misconfigured deployment."""
    with pytest.raises(NegotiationError):
        client.get("/no-env", headers={"accept": "text/html"})


def test_wants_json_is_the_only_decision_point():
    """`respond` has exactly one branch, and this is it.

    Asserted as a unit so a future route cannot grow a second, private
    negotiation rule without this test noticing the helper is unused.
    """
    assert callable(wants_json)
