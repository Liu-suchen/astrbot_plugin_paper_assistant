from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

from .models import PaperError
from .settings import settings


class Store:
    def __init__(self, path: Path, config=None):
        self.config = settings(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, origin TEXT NOT NULL,
                action TEXT NOT NULL, paper TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS choices(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, action TEXT NOT NULL,
                papers TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bindings(
                owner TEXT NOT NULL, message TEXT NOT NULL, paper TEXT NOT NULL,
                PRIMARY KEY(owner,message)
            );
            CREATE TABLE IF NOT EXISTS cache(
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)
        self.db.execute("UPDATE jobs SET state='queued' WHERE state='running'")
        self.db.commit()

    def choice(self, owner: str, action: str, papers: list[dict]) -> str:
        key = secrets.token_hex(4)
        self.db.execute("INSERT INTO choices VALUES(?,?,?,?,?)",
                        (key, owner, action, json.dumps(papers), time.time() + self.config.get("choice_ttl")))
        self.db.commit()
        return key

    def pick(self, owner: str, key: str, index: int) -> tuple[str, dict]:
        row = self.db.execute(
            "SELECT * FROM choices WHERE id=? AND owner=? AND expires>?",
            (key, owner, time.time()),
        ).fetchone()
        if not row:
            raise PaperError("候选请求不存在或已过期，请重新查询。")
        papers = json.loads(row["papers"])
        if index < 1 or index > len(papers):
            raise PaperError("候选序号不正确。")
        return row["action"], papers[index - 1]

    def enqueue(self, owner: str, origin: str, action: str, paper: dict) -> tuple[str, bool]:
        encoded = json.dumps(paper, sort_keys=True)
        row = self.db.execute(
            "SELECT id FROM jobs WHERE owner=? AND action=? AND paper=? "
            "AND state IN ('queued','running')", (owner, action, encoded),
        ).fetchone()
        if row:
            return row["id"], False
        key = secrets.token_hex(5)
        self.db.execute(
            "INSERT INTO jobs(id,owner,origin,action,paper,state,created) VALUES(?,?,?,?,?,'queued',?)",
            (key, owner, origin, action, encoded, time.time()),
        )
        self.db.commit()
        return key, True

    def job(self, key: str, owner: str | None = None) -> dict | None:
        sql = "SELECT * FROM jobs WHERE id=?"
        args = [key]
        if owner is not None:
            sql += " AND owner=?"
            args.append(owner)
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def queued(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY created"
        )]

    def undelivered(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM jobs WHERE state IN ('done','failed') AND delivered=0 AND attempts<?",
            (self.config.get("delivery_attempts"),),
        )]

    def state(self, key: str, state: str, result: dict | None = None):
        if result is None:
            self.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, key))
        else:
            self.db.execute("UPDATE jobs SET state=?,result=? WHERE id=?",
                            (state, json.dumps(result), key))
        self.db.commit()

    def delivery(self, key: str, success: bool):
        self.db.execute("UPDATE jobs SET attempts=attempts+1,delivered=? WHERE id=?",
                        (int(success), key))
        self.db.commit()

    def bind(self, owner: str, message: str, paper: dict):
        self.db.execute("INSERT OR REPLACE INTO bindings VALUES(?,?,?)",
                        (owner, message, json.dumps(paper)))
        self.db.commit()

    def binding(self, owner: str, message: str) -> dict | None:
        row = self.db.execute("SELECT paper FROM bindings WHERE owner=? AND message=?",
                              (owner, message)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, key: str):
        row = self.db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (key, json.dumps(value)))
        self.db.commit()

    def stable_key(self, identity: str) -> str:
        return hashlib.sha256(identity.encode()).hexdigest()[:8].upper().translate(
            str.maketrans("01", "GH")
        )

    def close(self):
        self.db.close()
