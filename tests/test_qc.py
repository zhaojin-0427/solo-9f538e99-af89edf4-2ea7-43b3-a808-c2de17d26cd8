"""Pre-publish quality check: IoU gating, pending review, approval.

State machine under test:

* first valid revision of a relation is served directly (no baseline);
* a valid revision with IoU >= 0.5 vs the last served polygon is served;
* a valid revision with IoU < 0.5 is stored as ``pending`` (raw input,
  geometry and ratio retained) while /current keeps serving the old face;
* a pending revision that is still the latest can be approved (-> served);
  stale or non-pending approvals fail with 409 and change nothing;
* topologically invalid revisions follow the original stop-serving rule
  and can never be approved;
* uploads, approvals and reads interleave without the receipts and the
  served face ever pointing at different committed states.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.geometry import intersection_over_union
from app.main import app

from .factories import SQUARE_NODES, SQUARE_WAY, revision, shifted, way
from .test_api import post


def approve(client: TestClient, relation_id: int, rev: int):
    return client.post(f"/relations/{relation_id}/revisions/{rev}/approve")


# IoU reference values for the unit square shifted in longitude:
# shift 0.3 -> 0.7/1.3 ~= 0.538 (serves), shift 0.4 -> 0.6/1.4 ~= 0.429
# (pending), shift 10 -> 0 (pending).
SMALL_SHIFT = shifted(SQUARE_NODES, dlon=0.3)
DRIFTED = shifted(SQUARE_NODES, dlon=0.4)
FAR_AWAY = shifted(SQUARE_NODES, dlon=10.0)


# ------------------------------------------------------------------ IoU helper
def test_iou_identical_disjoint_and_partial():
    geom = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    same = intersection_over_union(geom, geom)
    assert same == pytest.approx(1.0)

    disjoint = {
        "type": "Polygon",
        "coordinates": [[[5, 0], [6, 0], [6, 1], [5, 1], [5, 0]]],
    }
    assert intersection_over_union(geom, disjoint) == pytest.approx(0.0)

    half = {
        "type": "Polygon",
        "coordinates": [[[0.5, 0], [1.5, 0], [1.5, 1], [0.5, 1], [0.5, 0]]],
    }
    # intersection 0.5, union 1.5
    assert intersection_over_union(geom, half) == pytest.approx(1.0 / 3.0)


# ------------------------------------------------------------- upload gating
def test_first_valid_revision_serves_without_baseline(client: TestClient, relation_id: int):
    resp = post(client, relation_id, revision(1, FAR_AWAY, [SQUARE_WAY]))
    body = resp.json()
    assert body["status"] == "valid"
    assert body["iou_ratio"] is None  # no serving polygon to compare against
    assert body["last_valid_revision"] == 1
    assert client.get(f"/relations/{relation_id}/current").json()["revision"] == 1


def test_first_valid_after_invalid_serves_directly(client: TestClient, relation_id: int):
    bad = post(client, relation_id, revision(1, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    assert bad.json()["status"] == "invalid"
    good = post(client, relation_id, revision(2, FAR_AWAY, [SQUARE_WAY]))
    body = good.json()
    assert body["status"] == "valid"  # still no served baseline -> direct
    assert body["iou_ratio"] is None
    assert client.get(f"/relations/{relation_id}/current").json()["revision"] == 2


def test_small_change_above_threshold_is_served(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    resp = post(client, relation_id, revision(2, SMALL_SHIFT, [SQUARE_WAY]))
    body = resp.json()
    assert body["status"] == "valid"
    assert body["iou_ratio"] == pytest.approx(0.7 / 1.3)
    assert body["last_valid_revision"] == 2
    assert client.get(f"/relations/{relation_id}/current").json()["revision"] == 2


def test_drifted_revision_is_pending_and_old_face_keeps_serving(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    resp = post(client, relation_id, revision(2, DRIFTED, [SQUARE_WAY]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["iou_ratio"] == pytest.approx(0.6 / 1.4)
    assert body["geometry"]["type"] == "Polygon"  # geometry retained
    assert body["error"] is None
    # Receipt and served face point at the same committed state.
    assert body["last_valid_revision"] == 1

    current = client.get(f"/relations/{relation_id}/current")
    assert current.status_code == 200
    cur = current.json()
    assert cur["revision"] == 1  # old face still served
    assert cur["pending_revision"] == 2  # review pointer
    assert cur["last_valid_revision"] == 1

    # The pending revision retains raw input, geometry and ratio.
    stored = client.get(f"/relations/{relation_id}/revisions/2").json()
    assert stored["status"] == "pending"
    assert stored["iou_ratio"] == pytest.approx(0.6 / 1.4)
    assert stored["geometry"]["type"] == "Polygon"
    assert stored["input"]["nodes"][0]["lon"] == pytest.approx(0.4)
    assert stored["last_valid_revision"] == 1

    listing = client.get(f"/relations/{relation_id}/revisions").json()["revisions"]
    assert listing == [
        {"revision": 1, "status": "valid"},
        {"revision": 2, "status": "pending"},
    ]


def test_pending_period_baseline_remains_last_served_face(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    drifted = post(client, relation_id, revision(2, FAR_AWAY, [SQUARE_WAY]))
    assert drifted.json()["status"] == "pending"

    # Revision 3 is the original square again: identical to the *served*
    # face (rev 1), disjoint from the pending one (rev 2).  A valid status
    # with IoU ~1.0 proves the baseline is still the last served polygon.
    resp = post(client, relation_id, revision(3, SQUARE_NODES, [SQUARE_WAY]))
    body = resp.json()
    assert body["status"] == "valid"
    assert body["iou_ratio"] == pytest.approx(1.0)
    assert client.get(f"/relations/{relation_id}/current").json()["revision"] == 3


def test_invalid_during_pending_follows_original_stop_rule(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, DRIFTED, [SQUARE_WAY]))  # pending
    resp = post(client, relation_id, revision(3, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    assert resp.json()["status"] == "invalid"
    assert resp.json()["last_valid_revision"] == 1

    cur = client.get(f"/relations/{relation_id}/current")
    assert cur.status_code == 409  # invalid latest stops serving, as before
    assert cur.json()["last_valid_revision"] == 1


# ------------------------------------------------------------------- approval
def test_approve_pending_latest_makes_it_serve(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, DRIFTED, [SQUARE_WAY]))
    assert client.get(f"/relations/{relation_id}/current").json()["revision"] == 1

    resp = approve(client, relation_id, 2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["iou_ratio"] == pytest.approx(0.6 / 1.4)
    assert body["last_valid_revision"] == 2

    cur = client.get(f"/relations/{relation_id}/current").json()
    assert cur["revision"] == 2
    assert cur["status"] == "approved"
    assert cur["pending_revision"] is None

    # The approved face becomes the QC baseline for the next upload:
    # a polygon near rev 2 passes, the original square is now the outlier.
    near = post(client, relation_id, revision(3, shifted(DRIFTED, dlon=0.3), [SQUARE_WAY]))
    assert near.json()["status"] == "valid"
    assert near.json()["iou_ratio"] == pytest.approx(0.7 / 1.3)

    back = post(client, relation_id, revision(4, SQUARE_NODES, [SQUARE_WAY]))
    assert back.json()["status"] == "pending"  # far from the approved face


def test_approve_stale_pending_reports_conflict_and_serving_unchanged(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, DRIFTED, [SQUARE_WAY]))  # pending
    post(client, relation_id, revision(3, SMALL_SHIFT, [SQUARE_WAY]))  # valid, served

    resp = approve(client, relation_id, 2)  # too late: rev 3 is latest
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["latest_revision"] == 3

    # Failed approval changed nothing.
    cur = client.get(f"/relations/{relation_id}/current").json()
    assert cur["revision"] == 3 and cur["status"] == "valid"
    stored = client.get(f"/relations/{relation_id}/revisions/2").json()
    assert stored["status"] == "pending"


def test_approve_invalid_revision_is_rejected(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))

    resp = approve(client, relation_id, 2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_pending"
    assert resp.json()["detail"]["status"] == "invalid"

    # Serving state untouched: latest is still invalid -> 409 stop-serving.
    assert client.get(f"/relations/{relation_id}/current").status_code == 409
    stored = client.get(f"/relations/{relation_id}/revisions/2").json()
    assert stored["status"] == "invalid"


def test_approve_valid_revision_is_rejected(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    resp = approve(client, relation_id, 1)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_pending"
    assert client.get(f"/relations/{relation_id}/current").json()["revision"] == 1


def test_approve_unknown_revision_is_404(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    assert approve(client, relation_id, 99).status_code == 404


def test_approval_persists_across_restart(client: TestClient, db_path: str, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, DRIFTED, [SQUARE_WAY]))
    assert approve(client, relation_id, 2).status_code == 200

    restarted = Database(db_path)
    app.state.db = restarted
    row = restarted.get_revision(relation_id, 2)
    assert row["status"] == "approved"
    assert row["iou_ratio"] == pytest.approx(0.6 / 1.4)
    assert restarted.get_last_valid(relation_id)["revision"] == 2
    assert client.get(f"/relations/{relation_id}/current").json()["revision"] == 2


# ------------------------------------------------------- interleaving safety
def test_interleaved_approve_and_upload_settle_on_one_committed_state(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    held = post(client, relation_id, revision(2, FAR_AWAY, [SQUARE_WAY]))
    assert held.json()["status"] == "pending"

    barrier = threading.Barrier(2)
    outcomes: dict[str, dict] = {}

    def do_approve() -> None:
        barrier.wait()
        r = approve(client, relation_id, 2)
        outcomes["approve"] = {"code": r.status_code, "body": r.json()}

    def do_upload() -> None:
        barrier.wait()
        r = post(client, relation_id, revision(3, SQUARE_NODES, [SQUARE_WAY]))
        outcomes["upload"] = {"code": r.status_code, "body": r.json()}

    threads = [threading.Thread(target=do_approve), threading.Thread(target=do_upload)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    current = client.get(f"/relations/{relation_id}/current").json()
    upload = outcomes["upload"]
    approval = outcomes["approve"]
    assert upload["code"] == 200

    if approval["code"] == 200:
        # Approval committed first: rev 2 serves, rev 3 was compared against
        # the approved (far-away) face and is pending.
        assert approval["body"]["status"] == "approved"
        assert upload["body"]["status"] == "pending"
        assert upload["body"]["iou_ratio"] == pytest.approx(0.0)
        assert current["revision"] == 2
        assert current["pending_revision"] == 3
    else:
        # Upload committed first: rev 3 serves, the late approval conflicts.
        assert approval["code"] == 409
        assert approval["body"]["detail"]["code"] == "stale_revision"
        assert upload["body"]["status"] == "valid"
        assert upload["body"]["iou_ratio"] == pytest.approx(1.0)
        assert current["revision"] == 3

    # Every stored revision is complete and consistent.
    for rev in (1, 2, 3):
        row = client.get(f"/relations/{relation_id}/revisions/{rev}").json()
        assert row["geometry"]["type"] == "Polygon"
        assert row["input"]["revision"] == rev


# ------------------------------------------------------------------ migration
LEGACY_SCHEMA = """
CREATE TABLE revisions (
    relation_id          INTEGER NOT NULL,
    revision             INTEGER NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('valid', 'invalid')),
    input_json           TEXT    NOT NULL,
    geometry_json        TEXT,
    error_code           TEXT,
    error_message        TEXT,
    error_way_ids_json   TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (relation_id, revision)
);
"""


def test_legacy_database_is_migrated_and_stays_readable(tmp_path, relation_id: int):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO revisions (relation_id, revision, status, input_json, geometry_json) "
        "VALUES (?, 1, 'valid', '{\"revision\": 1}', '{\"type\": \"Polygon\"}')",
        (relation_id,),
    )
    conn.execute(
        "INSERT INTO revisions (relation_id, revision, status, input_json, error_code, "
        "error_message, error_way_ids_json) VALUES (?, 2, 'invalid', '{\"revision\": 2}', "
        "'missing_nodes', 'gone', '[10]')",
        (relation_id,),
    )
    conn.commit()
    conn.close()

    db = Database(path)  # triggers the in-place migration
    valid = db.get_revision(relation_id, 1)
    assert valid["status"] == "valid"
    assert valid["iou_ratio"] is None
    assert valid["geometry"] == {"type": "Polygon"}
    invalid = db.get_revision(relation_id, 2)
    assert invalid["status"] == "invalid"
    assert invalid["error"]["code"] == "missing_nodes"
    assert db.get_last_valid(relation_id)["revision"] == 1

    # New statuses and the QC flow work on the migrated database.
    receipt = db.save_revision(
        relation_id,
        3,
        "valid",
        {"revision": 3},
        {"type": "Polygon"},
        None,
        iou_decider=lambda new, base: ("pending", 0.25),
    )
    assert receipt == {"status": "pending", "iou_ratio": 0.25, "serving_revision": 1}
    assert db.get_revision(relation_id, 3)["status"] == "pending"
    approval = db.approve_revision(relation_id, 3)
    assert approval["row"]["status"] == "approved"
    assert db.get_last_valid(relation_id)["revision"] == 3


# ----------------------------------------------------------------------- docs
def test_docs_expose_approval_endpoint(client: TestClient):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/relations/{relation_id}/revisions/{revision}/approve" in schema["paths"]
