"""Pure checks for host memory, pacing and the per-part host loop (R3, R4).

Every test runs on a fake clock: waiting advances it, nothing sleeps.
"""
from __future__ import annotations

import threading
import unittest

from zero2agent_osm_downloader.core import mirrors, transport
from zero2agent_osm_downloader.core.mirrors import (
    HostBoard,
    RequestBudget,
    ask_hosts,
    cooldown_seconds,
)

FIRST, SECOND = "overpass.private.coffee", "overpass-api.de"
HOSTS = (FIRST, SECOND)

HANG = transport.classify(0, b"", 5, "", transfer_s=55)
RATE = transport.classify(429, b"", 299, "server replied: Too Many Requests")
REFUSED = transport.classify(406, b"", 299, "server replied: Not Acceptable")
BUSY = transport.classify(502, b"at capacity", 499, "server replied: Bad Gateway")
RESET = transport.classify(0, b"", 2, "Connection closed")
HEAVY = transport.classify(200, b'{"elements": [], "remark": "runtime error: Query timed out"}')
OVERSIZE = transport.classify(200, b"", oversize=True)
OUT_OF_MEMORY = transport.classify(
    200, b'{"elements": [], "remark": "runtime error: Query ran out of memory"}'
)
INVALID = transport.classify(400, b"parse error", 302, "server replied: Bad Request")
MOVED = transport.classify(404, b"<html>404 Not Found</html>", 203, "server replied: Not Found")
OK = transport.classify(200, b'{"elements": [{"type": "node", "id": 1}]}')


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class Scripted:
    """Answers each host from a script; records requests with their time."""

    def __init__(self, clock: FakeClock, script: dict, duration: float = 0.0) -> None:
        self.clock = clock
        self.script = {host: list(answers) for host, answers in script.items()}
        self.duration = duration
        self.requests = []
        self.pauses = []

    def attempt(self, host: str) -> transport.Verdict:
        self.requests.append((host, self.clock.now))
        answers = self.script[host]
        verdict = answers.pop(0) if len(answers) > 1 else answers[0]
        self.clock.advance(self.duration)
        return verdict

    def pause(self, seconds: float, host: str, why: str) -> None:
        self.pauses.append((round(seconds, 3), host, why))
        self.clock.advance(seconds)


def _ask(board, script, budget=None, notify=None):
    return ask_hosts(
        HOSTS, script.attempt, board, budget or RequestBudget(), script.pause, notify
    )


class CooldownTests(unittest.TestCase):
    def test_base_rest_per_answer(self) -> None:
        self.assertEqual(
            {kind: cooldown_seconds(kind, 1) for kind in mirrors.COOLDOWN_SECONDS},
            {
                transport.RATE: 60.0,
                transport.BLOCKED: 3600.0,
                transport.BUSY: 45.0,
                transport.CLIENT_TIMEOUT: 120.0,
                transport.NETWORK: 120.0,
                transport.TLS: 3600.0,
                transport.HOST_ERROR: 120.0,
            },
        )
        # A server timeout rests a fixed 30 s and is no strike.
        self.assertEqual(mirrors.FLAT_REST_SECONDS, {transport.SERVER_TIMEOUT: 30.0})
        # Too big or out of memory says nothing about the host's health.
        for kind in (transport.TOO_LARGE, transport.SERVER_MEMORY, transport.BAD_QUERY,
                     transport.SERVER_TIMEOUT, transport.OK):
            self.assertEqual(cooldown_seconds(kind, 3), 0.0)

    def test_doubling_is_capped_at_the_larger_of_base_and_fifteen_minutes(self) -> None:
        self.assertEqual(
            [cooldown_seconds(transport.RATE, n) for n in range(1, 7)],
            [60.0, 120.0, 240.0, 480.0, 900.0, 900.0],
        )
        self.assertEqual(
            [cooldown_seconds(transport.BUSY, n) for n in range(1, 7)],
            [45.0, 90.0, 180.0, 360.0, 720.0, 900.0],
        )
        self.assertEqual(cooldown_seconds(transport.BLOCKED, 4), 3600.0)
        self.assertEqual(cooldown_seconds(transport.TLS, 9), 3600.0)
        self.assertEqual(cooldown_seconds(transport.CLIENT_TIMEOUT, 50), 900.0)


class BoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.board = HostBoard(clock=self.clock)

    def test_strikes_rest_double_and_a_success_clears_them(self) -> None:
        self.assertEqual(self.board.strike(FIRST, transport.RATE, RATE.detail), 60.0)
        self.assertEqual(self.board.remaining(FIRST), 60.0)
        self.clock.advance(61)
        self.assertEqual(self.board.remaining(FIRST), 0.0)
        self.assertEqual(self.board.strike(FIRST, transport.RATE), 120.0)
        self.assertEqual(self.board.strikes(FIRST), 2)
        self.board.success(FIRST)
        self.assertEqual((self.board.strikes(FIRST), self.board.remaining(FIRST)), (0, 0.0))
        self.assertEqual(self.board.strike(FIRST, transport.BUSY), 45.0)

    def test_oversize_and_invalid_answers_leave_health_alone(self) -> None:
        for verdict in (OVERSIZE, OUT_OF_MEMORY, INVALID):
            self.assertEqual(self.board.strike(FIRST, verdict.kind, verdict.detail), 0.0)
        self.assertEqual((self.board.remaining(FIRST), self.board.strikes(FIRST)), (0.0, 0))
        self.assertEqual(self.board.last_answer(FIRST)[:2], (INVALID.kind, INVALID.detail))

    def test_a_server_timeout_rests_the_host_briefly(self) -> None:
        # The query may only have timed out because the host is overloaded.
        self.assertEqual(HEAVY.kind, transport.SERVER_TIMEOUT)
        self.assertEqual(self.board.strike(FIRST, HEAVY.kind, HEAVY.detail), 30.0)
        self.assertEqual(self.board.remaining(FIRST), 30.0)
        self.assertEqual(self.board.order(HOSTS), (SECOND,))
        self.clock.advance(30)
        self.assertEqual(self.board.order(HOSTS), HOSTS)
        # Never longer, however often: a dense spot must not leave every host
        # resting for minutes while light parts wait behind it.
        for _ in range(5):
            self.assertEqual(self.board.strike(FIRST, HEAVY.kind, HEAVY.detail), 30.0)
            self.clock.advance(30)
        self.assertEqual(self.board.strikes(FIRST), 0)
        # Nor does a timeout double the next real failure's rest.
        self.assertEqual(self.board.strike(FIRST, transport.BUSY, BUSY.detail), 45.0)

    def test_a_moved_endpoint_rests_the_host_like_a_network_failure(self) -> None:
        self.assertEqual(MOVED.kind, transport.HOST_ERROR)
        self.assertEqual(self.board.strike(FIRST, MOVED.kind, MOVED.detail), 120.0)
        self.assertEqual(self.board.order(HOSTS), (SECOND,))

    def test_order_skips_resting_hosts_and_prefers_the_latest_success(self) -> None:
        self.assertEqual(self.board.order(HOSTS), HOSTS)
        self.board.strike(FIRST, transport.CLIENT_TIMEOUT)
        self.assertEqual(self.board.order(HOSTS), (SECOND,))
        self.board.success(SECOND)
        self.clock.advance(200)
        # Both may be asked; the one that last delivered goes first.
        self.assertEqual(self.board.order(HOSTS), (SECOND, FIRST))
        self.board.success(FIRST)
        self.assertEqual(self.board.order(HOSTS), (FIRST, SECOND))
        # Pin order breaks ties between hosts that never delivered.
        self.assertEqual(HostBoard(clock=self.clock).order((SECOND, FIRST)), (SECOND, FIRST))

    def test_last_answer_reports_its_age(self) -> None:
        self.assertIsNone(self.board.last_answer(FIRST))
        self.board.strike(FIRST, transport.BUSY, BUSY.detail)
        self.clock.advance(20)
        self.assertEqual(self.board.last_answer(FIRST), (transport.BUSY, BUSY.detail, 20.0))

    def test_one_request_at_a_time_at_least_a_second_apart(self) -> None:
        self.assertEqual(self.board.try_acquire(FIRST), (0.0, ""))
        wait, why = self.board.try_acquire(FIRST)
        self.assertEqual(why, mirrors.IN_USE)
        self.assertGreater(wait, 0)
        # Another host is independent.
        self.assertEqual(self.board.try_acquire(SECOND), (0.0, ""))
        self.board.release(FIRST)
        self.assertEqual(self.board.try_acquire(FIRST), (1.0, mirrors.SPACING))
        self.clock.advance(0.4)
        self.assertAlmostEqual(self.board.try_acquire(FIRST)[0], 0.6)
        self.clock.advance(0.6)
        self.assertEqual(self.board.try_acquire(FIRST), (0.0, ""))

    def test_reset_forgets_everything(self) -> None:
        self.board.strike(FIRST, transport.BLOCKED)
        self.board.reset()
        self.assertEqual(self.board.remaining(FIRST), 0.0)
        self.assertIsNone(self.board.last_answer(FIRST))

    def test_shared_board_is_thread_safe(self) -> None:
        board = HostBoard(spacing_s=0.0)
        holders = []
        overlap = []
        errors = []
        guard = threading.Lock()

        def worker(index: int) -> None:
            try:
                for round_number in range(300):
                    while board.try_acquire(FIRST)[0] > 0:
                        pass
                    with guard:
                        holders.append(index)
                        if len(holders) > 1:
                            overlap.append(tuple(holders))
                    board.strike(SECOND, transport.BUSY)
                    if round_number % 7 == 0:
                        board.success(SECOND)
                    board.order(HOSTS)
                    with guard:
                        holders.remove(index)
                    board.release(FIRST)
            except Exception as error:  # noqa: BLE001 - collected for the assert
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(overlap, [])


class AskHostsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.board = HostBoard(clock=self.clock)

    def test_every_host_hanging_costs_one_request_each(self) -> None:
        script = Scripted(self.clock, {FIRST: [HANG], SECOND: [HANG]}, duration=55)
        result = _ask(self.board, script)
        self.assertEqual(result.kind, mirrors.LOST)
        self.assertEqual([host for host, _t in script.requests], [FIRST, SECOND])
        self.assertEqual(self.clock.now - 1000.0, 110.0)
        self.assertEqual([verdict.kind for _h, verdict in result.answers],
                         [transport.CLIENT_TIMEOUT] * 2)
        # The next part asks nobody: both rest 120 s, longer than 30 s.
        again = _ask(self.board, script)
        self.assertEqual((again.kind, len(script.requests)), (mirrors.LOST, 2))

    def test_a_hung_first_host_is_left_alone_for_the_next_parts(self) -> None:
        script = Scripted(self.clock, {FIRST: [HANG], SECOND: [OK]}, duration=0.0)
        first = _ask(self.board, script)
        self.assertEqual((first.kind, first.host), (transport.OK, SECOND))
        for _ in range(5):
            self.assertEqual(_ask(self.board, script).host, SECOND)
        self.assertEqual([host for host, _t in script.requests], [FIRST] + [SECOND] * 6)
        # Pacing: the same host was never asked within a second of itself.
        times = [moment for host, moment in script.requests if host == SECOND]
        self.assertTrue(all(b - a >= 1.0 for a, b in zip(times, times[1:])))

    def test_rate_limited_hosts_are_never_asked_twice_in_their_rest(self) -> None:
        script = Scripted(self.clock, {FIRST: [RATE], SECOND: [RATE]})
        self.assertEqual(_ask(self.board, script).kind, mirrors.LOST)
        for _ in range(24):
            self.assertEqual(_ask(self.board, script).kind, mirrors.LOST)
        self.assertEqual(len(script.requests), 2)
        # 59 s later the rest has 1 s left, within the 30 s a part may wait:
        # the part waits it out, and each host is asked again only after its
        # full 60 s (the operators ask for at least 30 s after a 429).
        self.clock.advance(59)
        self.assertEqual(_ask(self.board, script).kind, mirrors.LOST)
        first_times = [moment for host, moment in script.requests if host == FIRST]
        self.assertEqual(len(first_times), 2)
        self.assertGreaterEqual(first_times[1] - first_times[0], 60.0)
        # The second strike doubles the rest.
        self.assertEqual(self.board.remaining(FIRST), 120.0)

    def test_a_short_rest_is_waited_out_a_long_one_is_not(self) -> None:
        script = Scripted(self.clock, {FIRST: [OK], SECOND: [OK]})
        self.board.strike(FIRST, transport.BUSY)    # 45 s
        self.board.strike(SECOND, transport.RATE)   # 60 s
        self.assertEqual(_ask(self.board, script).kind, mirrors.LOST)
        self.assertEqual(script.requests, [])
        self.clock.advance(20)                       # FIRST back in 25 s
        result = _ask(self.board, script)
        self.assertEqual((result.kind, result.host), (transport.OK, FIRST))
        self.assertEqual(script.pauses, [(25.0, FIRST, mirrors.RESTING)])

    def test_a_dropped_connection_gets_exactly_one_more_try(self) -> None:
        script = Scripted(self.clock, {FIRST: [RESET, OK], SECOND: [OK]})
        result = _ask(self.board, script)
        self.assertEqual((result.kind, result.host), (transport.OK, FIRST))
        self.assertEqual([host for host, _t in script.requests], [FIRST, FIRST])
        self.assertIn((5.0, FIRST, mirrors.RESET), script.pauses)
        twice = Scripted(self.clock, {FIRST: [RESET, RESET], SECOND: [OK]})
        board = HostBoard(clock=self.clock)
        result = _ask(board, twice)
        self.assertEqual([host for host, _t in twice.requests], [FIRST, FIRST, SECOND])
        self.assertEqual(board.remaining(FIRST), 120.0)

    def test_no_retry_after_a_timeout_a_429_or_a_5xx(self) -> None:
        for verdict in (HANG, RATE, REFUSED, BUSY):
            with self.subTest(kind=verdict.kind):
                board = HostBoard(clock=self.clock)
                script = Scripted(self.clock, {FIRST: [verdict, OK], SECOND: [OK]})
                result = _ask(board, script)
                self.assertEqual(result.host, SECOND)
                self.assertEqual([host for host, _t in script.requests], [FIRST, SECOND])

    def test_proof_of_density_and_invalid_answers_are_not_sent_to_the_next_host(self) -> None:
        # Out of memory, over 64 MB or 150,000 objects: the same request
        # fails the same way everywhere, so the part is split at once.
        for verdict, kind in (
            (OVERSIZE, transport.TOO_LARGE),
            (OUT_OF_MEMORY, transport.SERVER_MEMORY),
            (INVALID, transport.BAD_QUERY),
        ):
            with self.subTest(kind=kind):
                board = HostBoard(clock=self.clock)
                script = Scripted(self.clock, {FIRST: [verdict], SECOND: [OK]})
                result = _ask(board, script)
                self.assertEqual((result.kind, result.host), (kind, FIRST))
                self.assertEqual([host for host, _t in script.requests], [FIRST])
                self.assertEqual(board.remaining(FIRST), 0.0)

    def test_a_server_timeout_asks_the_next_host_at_the_same_size(self) -> None:
        # An overloaded host times out on a light part too: the other host
        # gets the same part, and its answer is the part.
        told = []
        script = Scripted(self.clock, {FIRST: [HEAVY], SECOND: [OK]})
        result = _ask(self.board, script, notify=lambda host, verdict: told.append(host))
        self.assertEqual((result.kind, result.host), (transport.OK, SECOND))
        self.assertEqual([host for host, _t in script.requests], [FIRST, SECOND])
        self.assertEqual(self.board.remaining(FIRST), 30.0)
        self.assertEqual(told, [FIRST])
        # The next parts go to the host that delivered.
        for _ in range(4):
            self.assertEqual(_ask(self.board, script).host, SECOND)
        self.assertEqual([host for host, _t in script.requests], [FIRST] + [SECOND] * 5)

    def test_a_part_is_too_heavy_only_when_every_host_asked_ran_out_of_time(self) -> None:
        both = Scripted(self.clock, {FIRST: [HEAVY], SECOND: [HEAVY]})
        result = _ask(HostBoard(clock=self.clock), both)
        self.assertEqual((result.kind, result.host), (transport.SERVER_TIMEOUT, FIRST))
        self.assertEqual([host for host, _t in result.answers], [FIRST, SECOND])
        # One timeout and one refusal prove nothing: the part is lost.
        mixed = Scripted(self.clock, {FIRST: [HEAVY], SECOND: [BUSY]})
        self.assertEqual(_ask(HostBoard(clock=self.clock), mixed).kind, mirrors.LOST)
        # A host resting longer than a part waits counts as not asked: with
        # no other host to ask, the one timeout decides.
        board = HostBoard(clock=self.clock)
        board.strike(SECOND, transport.RATE)            # 60 s
        alone = Scripted(self.clock, {FIRST: [HEAVY], SECOND: [OK]})
        result = _ask(board, alone)
        self.assertEqual(result.kind, transport.SERVER_TIMEOUT)
        self.assertEqual([host for host, _t in alone.requests], [FIRST])
        # No host asked at all is no timeout either.
        self.assertEqual(mirrors.unanswered(()).kind, mirrors.LOST)

    def test_inside_a_part_split_for_timeouts_the_first_timeout_decides(self) -> None:
        # Density is suspected there: no other host is asked after a timeout.
        script = Scripted(self.clock, {FIRST: [HEAVY], SECOND: [OK]})
        result = ask_hosts(HOSTS, script.attempt, HostBoard(clock=self.clock), RequestBudget(),
                           script.pause, timeout_ends=True)
        self.assertEqual((result.kind, result.host), (transport.SERVER_TIMEOUT, FIRST))
        self.assertEqual([host for host, _t in script.requests], [FIRST])
        # A refusal still hands the part on; the next host's timeout decides.
        busy_first = Scripted(self.clock, {FIRST: [BUSY], SECOND: [HEAVY]})
        result = ask_hosts(HOSTS, busy_first.attempt, HostBoard(clock=self.clock),
                           RequestBudget(), busy_first.pause, timeout_ends=True)
        self.assertEqual((result.kind, result.host), (transport.SERVER_TIMEOUT, SECOND))

    def test_no_host_is_asked_once_the_caller_says_stop(self) -> None:
        # The download's cap on timeouts: after the one this part may still
        # spend, the second host is not asked.
        script = Scripted(self.clock, {FIRST: [HEAVY], SECOND: [OK]})
        result = ask_hosts(
            HOSTS, script.attempt, HostBoard(clock=self.clock), RequestBudget(), script.pause,
            may_ask=lambda answers: not any(v.kind == transport.SERVER_TIMEOUT for _h, v in answers),
        )
        self.assertEqual([host for host, _t in script.requests], [FIRST])
        self.assertEqual(result.kind, transport.SERVER_TIMEOUT)
        nobody = Scripted(self.clock, {FIRST: [OK], SECOND: [OK]})
        result = ask_hosts(HOSTS, nobody.attempt, HostBoard(clock=self.clock), RequestBudget(),
                           nobody.pause, may_ask=lambda answers: False)
        self.assertEqual((result.kind, nobody.requests), (mirrors.LOST, []))

    def test_after_an_oversize_answer_the_first_host_stays_first(self) -> None:
        board = HostBoard(clock=self.clock)
        big = Scripted(self.clock, {FIRST: [OVERSIZE, OK], SECOND: [OK]})
        self.assertEqual(_ask(board, big).kind, transport.TOO_LARGE)
        self.assertEqual(_ask(board, big).host, FIRST)
        self.assertEqual([host for host, _t in big.requests], [FIRST, FIRST])

    def test_a_host_level_4xx_hands_the_same_part_to_the_next_host(self) -> None:
        script = Scripted(self.clock, {FIRST: [MOVED], SECOND: [OK]})
        result = _ask(self.board, script)
        self.assertEqual((result.kind, result.host), (transport.OK, SECOND))
        self.assertEqual([host for host, _t in script.requests], [FIRST, SECOND])
        self.assertEqual(self.board.remaining(FIRST), 120.0)
        # The next part does not ask the moved endpoint again in its rest.
        self.assertEqual(_ask(self.board, script).host, SECOND)
        self.assertEqual([host for host, _t in script.requests], [FIRST, SECOND, SECOND])

    def test_the_request_budget_is_respected(self) -> None:
        script = Scripted(self.clock, {FIRST: [BUSY], SECOND: [OK]})
        budget = RequestBudget(1)
        result = _ask(self.board, script, budget)
        self.assertEqual(result.kind, mirrors.NO_BUDGET)
        self.assertEqual(([host for host, _t in script.requests], budget.left), ([FIRST], 0))
        # A reset retry also needs budget.
        board = HostBoard(clock=self.clock)
        resets = Scripted(self.clock, {FIRST: [RESET, OK], SECOND: [OK]})
        result = _ask(board, resets, RequestBudget(1))
        self.assertEqual([host for host, _t in resets.requests], [FIRST])

    def test_switching_hosts_is_announced(self) -> None:
        told = []
        script = Scripted(self.clock, {FIRST: [BUSY], SECOND: [BUSY]})
        _ask(self.board, script, notify=lambda host, verdict: told.append((host, verdict.kind)))
        # Only a host followed by another one is announced as a switch.
        self.assertEqual(told, [(FIRST, transport.BUSY)])


if __name__ == "__main__":
    unittest.main()
