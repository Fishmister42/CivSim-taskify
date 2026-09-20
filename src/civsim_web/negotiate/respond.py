"""The content-negotiation seam (T012).

**This is the only branch point of its kind in the codebase.** Every route
follows the same three steps: build one view model from the store and the Panel
Registry, decide HTML or JSON, render. Step two happens here and nowhere else.

That single-seam rule is the mechanism behind Principle VI. There is no second,
hand-maintained "API view" that could drift from the "UI view" -- the browser
and the directing Claude Code session are served by the same function up to the
final serialization, so "the session can retrieve anything the user can see"
(FR-007) is a property of the code path rather than a promise to keep two
implementations in sync (contracts/web-read-api.md, UP-002).

Negotiation rules, in order:

1. ``?format=json`` -> JSON. The explicit override, for a human who wants to
   inspect the raw body in a browser tab.
2. ``?format=html`` -> HTML. The mirror override, so a machine caller can fetch
   the page as the user sees it.
3. ``Accept`` includes ``application/json`` -> JSON.
4. ``Accept`` includes ``text/html`` -> HTML.
5. Anything else, including a bare fetch with no ``Accept`` at all -> JSON.

Rule 5 is the one worth stating: a client that expressed no preference is far
more likely to be a script than a browser (browsers always send an ``Accept``
naming ``text/html``), and JSON is the safer default for a caller that did not
ask for a page.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

__all__ = [
    "NegotiationError",
    "render_html",
    "respond",
    "wants_json",
]


class NegotiationError(RuntimeError):
    """An HTML response was asked for with no template environment configured."""


def wants_json(request: Request) -> bool:
    """Decide the one branch. See the module docstring for the rule order."""
    explicit = request.query_params.get("format")
    if explicit == "json":
        return True
    if explicit == "html":
        return False

    accept = request.headers.get("accept", "")
    lowered = accept.lower()
    if "application/json" in lowered:
        return True
    if "text/html" in lowered:
        return False
    return True


def _payload(model: BaseModel) -> Any:
    """The view model as JSON-ready data. One call, one source of truth.

    ``mode="json"`` is what makes the HTML and JSON renderings provably the
    same content: the template receives the constructed model, and the JSON
    body is that same model serialized -- not a separately-assembled dict that
    could acquire or lose a field.
    """
    return model.model_dump(mode="json")


def render_html(
    request: Request,
    model: BaseModel,
    template_name: str,
    *,
    status_code: int = 200,
    extra_context: dict[str, Any] | None = None,
) -> HTMLResponse:
    """Render ``template_name`` with the view model itself as its context.

    The template is handed the model (as ``view``) *and* its serialized form
    (as ``data``). Both are the same object's content; the second exists so a
    template can iterate fields generically -- which is how the JSON/HTML
    field-parity contract test (T033/T039) stays honest as models grow.
    """
    env = getattr(request.app.state, "jinja_env", None)
    if env is None:
        raise NegotiationError(
            "no Jinja2 environment configured on app.state.jinja_env; "
            "an HTML response cannot be rendered"
        )
    template = env.get_template(template_name)
    context: dict[str, Any] = {
        "request": request,
        "view": model,
        "data": _payload(model),
    }
    if extra_context:
        context.update(extra_context)
    return HTMLResponse(template.render(**context), status_code=status_code)


def respond(
    request: Request,
    model: BaseModel,
    template_name: str | None = None,
    *,
    status_code: int = 200,
    extra_context: dict[str, Any] | None = None,
) -> Response:
    """Serve one view model to whichever reader asked for it.

    ``template_name=None`` means this route has no HTML rendering yet; the JSON
    body is returned to both readers. That is a deliberate, visible state --
    the user sees the same content the session does, just unstyled -- rather
    than a route that silently 406s a browser.
    """
    if template_name is None or wants_json(request):
        return JSONResponse(_payload(model), status_code=status_code)
    return render_html(
        request,
        model,
        template_name,
        status_code=status_code,
        extra_context=extra_context,
    )
