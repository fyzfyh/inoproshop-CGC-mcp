from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from pathlib import Path

from .common import DomainError, atomic_json, canonical, digest, uid, utc

TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "CANCELLED", "RESTORED", "OUTCOME_UNKNOWN", "NEEDS_RECOVERY"}


class Store:
    """Single-controller durable ledger. Artifacts are immutable, metadata transactional."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "ledger.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS kv(kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id));
          CREATE TABLE IF NOT EXISTS artifacts(kind TEXT,id TEXT PRIMARY KEY,project_id TEXT,path TEXT,sha TEXT);
          CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,project_id TEXT,tool TEXT,idem TEXT,
            args_hash TEXT,state TEXT,value TEXT, UNIQUE(project_id,tool,idem));
          CREATE TABLE IF NOT EXISTS events(cursor INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT,value TEXT);
        """)
        self.db.commit()
        token_path = self.root / "token"
        if not token_path.exists():
            try:
                with token_path.open("x", encoding="ascii") as stream:
                    stream.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
        self.token = token_path.read_text(encoding="ascii").strip()
        if len(self.token) < 24:
            raise DomainError("INVALID_CONFIGURATION", "Local service token is missing or too short.")
        with self.lock:
            rows = self.db.execute("SELECT value FROM operations").fetchall()
        for row in rows:
            op = json.loads(row[0])
            if op["state"] not in TERMINAL:
                before_write = op["state"] in ("QUEUED", "CHECKING", "CHECKPOINTED") or (
                    op["tool"] == "ino_apply_change" and op["state"] == "COMPILING" and not op.get("checkpoint_id"))
                read_only = op["tool"] in ("ino_compile", "context_index_snapshot")
                state = "CANCELLED" if before_write else "FAILED" if read_only else "OUTCOME_UNKNOWN"
                self.update_operation(op["operation_id"], state=state,
                                      error={"code": "SERVICE_RESTARTED", "message": "Controller restarted; no write was replayed."})

    def close(self):
        with self.lock:
            self.db.close()

    def put(self, kind, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?)", (kind, key, canonical(value)))

    def get(self, kind, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, kind):
        with self.lock:
            rows = self.db.execute("SELECT value FROM kv WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def artifact(self, kind, project_id, value, artifact_id=None):
        artifact_id = artifact_id or uid(kind)
        path = self.root / "artifacts" / kind / (artifact_id + ".json")
        with self.lock:
            if self.db.execute("SELECT 1 FROM artifacts WHERE id=?", (artifact_id,)).fetchone():
                raise DomainError("IMMUTABLE_CONFLICT", "Artifact identifiers cannot be reused.")
            atomic_json(path, value)
            with self.db:
                self.db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)",
                                (kind, artifact_id, project_id, str(path), digest(value)))
        return artifact_id

    def read_artifact(self, artifact_id, project_id=None, kind=None):
        with self.lock:
            row = self.db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row or (project_id is not None and row["project_id"] != project_id) or (kind and row["kind"] != kind):
            raise DomainError("ARTIFACT_NOT_FOUND", "Artifact does not belong to this project or type.")
        value = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
        if digest(value) != row["sha"]:
            raise DomainError("ARTIFACT_CORRUPTED", "Artifact integrity check failed.")
        return value

    def create_operation(self, project_id, tool, idem, args):
        if not isinstance(idem, str) or not idem.strip() or len(idem) > 200:
            raise DomainError("INVALID_ARGUMENT", "A nonempty idempotency_key is required.")
        args_hash = digest(args)
        with self.lock:
            row = self.db.execute("SELECT args_hash,value FROM operations WHERE project_id=? AND tool=? AND idem=?",
                                  (project_id, tool, idem)).fetchone()
            if row:
                if row["args_hash"] != args_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "The same idempotency key has different arguments.")
                return json.loads(row["value"]), False
            op = {"operation_id": uid("op"), "project_id": project_id, "tool": tool,
                  "state": "QUEUED", "created_at": utc(), "updated_at": utc(),
                  "cancel_requested": False, "result": None, "error": None,
                  "checkpoint_id": None, "stages": [{"state": "QUEUED", "at": utc()}]}
            with self.db:
                self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                                (op["operation_id"], project_id, tool, idem, args_hash, op["state"], canonical(op)))
            return op, True

    def operation(self, op_id, project_id=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM operations WHERE id=?", (op_id,)).fetchone()
        if not row:
            raise DomainError("OPERATION_NOT_FOUND", "Unknown operation.")
        op = json.loads(row[0])
        if project_id is not None and op["project_id"] != project_id:
            raise DomainError("OPERATION_NOT_FOUND", "Operation belongs to a different project.")
        return op

    def update_operation(self, op_id, **fields):
        with self.lock:
            op = self.operation(op_id)
            if "state" in fields and fields["state"] != op["state"]:
                op["stages"].append({"state": fields["state"], "at": utc()})
            op.update(fields, updated_at=utc())
            with self.db:
                self.db.execute("UPDATE operations SET state=?,value=? WHERE id=?", (op["state"], canonical(op), op_id))
            return op

    def unresolved(self, project_id):
        with self.lock:
            rows = self.db.execute("SELECT value FROM operations WHERE project_id=? AND state IN ('OUTCOME_UNKNOWN','NEEDS_RECOVERY') "
                                   "AND tool IN ('ino_apply_change','ino_restore_checkpoint','ino_save_copy','ino_open_project','ino_create_project')",
                                   (project_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def event(self, project_id, value):
        with self.lock, self.db:
            cursor = self.db.execute("INSERT INTO events(project_id,value) VALUES(?,?)",
                                     (project_id, canonical(dict(value, at=utc())))).lastrowid
        return cursor

    def events(self, project_id, after=0, limit=100):
        with self.lock:
            latest = self.db.execute("SELECT COALESCE(MAX(cursor),0) FROM events WHERE project_id=?", (project_id,)).fetchone()[0]
            rows = self.db.execute("SELECT cursor,value FROM events WHERE project_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                                   (project_id, after, limit)).fetchall()
        if after < 0 or (after > latest and after != 0):
            raise DomainError("CURSOR_INVALID", "Resynchronize with a new snapshot.")
        return {"events": [dict(json.loads(r["value"]), cursor=r["cursor"]) for r in rows],
                "cursor": rows[-1]["cursor"] if rows else after}
