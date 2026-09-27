"""HTTP 接口冒烟测试：建链、回绕裁决、过期、重复、回执幂等与冲突。"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEIVER_DB", str(tmp_path / "api.db"))
    from app import main
    importlib.reload(main)  # 用临时 DB 重新构造应用
    with TestClient(main.app) as c:
        yield c


def post_frame(client, link, raw, receipt, payload=None):
    return client.post(
        f"/api/links/{link}/frames",
        json={"raw_count": raw, "receipt_id": receipt, "payload": payload},
    )


def test_health_and_link_lifecycle(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/api/links", json={"link_id": "mars"})
    assert r.status_code == 201
    assert r.json()["highest"] == 0 and r.json()["accepted_positions"] == []
    assert client.post("/api/links", json={"link_id": "mars"}).status_code == 409
    assert client.get("/api/links/mars").status_code == 200
    assert client.get("/api/links/missing").status_code == 404
    assert client.get("/").status_code == 200


def test_wrap_duplicate_stale_over_http(client):
    client.post("/api/links", json={"link_id": "L"})
    M = 1 << 32

    r = post_frame(client, "L", M - 2, "a", {"k": 1})
    assert r.status_code == 200 and r.json()["verdict"] == "accepted"

    r = post_frame(client, "L", M - 1, "b", {"k": 2})
    assert r.json()["extended_seq"] == M - 1

    r = post_frame(client, "L", 0, "c", {"k": 3})  # 回绕后的 0
    body = r.json()
    assert body["verdict"] == "accepted" and body["extended_seq"] == M
    assert body["highest"] == M

    r = post_frame(client, "L", 0, "d", {"k": 4})  # 新回执，同位置
    assert r.json()["verdict"] == "duplicate"

    r = post_frame(client, "L", 0, "c", {"k": 3})  # 相同回执+相同载荷
    body = r.json()
    assert body["verdict"] == "accepted" and body["replayed"] is True

    r = post_frame(client, "L", 0, "c", {"k": 9})  # 复用回执但改载荷
    assert r.status_code == 409

    r = post_frame(client, "L", M - 1, "b", {"k": 2})
    assert r.json()["replayed"] is True and r.json()["verdict"] == "accepted"

    # 推进 64 格以上后，旧帧为过期。
    for i in range(1, 70):
        post_frame(client, "L", i, f"f{i}", {"k": i})
    r = post_frame(client, "L", M - 2, "late", {"k": "z"})
    assert r.json()["verdict"] == "stale"

    snapshot = client.get("/api/links/L").json()
    assert snapshot["highest"] == M + 69
    assert len(snapshot["accepted_positions"]) == 64
    assert snapshot["accepted_positions"][0] == M + 69


def test_receipt_cannot_cross_link(client):
    client.post("/api/links", json={"link_id": "L1"})
    client.post("/api/links", json={"link_id": "L2"})
    post_frame(client, "L1", 5, "shared", {"x": 1})
    r = post_frame(client, "L2", 5, "shared", {"x": 1})
    assert r.status_code == 409  # 复用标识却改变链路
    assert post_frame(client, "L2", 5, "other", {"x": 1}).status_code == 200


def test_invalid_count_rejected(client):
    client.post("/api/links", json={"link_id": "L"})
    r = client.post(
        "/api/links/L/frames",
        json={"raw_count": 2**32, "receipt_id": "x", "payload": None},
    )
    assert r.status_code == 422
    assert post_frame(client, "ghost", 1, "x").status_code == 404
