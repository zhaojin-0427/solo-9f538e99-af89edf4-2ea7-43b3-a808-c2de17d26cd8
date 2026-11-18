"""SQLite-backed revision storage.

Durability and concurrency contract:

* the database runs in WAL mode with ``synchronous=FULL``: every commit
  is fsync-ed and survives a process restart; readers never block on a
  writer and always observe a whole committed revision (or none of it);
* every write happens inside one ``BEGIN IMMEDIATE`` transaction which
  both serialises writers (across threads and processes) and makes the
  "revision must be strictly increasing" check atomic with the insert —
  a failed transaction rolls back completely, so storage errors can
  never leave a half revision;
* the pre-publish quality check (IoU vs the last served polygon) runs
  *inside* the same transaction as the insert, so the baseline the
  receipt reports is exactly the committed serving state the new
  revision was compared against — an interleaved commit can never make
  the receipt point at a stale baseline;
* approval ("is this pending revision still the latest?" + status flip)
  is likewise a single transaction, so a late approval can never
  overwrite a newer committed revision;
* multi-row reads (latest + serving, or one revision + serving) run in
  a single read transaction, pinning one WAL snapshot per request;
* raw input and computed state are stored together, therefore a reader
  of any revision sees input, geometry and status from the same commit.

Schema migrations: databases created before the quality-check feature
(lacking ``iou_ratio`` and the extended status CHECK) are rebuilt in
place on open; all existing rows are preserved and remain readable.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    relation_id          INTEGER NOT NULL,
    revision             INTEGER NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('valid', 'invalid', 'pending', 'approved')),
    input_json           TEXT    NOT NULL,
    geometry_json        TEXT,
    error_code           TEXT,
    error_message        TEXT,
    error_way_ids_json   TEXT,
    iou_ratio            REAL,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (relation_id, revision)
);
"""

# Statuses that make a revision the currently served polygon.
SERVING_STATUSES = ("valid", "approved")

# No in-process lock is needed on purpose: SQLite's own BEGIN IMMEDIATE
# write lock serialises writers (across threads AND processes, e.g.
# multiple uvicorn workers), while WAL lets readers proceed concurrently
# without blocking a commit. A 30 s busy timeout makes contending writers
# queue instead of failing.
BUSY_TIMEOUT_SECONDS = 30.0
LOCK_RETRIES = 5


class Database:
    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False: one short-lived connection per request,
        # shared nothing between threads.
        self._connect_kwargs = dict(
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # manual transaction control
            check_same_thread=False,
        )
        self.init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, **self._connect_kwargs)
        try:
            conn.row_factory = sqlite3.Row
            # Persistent WAL keeps working across restarted connections.
            # PRAGMAs must run outside an explicit transaction.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection) -> None:
        attempt = 0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                # Another writer holds the lock; busy_timeout usually
                # makes the execute block, but keep an explicit retry
                # for the edge case where it returns immediately.
                if "locked" in str(exc).lower() and attempt < LOCK_RETRIES:
                    attempt += 1
                    time.sleep(0.05 * attempt)
                    continue
                raise

    def init_schema(self) -> None:
        with self._connect() as conn:
            self._begin_immediate(conn)
            try:
                conn.execute(SCHEMA)
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(revisions)")
                }
                if "iou_ratio" not in columns:
                    # Legacy table (pre quality-check): its CHECK constraint
                    # only allows valid/invalid and cannot be altered, so
                    # rebuild the table and carry every row over.
                    conn.execute("ALTER TABLE revisions RENAME TO revisions_legacy")
                    conn.execute(SCHEMA)
                    conn.execute(
                        """
                        INSERT INTO revisions (
                            relation_id, revision, status, input_json, geometry_json,
                            error_code, error_message, error_way_ids_json,
                            iou_ratio, created_at
                        )
                        SELECT relation_id, revision, status, input_json, geometry_json,
                               error_code, error_message, error_way_ids_json,
                               NULL, created_at
                        FROM revisions_legacy
                        """
                    )
                    conn.execute("DROP TABLE revisions_legacy")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ writes
    def save_revision(
        self,
        relation_id: int,
        revision: int,
        status: str,
        input_payload: dict,
        geometry: dict | None,
        error: dict | None,
        iou_decider: Callable[[dict, dict], tuple[str, float]] | None = None,
    ) -> dict:
        """Atomically persist one full revision and return its receipt.

        ``status`` is the geometry-level verdict (``valid``/``invalid``).
        When the revision is geometrically valid and an ``iou_decider``
        is given, the decider is invoked *inside the write transaction*
        with ``(new_geometry, serving_geometry)`` and may downgrade the
        stored status (e.g. to ``pending``); its IoU value is persisted
        alongside.  The serving baseline is read in the same transaction,
        so the receipt always reflects the committed state the decision
        was made against.

        The returned receipt carries the final ``status``, the
        ``iou_ratio`` (or None) and ``serving_revision`` — the revision
        whose polygon is served after this commit.

        Raises :class:`RevisionConflict` when ``revision`` is not strictly
        greater than every existing revision of the relation.
        """
        with self._connect() as conn:
            self._begin_immediate(conn)
            try:
                row = conn.execute(
                    "SELECT MAX(revision) AS max_rev FROM revisions WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                max_rev = row["max_rev"]
                if max_rev is not None and revision <= max_rev:
                    conn.rollback()
                    raise RevisionConflict(
                        f"revision {revision} is not greater than the latest revision {max_rev}",
                        latest=max_rev,
                    )

                serving = self._last_serving_row(conn, relation_id)
                final_status = status
                iou_ratio = None
                if status == "valid" and iou_decider is not None and serving is not None:
                    final_status, iou_ratio = iou_decider(geometry, serving["geometry"])

                conn.execute(
                    """
                    INSERT INTO revisions (
                        relation_id, revision, status, input_json, geometry_json,
                        error_code, error_message, error_way_ids_json, iou_ratio
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relation_id,
                        revision,
                        final_status,
                        json.dumps(input_payload, separators=(",", ":"), ensure_ascii=False),
                        json.dumps(geometry, separators=(",", ":")) if geometry else None,
                        error["code"] if error else None,
                        error["message"] if error else None,
                        json.dumps(error["way_ids"]) if error else None,
                        iou_ratio,
                    ),
                )
                if final_status in SERVING_STATUSES:
                    serving_revision = revision
                else:
                    serving_revision = serving["revision"] if serving else None
                conn.commit()
                return {
                    "status": final_status,
                    "iou_ratio": iou_ratio,
                    "serving_revision": serving_revision,
                }
            except RevisionConflict:
                raise
            except Exception:
                conn.rollback()
                raise

    def approve_revision(self, relation_id: int, revision: int) -> dict:
        """Atomically approve a pending revision, making it the served face.

        The "still the latest revision?" check and the status flip commit
        together, so a late approval racing a newer upload can never
        resurrect a superseded polygon.

        Returns a receipt with the updated ``row`` and the
        ``serving_revision`` after the commit.

        Raises :class:`RevisionNotFound` when the revision does not exist
        and :class:`ApprovalConflict` when it is not pending or no longer
        the latest revision.
        """
        with self._connect() as conn:
            self._begin_immediate(conn)
            try:
                row = conn.execute(
                    "SELECT * FROM revisions WHERE relation_id = ? AND revision = ?",
                    (relation_id, revision),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise RevisionNotFound(
                        f"revision {revision} does not exist for relation {relation_id}"
                    )
                latest_row = conn.execute(
                    "SELECT MAX(revision) AS max_rev FROM revisions WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                latest = latest_row["max_rev"]
                if row["status"] != "pending":
                    conn.rollback()
                    raise ApprovalConflict(
                        code="not_pending",
                        message=(
                            f"revision {revision} has status '{row['status']}'; "
                            "only pending revisions can be approved"
                        ),
                        latest=latest,
                        status=row["status"],
                    )
                if latest != revision:
                    conn.rollback()
                    raise ApprovalConflict(
                        code="stale_revision",
                        message=(
                            f"revision {revision} is no longer the latest revision "
                            f"({latest}); approving it would serve a superseded polygon"
                        ),
                        latest=latest,
                        status=row["status"],
                    )
                conn.execute(
                    "UPDATE revisions SET status = 'approved' "
                    "WHERE relation_id = ? AND revision = ?",
                    (relation_id, revision),
                )
                updated = conn.execute(
                    "SELECT * FROM revisions WHERE relation_id = ? AND revision = ?",
                    (relation_id, revision),
                ).fetchone()
                conn.commit()
                return {
                    "row": self._row_to_dict(updated),
                    "serving_revision": revision,
                }
            except (RevisionNotFound, ApprovalConflict):
                raise
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------- reads
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        error = None
        if row["error_code"]:
            error = {
                "code": row["error_code"],
                "message": row["error_message"],
                "way_ids": json.loads(row["error_way_ids_json"] or "[]"),
            }
        return {
            "relation_id": row["relation_id"],
            "revision": row["revision"],
            "status": row["status"],
            "input": json.loads(row["input_json"]),
            "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None,
            "error": error,
            "iou_ratio": row["iou_ratio"],
            "created_at": row["created_at"],
        }

    def _last_serving_row(self, conn: sqlite3.Connection, relation_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM revisions WHERE relation_id = ? AND status IN ('valid', 'approved') "
            "ORDER BY revision DESC LIMIT 1",
            (relation_id,),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_revision(self, relation_id: int, revision: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? AND revision = ?",
                (relation_id, revision),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_latest_revision(self, relation_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (relation_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_last_valid(self, relation_id: int) -> dict | None:
        """Most recent serving revision (status valid or approved)."""
        with self._connect() as conn:
            return self._last_serving_row(conn, relation_id)

    # Alias expressing the serving-face semantics at call sites.
    get_last_serving = get_last_valid

    def get_current_view(self, relation_id: int) -> dict:
        """Latest revision and serving face from one committed snapshot.

        Both SELECTs run inside a single read transaction, so a writer
        committing between them cannot make the two rows disagree.
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                latest_row = conn.execute(
                    "SELECT * FROM revisions WHERE relation_id = ? "
                    "ORDER BY revision DESC LIMIT 1",
                    (relation_id,),
                ).fetchone()
                serving = self._last_serving_row(conn, relation_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "latest": self._row_to_dict(latest_row) if latest_row else None,
            "serving": serving,
        }

    def get_revision_view(self, relation_id: int, revision: int) -> dict:
        """One stored revision plus the serving face, from one snapshot."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    "SELECT * FROM revisions WHERE relation_id = ? AND revision = ?",
                    (relation_id, revision),
                ).fetchone()
                serving = self._last_serving_row(conn, relation_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "row": self._row_to_dict(row) if row else None,
            "serving": serving,
        }

    def list_revisions(self, relation_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT revision, status FROM revisions WHERE relation_id = ? "
                "ORDER BY revision",
                (relation_id,),
            ).fetchall()
            return [dict(r) for r in rows]


class RevisionConflict(Exception):
    def __init__(self, message: str, latest: int):
        super().__init__(message)
        self.latest = latest


class RevisionNotFound(Exception):
    pass


class ApprovalConflict(Exception):
    """Approval rejected because of the committed state (HTTP 409)."""

    def __init__(self, code: str, message: str, latest: int, status: str):
        super().__init__(message)
        self.code = code
        self.latest = latest
        self.status = status
