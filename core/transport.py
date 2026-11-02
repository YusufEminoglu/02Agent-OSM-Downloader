"""What one Overpass attempt came to, and how to say it to a person.

Pure Python by design: the Processing layer owns the network stack and hands
this module the plain facts of one attempt (HTTP status, body, Qt's error
number and text).  Every rule that decides retry, cool-down and splitting is
therefore tested without QGIS.

Status is read first and the body second, because QGIS reports any 4xx or
5xx reply as a failed request while the status and the server's own error
page stay readable; reading only the request's error turned a 429 into a
retry and a 400 into "try another server".  Only a 400 says the request
itself is wrong: a 404 or 405 from a moved endpoint or a proxy is that
host's failure, and the next host may well answer.  An empty body with no
status is the client giving up, which Qt 5 even reports as success.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .query import MAX_FEATURES, MAX_RESPONSE_BYTES

# ---------------------------------------------------------------- classes
OK = "ok"
RATE = "rate"                      # HTTP 429
BLOCKED = "blocked"                # HTTP 403 / 406
BUSY = "busy"                      # 5xx, capacity page, duplicate query
CLIENT_TIMEOUT = "client_timeout"  # nothing usable arrived in time
NETWORK = "network"                # refused, unreachable, reset, cut off
TLS = "tls"                        # the secure connection failed
SERVER_TIMEOUT = "server_timeout"  # `remark`: the query ran out of time
SERVER_MEMORY = "server_memory"    # `remark`: the query ran out of memory
TOO_LARGE = "too_large"            # over 64 MB or over MAX_FEATURES objects
BAD_QUERY = "bad_query"            # HTTP 400: the request itself is refused
HOST_ERROR = "host_error"          # any other status that is not data

#: Answers that say the part may be too heavy for one request.
HEAVY = frozenset({SERVER_TIMEOUT, SERVER_MEMORY, TOO_LARGE})
#: The heavy answers that prove it: the same request would fail the same way
#: on every host, so the part is split at once.  A server timeout alone does
#: not: an overloaded host times out on a light part too, so the next host is
#: asked for the same part first (core/mirrors.py).
DENSE = frozenset({SERVER_MEMORY, TOO_LARGE})

# ------------------------------------------------------------- categories
# What a download that ended without data is called, in the words the
# final message uses.  A category is chosen from the classes of the answers.
BUSY_SERVERS = "busy"
RATE_LIMITED = "rate_limited"
REFUSED = "refused"
OFFLINE = "offline"
TOO_DENSE = "too_dense"
INVALID_REQUEST = "bad_query"
SERVER_ERROR = "server_error"
TOO_MANY = "too_many"
CANCELED = "canceled"
ERROR = "error"

# QNetworkReply::NetworkError values this module reads.
_QT_CONNECTION_REFUSED = 1
_QT_REMOTE_HOST_CLOSED = 2
_QT_HOST_NOT_FOUND = 3
_QT_TIMEOUT = 4
_QT_OPERATION_CANCELED = 5
_QT_SSL_HANDSHAKE_FAILED = 6

# ------------------------------------------------------------- time limits
#: Longest a transfer waits.  QGIS aborts any request at its own network
#: timeout (60 s by default) and reports it as an empty success, so waiting
#: longer than that can never receive anything.
MAX_TRANSFER_SECONDS = 55.0
_QGIS_TIMER_MARGIN_SECONDS = 2.0
#: An accepted query can sit up to 15 s in the server's queue before it runs.
QUEUE_SECONDS = 15.0
MIN_SERVER_TIMEOUT_SECONDS = 10
DEFAULT_QGIS_TIMEOUT_MS = 60_000


def transfer_seconds(qgis_timeout_ms: object) -> float:
    """T = min(55 s, QGIS network timeout - 2 s)."""
    try:
        qgis_seconds = float(qgis_timeout_ms) / 1000.0
    except (TypeError, ValueError):
        qgis_seconds = DEFAULT_QGIS_TIMEOUT_MS / 1000.0
    if not math.isfinite(qgis_seconds) or qgis_seconds <= 0:
        qgis_seconds = DEFAULT_QGIS_TIMEOUT_MS / 1000.0
    return max(1.0, min(MAX_TRANSFER_SECONDS, qgis_seconds - _QGIS_TIMER_MARGIN_SECONDS))


def server_timeout_seconds(transfer_s: float) -> int:
    """`[timeout:N]` with N = max(10, T - 15): what the client can wait out."""
    return max(MIN_SERVER_TIMEOUT_SECONDS, int(float(transfer_s) - QUEUE_SECONDS))


def transfer_for(server_timeout_s: float, transfer_s: float) -> float:
    """The wait for a request that claims less server time than a tile."""
    return max(1.0, min(float(transfer_s), float(server_timeout_s) + QUEUE_SECONDS))


# ----------------------------------------------------------------- verdict
@dataclass(frozen=True)
class Verdict:
    """One attempt on one host.

    `detail` is a verb phrase that completes "<host> ...", for example
    "was busy (HTTP 502)", so messages can name each host and its answer.
    """

    kind: str
    detail: str
    payload: Optional[Dict[str, Any]] = None
    #: Only a dropped connection earns one more try on the same host.
    retry_same_host: bool = False

    @property
    def ok(self) -> bool:
        return self.kind == OK


def _seconds_text(seconds: float) -> str:
    whole = max(1, int(round(float(seconds))))
    return "1 second" if whole == 1 else f"{whole} seconds"


def _short(text: object, limit: int = 90) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _status_verdict(status: int, lowered_head: str) -> Optional[Verdict]:
    if status == 429:
        return Verdict(RATE, "asked this computer to slow down (HTTP 429)")
    if status in (403, 406):
        return Verdict(BLOCKED, f"refused this computer (HTTP {status})")
    if 500 <= status <= 599:
        if "capacity" in lowered_head:
            return Verdict(BUSY, f"was at capacity (HTTP {status})")
        return Verdict(BUSY, f"was busy (HTTP {status})")
    if status == 400:
        return Verdict(BAD_QUERY, "rejected the request as invalid (HTTP 400)")
    if status and not 200 <= status <= 299:
        # 404, 405, 410, 451 ... from a moved endpoint or a proxy, or a
        # redirect that was not followed: this host failed, not the request.
        return Verdict(HOST_ERROR, f"answered with HTTP {status} instead of data")
    return None


def _error_verdict(error_code: int, error_text: str, transfer_s: float) -> Verdict:
    lowered = error_text.casefold()
    detail = _short(error_text)
    if error_code == _QT_SSL_HANDSHAKE_FAILED or any(
        word in lowered for word in ("ssl", "tls", "certificate", "handshake")
    ):
        return Verdict(TLS, f"could not set up a secure connection ({detail or 'TLS'})")
    if error_code in (_QT_TIMEOUT, _QT_OPERATION_CANCELED) or "timed out" in lowered:
        return Verdict(CLIENT_TIMEOUT, f"did not answer within {_seconds_text(transfer_s)}")
    if error_code == _QT_REMOTE_HOST_CLOSED or "closed" in lowered or "reset" in lowered:
        return Verdict(NETWORK, "closed the connection", retry_same_host=True)
    if error_code == _QT_HOST_NOT_FOUND or "not found" in lowered:
        # Qt's text only repeats the host name ("Host x not found"), and
        # without it every host that cannot be found reads alike.
        return Verdict(NETWORK, "could not be found")
    if error_code == _QT_CONNECTION_REFUSED or "refused" in lowered:
        return Verdict(NETWORK, f"could not be reached ({detail or 'connection refused'})")
    return Verdict(NETWORK, f"could not be reached ({detail or 'network error'})")


def _dispatcher_verdict(lowered: str) -> Optional[Verdict]:
    """The dispatcher's refusals, before the query ever ran.

    Overpass writes them into an error page or, for JSON output, into a
    `remark`.  `request_read_and_idx::timeout` means no query slot came free:
    another host may have one, so it must not read as a query that ran out
    of time, which would split the part instead.
    """
    if "rate_limited" in lowered or "too many requests" in lowered:
        return Verdict(RATE, "asked this computer to slow down")
    if "duplicate_query" in lowered:
        return Verdict(BUSY, "was still running the same query")
    if "request_read_and_idx::timeout" in lowered or "too busy" in lowered:
        return Verdict(BUSY, "was too busy to start the query")
    return None


def _page_verdict(lowered_head: str, body: bytes) -> Verdict:
    """A 2xx answer that is not JSON: an error page, or a cut-off answer."""
    refused = _dispatcher_verdict(lowered_head)
    if refused is not None:
        return refused
    if "capacity" in lowered_head:
        return Verdict(BUSY, "was at capacity")
    if "query timed out" in lowered_head:
        return Verdict(SERVER_TIMEOUT, "ran out of time on this query")
    if "out of memory" in lowered_head:
        return Verdict(SERVER_MEMORY, "ran out of memory on this query")
    if body.lstrip()[:1] in (b"{", b"["):
        return Verdict(NETWORK, "cut the answer off before it was complete")
    return Verdict(BUSY, "answered with an error page instead of data")


def _payload_verdict(document: Any) -> Verdict:
    if not isinstance(document, dict):
        return Verdict(BUSY, "sent an answer that is not OSM data")
    remark = str(document.get("remark") or "").strip()
    if remark:
        lowered = remark.casefold()
        refused = _dispatcher_verdict(lowered)
        if refused is not None:
            return refused
        if "timed out" in lowered or "timeout" in lowered:
            return Verdict(SERVER_TIMEOUT, "ran out of time on this query")
        if "out of memory" in lowered or "memory" in lowered:
            return Verdict(SERVER_MEMORY, "ran out of memory on this query")
        return Verdict(BUSY, f"reported a problem ({_short(remark, 70)})")
    elements = document.get("elements")
    if not isinstance(elements, list):
        return Verdict(BUSY, "sent an answer without OSM data")
    if len(elements) > MAX_FEATURES:
        return Verdict(TOO_LARGE, f"sent more than {MAX_FEATURES:,} objects")
    if any(not isinstance(item, dict) for item in elements):
        return Verdict(BUSY, "sent a damaged answer")
    return Verdict(OK, "delivered the data", document)


def classify(
    status: int = 0,
    body: bytes = b"",
    error_code: int = 0,
    error_text: str = "",
    oversize: bool = False,
    transfer_s: float = MAX_TRANSFER_SECONDS,
) -> Verdict:
    """Classify one finished attempt.

    `status` is the HTTP status (0 when no reply arrived), `body` the reply
    content, `error_code` Qt's network error number, `error_text` the
    request's error message (empty when the request reported success), and
    `oversize` is True when the transfer was stopped for passing 64 MB.
    """
    if oversize or len(body) > MAX_RESPONSE_BYTES:
        megabytes = MAX_RESPONSE_BYTES // (1024 * 1024)
        return Verdict(TOO_LARGE, f"sent more than {megabytes} MB")
    head = bytes(body[:2048]).decode("utf-8", "replace")
    lowered_head = head.casefold()
    try:
        status_code = int(status or 0)
    except (TypeError, ValueError):
        status_code = 0
    by_status = _status_verdict(status_code, lowered_head)
    if by_status is not None:
        return by_status
    code = int(error_code or 0)
    text = str(error_text or "").strip()
    if code or text:
        return _error_verdict(code, text, transfer_s)
    if not body:
        if status_code:
            return Verdict(NETWORK, "sent an empty answer")
        # Qt 5 and QGIS's own timer end a stalled transfer this way.
        return Verdict(CLIENT_TIMEOUT, f"did not answer within {_seconds_text(transfer_s)}")
    try:
        document = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _page_verdict(lowered_head, bytes(body))
    return _payload_verdict(document)


# ------------------------------------------------------------- messages
@dataclass(frozen=True)
class HostAnswer:
    """What one host last said, for the message that closes a download."""

    host: str
    kind: str = ""
    detail: str = ""
    asked: bool = True
    age_s: float = 0.0

    def said(self) -> str:
        """What became of the host, without its name: "was busy (HTTP 502)"."""
        if self.asked and self.detail:
            return self.detail
        if self.detail:
            return (
                f"not asked, because it {self.detail} "
                f"{duration_text(self.age_s)} ago"
            )
        return "not asked"


@dataclass(frozen=True)
class FailureReport:
    kind: str
    category: str
    summary: str
    message: str


def duration_text(seconds: float) -> str:
    value = max(0.0, float(seconds))
    if value < 2:
        return "a moment"
    if value < 90:
        return _seconds_text(value)
    if value < 3300:
        return f"{int(math.ceil(value / 60.0))} minutes"
    hours = int(round(value / 3600.0))
    return "an hour" if hours <= 1 else f"{hours} hours"


def wait_text(seconds: float) -> str:
    """'about a minute', 'about 4 minutes', 'about an hour'."""
    value = max(0.0, float(seconds))
    if value <= 90:
        return "about a minute"
    if value < 3300:
        return f"about {int(math.ceil(value / 60.0))} minutes"
    hours = int(math.ceil(value / 3600.0))
    return "about an hour" if hours <= 1 else f"about {hours} hours"


def host_summary(answers: Iterable[HostAnswer]) -> str:
    """Each host and its answer: "a, b: did not answer within 55 seconds".

    Hosts that answered alike share one entry; entries keep the order the
    hosts were first listed in and are separated by semicolons.  A host that
    was not asked because of an earlier answer keeps its own entry, whose
    "because it ..." speaks of one host.
    """
    groups: Dict[Tuple[str, str], List[str]] = {}
    for answer in answers:
        said = answer.said()
        alone = answer.host if not answer.asked and answer.detail else ""
        groups.setdefault((alone, said), []).append(answer.host)
    return "; ".join(
        f"{', '.join(hosts)}: {said}" for (_alone, said), hosts in groups.items()
    )


def category_for(kinds: Sequence[str]) -> Tuple[str, str]:
    """(category, dominant class) for the classes the hosts answered with."""
    present = [kind for kind in kinds if kind and kind != OK]
    if not present:
        return BUSY_SERVERS, ""
    if BAD_QUERY in present:
        return INVALID_REQUEST, BAD_QUERY
    if all(kind in HEAVY for kind in present):
        return TOO_DENSE, present[0]
    if all(kind in (NETWORK, TLS) for kind in present):
        return OFFLINE, present[0]
    if BLOCKED in present:
        return REFUSED, BLOCKED
    if RATE in present:
        return RATE_LIMITED, RATE
    for kind in (BUSY, CLIENT_TIMEOUT):
        if kind in present:
            return BUSY_SERVERS, kind
    if HOST_ERROR in present:
        return SERVER_ERROR, HOST_ERROR
    return BUSY_SERVERS, present[0]


_SUBJECTS = {
    "download": "Nothing was downloaded.",
    "place search": "The place search did not run.",
}

# What happened and what to do come first; the hosts and their answers follow
# in parentheses, because a narrow dock shows the start of a message and a
# list of host names pushed the advice out of sight.
_TEMPLATES = {
    BUSY_SERVERS: "The OpenStreetMap servers are busy. {nothing} Try again in {wait}.",
    RATE_LIMITED: (
        "The OpenStreetMap servers asked this computer to slow down. "
        "{nothing} Wait {wait} before trying again."
    ),
    REFUSED: (
        "The OpenStreetMap servers refused this computer. This usually means a "
        "daily usage limit was reached. {nothing} Try again in {wait}."
    ),
    OFFLINE: (
        "No OpenStreetMap server could be reached. {nothing} Check the "
        "internet connection and try again."
    ),
    TOO_DENSE: (
        "This area holds too much data for the servers, even split into the "
        "smallest parts this plugin uses. {nothing} Choose fewer datasets or a "
        "smaller area; for a whole city, a regional extract such as "
        "Geofabrik's is faster."
    ),
    INVALID_REQUEST: (
        "The OpenStreetMap server rejected the request as invalid. {nothing} "
        "Check the tag keys and values; a regular expression may use a form "
        "the server does not accept."
    ),
    SERVER_ERROR: (
        "The OpenStreetMap servers answered with an error instead of data. "
        "{nothing} Try again in {wait}."
    ),
}


def too_dense_note(count: int) -> str:
    """The sentence for parts skipped as too dense at the smallest size."""
    if count == 1:
        return (
            "1 part is too dense even at the smallest size and was skipped; "
            "it will not be asked again for 24 hours."
        )
    return (
        f"{count:,} parts are too dense even at the smallest size and were "
        "skipped; they will not be asked again for 24 hours."
    )


def timeouts_note(count: int) -> str:
    """The sentence for a download stopped after `count` server timeouts."""
    return (
        f"The servers kept running out of time ({count} times), so no more "
        "requests were sent."
    )


def failure_report(
    answers: Sequence[HostAnswer],
    wait_s: float = 0.0,
    subject: str = "download",
    note: str = "",
    category: str = "",
) -> FailureReport:
    """The complete sentence for a download that ended without any data.

    The category is decided by the hosts asked during this download; when
    none could be asked because all were resting after earlier failures,
    by what they answered then, unless the caller names one.  The message
    is the advice, then `note` when there is one, then the hosts and their
    answers in parentheses (`summary`).
    """
    asked = [answer for answer in answers if answer.asked and answer.kind]
    relevant = asked or [answer for answer in answers if answer.kind]
    found, kind = category_for([answer.kind for answer in relevant])
    category = category if category in _TEMPLATES else found
    summary = host_summary(answers) or "no server was asked"
    advice = _TEMPLATES[category].format(
        nothing=_SUBJECTS.get(subject, _SUBJECTS["download"]),
        wait=wait_text(wait_s),
    )
    if note:
        advice = f"{advice} {note}"
    return FailureReport(kind, category, summary, f"{advice} ({summary})")


#: Words the closing messages must never use: they mean nothing to the
#: person reading them.
JARGON = ("mirror", "pinned", "backend", "endpoint", "remark", "tile")
