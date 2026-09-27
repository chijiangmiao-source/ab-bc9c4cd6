"""SQLite 持久化：链路、滑动窗口状态、稳定回执记录。

每次到达帧的全部推导（回执查重、窗口裁决、状态落盘）都在同一个
``BEGIN IMMEDIATE`` 事务里完成：写锁串行化并发到达，重启后状态也只
来自同一批窗口与回执记录，因此裁决始终一致。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import window

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    link_id    TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS window_state (
    link_id     TEXT PRIMARY KEY REFERENCES links(link_id),
    initialized INTEGER NOT NULL,
    highest     TEXT NOT NULL,
    bitmap      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id   TEXT PRIMARY KEY,
    link_id      TEXT NOT NULL REFERENCES links(link_id),
    raw_count    INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    extended_seq TEXT,
    created_at   REAL NOT NULL
);
"""


class ReceiptConflict(Exception):
    """回执标识被复用但链路、计数或载荷与首次不一致。"""


def payload_digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()

    def connect(self) -> sqlite3.Connection:
        # 每线程一条连接；写操作另以 BEGIN IMMEDIATE 获取保留锁。
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._tls.conn = conn
        return conn

    def create_link(self, link_id: str, now: float) -> bool:
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            exists = conn.execute(
                "SELECT 1 FROM links WHERE link_id=?", (link_id,)
            ).fetchone()
            if exists:
                conn.execute("ROLLBACK")
                return False
            conn.execute("INSERT INTO links(link_id, created_at) VALUES(?,?)", (link_id, now))
            conn.execute(
                "INSERT INTO window_state(link_id, initialized, highest, bitmap)"
                " VALUES(?,0,'0','0')",
                (link_id,),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def link_exists(self, link_id: str) -> bool:
        conn = self.connect()
        return conn.execute("SELECT 1 FROM links WHERE link_id=?", (link_id,)).fetchone() is not None

    def get_state(self, link_id: str) -> window.State:
        conn = self.connect()
        row = conn.execute(
            "SELECT initialized, highest, bitmap FROM window_state WHERE link_id=?",
            (link_id,),
        ).fetchone()
        if row is None:
            raise KeyError(link_id)
        # highest/bitmap 是无符号 64 位量，以十六进制文本存储，
        # 避免 SQLite 有符号 INTEGER 在 2**63 以上溢出。
        return window.State(bool(row[0]), int(row[1], 16), int(row[2], 16))

    def ingest(
        self,
        link_id: str,
        raw_count: int,
        receipt_id: str,
        payload: Any,
        now: float,
    ) -> dict[str, Any]:
        """在一个 SQLite 事务中处理一次帧到达。

        返回裁决字典；``replayed=True`` 表示这是回执重传，裁决完全取自
        首次记录。
        """
        digest = payload_digest(payload)
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute(
                "SELECT 1 FROM links WHERE link_id=?", (link_id,)
            ).fetchone():
                raise KeyError(link_id)

            prior = conn.execute(
                "SELECT link_id, raw_count, payload_hash, verdict, extended_seq"
                " FROM receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()

            if prior is not None:
                old_link, old_count, old_hash, verdict, extended_hex = prior
                if old_link != link_id or old_count != raw_count or old_hash != digest:
                    raise ReceiptConflict(receipt_id)
                state = self.get_state(link_id)
                conn.execute("ROLLBACK")
                return {
                    "verdict": verdict,
                    "extended_seq": int(extended_hex, 16) if extended_hex is not None else None,
                    "replayed": True,
                    "state": state,
                }

            state = self.get_state(link_id)
            verdict, new_state, extended = window.decide(state, raw_count)

            conn.execute(
                "UPDATE window_state SET initialized=?, highest=?, bitmap=? WHERE link_id=?",
                (
                    int(new_state.initialized),
                    f"{new_state.highest:x}",
                    f"{new_state.bitmap:x}",
                    link_id,
                ),
            )
            conn.execute(
                "INSERT INTO receipts(receipt_id, link_id, raw_count, payload_hash,"
                " verdict, extended_seq, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    link_id,
                    raw_count,
                    digest,
                    verdict,
                    f"{extended:x}" if extended is not None else None,
                    now,
                ),
            )
            conn.execute("COMMIT")
            return {
                "verdict": verdict,
                "extended_seq": extended,
                "replayed": False,
                "state": new_state,
            }
        except BaseException:
            conn.execute("ROLLBACK")
            raise
