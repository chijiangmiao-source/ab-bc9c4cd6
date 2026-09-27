"""verify 服务使用的实时 HTTP 冒烟脚本。

在已启动的 web 容器上覆盖：建链、回绕前后乱序各接受一次、重复、
过期、相同回执重放、复用回执改载荷/跨链路拒绝。任一断言失败即以
非零退出码结束。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://web:8000")
M = 1 << 32

failures: list[str] = []


def request(method: str, path: str, body=None, expected: int = 200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status, payload = resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read() or b"{}")
    if status != expected:
        failures.append(f"{method} {path}: 期望 {expected}，实得 {status} ({payload})")
    return status, payload


def check(name: str, condition: bool, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def wait_for_health(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.5)
    return False


def main() -> int:
    if not wait_for_health():
        print("[FAIL] web 服务健康检查超时")
        return 1

    link = f"verify-{int(time.time())}"
    _, payload = request("POST", "/api/links", {"link_id": link}, 201)
    check("创建链路", payload.get("created") is True)

    def frame(raw, receipt, payload_body=None, expected=200):
        status, body = request(
            "POST", f"/api/links/{link}/frames",
            {"raw_count": raw, "receipt_id": receipt, "payload": payload_body},
            expected,
        )
        return body

    def frame_expect_status(raw, receipt, payload_body, expected):
        status, _ = request(
            "POST", f"/api/links/{link}/frames",
            {"raw_count": raw, "receipt_id": receipt, "payload": payload_body},
            expected,
        )
        return status

    # 回绕边界前后乱序到达，各接受一次且纪元扩展正确。
    r = frame(M - 2, "a", {"n": 1}); check("回绕前 M-2 接受", r.get("verdict") == "accepted")
    r = frame(0, "c", {"n": 3})
    check("回绕后 0 接受并扩展为 2^32", r.get("verdict") == "accepted" and r.get("highest") == M)
    r = frame(M - 1, "b", {"n": 2})
    check("乱序旧帧 M-1 接受", r.get("verdict") == "accepted" and r.get("extended_seq") == M - 1)

    r = frame(M - 1, "b-dup", {"n": 9})
    check("同位置再次投递判重复", r.get("verdict") == "duplicate")

    r = frame(M - 1, "b", {"n": 2})
    check("相同回执+相同载荷返回首次裁决",
          r.get("verdict") == "accepted" and r.get("replayed") is True)

    status = frame_expect_status(M - 1, "b", {"n": 99}, 409)
    check("复用回执却改变载荷 -> 409", status == 409)

    # 跨链路复用标识。
    other = link + "-b"
    request("POST", "/api/links", {"link_id": other}, 201)
    status, _ = request(
        "POST", f"/api/links/{other}/frames",
        {"raw_count": 7, "receipt_id": "b", "payload": {"n": 2}}, expected=409,
    )
    check("复用回执却改变链路 -> 409", status == 409)

    # 推进超过 64 窗口后旧帧过期，绝不复活。
    for i in range(1, 70):
        frame(i, f"adv-{i}", {"i": i})
    r = frame(M - 2, "late", {"n": "z"})
    check("窗口外旧帧判过期", r.get("verdict") == "stale")
    check("位图最多保留最近 64 个位置", len(r.get("accepted_positions", [])) == 64)

    if failures:
        print(f"\n冒烟失败 {len(failures)} 项")
        return 1
    print("\n全部 HTTP 冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
