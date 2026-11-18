"""FastAPI application: upload relation revisions and read water polygons."""

from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .database import (
    ApprovalConflict,
    Database,
    RevisionConflict,
    RevisionNotFound,
)
from .geometry import GeometryError, build_polygon_geojson, intersection_over_union
from .models import RevisionIn, RevisionResult

# Pre-publish quality check: a topologically valid revision whose IoU vs
# the last served polygon falls below this threshold is retained but held
# back as "pending" until a reviewer approves it via the API (/docs).
QC_IOU_THRESHOLD = 0.5


def _database_path() -> str:
    # Read lazily so tests (and redeployed env) can override FLOOD_DB_PATH.
    return os.environ.get("FLOOD_DB_PATH", "/data/flood.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(_database_path())
    yield


app = FastAPI(
    title="OSM Flood Water-Area API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Submit complete, incrementing revisions of an OSM water relation "
        "(nodes + unordered outer/inner ways). Valid revisions become a "
        "GeoJSON Polygon; invalid revisions retain their raw input and an "
        "error, while the last valid polygon keeps being served. Before a "
        "new polygon is published it is quality-checked against the last "
        "served polygon: an intersection-over-union below "
        f"{QC_IOU_THRESHOLD} marks the revision as pending review instead "
        "of serving it; reviewers approve it via the documented endpoint."
    ),
)


def _db(request: Request) -> Database:
    return request.app.state.db


def _stored_to_result(row: dict) -> RevisionResult:
    return RevisionResult(
        relation_id=row["relation_id"],
        revision=row["revision"],
        status=row["status"],
        geometry=row["geometry"],
        error=row["error"],
        iou_ratio=row["iou_ratio"],
    )


def _qc_decision(new_geometry: dict, serving_geometry: dict) -> tuple[str, float]:
    """Quality gate for geometrically valid revisions.

    Returns ``(status, iou)``: ``pending`` when the new polygon drifted
    too far from the currently served one, else ``valid``.
    """
    iou = intersection_over_union(new_geometry, serving_geometry)
    return ("pending" if iou < QC_IOU_THRESHOLD else "valid"), iou


@app.get("/health", tags=["meta"])
def health(request: Request) -> dict:
    db = _db(request)
    return {"status": "ok", "version": __version__, "db": db.path}


@app.post(
    "/relations/{relation_id}/revisions",
    response_model=RevisionResult,
    status_code=200,
    tags=["revisions"],
    summary="Upload one complete relation revision",
)
def upload_revision(relation_id: int, payload: RevisionIn, request: Request) -> RevisionResult:
    """Validate, quality-check and store a revision.

    The raw input is always stored atomically together with its computed
    geometry/state. Geometric failure yields ``status="invalid"`` (HTTP
    200, the revision is retained); a geometrically valid revision whose
    IoU vs the last served polygon is below threshold is stored as
    ``status="pending"`` and keeps the previous polygon serving; only a
    non-incrementing revision number is rejected (HTTP 409, nothing is
    stored).  The first valid revision of a relation has no baseline and
    is served directly.
    """
    error = None
    geometry = None
    status = "valid"
    try:
        geometry = build_polygon_geojson(payload)
    except GeometryError as exc:
        status = "invalid"
        error = exc.to_detail()

    db = _db(request)
    try:
        receipt = db.save_revision(
            relation_id=relation_id,
            revision=payload.revision,
            status=status,
            input_payload=payload.model_dump(),
            geometry=geometry,
            error=error,
            iou_decider=_qc_decision,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "latest_revision": exc.latest,
            },
        ) from exc

    return RevisionResult(
        relation_id=relation_id,
        revision=payload.revision,
        status=receipt["status"],
        geometry=geometry,
        error=error,
        iou_ratio=receipt["iou_ratio"],
        last_valid_revision=receipt["serving_revision"],
    )


@app.post(
    "/relations/{relation_id}/revisions/{revision}/approve",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Approve a pending revision so it becomes the served polygon",
)
def approve_revision(relation_id: int, revision: int, request: Request) -> RevisionResult:
    """Reviewer approval of a quality-held revision.

    Only a ``pending`` revision that is still the relation's latest
    revision can be approved; the check and the status flip commit
    atomically.  Approving a superseded (no longer latest) or a
    non-pending revision — including topologically invalid ones, which
    can never be approved — fails with HTTP 409 and changes nothing.
    """
    db = _db(request)
    try:
        receipt = db.approve_revision(relation_id, revision)
    except RevisionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc
    except ApprovalConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": str(exc),
                "status": exc.status,
                "latest_revision": exc.latest,
            },
        ) from exc

    result = _stored_to_result(receipt["row"])
    result.last_valid_revision = receipt["serving_revision"]
    return result


@app.get(
    "/relations/{relation_id}/revisions/{revision}",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read input, geometry and status of one stored revision",
)
def read_revision(relation_id: int, revision: int, request: Request) -> RevisionResult:
    view = _db(request).get_revision_view(relation_id, revision)
    row = view["row"]
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "revision does not exist"},
        )
    result = _stored_to_result(row)
    result.input = row["input"]
    serving = view["serving"]
    result.last_valid_revision = serving["revision"] if serving else None
    return result


@app.get(
    "/relations/{relation_id}/revisions",
    tags=["revisions"],
    summary="List all stored revisions of a relation",
)
def list_revisions(relation_id: int, request: Request) -> dict:
    return {"relation_id": relation_id, "revisions": _db(request).list_revisions(relation_id)}


@app.get(
    "/relations/{relation_id}/current",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read the currently served polygon",
)
def read_current(relation_id: int, request: Request) -> RevisionResult:
    """Current face semantics.

    * latest revision valid/approved -> serve its polygon;
    * latest revision pending -> the previous served polygon keeps being
      served (HTTP 200) and ``pending_revision`` points at the revision
      awaiting review;
    * latest revision invalid -> stop serving: 409, but the response
      points at ``last_valid_revision`` which remains fully readable via
      the per-revision endpoint;
    * a later valid (or approved) revision resumes serving automatically.
    """
    view = _db(request).get_current_view(relation_id)
    latest = view["latest"]
    serving = view["serving"]
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "no revision stored for this relation"},
        )

    if latest["status"] in ("valid", "approved"):
        result = _stored_to_result(latest)
        result.last_valid_revision = latest["revision"]
        return result

    if latest["status"] == "pending":
        if serving is None:  # pragma: no cover - pending implies a QC baseline existed
            raise HTTPException(
                status_code=409,
                detail={"code": "no_serving_face", "message": "no polygon has ever been served"},
            )
        result = _stored_to_result(serving)
        result.last_valid_revision = serving["revision"]
        result.pending_revision = latest["revision"]
        return result

    return JSONResponse(
        status_code=409,
        content={
            "relation_id": relation_id,
            "revision": latest["revision"],
            "status": "invalid",
            "geometry": None,
            "error": latest["error"],
            "iou_ratio": None,
            "last_valid_revision": serving["revision"] if serving else None,
            "pending_revision": None,
            "detail": (
                "current revision is invalid and the face is not being served; "
                "fetch last_valid_revision for the previous valid polygon"
            ),
        },
    )
