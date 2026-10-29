"""Read model for the API: app JSON + read-only warehouse + a small SQLite for analyst actions."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache

import duckdb

STATUS_FOR_ACTION = {
    "start": "Investigating", "escalate": "Escalated", "freeze": "Frozen", "str": "STR filed",
    "false_positive": "False positive", "close": "Closed", "reopen": "Open",
}


class Store:
    def __init__(self, data_dir: str = "data"):
        self.dir = data_dir
        self.app = os.path.join(data_dir, "app")
        if not os.path.exists(os.path.join(self.app, "overview.json")):
            raise FileNotFoundError("app data not found - run `python -m ringbreaker build` first")
        self._wh = duckdb.connect(os.path.join(data_dir, "warehouse.duckdb"), read_only=True)
        self._wh_lock = threading.Lock()
        # analyst decisions live outside the rebuildable data folder (a mounted volume in Docker)
        self.state_dir = os.environ.get("RINGBREAKER_STATE_DIR", data_dir)
        os.makedirs(self.state_dir, exist_ok=True)
        self._db_path = os.path.join(self.state_dir, "analyst.db")
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY, case_id TEXT, action TEXT,
                          status TEXT, note TEXT, actor TEXT, at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS investigations (case_id TEXT PRIMARY KEY, report TEXT,
                          engine TEXT, steps TEXT, at TEXT)""")

    def _db(self):
        return sqlite3.connect(self._db_path)

    @lru_cache(maxsize=None)
    def _json(self, rel: str):
        with open(os.path.join(self.app, rel)) as f:
            return json.load(f)

    # ---- reads -------------------------------------------------------------------
    def overview(self):
        o = dict(self._json("overview.json"))
        st = self.statuses()
        o["status_counts"] = {}
        for c in self.cases():
            s = st.get(c["case_id"], {}).get("status", "Open")
            o["status_counts"][s] = o["status_counts"].get(s, 0) + 1
        return o

    def cases(self):
        st = self.statuses()
        out = []
        for c in self._json("cases.json"):
            c = dict(c)
            c["status"] = st.get(c["case_id"], {}).get("status", "Open")
            out.append(c)
        return out

    def case(self, case_id: str):
        path = os.path.join(self.app, "cases", f"{case_id}.json")
        if not os.path.basename(path).startswith("CASE-") or not os.path.exists(path):
            return None
        c = dict(self._json(f"cases/{case_id}.json"))
        c["status"] = self.statuses().get(case_id, {}).get("status", "Open")
        c["history"] = self.history(case_id)
        c["investigation"] = self.investigation(case_id)
        return c

    def account(self, account_id: str):
        if not account_id.isalnum():
            return None
        p = os.path.join(self.app, "accounts", f"{account_id}.json")
        return self._json(f"accounts/{account_id}.json") if os.path.exists(p) else None

    def alerts(self):
        return self._json("alerts.json")

    def model(self):
        m = dict(self._json("model.json"))
        with self._db() as db:
            rows = db.execute("SELECT status, COUNT(*) FROM (SELECT case_id, status FROM actions a WHERE id = "
                              "(SELECT MAX(id) FROM actions b WHERE b.case_id = a.case_id)) GROUP BY 1").fetchall()
        m["feedback"] = dict(rows)
        return m

    def network(self):
        return self._json("network.json")

    def pipeline(self):
        return self._json("pipeline.json")

    def query(self, sql: str):
        with self._wh_lock:
            return self._wh.execute(sql).df()

    # ---- analyst actions ------------------------------------------------------------
    def statuses(self):
        with self._db() as db:
            rows = db.execute("SELECT case_id, status, at FROM actions a WHERE id = "
                              "(SELECT MAX(id) FROM actions b WHERE b.case_id = a.case_id)").fetchall()
        return {r[0]: {"status": r[1], "at": r[2]} for r in rows}

    def history(self, case_id):
        with self._db() as db:
            rows = db.execute("SELECT action, status, note, actor, at FROM actions WHERE case_id=? ORDER BY id",
                              (case_id,)).fetchall()
        return [dict(zip(("action", "status", "note", "actor", "at"), r)) for r in rows]

    def act(self, case_id, action, note="", actor="analyst"):
        if action not in STATUS_FOR_ACTION:
            raise ValueError("unknown action")
        status = STATUS_FOR_ACTION[action]
        with self._db() as db:
            db.execute("INSERT INTO actions (case_id, action, status, note, actor, at) VALUES (?,?,?,?,?,?)",
                       (case_id, action, status, note[:1000], actor[:60], datetime.now(timezone.utc).isoformat()))
        return status

    def save_investigation(self, case_id, report, engine, steps):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO investigations VALUES (?,?,?,?,?)",
                       (case_id, json.dumps(report), engine, json.dumps(steps), datetime.now(timezone.utc).isoformat()))

    def investigation(self, case_id):
        with self._db() as db:
            r = db.execute("SELECT report, engine, steps, at FROM investigations WHERE case_id=?", (case_id,)).fetchone()
        if not r:
            return None
        return {"report": json.loads(r[0]), "engine": r[1], "steps": json.loads(r[2]), "at": r[3]}
