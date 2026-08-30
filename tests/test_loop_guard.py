"""Loop protection for the agent-to-agent mailbox (W2, item 2.3).

Two agents that answer each other fast enough will talk themselves in circles
with no human in the loop. The guard sits between the inbox and the prompt
path and refuses to keep feeding a pair once it has run too long or too fast.

The guard is pure state -- no filesystem, no clock of its own (a clock is
injected) -- so these tests drive it directly rather than through a watch loop.
"""

from agy_remote.loop_guard import LoopGuard


class _Clock:
    """A hand-advanced clock so the rate-limit window is deterministic."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _guard(**overrides) -> tuple[LoopGuard, _Clock]:
    clock = _Clock()
    # A high rate limit by default so the ping-pong latch is what a test is
    # usually exercising; the rate-limit tests drop it to 4 explicitly.
    kwargs = dict(rate_limit=50, window_seconds=100.0, ping_pong_limit=6, now=clock)
    kwargs.update(overrides)
    return LoopGuard(**kwargs), clock


# -- admit / commit ------------------------------------------------------------


def test_a_fresh_pair_is_admitted():
    guard, _ = _guard()
    decision = guard.admit("agy-a", "agy-b")
    assert decision.deliver
    assert decision.reason is None


def test_commit_is_what_counts_an_exchange():
    guard, _ = _guard()
    # admit alone must not consume budget: a message that is refused later
    # (target down) is retried, and a retry must not be double-counted.
    guard.admit("a", "b")
    guard.admit("a", "b")
    assert guard.pair_stats() == []
    guard.commit("a", "b")
    stats = guard.pair_stats()
    assert len(stats) == 1
    assert stats[0]["count"] == 1


def test_a_pair_is_undirected():
    guard, _ = _guard()
    guard.commit("a", "b")
    guard.commit("b", "a")  # the reply: same pair, same counter
    stats = guard.pair_stats()
    assert len(stats) == 1
    assert stats[0]["count"] == 2


def test_distinct_pairs_are_tracked_independently():
    guard, _ = _guard()
    guard.commit("a", "b")
    guard.commit("a", "c")
    stats = {tuple(sorted((p["a"], p["b"]))): p for p in guard.pair_stats()}
    assert set(stats) == {("a", "b"), ("a", "c")}
    assert stats[("a", "b")]["count"] == 1
    assert stats[("a", "c")]["count"] == 1


# -- the ping-pong latch -------------------------------------------------------


def test_a_pair_latches_when_it_runs_the_limit():
    guard, _ = _guard()  # ping_pong_limit=6
    for _ in range(5):
        assert guard.admit("a", "b").deliver
        assert guard.commit("a", "b") is False  # not the trip yet
    # the sixth exchange trips the latch; it is still delivered
    assert guard.admit("a", "b").deliver
    assert guard.commit("a", "b") is True  # just looped
    # ...and the pair is paused from here on
    assert guard.admit("a", "b") is not None
    decision = guard.admit("a", "b")
    assert not decision.deliver
    assert decision.reason == "looping"


def test_the_latch_is_undirected():
    guard, _ = _guard()
    for _ in range(5):
        guard.commit("a", "b")
    guard.commit("b", "a")  # trips the latch
    decision = guard.admit("b", "a")
    assert not decision.deliver
    assert decision.reason == "looping"


def test_the_latch_fires_exactly_once():
    guard, _ = _guard()
    for _ in range(5):
        guard.commit("a", "b")
    assert guard.commit("a", "b") is True  # the trip
    assert guard.commit("a", "b") is False  # latched; no second trip


def test_a_human_prompt_resets_the_pair():
    guard, _ = _guard()
    for _ in range(5):
        guard.commit("a", "b")
    guard.commit("a", "b")  # latch
    assert not guard.admit("a", "b").deliver
    guard.note_human_prompt("a")  # the human types into a: the pair resets
    assert guard.admit("a", "b").deliver
    # the counter started over: the next five are fine, the sixth trips again
    for _ in range(5):
        assert guard.commit("a", "b") is False
    assert guard.commit("a", "b") is True


def test_a_human_prompt_to_either_member_resets_the_pair():
    guard, _ = _guard()
    for _ in range(6):
        guard.commit("a", "b")  # latched
    assert not guard.admit("a", "b").deliver
    guard.note_human_prompt("b")
    assert guard.admit("a", "b").deliver


def test_a_human_prompt_leaves_other_pairs_alone():
    guard, _ = _guard()
    for _ in range(6):
        guard.commit("a", "b")  # a<->b latched
    guard.commit("a", "c")
    guard.note_human_prompt("a")  # resets a<->b and a<->c
    assert guard.admit("a", "b").deliver
    assert guard.admit("a", "c").deliver
    # a third pair that never involved the human is untouched by the reset --
    # but it was never latched either, so there is nothing to clear.
    assert guard.admit("a", "c").deliver


def test_a_reset_pair_still_counts_its_rate_budget():
    guard, _ = _guard(rate_limit=4)
    for _ in range(4):
        guard.commit("a", "b")
    assert guard.admit("a", "b").reason == "rate_limited"
    guard.note_human_prompt("a")  # resets the loop counter, not the window
    assert guard.admit("a", "b").reason == "rate_limited"


# -- the rate limit -------------------------------------------------------------


def test_the_rate_limit_refuses_a_full_window():
    guard, _ = _guard(rate_limit=4)
    for _ in range(4):
        assert guard.admit("a", "b").deliver
        guard.commit("a", "b")
    decision = guard.admit("a", "b")
    assert not decision.deliver
    assert decision.reason == "rate_limited"


def test_the_rate_limit_recovers_when_the_window_slides():
    guard, clock = _guard(rate_limit=4, window_seconds=100.0)
    for _ in range(4):
        guard.admit("a", "b")
        guard.commit("a", "b")
    assert not guard.admit("a", "b").deliver
    clock.advance(101.0)
    assert guard.admit("a", "b").deliver


def test_the_rate_limit_counts_per_pair():
    guard, _ = _guard(rate_limit=4)
    for _ in range(4):
        guard.commit("a", "b")
    assert not guard.admit("a", "b").deliver
    assert guard.admit("a", "c").deliver  # a different pair has its own budget


def test_admit_does_not_consume_rate_budget():
    guard, _ = _guard(rate_limit=4)
    for _ in range(10):
        assert guard.admit("a", "b").deliver  # checked, never committed
    guard.commit("a", "b")
    guard.commit("a", "b")
    guard.commit("a", "b")
    assert guard.admit("a", "b").deliver  # still three under the cap of four


# -- mute -------------------------------------------------------------------------


def test_a_muted_pair_is_refused():
    guard, _ = _guard()
    guard.mute_pair("a", "b")
    decision = guard.admit("a", "b")
    assert not decision.deliver
    assert decision.reason == "muted"


def test_mute_is_undirected():
    guard, _ = _guard()
    guard.mute_pair("a", "b")
    assert not guard.admit("b", "a").deliver


def test_unmute_reopens_the_pair():
    guard, _ = _guard()
    guard.mute_pair("a", "b")
    guard.unmute_pair("a", "b")
    assert guard.admit("a", "b").deliver


def test_mute_wins_over_everything():
    guard, _ = _guard()
    for _ in range(6):
        guard.commit("a", "b")  # latched
    guard.mute_pair("a", "b")
    decision = guard.admit("a", "b")
    assert decision.reason == "muted"  # mute reports before the loop latch
    guard.note_human_prompt("a")  # breaks the latch...
    decision = guard.admit("a", "b")
    assert not decision.deliver
    assert decision.reason == "muted"  # ...but the pair stays muted


def test_unmute_is_the_only_way_back_from_a_mute():
    guard, _ = _guard()
    guard.mute_pair("a", "b")
    guard.note_human_prompt("a")
    guard.note_human_prompt("b")
    assert not guard.admit("a", "b").deliver
    guard.unmute_pair("a", "b")
    assert guard.admit("a", "b").deliver


def test_muting_one_pair_leaves_another_open():
    guard, _ = _guard()
    guard.mute_pair("a", "b")
    assert guard.admit("a", "c").deliver


# -- stats for the phone ----------------------------------------------------------


def test_pair_stats_reports_counts_loop_and_mute():
    guard, _ = _guard()
    guard.commit("a", "b")
    guard.commit("a", "c")
    for _ in range(5):
        guard.commit("a", "c")  # latches a<->c
    guard.mute_pair("b", "c")
    stats = {(p["a"], p["b"]): p for p in guard.pair_stats()}
    assert stats[("a", "b")]["count"] == 1
    assert stats[("a", "b")]["looping"] is False
    assert stats[("a", "b")]["muted"] is False
    assert stats[("a", "c")]["count"] == 6
    assert stats[("a", "c")]["looping"] is True
    assert stats[("a", "c")]["muted"] is False
    assert stats[("b", "c")]["count"] == 0
    assert stats[("b", "c")]["muted"] is True


def test_pairs_for_lists_a_sessions_partners():
    guard, _ = _guard()
    guard.commit("a", "b")
    guard.commit("a", "c")
    guard.commit("b", "c")
    partners = guard.pairs_for("a")
    by_peer = {p["peer"]: p for p in partners}
    assert set(by_peer) == {"b", "c"}
    assert by_peer["b"]["count"] == 1
    assert by_peer["c"]["count"] == 1
    assert guard.pairs_for("nobody") == []
