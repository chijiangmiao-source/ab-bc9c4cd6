"""32 位帧计数 -> 64 位扩展序号的滑动窗口裁决算法（纯函数，无 I/O）。

规则
----
* 链路上每个帧携带 32 位无符号计数 raw (0 .. 2**32-1)，计数会自然回绕。
* 接收方以「最高 64 位扩展序号 highest」+「64 位位图 bitmap」维护窗口：
  位图第 k 位（k = 0..63）表示扩展序号 (highest - k) 是否已接受，
  第 0 位恒为 1（highest 自身）。
* 纪元解绕：给定 raw，取其相对 highest 所在纪元的相邻三个纪元候选
  (epoch-1, epoch, epoch+1)，选择距离 highest 最近的一个；两个候选
  距离并列时拒绝（无法判定纪元）。
* 高于 highest：接受并把位图左移差值位；低于窗口下界（落后 >= 64）
  判定过期；窗口内已置位判定重复，未置位则接受并置位。
"""

from __future__ import annotations

from dataclasses import dataclass

MOD = 1 << 32
MASK32 = MOD - 1
WIDTH = 64
MASK64 = (1 << WIDTH) - 1

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
STALE = "stale"
REJECTED = "rejected"


@dataclass(frozen=True)
class State:
    """不可变窗口状态。未初始化时 initialized=False。"""

    initialized: bool = False
    highest: int = 0
    bitmap: int = 0


def fresh() -> State:
    return State(False)


def epoch_candidates(raw: int, highest: int) -> list[int]:
    """返回 raw 相对 highest 所在纪元的三个相邻纪元扩展候选。"""
    base = raw & MASK32
    epoch = highest // MOD
    return [base + (epoch + delta) * MOD for delta in (-1, 0, 1)]


def extend(raw: int, highest: int) -> int | None:
    """把 32 位 raw 解析为 64 位扩展序号。

    返回 None 表示相邻纪元候选与 highest 距离并列，无法安全解绕。
    """
    candidates = epoch_candidates(raw, highest)
    distances = [(abs(candidate - highest), candidate) for candidate in candidates]
    best_distance = min(distance for distance, _ in distances)
    nearest = [candidate for distance, candidate in distances if distance == best_distance]
    if len(nearest) > 1:
        return None
    return nearest[0]


def decide(state: State, raw: int) -> tuple[str, State, int | None]:
    """对一个到达帧作出裁决。

    返回 (裁决码, 新窗口状态, 解析出的扩展序号)；扩展序号在纪元并列
    拒绝时为 None。
    """
    raw &= MASK32

    # 首次帧初始化窗口：以 raw 自身（纪元 0）作为最高序号。
    if not state.initialized:
        return ACCEPTED, State(True, raw, 1), raw

    extended = extend(raw, state.highest)
    if extended is None:
        return REJECTED, state, None

    if extended > state.highest:
        delta = extended - state.highest
        new_bitmap = ((state.bitmap << delta) & MASK64) | 1
        return ACCEPTED, State(True, extended, new_bitmap), extended

    gap = state.highest - extended
    if gap >= WIDTH:
        return STALE, state, extended
    if (state.bitmap >> gap) & 1:
        return DUPLICATE, state, extended

    new_bitmap = state.bitmap | (1 << gap)
    return ACCEPTED, State(True, state.highest, new_bitmap), extended


def positions(state: State) -> list[int]:
    """返回位图中已接受位置（扩展序号），按从新到旧排序，至多 64 个。"""
    if not state.initialized:
        return []
    return [state.highest - gap for gap in range(WIDTH) if (state.bitmap >> gap) & 1]
