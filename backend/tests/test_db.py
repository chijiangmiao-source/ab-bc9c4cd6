"""持久化与事务一致性测试：回执重放、复用冲突、重启恢复、并发到达。"""

import threading
from pathlib import Path

import pytest

from app import window
from app.db import Database, ReceiptConflict


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "rx.db")
    database.create_link("link-A", 0.0)
    return database


def payload(**overrides):
    body = {"telemetry": 1, "note": "stable"}
    body.update(overrides)
    return body


def test_same_receipt_and_payload_replays_first_verdict(db: Database):
    first = db.ingest("link-A", 42, "r-1", payload(), 1.0)
    assert first["verdict"] == window.ACCEPTED and first["replayed"] is False

    # 相同回执标识 + 完全相同载荷的重传，裁决取自首次记录。
    replay = db.ingest("link-A", 42, "r-1", payload(), 2.0)
    assert replay["verdict"] == window.ACCEPTED
    assert replay["replayed"] is True

    # 即使原序号已因窗口推进而「过期」，重传仍返回首次裁决。
    db.ingest("link-A", 200, "r-2", payload(x=2), 3.0)
    stale_looking = db.ingest("link-A", 42, "r-1", payload(), 4.0)
    assert stale_looking["verdict"] == window.ACCEPTED
    assert stale_looking["replayed"] is True


def test_receipt_reuse_changed_payload_rejected(db: Database):
    db.ingest("link-A", 42, "r-1", payload(), 1.0)
    with pytest.raises(ReceiptConflict):
        db.ingest("link-A", 42, "r-1", payload(note="tampered"), 2.0)
    with pytest.raises(ReceiptConflict):
        db.ingest("link-A", 43, "r-1", payload(), 2.0)  # 改变计数

    db.create_link("link-B", 0.0)
    with pytest.raises(ReceiptConflict):
        db.ingest("link-B", 42, "r-1", payload(), 2.0)  # 改变链路


def test_conflict_does_not_mutate_state(db: Database):
    db.ingest("link-A", 42, "r-1", payload(), 1.0)
    with pytest.raises(ReceiptConflict):
        db.ingest("link-A", 43, "r-1", payload(), 2.0)
    state = db.get_state("link-A")
    assert state.highest == 42 and state.bitmap == 0b1


def test_restart_rebuilds_consistent_state(tmp_path: Path):
    path = tmp_path / "rx.db"
    db = Database(path)
    db.create_link("link-A", 0.0)
    for raw, rid in [(0xFFFFFFFE, "a"), (0xFFFFFFFF, "b"), (0, "c"), (5, "d")]:
        db.ingest("link-A", raw, rid, payload(n=raw), 1.0)
    db.ingest("link-A", 2, "e", payload(n=2), 1.0)

    # 服务重启：同一 SQLite 文件重新打开，窗口与回执记录均在。
    del db
    db2 = Database(path)
    state = db2.get_state("link-A")
    assert state.highest == (1 << 32) + 5
    # 乱序的旧帧在重启后仍是重复，而不会被当作新帧。
    again = db2.ingest("link-A", 0xFFFFFFFF, "b", payload(n=0xFFFFFFFF), 2.0)
    assert again["verdict"] == window.ACCEPTED and again["replayed"] is True
    dup = db2.ingest("link-A", 0xFFFFFFFF, "fresh-id", payload(n=9), 2.0)
    assert dup["verdict"] == window.DUPLICATE


def test_concurrent_arrivals_each_counted_once(tmp_path: Path):
    path = tmp_path / "rx.db"
    db = Database(path)
    db.create_link("L", 0.0)
    errors: list[Exception] = []

    # 100 个线程并发投递计数 1..100，各自使用不同回执标识。
    def worker(raw: int):
        try:
            db.ingest("L", raw, f"rcpt-{raw}", payload(n=raw), 1.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 101)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    state = db.get_state("L")
    assert state.highest == 100
    assert len(window.positions(state)) == 64  # 位图只保留最近 64 个位置

    # 100 个不同回执各只产生一条首次记录。
    count = db.connect().execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    assert count == 100

    # 计数 100 在任何到达顺序下都高于其它帧，首次裁决必为接受；
    # 并发重传（相同回执标识）必须全部得到首次裁决。
    accepted = []

    def replay(_):
        r = db.ingest("L", 100, "rcpt-100", payload(n=100), 2.0)
        accepted.append((r["verdict"], r["replayed"]))

    threads = [threading.Thread(target=replay, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert accepted == [(window.ACCEPTED, True)] * 20
