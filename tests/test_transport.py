"""Pure checks: classifying one Overpass attempt, and the closing messages."""
from __future__ import annotations

import json
import unittest

from zero2agent_osm_downloader.core import transport
from zero2agent_osm_downloader.core.query import (
    MAX_FEATURES,
    MAX_RESPONSE_BYTES,
    OVERPASS_TIMEOUT_SECONDS,
)

# Real answers saved by the 2026-11-02 audit (overpass-api.de, live): an
# empty result and three buildings in Konak, byte for byte.
LIVE_EMPTY = """{
  "version": 0.6,
  "generator": "Overpass API 0.7.62.11 87bfad18",
  "osm3s": {
    "timestamp_osm_base": "2026-11-02T06:56:30Z",
    "copyright": "The data included in this document is from www.openstreetmap.org. The data is made available under ODbL."
  },
  "elements": [



  ]
}
"""
LIVE_BUILDINGS = """{
  "version": 0.6,
  "generator": "Overpass API 0.7.62.11 87bfad18",
  "osm3s": {
    "timestamp_osm_base": "2026-11-02T06:58:39Z",
    "copyright": "The data included in this document is from www.openstreetmap.org. The data is made available under ODbL."
  },
  "elements": [

{
  "type": "way",
  "id": 867516394,
  "bounds": {
    "minlat": 38.4206691,
    "minlon": 27.1317155,
    "maxlat": 38.4207691,
    "maxlon": 27.1318471
  },
  "nodes": [
    8086098017,
    8086098020,
    8086098021,
    8086098018,
    8086098017
  ],
  "geometry": [
    { "lat": 38.4206916, "lon": 27.1318471 },
    { "lat": 38.4207691, "lon": 27.1317960 },
    { "lat": 38.4207320, "lon": 27.1317155 },
    { "lat": 38.4206691, "lon": 27.1317489 },
    { "lat": 38.4206916, "lon": 27.1318471 }
  ],
  "tags": {
    "building": "retail",
    "building:levels": "2",
    "roof:levels": "0"
  }
},
{
  "type": "way",
  "id": 867516395,
  "bounds": {
    "minlat": 38.4206510,
    "minlon": 27.1316623,
    "maxlat": 38.4207320,
    "maxlon": 27.1317489
  },
  "nodes": [
    8086098021,
    8086098022,
    8086098023,
    8086098018,
    8086098021
  ],
  "geometry": [
    { "lat": 38.4207320, "lon": 27.1317155 },
    { "lat": 38.4207155, "lon": 27.1316623 },
    { "lat": 38.4206510, "lon": 27.1316971 },
    { "lat": 38.4206691, "lon": 27.1317489 },
    { "lat": 38.4207320, "lon": 27.1317155 }
  ],
  "tags": {
    "building": "retail",
    "building:levels": "2",
    "roof:levels": "0"
  }
},
{
  "type": "way",
  "id": 867516396,
  "bounds": {
    "minlat": 38.4206306,
    "minlon": 27.1315993,
    "maxlat": 38.4207155,
    "maxlon": 27.1316971
  },
  "nodes": [
    8086098022,
    8086098024,
    8086098025,
    8086098023,
    8086098022
  ],
  "geometry": [
    { "lat": 38.4207155, "lon": 27.1316623 },
    { "lat": 38.4206950, "lon": 27.1315993 },
    { "lat": 38.4206306, "lon": 27.1316322 },
    { "lat": 38.4206510, "lon": 27.1316971 },
    { "lat": 38.4207155, "lon": 27.1316623 }
  ],
  "tags": {
    "building": "retail",
    "building:levels": "2",
    "roof:levels": "0"
  }
}

  ]
}
"""
# The live 502 overpass-api.de answered in 0.22 s on 2026-11-02.
LIVE_502 = (
    "# 502 Gateway Timeout\n\n"
    "This Overpass API server is currently at capacity. All query workers are busy\n"
    "and the request queue is full.\n\n"
    "## What to do\n\n"
    "Retry your request using exponential backoff: wait a short interval, then retry.\n"
    "If the request fails again, double the wait time and retry again.\n\n"
    "## For heavy or automated use\n\n"
    "This public endpoint is intended for browser-based and human-operated tools.\n"
    "For automated or high-volume use, run a local Overpass API instance:\n\n"
    "https://github.com/b1tw153/Overpass-API/blob/main/README.md\n"
)


def _overpass_page(text: str) -> str:
    """The error page frontend/web_output.cc writes (branch minor_issues)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"\n'
        '    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n'
        "<head>\n"
        '  <meta http-equiv="content-type" content="text/html; charset=utf-8" lang="en"/>\n'
        "  <title>OSM3S Response</title>\n"
        "</head>\n"
        "<body>\n\n"
        "<p>The data included in this document is from www.openstreetmap.org. "
        "The data is made available under ODbL.</p>\n"
        f'<p><strong style="color:#FF0000">Error</strong>: {text} </p>\n'
        "\n</body>\n</html>\n"
    )


PAGE_429 = _overpass_page(
    "runtime error: open64: 0 Success /osm3s_osm_base "
    "Dispatcher_Client::request_read_and_idx::rate_limited. "
    "Please check /api/status for the quota of your IP address."
)
PAGE_504 = _overpass_page(
    "runtime error: open64: 0 Success /osm3s_osm_base "
    "Dispatcher_Client::request_read_and_idx::timeout. "
    "The server is probably too busy to handle your request."
)
PAGE_DUPLICATE = _overpass_page(
    "runtime error: open64: 0 Success /osm3s_osm_base "
    "Dispatcher_Client::request_read_and_idx::duplicate_query"
)
PAGE_400 = _overpass_page("line 2: parse error: Unknown type &quot;&quot;")


def _with_remark(remark: str) -> str:
    """Output_JSON::write_footer: the elements so far, then a "remark"."""
    head, _sep, _tail = LIVE_BUILDINGS.rpartition("]")
    return head + "]" + ',\n"remark": ' + json.dumps(remark) + "\n}\n"


REMARK_TIMEOUT = _with_remark(
    'runtime error: Query timed out in "query" at line 3 after 46 seconds.'
)
REMARK_MEMORY = _with_remark(
    'runtime error: Query ran out of memory in "query" at line 3. '
    "It would need at least 513 MB of RAM to continue."
)
# The dispatcher's refusals, as Output_JSON writes them into a remark
# (frontend/web_output.cc): the query never ran, so nothing is too heavy.
REMARK_NO_SLOT = _with_remark(
    "runtime error: open64: 0 Success /osm3s_osm_base "
    "Dispatcher_Client::request_read_and_idx::timeout. "
    "The server is probably too busy to handle your request."
)
REMARK_RATE = _with_remark(
    "runtime error: open64: 0 Success /osm3s_osm_base "
    "Dispatcher_Client::request_read_and_idx::rate_limited. "
    "Please check /api/status for the quota of your IP address."
)
# What QgsBlockingNetworkRequest reports, measured on QGIS 3.44 and 4.2.
QT_REPLIED_TOO_MANY = (
    "Error transferring https://overpass-api.de/api/interpreter - "
    "server replied: Too Many Requests"
)


def _b(text: str) -> bytes:
    return text.encode("utf-8")


class ClassifyByStatusTests(unittest.TestCase):
    """The status decides first, even though QGIS reports the request failed."""

    def test_rate_and_refusal_classes(self) -> None:
        cases = (
            (429, PAGE_429, transport.RATE),
            (403, "<html>Forbidden</html>", transport.BLOCKED),
            (406, "<html><h1>Not Acceptable</h1></html>", transport.BLOCKED),
            (502, LIVE_502, transport.BUSY),
            (503, "<html><body>Service Unavailable</body></html>", transport.BUSY),
            (504, PAGE_504, transport.BUSY),
            (400, PAGE_400, transport.BAD_QUERY),
            (404, "<html>404 Not Found</html>", transport.HOST_ERROR),
        )
        for status, body, expected in cases:
            with self.subTest(status=status):
                verdict = transport.classify(
                    status, _b(body), error_code=299, error_text=QT_REPLIED_TOO_MANY
                )
                self.assertEqual(verdict.kind, expected)
                self.assertFalse(verdict.retry_same_host)
                self.assertIsNone(verdict.payload)
                self.assertIn(str(status), verdict.detail)

    def test_capacity_page_is_named(self) -> None:
        verdict = transport.classify(502, _b(LIVE_502), 499, "server replied: Bad Gateway")
        self.assertEqual(verdict.detail, "was at capacity (HTTP 502)")

    def test_only_400_says_the_request_itself_is_invalid(self) -> None:
        # A moved endpoint or a proxy answers 404, 405, 410 or 451: that host
        # failed, and the next one may well answer the same request.
        bad = transport.classify(400, _b(PAGE_400), 302, "server replied: Bad Request")
        self.assertEqual(
            (bad.kind, bad.detail),
            (transport.BAD_QUERY, "rejected the request as invalid (HTTP 400)"),
        )
        for status, body in (
            (401, "<html>Unauthorized</html>"),
            (404, "<html><body><h1>404 Not Found</h1></body></html>"),
            (405, "<html>Method Not Allowed</html>"),
            (410, "<html>Gone</html>"),
            (451, "<html>Unavailable For Legal Reasons</html>"),
            (418, ""),
            (301, "<html>Moved Permanently</html>"),
        ):
            with self.subTest(status=status):
                verdict = transport.classify(status, _b(body), 203, "server replied: Not Found")
                self.assertEqual(
                    (verdict.kind, verdict.detail),
                    (transport.HOST_ERROR, f"answered with HTTP {status} instead of data"),
                )
                self.assertNotIn(verdict.kind, transport.HEAVY)


class ClassifyWithoutStatusTests(unittest.TestCase):
    def test_client_timeout_on_both_runtimes(self) -> None:
        # Qt 5: success, empty body, OperationCanceledError.  Qt 6: an error
        # "Operation timed out" with TimeoutError.  Both are "no answer".
        qt5 = transport.classify(0, b"", error_code=5, error_text="", transfer_s=55)
        qt6 = transport.classify(0, b"", 4, "Operation timed out", transfer_s=55)
        qgis_timer = transport.classify(0, b"", 0, "", transfer_s=55)
        for verdict in (qt5, qt6, qgis_timer):
            self.assertEqual(verdict.kind, transport.CLIENT_TIMEOUT)
            self.assertEqual(verdict.detail, "did not answer within 55 seconds")
            self.assertFalse(verdict.retry_same_host)

    def test_network_classes_and_the_one_reset_retry(self) -> None:
        refused = transport.classify(0, b"", 1, "Connection refused")
        missing = transport.classify(0, b"", 3, "Host overpass-a.invalid not found")
        reset = transport.classify(0, b"", 2, "Connection closed")
        secure = transport.classify(0, b"", 6, "SSL handshake failed")
        self.assertEqual(
            [refused.kind, missing.kind, reset.kind, secure.kind],
            [transport.NETWORK, transport.NETWORK, transport.NETWORK, transport.TLS],
        )
        self.assertEqual(
            [refused.retry_same_host, missing.retry_same_host, reset.retry_same_host,
             secure.retry_same_host],
            [False, False, True, False],
        )
        self.assertEqual(refused.detail, "could not be reached (Connection refused)")
        # Qt's "Host x not found" only repeats the name the message puts
        # first, and without it two missing hosts read alike.
        self.assertEqual(missing.detail, "could not be found")

    def test_truncated_answer_with_a_dropped_connection_is_a_reset(self) -> None:
        # A Content-Length mismatch: status 200, half a body, "Connection closed".
        half = _b(LIVE_BUILDINGS[: len(LIVE_BUILDINGS) // 2])
        verdict = transport.classify(200, half, 2, "Connection closed")
        self.assertEqual((verdict.kind, verdict.retry_same_host), (transport.NETWORK, True))


class ClassifyBodyTests(unittest.TestCase):
    def test_live_answers_are_accepted_with_their_elements(self) -> None:
        verdict = transport.classify(200, _b(LIVE_BUILDINGS))
        self.assertTrue(verdict.ok)
        self.assertEqual(
            [element["id"] for element in verdict.payload["elements"]],
            [867516394, 867516395, 867516396],
        )
        self.assertEqual(
            verdict.payload["osm3s"]["timestamp_osm_base"], "2026-11-02T06:58:39Z"
        )
        empty = transport.classify(200, _b(LIVE_EMPTY))
        self.assertTrue(empty.ok)
        self.assertEqual(empty.payload["elements"], [])

    def test_heavy_answers_split_instead_of_failing_over(self) -> None:
        timeout = transport.classify(200, _b(REMARK_TIMEOUT))
        memory = transport.classify(200, _b(REMARK_MEMORY))
        oversize = transport.classify(200, b"", oversize=True)
        self.assertEqual(timeout.kind, transport.SERVER_TIMEOUT)
        self.assertEqual(memory.kind, transport.SERVER_MEMORY)
        self.assertEqual(oversize.kind, transport.TOO_LARGE)
        # The partial elements before a remark are never accepted.
        self.assertIsNone(timeout.payload)
        self.assertTrue({timeout.kind, memory.kind, oversize.kind} <= transport.HEAVY)
        self.assertEqual(oversize.detail, "sent more than 64 MB")

    def test_size_and_count_ceilings(self) -> None:
        too_long = b" " * (MAX_RESPONSE_BYTES + 1)
        self.assertEqual(transport.classify(200, too_long).kind, transport.TOO_LARGE)
        crowded = _b('{"elements":[' + ",".join(["{}"] * (MAX_FEATURES + 1)) + "]}")
        verdict = transport.classify(200, crowded)
        self.assertEqual(verdict.kind, transport.TOO_LARGE)
        self.assertIn(f"{MAX_FEATURES:,}", verdict.detail)
        at_limit = _b('{"elements":[' + ",".join(["{}"] * MAX_FEATURES) + "]}")
        self.assertTrue(transport.classify(200, at_limit).ok)

    def test_a_dispatcher_remark_is_not_a_heavy_part(self) -> None:
        # No query slot came free: another host may have one, so the part
        # goes to it instead of being split.
        no_slot = transport.classify(200, _b(REMARK_NO_SLOT))
        self.assertEqual(
            (no_slot.kind, no_slot.detail), (transport.BUSY, "was too busy to start the query")
        )
        self.assertNotIn(no_slot.kind, transport.HEAVY)
        rate = transport.classify(200, _b(REMARK_RATE))
        self.assertEqual(rate.kind, transport.RATE)
        # A query that did run out of time is still a heavy part.
        self.assertEqual(transport.classify(200, _b(REMARK_TIMEOUT)).kind, transport.SERVER_TIMEOUT)

    def test_error_pages_sent_with_status_200(self) -> None:
        cases = (
            (PAGE_DUPLICATE, transport.BUSY, "was still running the same query"),
            (PAGE_504, transport.BUSY, "was too busy to start the query"),
            (PAGE_429, transport.RATE, "asked this computer to slow down"),
            ("<html><body>Proxy error</body></html>", transport.BUSY,
             "answered with an error page instead of data"),
        )
        for body, kind, detail in cases:
            with self.subTest(detail=detail):
                verdict = transport.classify(200, _b(body))
                self.assertEqual((verdict.kind, verdict.detail), (kind, detail))

    def test_cut_off_json_is_a_network_failure_not_an_error_page(self) -> None:
        half = _b(LIVE_BUILDINGS[: len(LIVE_BUILDINGS) // 2])
        verdict = transport.classify(200, half)
        self.assertEqual(verdict.kind, transport.NETWORK)
        self.assertFalse(verdict.retry_same_host)
        self.assertIn("cut the answer off", verdict.detail)

    def test_answers_without_usable_data(self) -> None:
        self.assertEqual(transport.classify(200, b"[]").kind, transport.BUSY)
        no_elements = transport.classify(200, b'{"version": 0.6}')
        self.assertEqual(no_elements.kind, transport.BUSY)
        damaged = transport.classify(200, b'{"elements": [1, 2]}')
        self.assertEqual(damaged.kind, transport.BUSY)
        empty_reply = transport.classify(200, b"")
        self.assertEqual(empty_reply.kind, transport.NETWORK)


class TimeLimitTests(unittest.TestCase):
    """R5: T = min(55 s, QGIS timeout - 2 s); [timeout:max(10, T - 15)]."""

    def test_formula_table(self) -> None:
        for qgis_ms, transfer, server in (
            (60_000, 55.0, 40),
            (120_000, 55.0, 40),
            (40_000, 38.0, 23),
            (30_000, 28.0, 13),
            (20_000, 18.0, 10),
            (4_000, 2.0, 10),
        ):
            with self.subTest(qgis_ms=qgis_ms):
                self.assertEqual(transport.transfer_seconds(qgis_ms), transfer)
                self.assertEqual(transport.server_timeout_seconds(transfer), server)

    def test_default_header_matches_the_default_qgis_timeout(self) -> None:
        transfer = transport.transfer_seconds(transport.DEFAULT_QGIS_TIMEOUT_MS)
        self.assertEqual(transport.server_timeout_seconds(transfer), OVERPASS_TIMEOUT_SECONDS)

    def test_nonsense_settings_fall_back_to_the_qgis_default(self) -> None:
        for value in (0, -5, None, "x", float("nan")):
            with self.subTest(value=value):
                self.assertEqual(transport.transfer_seconds(value), 55.0)

    def test_short_claims_get_a_short_wait(self) -> None:
        self.assertEqual(transport.transfer_for(15, 55.0), 30.0)
        self.assertEqual(transport.transfer_for(15, 18.0), 18.0)


class MessageTests(unittest.TestCase):
    HOSTS = ("overpass.private.coffee", "overpass-api.de")

    def _report(self, *answers, wait_s=0.0, subject="download", note="", category=""):
        return transport.failure_report(answers, wait_s, subject, note, category)

    def _assert_plain_sentence(self, message: str) -> None:
        # What happened and what to do first, then the hosts in parentheses:
        # a narrow dock shows the start of a message.
        advice, opening, hosts = message.partition(" (")
        self.assertTrue(opening and message.endswith(")"), message)
        self.assertTrue(advice[0].isupper())
        self.assertTrue(advice.endswith("."), advice)
        lowered = message.casefold()
        for word in transport.JARGON:
            self.assertNotIn(word, lowered)
        for host in self.HOSTS:
            self.assertNotIn(host, advice)
            self.assertIn(host, hosts)

    def test_every_host_hanging_reads_as_busy_and_names_both(self) -> None:
        hang = transport.classify(0, b"", 5, "", transfer_s=55)
        report = self._report(
            transport.HostAnswer(self.HOSTS[0], hang.kind, hang.detail),
            transport.HostAnswer(self.HOSTS[1], hang.kind, hang.detail),
            wait_s=118,
        )
        self.assertEqual((report.category, report.kind), ("busy", transport.CLIENT_TIMEOUT))
        self.assertEqual(
            report.message,
            "The OpenStreetMap servers are busy. Nothing was downloaded. Try again "
            "in about 2 minutes. (overpass.private.coffee, overpass-api.de: did not "
            "answer within 55 seconds)",
        )
        self.assertEqual(
            report.summary,
            "overpass.private.coffee, overpass-api.de: did not answer within 55 seconds",
        )
        self._assert_plain_sentence(report.message)

    def test_the_action_comes_first_and_the_hosts_last(self) -> None:
        missing = transport.classify(0, b"", 3, "Host overpass.private.coffee not found")
        report = self._report(
            transport.HostAnswer(self.HOSTS[0], missing.kind, missing.detail),
            transport.HostAnswer(self.HOSTS[1], missing.kind, missing.detail),
        )
        self.assertEqual(
            report.message,
            "No OpenStreetMap server could be reached. Nothing was downloaded. "
            "Check the internet connection and try again. "
            "(overpass.private.coffee, overpass-api.de: could not be found)",
        )
        # Different answers each keep their own entry, in the order asked.
        busy = transport.classify(502, _b(LIVE_502), 499, "server replied: Bad Gateway")
        hang = transport.classify(0, b"", 5, "", transfer_s=55)
        mixed = self._report(
            transport.HostAnswer(self.HOSTS[0], busy.kind, busy.detail),
            transport.HostAnswer(self.HOSTS[1], hang.kind, hang.detail),
            wait_s=40,
        )
        self.assertEqual(
            mixed.message,
            "The OpenStreetMap servers are busy. Nothing was downloaded. Try again "
            "in about a minute. (overpass.private.coffee: was at capacity "
            "(HTTP 502); overpass-api.de: did not answer within 55 seconds)",
        )

    def test_parts_skipped_as_too_dense_are_said_so(self) -> None:
        self.assertEqual(
            transport.too_dense_note(1),
            "1 part is too dense even at the smallest size and was skipped; it will "
            "not be asked again for 24 hours.",
        )
        self.assertEqual(
            transport.too_dense_note(16),
            "16 parts are too dense even at the smallest size and were skipped; they "
            "will not be asked again for 24 hours.",
        )
        # Every part skipped on a re-run, no server asked: still too dense.
        report = self._report(
            transport.HostAnswer(self.HOSTS[0], asked=False),
            transport.HostAnswer(self.HOSTS[1], asked=False),
            note=transport.too_dense_note(2), category="too_dense",
        )
        self.assertEqual(report.category, "too_dense")
        self.assertEqual(
            report.message,
            "This area holds too much data for the servers, even split into the smallest "
            "parts this plugin uses. Nothing was downloaded. Choose fewer datasets or a "
            "smaller area; for a whole city, a regional extract such as Geofabrik's is "
            "faster. 2 parts are too dense even at the smallest size and were skipped; "
            "they will not be asked again for 24 hours. (overpass.private.coffee, "
            "overpass-api.de: not asked)",
        )
        self._assert_plain_sentence(report.message)

    def test_too_dense_names_no_part_size(self) -> None:
        heavy = transport.classify(200, _b(REMARK_TIMEOUT))
        report = self._report(
            transport.HostAnswer(self.HOSTS[0], heavy.kind, heavy.detail),
            transport.HostAnswer(self.HOSTS[1], asked=False),
        )
        self.assertEqual(report.category, "too_dense")
        # A job under 100 km² is split far below 6 km², so no size is named.
        self.assertTrue(report.message.startswith(
            "This area holds too much data for the servers, even split into the "
            "smallest parts this plugin uses. Nothing was downloaded."
        ), report.message)
        self.assertNotIn("km²", report.message)
        self.assertTrue(report.message.endswith(
            "(overpass.private.coffee: ran out of time on this query; "
            "overpass-api.de: not asked)"
        ), report.message)

    def test_category_per_mix_of_answers(self) -> None:
        def answer(host, status=0, code=0, text="", body=""):
            verdict = transport.classify(status, _b(body), code, text)
            return transport.HostAnswer(host, verdict.kind, verdict.detail)

        first, second = self.HOSTS
        cases = (
            ((answer(first, 429), answer(second, 429)), "rate_limited", "slow down"),
            ((answer(first, 406), answer(second, 502, body=LIVE_502)), "refused",
             "daily usage limit"),
            ((answer(first, code=1, text="Connection refused"),
              answer(second, code=6, text="SSL handshake failed")), "offline",
             "internet connection"),
            ((answer(first, 200, body=REMARK_TIMEOUT),
              transport.HostAnswer(second, asked=False)), "too_dense", "regional extract"),
            ((answer(first, 400, body=PAGE_400),
              transport.HostAnswer(second, asked=False)), "bad_query", "regular expression"),
            ((answer(first, 502, body=LIVE_502), answer(second, code=5)), "busy",
             "Try again"),
            ((answer(first, 404, body="<html>Not Found</html>"),
              answer(second, 405, body="<html>Method Not Allowed</html>")), "server_error",
             "answered with an error instead of data"),
        )
        for answers, category, words in cases:
            with self.subTest(category=category):
                report = self._report(*answers)
                self.assertEqual(report.category, category)
                self.assertIn(words, report.message)
                self._assert_plain_sentence(report.message)

    def test_resting_hosts_are_named_with_their_last_answer(self) -> None:
        report = self._report(
            transport.HostAnswer(
                self.HOSTS[0], transport.BUSY, "was busy (HTTP 502)", asked=False, age_s=20
            ),
            transport.HostAnswer(
                self.HOSTS[1], transport.RATE,
                "asked this computer to slow down (HTTP 429)", asked=False, age_s=10,
            ),
            wait_s=25,
        )
        self.assertEqual(report.category, "rate_limited")
        self.assertIn(
            "(overpass.private.coffee: not asked, because it was busy (HTTP 502) "
            "20 seconds ago; overpass-api.de: not asked, because it asked this "
            "computer to slow down (HTTP 429) 10 seconds ago)",
            report.message,
        )
        self.assertIn("Wait about a minute", report.message)
        # Resting for the same reason, each host still keeps its own entry:
        # "because it ..." speaks of one host.
        same = self._report(
            transport.HostAnswer(self.HOSTS[0], transport.BUSY, "was busy (HTTP 502)",
                                 asked=False, age_s=20),
            transport.HostAnswer(self.HOSTS[1], transport.BUSY, "was busy (HTTP 502)",
                                 asked=False, age_s=20),
        )
        self.assertIn(
            "(overpass.private.coffee: not asked, because it was busy (HTTP 502) 20 "
            "seconds ago; overpass-api.de: not asked, because it was busy (HTTP 502) "
            "20 seconds ago)", same.message,
        )

    def test_place_search_wording(self) -> None:
        report = self._report(
            transport.HostAnswer(self.HOSTS[0], transport.BUSY, "was busy (HTTP 503)"),
            transport.HostAnswer(self.HOSTS[1], transport.BUSY, "was busy (HTTP 503)"),
            subject="place search",
        )
        self.assertIn("The place search did not run.", report.message)
        self.assertNotIn("Nothing was downloaded", report.message)

    def test_wait_and_duration_phrases(self) -> None:
        self.assertEqual(transport.wait_text(45), "about a minute")
        self.assertEqual(transport.wait_text(240), "about 4 minutes")
        self.assertEqual(transport.wait_text(3600), "about an hour")
        self.assertEqual(transport.duration_text(1), "a moment")
        self.assertEqual(transport.duration_text(30), "30 seconds")
        self.assertEqual(transport.duration_text(600), "10 minutes")


if __name__ == "__main__":
    unittest.main()
