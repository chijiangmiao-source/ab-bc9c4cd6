"""滑动窗口纯算法测试：初始化、推进、乱序、回绕、并列拒绝、过期/重复。"""

from app import window as w

M = 1 << 32


def test_first_frame_initializes_window():
    state = w.fresh()
    verdict, state2, ext = w.decide(state, 12345)
    assert verdict == w.ACCEPTED
    assert state2.initialized
    assert state2.highest == 12345
    assert state2.bitmap == 0b1
    assert ext == 12345
    assert w.positions(state2) == [12345]


def test_in_order_advance_shifts_bitmap():
    state = w.fresh()
    for raw in (10, 11, 12, 13):
        verdict, state, _ = w.decide(state, raw)
        assert verdict == w.ACCEPTED
    assert state.highest == 13
    assert state.bitmap == 0b1111
    assert w.positions(state) == [13, 12, 11, 10]


def test_gap_within_window_then_duplicate():
    state = w.fresh()
    _, state, _ = w.decide(state, 10)
    _, state, _ = w.decide(state, 13)  # 跳过 11、12
    assert state.bitmap == 0b1001

    verdict, state2, _ = w.decide(state, 11)
    assert verdict == w.ACCEPTED
    assert state2.bitmap == 0b1101

    verdict, _, _ = w.decide(state2, 11)
    assert verdict == w.DUPLICATE  # 同一位置只能接受一次

    verdict, _, _ = w.decide(state2, 10)
    assert verdict == w.DUPLICATE


def test_window_boundary_gap_63_accepted_gap_64_stale():
    state = w.State(initialized=True, highest=13, bitmap=1)
    raw63 = (13 - 63) & 0xFFFFFFFF  # 扩展序号 -50 的 32 位表示
    verdict, _, ext = w.decide(state, raw63)
    assert verdict == w.ACCEPTED and ext == -50

    state = w.State(initialized=True, highest=13, bitmap=1)
    raw64 = (13 - 64) & 0xFFFFFFFF  # 扩展序号 -51，恰在窗口外
    verdict, state2, ext = w.decide(state, raw64)
    assert verdict == w.STALE and ext == -51
    assert state2 == state  # 过期不改变窗口


def test_epoch_nearest_around_wrap():
    assert w.extend(0, M - 1) == M           # 0xFFFFFFFF 之后的 0 => 2**32
    assert w.extend(0xFFFFFFFF, M) == M - 1  # 回绕后旧帧落在本纪元
    assert w.extend(5, M) == M + 5


def test_epoch_tie_is_rejected():
    # highest=2**31 时，raw=0 的纪元 0 与纪元 1 候选距离完全并列。
    state = w.State(initialized=True, highest=M // 2, bitmap=1)
    verdict, state2, ext = w.decide(state, 0)
    assert verdict == w.REJECTED
    assert ext is None
    assert state2 == state


def test_full_wrap_each_out_of_order_frame_accepted_once():
    state = w.fresh()
    # 回绕边界前后的帧，乱序到达。
    sequence = [0xFFFFFFFE, 0xFFFFFFFF, 0, 5, 2, 4, 1, 3]
    accepted = []
    for raw in sequence:
        verdict, state, ext = w.decide(state, raw)
        assert verdict == w.ACCEPTED, (raw, verdict)
        accepted.append((raw, ext))

    extended = dict(accepted)
    assert extended[0xFFFFFFFF] == M - 1
    assert extended[0] == M
    assert extended[5] == M + 5
    assert state.highest == M + 5

    # 回绕前后的每一帧再次投递都只能是重复。
    for raw in sequence:
        verdict, _, _ = w.decide(state, raw)
        assert verdict == w.DUPLICATE, raw

    # 旧纪元帧（gap=16 仍在 64 窗口内）也是重复而非重新接受。
    state = w.fresh()
    for raw in (0xFFFFFFF0, 0xFFFFFFFF, 0):
        _, state, _ = w.decide(state, raw)
    verdict, _, _ = w.decide(state, 0xFFFFFFF0)
    assert verdict == w.DUPLICATE  # gap=16，窗口内，已置位


def test_old_frame_before_window_after_wrap_is_stale():
    state = w.fresh()
    _, state, _ = w.decide(state, 100)
    _, state, _ = w.decide(state, 200)
    verdict, _, _ = w.decide(state, 100)
    assert verdict == w.STALE  # gap=100 >= 64，旧帧不得因计数接近而复活


def test_positions_lists_at_most_64():
    state = w.fresh()
    _, state, _ = w.decide(state, 0)
    for raw in range(1, 100):
        _, state, _ = w.decide(state, raw)
    positions = w.positions(state)
    assert positions == list(range(99, 35, -1))  # 最近 64 个位置
    assert len(positions) == 64


def test_near_zero_highest_extends_into_negative_epoch():
    # 首次帧计数为 5：0xFFFFFFFF 距上一纪元 -1 更近，应解释为 -1 而非 2**32-1。
    state = w.fresh()
    _, state, _ = w.decide(state, 5)
    verdict, state, ext = w.decide(state, 0xFFFFFFFF)
    assert verdict == w.ACCEPTED and ext == -1
    assert state.highest == 5
    # 之后 0 解释为纪元 0 的 0，正常推进。
    verdict, state, ext = w.decide(state, 0)
    assert verdict == w.ACCEPTED and ext == 0
    # -1 位置再次投递为重复。
    verdict, _, _ = w.decide(state, 0xFFFFFFFF)
    assert verdict == w.DUPLICATE
