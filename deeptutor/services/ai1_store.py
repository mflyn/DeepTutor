"""Private AI-1 operation journal. No Provider calls, workers, or application startup.

The service layer validates the closed request/profile before accept, and supplies
a fresh policy check under the claim transaction. Opening this journal is NOT a
proof of storage continuity; the runtime must deny access after backup restore.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class OperationStore:
    """One connection per transaction; a committed claim is never reclaimed."""

    def __init__(self, path, *, initialize=False):
        self.path = Path(path).absolute()
        parent = self.path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("AI_STORE_UNSAFE")
        if parent.stat().st_mode & 0o077:
            raise ValueError("AI_STORE_UNSAFE")
        if initialize:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self._check_path()
        with self._connection() as db:
            if initialize:
                db.execute("PRAGMA journal_mode=DELETE")
                with db:
                    db.execute("""CREATE TABLE operations (
                        id TEXT PRIMARY KEY NOT NULL,
                        request_hash TEXT NOT NULL,
                        snapshot TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN
                          ('accepted','sending','succeeded','failed_not_sent','failed_sent','unknown')),
                        owner TEXT,
                        result TEXT,
                        error TEXT,
                        usage TEXT,
                        updated_at TEXT NOT NULL,
                        CHECK ((status IN ('accepted','failed_not_sent') AND owner IS NULL)
                            OR (status IN ('sending','succeeded','failed_sent','unknown') AND owner IS NOT NULL))
                    )""")
                directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            else:
                db.execute("SELECT id, snapshot, status, owner FROM operations LIMIT 0")

    def _check_path(self):
        parent = self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
            raise ValueError("AI_STORE_UNSAFE")
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("AI_STORE_UNSAFE")

    def check_ready(self, *, writable=False):
        with self._connection(readonly=not writable) as db:
            columns = db.execute("PRAGMA table_info(operations)").fetchall()
            if (set(row["name"] for row in columns) != {"id", "request_hash", "snapshot", "status", "owner",
                                                       "result", "error", "usage", "updated_at"}
                    or [(row["name"], row["pk"], row["notnull"]) for row in columns if row["pk"]] != [("id", 1, 1)]):
                raise ValueError("AI_STORE_UNSAFE")
            unique_id = False
            for index in db.execute("PRAGMA index_list(operations)"):
                if index["unique"] and not index["partial"]:
                    fields = db.execute("SELECT * FROM pragma_index_xinfo(?) WHERE key=1", (index["name"],)).fetchall()
                    unique_id |= [(row["name"], row["coll"]) for row in fields] == [("id", "BINARY")]
            if not unique_id:
                raise ValueError("AI_STORE_UNSAFE")
            if writable:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
    @contextmanager
    def _connection(self, *, readonly=False):
        self._check_path()
        db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                             uri=True, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _operation(row):
        if row is None:
            return None
        snapshot = json.loads(row["snapshot"])
        return {
            "remoteOperationId": row["id"], "requestHash": row["request_hash"],
            "modelProfileVersion": snapshot["modelProfileVersion"],
            "pricingProfileVersion": snapshot["pricingProfileVersion"],
            "sendNotAfter": snapshot["sendNotAfter"], "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] is not None else None,
            "error": row["error"],
            "usage": json.loads(row["usage"]) if row["usage"] is not None else None,
            "sendClaimed": row["owner"] is not None, "updatedAt": row["updated_at"],
        }

    def get(self, operation_id):
        with self._connection(readonly=True) as db:
            return self._operation(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def snapshot(self, operation_id):
        with self._connection(readonly=True) as db:
            row = db.execute("SELECT snapshot FROM operations WHERE id=?", (operation_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def accept(self, operation_id, validated_request):
        snapshot = _encode(validated_request)
        with self._write() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row:
                if row["request_hash"] != validated_request["requestHash"] or json.loads(row["snapshot"]) != validated_request:
                    raise ValueError("IDEMPOTENCY_KEY_REUSED")
                return self._operation(row), False
            db.execute("INSERT INTO operations(id,request_hash,snapshot,status,updated_at) VALUES(?,?,?,'accepted',?)",
                       (operation_id, validated_request["requestHash"], snapshot, _now()))
            return self._operation(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()), True

    def claim(self, operation_id, check_policy):
        with self._write() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None or row["status"] != "accepted":
                return None
            # The clock/profile check is inside the write lock, not before waiting for it.
            failure = check_policy(json.loads(row["snapshot"]))
            if failure is not None:
                if failure not in {"VALIDATION_ERROR", "PROFILE_UNAVAILABLE", "PREVIEW_STALE"}:
                    raise ValueError("AI_INVALID_POLICY_RESULT")
                db.execute("UPDATE operations SET status='failed_not_sent',error=?,updated_at=? WHERE id=? AND status='accepted'",
                           (failure, _now(), operation_id))
                return None
            owner = str(uuid4())
            db.execute("UPDATE operations SET status='sending',owner=?,updated_at=? WHERE id=? AND status='accepted'",
                       (owner, _now(), operation_id))
            return owner

    def reject(self, operation_id, failure):
        if failure not in {"VALIDATION_ERROR", "PROFILE_UNAVAILABLE", "PREVIEW_STALE"}:
            raise ValueError("AI_INVALID_POLICY_RESULT")
        with self._write() as db:
            cursor = db.execute("""UPDATE operations SET status='failed_not_sent',error=?,updated_at=?
                WHERE id=? AND status='accepted' AND owner IS NULL""", (failure, _now(), operation_id))
            return cursor.rowcount == 1

    def finish(self, operation_id, owner, status, *, result=None, usage=None):
        if status not in {"succeeded", "failed_sent", "unknown"}:
            raise ValueError("AI_INVALID_TERMINAL")
        if (status == "succeeded") != (result is not None) or (status == "unknown" and usage is not None):
            raise ValueError("AI_INVALID_TERMINAL")
        error = {"succeeded": None, "failed_sent": "PROVIDER_FAILED", "unknown": "RESULT_UNKNOWN"}[status]
        with self._write() as db:
            cursor = db.execute("""UPDATE operations SET status=?,result=?,error=?,usage=?,updated_at=?
                WHERE id=? AND owner=? AND status IN ('sending','unknown')""",
                (status, _encode(result) if result is not None else None, error,
                 _encode(usage) if usage is not None else None, _now(), operation_id, owner))
            return cursor.rowcount == 1
