"""Loop protection for the agent-to-agent mailbox (W2, item 2.3).

Two agents that answer each other will eventually talk in circles: A reports
"tests are red", B replies "fixing", A reports "still red", and so on, each
message a legitimate reply to the last, each one a fresh tool call the human
approved once. Nobody is in the wrong; the loop is in the pattern.

The guard sits between the inbox and the prompt path and watches that pattern.
It is pure state -- no files, no sockets, a clock it is handed -- so the watch
loop can hold one and these tests can drive one directly.

Three layers, outermost first:

- **Mute**: a deliberate human switch on one pair. Checked first, cleared only
  by a human. A muted pair is dark, full stop.
- **Ping-pong latch**: a pair that has exchanged N consecutive agent messages
  with no human prompt in between is *looping*. The latch holds until a human
  types into either member of the pair -- the human is the only one who can
  say the loop is over. The message that completes the pattern is still
  delivered; the pause is for what comes after.
- **Rate limit**: a sliding window of delivered exchanges per pair. The backstop
  for fast spam that the latch would catch anyway, and the reason a pair that
  has been muted or latched *and then reset* still cannot burst.

Two rules keep it honest. The budget is spent only by `commit`, never by
`admit`: a message the target refuses (its tmux is down) is retried on the
next poll, and a retry must not be counted twice. And the latch and the
rate window are separate -- a human prompt breaks the loop but does not widen
the pipe.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["Admission", "LoopGuard"]


@dataclass(frozen=True)
class Admission:
    """The guard's verdict on one message before it is typed into a session."""

    deliver: bool
    #: Why not, when refused: "muted", "looping", or "rate_limited".
    reason: str | None = None


class _PairState:
    """One undirected pair's bookkeeping. Kept private to the guard."""

    __slots__ = ("window", "consecutive", "looping", "muted", "total", "last_at")

    def __init__(self) -> None:
        #: Timestamps of delivered exchanges still inside the rate window.
        self.window: deque[float] = deque()
        #: Delivered exchanges since the last human prompt (or the start).
        self.consecutive = 0
        #: Latched: the pair ran the ping-pong limit with no human in between.
        self.looping = False
        #: A human muted this pair; only a human unmutes it.
        self.muted = False
        #: Every exchange ever delivered, for the PWA's counters.
        self.total = 0
        self.last_at: float | None = None

    def purge(self, cutoff: float) -> None:
        while self.window and self.window[0] <= cutoff:
            self.window.popleft()


class LoopGuard:
    """Decides which agent-to-agent messages keep flowing and which are held.

    Pairs are undirected: a message from A to B and one from B to A are the
    same conversation, so the counter is kept under the sorted (a, b) key.
    """

    def __init__(
        self,
        rate_limit: int = 10,
        window_seconds: float = 600.0,
        ping_pong_limit: int = 20,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.rate_limit = rate_limit
        self.window_seconds = window_seconds
        self.ping_pong_limit = ping_pong_limit
        self._now = now or time.monotonic
        self._pairs: dict[tuple[str, str], _PairState] = {}

    @staticmethod
    def _key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def _state(self, a: str, b: str) -> _PairState:
        state = self._pairs.get(self._key(a, b))
        if state is None:
            state = _PairState()
            self._pairs[self._key(a, b)] = state
        return state

    def admit(self, from_id: str, to_id: str, ts: float | None = None) -> Admission:
        """May this message be typed into its target right now?

        A pure check: it reads the pair's state and spends nothing, so a
        message that is later refused by a dead target can be retried without
        double-paying the budget.
        """
        t = self._now() if ts is None else ts
        state = self._pairs.get(self._key(from_id, to_id))
        if state is None:
            return Admission(True)
        state.purge(t - self.window_seconds)
        if state.muted:
            return Admission(False, "muted")
        if state.looping:
            return Admission(False, "looping")
        if len(state.window) >= self.rate_limit:
            return Admission(False, "rate_limited")
        return Admission(True)

    def commit(self, from_id: str, to_id: str, ts: float | None = None) -> bool:
        """Record one delivered exchange; True when it tripped the latch.

        Call this only after the message actually landed in the target's
        prompt path -- counting a message the target never received would
        make the budget leak on every retry.
        """
        t = self._now() if ts is None else ts
        state = self._state(from_id, to_id)
        state.window.append(t)
        state.last_at = t
        state.total += 1
        state.consecutive += 1
        if state.consecutive >= self.ping_pong_limit and not state.looping:
            state.looping = True
            return True
        return False

    def note_human_prompt(self, session_id: str) -> None:
        """A human typed into this session: every pair it belongs to resets.

        The ping-pong counter and the latch are the *loop* record, so a human
        in the conversation is what clears them. The rate window is the *pipe*
        record and slides on its own; a reset that widened it would let a
        fresh burst through the very pair that just ran away.
        """
        for (a, b), state in self._pairs.items():
            if session_id in (a, b):
                state.consecutive = 0
                state.looping = False

    def mute_pair(self, a: str, b: str) -> None:
        self._state(a, b).muted = True

    def unmute_pair(self, a: str, b: str) -> None:
        state = self._pairs.get(self._key(a, b))
        if state is not None:
            state.muted = False

    def is_muted(self, a: str, b: str) -> bool:
        state = self._pairs.get(self._key(a, b))
        return bool(state and state.muted)

    def pair_stats(self) -> list[dict[str, object]]:
        """Every pair the guard knows, for the PWA and the API.

        Includes pairs that were muted before ever exchanging a message: the
        mute is the state that matters, and hiding it would make the PWA's
        toggle unable to show a pair it is about to unmute.
        """
        stats = []
        for (a, b), state in sorted(self._pairs.items()):
            stats.append(
                {
                    "a": a,
                    "b": b,
                    "count": state.total,
                    "looping": state.looping,
                    "muted": state.muted,
                    "last_at": state.last_at,
                }
            )
        return stats

    def pairs_for(self, session_id: str) -> list[dict[str, object]]:
        """The pairs one session belongs to, addressed as peers.

        The shape the session drawer wants: for each row, who the agent is
        talking to, how much, and whether that traffic is held.
        """
        result = []
        for (a, b), state in sorted(self._pairs.items()):
            if session_id not in (a, b):
                continue
            peer = b if a == session_id else a
            result.append(
                {
                    "peer": peer,
                    "count": state.total,
                    "looping": state.looping,
                    "muted": state.muted,
                }
            )
        return result
