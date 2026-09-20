"""``GET /captures/{capture_id}/image`` -- the capture beside the numbers (T029).

Two gates, in this order, and the order matters:

1. **``CaptureView.available``.** Computed as ``screening_status ==
   "screened_clean"``, full stop (data-model.md V2). ``withheld``, a missing
   record, and a ``screening_status`` this code has never heard of all resolve
   to unavailable. Anything not available is a ``404`` whose body names
   ``unavailable_reason`` -- *never* a ``200`` with a placeholder image standing
   in for the real one, which contracts/web-read-api.md rules out explicitly
   because a placeholder can be mistaken for content.
2. **Can the store resolve the bytes at all?** This is the gap the foundation
   notes recorded and it is real: ``get_capture`` returns the record carrying
   ``blob_ref``, and ``match-store-port.md`` publishes *no* operation that turns
   a ``blob_ref`` into bytes. A store may offer the optional
   ``CaptureBlobReader`` capability (``store_client/port.py``), which is probed
   for here through ``store_client/reads.py``. A store that does not offer it
   gets a ``503`` naming the port gap -- a store-side inability, not a statement
   about this capture, which is why it is not folded into the ``404`` above.

The route is deliberately its own endpoint rather than a data URI inlined into
the turn: FR-036 requires that viewing a turn not pull in captures beyond the
ones being viewed.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from civsim_web.routes.common import ErrorView, WebError, registry_of, require_store
from civsim_web.store_client import reads
from civsim_web.viewmodels.capture import build_capture_view

__all__ = ["router"]

router = APIRouter()

#: Magic-byte sniffing, over four formats. The store records no content type,
#: and guessing from ``blob_ref``'s extension would trust a string this feature
#: never validated. An unrecognised signature falls back to
#: ``application/octet-stream`` rather than claiming a type it cannot see.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _content_type(blob: bytes) -> str:
    for signature, media_type in _SIGNATURES:
        if blob.startswith(signature):
            return media_type
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


@router.get("/captures/{capture_id}/image")
def get_capture_image(request: Request, capture_id: str) -> Response:
    """The capture's bytes, or an explicit account of why there are none."""
    store = require_store(request)
    registry = registry_of(request)

    record = store.get_capture(capture_id)
    view = build_capture_view(record, registry=registry, expected=True)

    if not view.available:
        raise WebError(
            404,
            ErrorView(
                kind="capture_unavailable",
                message=(
                    f"This capture is not viewable: "
                    f"{view.unavailable_reason or 'missing_record'}. Its turn's "
                    f"structured panels still render (FR-034)."
                ),
                detail={
                    "capture_id": capture_id,
                    "unavailable_reason": str(view.unavailable_reason or "missing_record"),
                    "screening_status": view.screening_status,
                },
            ),
        )

    blob = reads.capture_blob(store, capture_id)
    if not blob:
        raise WebError(
            503,
            ErrorView(
                kind="capture_blob_unreachable",
                message=(
                    "This capture passed screening, but the configured store cannot "
                    "resolve its blob_ref to image bytes. The published MatchStore "
                    "port has no blob-fetch operation; a store may offer the optional "
                    "CaptureBlobReader capability, and this one does not. The turn's "
                    "structured panels are unaffected."
                ),
                detail={"capture_id": capture_id},
            ),
        )

    return Response(
        content=blob,
        media_type=_content_type(blob),
        # Captures are immutable once screened, and the turn they belong to is
        # already addressable; caching them is what keeps FR-036 ("viewing a
        # turn must not require loading captures beyond those being viewed")
        # cheap when a poll re-renders the same turn.
        headers={"Cache-Control": "private, max-age=3600"},
    )
