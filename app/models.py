"""Pydantic request/response models.

Each submission is a *complete* snapshot of one relation revision:
all nodes referenced by the ways must be included, and the ways are
unordered (the stitcher may traverse them in either direction).
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Hard limits required by the specification.
MAX_WAYS_PER_REVISION = 8
MAX_NODES_PER_REVISION = 32


class NodeIn(BaseModel):
    id: int = Field(..., ge=1, description="OSM node id")
    lat: float = Field(..., ge=-90.0, le=90.0, description="Latitude (WGS84)")
    lon: float = Field(..., ge=-180.0, le=180.0, description="Longitude (WGS84)")


class WayIn(BaseModel):
    id: int = Field(..., ge=1, description="OSM way id")
    role: Literal["outer", "inner"] = Field(..., description="Relation role")
    node_refs: list[int] = Field(
        ...,
        min_length=2,
        description="Ordered node ids as they appear in the OSM way; "
        "traversal may be reversed while stitching.",
    )


class RevisionIn(BaseModel):
    revision: int = Field(..., ge=1, description="Monotonically increasing revision number")
    nodes: list[NodeIn] = Field(..., min_length=1, max_length=MAX_NODES_PER_REVISION)
    ways: list[WayIn] = Field(..., min_length=1, max_length=MAX_WAYS_PER_REVISION)

    @model_validator(mode="after")
    def _unique_ids(self) -> "RevisionIn":
        node_ids = [n.id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node id in nodes")
        way_ids = [w.id for w in self.ways]
        if len(set(way_ids)) != len(way_ids):
            raise ValueError("duplicate way id in ways")
        return self


class ErrorDetail(BaseModel):
    code: str
    message: str
    way_ids: list[int] = Field(default_factory=list)


class RevisionResult(BaseModel):
    relation_id: int
    revision: int
    status: Literal["valid", "invalid", "pending", "approved"] = Field(
        ...,
        description="valid: serving; pending: geometrically valid but IoU vs the "
        "last served polygon is below threshold, awaiting review; approved: a "
        "pending revision confirmed by a reviewer, now serving; invalid: "
        "topologically broken, never served",
    )
    geometry: dict | None = Field(
        default=None, description="GeoJSON Polygon, present unless status is invalid"
    )
    error: ErrorDetail | None = None
    iou_ratio: float | None = Field(
        default=None,
        description="Intersection-over-union vs the last served polygon, computed "
        "during the pre-publish quality check (null when no served polygon "
        "existed or the revision is invalid)",
    )
    input: dict | None = Field(
        default=None,
        description="Retained raw submission of the stored revision "
        "(populated by the per-revision read endpoint)",
    )
    last_valid_revision: int | None = Field(
        default=None,
        description="Most recent serving revision (status valid or approved)",
    )
    pending_revision: int | None = Field(
        default=None,
        description="Newer revision awaiting review; set by /current when the "
        "latest revision is pending while an older polygon keeps being served",
    )
