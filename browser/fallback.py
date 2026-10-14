"""Persistent, shared rolling-hour budget for non-identity fallback jobs."""

import math
import os
import sqlite3
import time
from contextlib import closing

EXIT_BUDGET = 9
WINDOW_S = 3600


class FallbackBudget:
    def __init__(self, path):
        self.path = path
        self.limit = int(os.environ.get("GBROWSER_FALLBACK_PER_HOUR", "20"))
        if self.limit < 1:
            raise ValueError("GBROWSER_FALLBACK_PER_HOUR must be a positive integer")

    def _connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL,
            mode TEXT NOT NULL, holder TEXT NOT NULL,
            bytes INTEGER NOT NULL DEFAULT 0, ok INTEGER
        )""")
        return db

    def _status(self, db, now):
        jobs = [dict(row) for row in db.execute(
            "SELECT * FROM jobs WHERE started_at > ? ORDER BY started_at, id",
            (now - WINDOW_S,),
        )]
        retry_at = jobs[-self.limit]["started_at"] + WINDOW_S if len(jobs) >= self.limit else None
        return {"limit": self.limit, "used": len(jobs),
                "remaining": max(0, self.limit - len(jobs)),
                "retry_at": retry_at, "jobs": jobs}

    def reserve(self, mode, holder):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("DELETE FROM jobs WHERE started_at <= ?", (now - WINDOW_S,))
            status = self._status(db, now)
            if not status["remaining"]:
                retry_at = status["retry_at"]
                when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(math.ceil(retry_at)))
                return {"ok": False, "exit_code": EXIT_BUDGET,
                        "error": f"fallback budget exhausted ({self.limit} jobs/hour); budget frees at {when}",
                        "retry_at": retry_at, "retry_after_s": math.ceil(retry_at - now)}
            row = db.execute("INSERT INTO jobs (started_at, mode, holder) VALUES (?, ?, ?)",
                             (now, mode, holder))
            return {"ok": True, "job_id": row.lastrowid}

    def finish(self, job_id, bytes_used, ok):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE jobs SET bytes = ?, ok = ? WHERE id = ?",
                       (bytes_used, ok, job_id))

    def status(self):
        with closing(self._connect()) as db:
            return self._status(db, time.time())
