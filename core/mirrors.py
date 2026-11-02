"""Process-wide memory of how each Overpass host is doing, and its pacing.

Pure Python and thread-safe: the dock, the Processing toolbox and a batch run
all download on their own worker threads, and they share one board, so a host
that just refused one download is rested for the next one as well.

* A failed host rests for a cool-down that depends on what it answered, and
  doubles while it keeps failing (capped at the larger of its base and
  15 minutes).  A success clears its record.
* Hosts are offered in this order: not resting, most recent success first,
  then the order the caller pinned them in.
* One request at a time per host, at least one second apart, across every
  download in this QGIS session ("one user, one script").

`ask_hosts` asks the hosts for one part of a download under those rules: at
most one attempt per host, one more only after a dropped connection, and a
wait for a resting host only when it is short.  A query that ran out of
server time is asked from the next host at the same size; the part counts as
too heavy only when every host asked for it ran out of time.

Time comes from an injected clock, so tests advance it instead of sleeping.
Nothing here sleeps: waiting goes through the caller's (cancellable) pause.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .query import MAX_JOB_REQUESTS
from .transport import (
    BAD_QUERY,
    BLOCKED,
    BUSY,
    CLIENT_TIMEOUT,
    DENSE,
    HOST_ERROR,
    NETWORK,
    OK,
    RATE,
    SERVER_TIMEOUT,
    TLS,
    Verdict,
)

#: Seconds a host rests after its first failure of each class.  429 and
#: 403/406 are the operators' explicit "stop" signals: their rule is at least
#: 30 s, and repeated 429s get an application banned.  An immediate 5xx is a
#: refusal at the door; a hang is an accepted query that never came back; a
#: 404 or 405 is an endpoint that moved or a proxy in the way.  An answer over
#: 64 MB or 150,000 objects, or out of memory, says only that the part is too
#: big; it costs the host nothing.
COOLDOWN_SECONDS: Dict[str, float] = {
    RATE: 60.0,
    BLOCKED: 3600.0,
    BUSY: 45.0,
    CLIENT_TIMEOUT: 120.0,
    NETWORK: 120.0,
    TLS: 3600.0,
    HOST_ERROR: 120.0,
}
#: Rests of a fixed length that are not strikes.  A query that ran out of
#: server time may mean an overloaded host or a dense part: the host rests
#: briefly, so the next part goes to another host first, but the rest never
#: grows, or the light parts after one dense spot would find every host
#: resting for minutes.
FLAT_REST_SECONDS: Dict[str, float] = {
    SERVER_TIMEOUT: 30.0,
}
COOLDOWN_CAP_SECONDS = 15 * 60.0
#: Gap between the end of one request to a host and the start of the next.
SPACING_SECONDS = 1.0
#: How soon to ask again while another download holds the host.
BUSY_POLL_SECONDS = 0.25
#: Longest wait for a resting host before the part is given up.
MAX_REST_WAIT_SECONDS = 30.0
#: A dropped connection earns one more try on the same host, this much later.
RESET_RETRY_DELAY_SECONDS = 5.0

# Why `try_acquire` or `ask_hosts` asks the caller to pause.
SPACING = "spacing"
IN_USE = "in_use"
RESTING = "resting"
RESET = "reset"

# How a part ended when no host delivered it.
LOST = "lost"            # every host failed, or rests for longer than the wait
NO_BUDGET = "budget"     # the download has spent its request ceiling


def cooldown_seconds(kind: str, strikes: int) -> float:
    """Rest after `strikes` failures in a row, the last one of class `kind`."""
    base = COOLDOWN_SECONDS.get(kind, 0.0)
    if base <= 0 or strikes < 1:
        return 0.0
    doubling = 2.0 ** min(int(strikes) - 1, 16)
    return min(max(base, COOLDOWN_CAP_SECONDS), base * doubling)


@dataclass
class _HostState:
    until: float = 0.0
    strikes: int = 0
    last_ok: Optional[float] = None
    last_end: Optional[float] = None
    in_use: bool = False
    kind: str = ""
    detail: str = ""
    answered_at: Optional[float] = None


class HostBoard:
    """Cool-downs, ordering and pacing for a set of hosts, behind one lock."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        spacing_s: float = SPACING_SECONDS,
    ) -> None:
        self._clock = clock
        self._spacing = max(0.0, float(spacing_s))
        self._lock = threading.Lock()
        self._hosts: Dict[str, _HostState] = {}

    def _state(self, host: str) -> _HostState:
        state = self._hosts.get(host)
        if state is None:
            state = self._hosts[host] = _HostState()
        return state

    # -------------------------------------------------------------- health
    def remaining(self, host: str) -> float:
        """Seconds the host still rests; 0.0 when it may be asked."""
        with self._lock:
            return max(0.0, self._state(host).until - self._clock())

    def order(self, hosts: Sequence[str]) -> Tuple[str, ...]:
        """The hosts that may be asked now, best first.

        `hosts` is the pinned order; a host with a success comes before one
        without, the most recent success first.
        """
        with self._lock:
            now = self._clock()
            ready = []
            for index, host in enumerate(dict.fromkeys(hosts)):
                state = self._state(host)
                if state.until > now:
                    continue
                recent = -state.last_ok if state.last_ok is not None else math.inf
                ready.append((recent, index, host))
            return tuple(host for _recent, _index, host in sorted(ready))

    def strike(self, host: str, kind: str, detail: str = "") -> float:
        """Record a failed answer; return the rest it earned (0 for none).

        Classes without a cool-down (a part too big or out of memory, an
        invalid request) are remembered for messages but leave the host's
        health alone: the host answered properly.  A server timeout rests
        the host for its fixed time and is not counted as a strike.
        """
        with self._lock:
            now = self._clock()
            state = self._state(host)
            state.kind, state.detail, state.answered_at = kind, detail, now
            if kind in FLAT_REST_SECONDS:
                rest = FLAT_REST_SECONDS[kind]
                state.until = max(state.until, now + rest)
                return rest
            if kind not in COOLDOWN_SECONDS:
                return 0.0
            state.strikes += 1
            rest = cooldown_seconds(kind, state.strikes)
            state.until = max(state.until, now + rest)
            return rest

    def success(self, host: str) -> None:
        with self._lock:
            now = self._clock()
            state = self._state(host)
            state.until = 0.0
            state.strikes = 0
            state.last_ok = now
            state.kind, state.detail, state.answered_at = OK, "", now

    def last_answer(self, host: str) -> Optional[Tuple[str, str, float]]:
        """(class, detail, seconds ago) of the host's last answer, if any."""
        with self._lock:
            state = self._hosts.get(host)
            if state is None or state.answered_at is None:
                return None
            return state.kind, state.detail, max(0.0, self._clock() - state.answered_at)

    def strikes(self, host: str) -> int:
        with self._lock:
            return self._state(host).strikes

    # -------------------------------------------------------------- pacing
    def try_acquire(self, host: str) -> Tuple[float, str]:
        """Take the host's single request slot.

        Returns (0.0, "") when the slot is now held by the caller, who must
        call `release` afterwards; otherwise the seconds to wait before
        asking again and why: SPACING (the gap after the previous request)
        or IN_USE (another download is using the host).
        """
        with self._lock:
            state = self._state(host)
            if state.in_use:
                return BUSY_POLL_SECONDS, IN_USE
            if state.last_end is not None:
                gap = state.last_end + self._spacing - self._clock()
                if gap > 0:
                    return gap, SPACING
            state.in_use = True
            return 0.0, ""

    def release(self, host: str) -> None:
        with self._lock:
            state = self._state(host)
            state.in_use = False
            state.last_end = self._clock()

    def reset(self) -> None:
        """Forget every host (a new session, or a test)."""
        with self._lock:
            self._hosts.clear()


#: The board every download in this QGIS session shares.
BOARD = HostBoard()


class RequestBudget:
    """The requests one download may still send (thread-confined)."""

    def __init__(self, limit: int = MAX_JOB_REQUESTS) -> None:
        self.limit = max(0, int(limit))
        self.spent = 0

    @property
    def left(self) -> int:
        return self.limit - self.spent

    def take(self) -> bool:
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


@dataclass(frozen=True)
class HostResult:
    """How the hosts answered one part.

    `kind` is OK, a heavy class, BAD_QUERY, LOST or NO_BUDGET; `answers`
    holds every (host, verdict) heard for this part, in the order asked.
    """

    kind: str
    payload: Optional[dict] = None
    host: str = ""
    detail: str = ""
    answers: Tuple[Tuple[str, Verdict], ...] = ()


def unanswered(answers: Sequence[Tuple[str, Verdict]]) -> HostResult:
    """How a part ended that no host delivered.

    SERVER_TIMEOUT when every host asked ran out of time on it -- a host
    that rested and was not asked does not count, so when no other host
    could be asked the one timeout decides -- and LOST otherwise, LOST too
    when no host was asked at all.  The result names the first host that
    ran out of time.
    """
    if answers and all(verdict.kind == SERVER_TIMEOUT for _host, verdict in answers):
        host, verdict = answers[0]
        return HostResult(SERVER_TIMEOUT, None, host, verdict.detail, tuple(answers))
    return HostResult(LOST, answers=tuple(answers))


def ask_hosts(
    hosts: Sequence[str],
    attempt: Callable[[str], Verdict],
    board: HostBoard,
    budget: RequestBudget,
    pause: Callable[[float, str, str], None],
    notify: Optional[Callable[[str, Verdict], None]] = None,
    max_rest_wait_s: float = MAX_REST_WAIT_SECONDS,
    reset_delay_s: float = RESET_RETRY_DELAY_SECONDS,
    timeout_ends: bool = False,
    may_ask: Optional[Callable[[Sequence[Tuple[str, Verdict]]], bool]] = None,
) -> HostResult:
    """Ask the hosts for one part, politely.

    * At most one attempt per host; the same host is asked once more only
      after it dropped the connection, `reset_delay_s` later.
    * Hosts are taken in the board's order.  When every host left is
      resting, the part waits for the first one only if it is back within
      `max_rest_wait_s`; otherwise no other host is asked.
    * An answer that proves the part too heavy (out of memory, over 64 MB
      or 150,000 objects) or an invalid request ends the part at once: the
      same request would fail the same way on every host.
    * A server timeout is not proof: that host rests briefly and the next
      host is asked for the same part at the same size.  When no host
      delivered, the part is SERVER_TIMEOUT only if every host asked ran
      out of time (`unanswered`), otherwise LOST.  With `timeout_ends`
      (the part lies inside one already split for timeouts) the first
      timeout ends the part, as proof does.
    * Every request takes one unit of `budget`; with none left, NO_BUDGET.
    * No further host is asked once `may_ask(answers so far)` is false; the
      part then ends as if no other host could be asked.

    `attempt(host)` sends one request and returns its verdict; `pause(seconds,
    host, why)` waits (and raises to cancel); `notify(host, verdict)` is told
    when a failed host is left for the next one.
    """
    pinned = tuple(dict.fromkeys(hosts))
    tried: List[str] = []
    answers: List[Tuple[str, Verdict]] = []

    def send(host: str) -> Verdict:
        while True:
            gap, why = board.try_acquire(host)
            if gap <= 0:
                break
            pause(gap, host, why)
        try:
            return attempt(host)
        finally:
            board.release(host)

    while True:
        if may_ask is not None and not may_ask(answers):
            return unanswered(answers)
        ready = [host for host in board.order(pinned) if host not in tried]
        if not ready:
            untried = [host for host in pinned if host not in tried]
            if not untried:
                return unanswered(answers)
            soonest = min(untried, key=board.remaining)
            rest = board.remaining(soonest)
            if rest > max_rest_wait_s:
                return unanswered(answers)
            pause(rest, soonest, RESTING)
            continue
        host = ready[0]
        if not budget.take():
            return HostResult(NO_BUDGET, answers=tuple(answers))
        tried.append(host)
        verdict = send(host)
        if verdict.retry_same_host and budget.take():
            pause(reset_delay_s, host, RESET)
            verdict = send(host)
        answers.append((host, verdict))
        if verdict.ok:
            board.success(host)
            return HostResult(OK, verdict.payload, host, verdict.detail, tuple(answers))
        board.strike(host, verdict.kind, verdict.detail)
        if (
            verdict.kind in DENSE
            or verdict.kind == BAD_QUERY
            or (timeout_ends and verdict.kind == SERVER_TIMEOUT)
        ):
            return HostResult(verdict.kind, None, host, verdict.detail, tuple(answers))
        if notify is not None and any(item not in tried for item in pinned):
            notify(host, verdict)
