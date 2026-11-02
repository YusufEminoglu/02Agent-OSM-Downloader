"""Preset and custom-tag OSM Processing algorithms."""
from __future__ import annotations

import configparser
import contextlib
import json
import re
import sqlite3
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

from qgis.PyQt.QtCore import QByteArray, QMetaType, Qt, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureSink,
    QgsFeedback,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsNetworkRequestParameters,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingOutputNumber,
    QgsProcessingOutputString,
    QgsReferencedRectangle,
    QgsRectangle,
    QgsProject,
    QgsWkbTypes,
)

from ..core import mirrors, postpass, transport
from ..core.around import (
    MAX_RADIUS_M,
    circle_bbox,
    circle_ring,
    validate_centre,
    validate_radius,
)
from ..core.cache import UNFINISHED_JOB_TTL_SECONDS, QueryCache, cache_refusal
from ..core.catalog import (
    PRESETS_BY_ID,
    GEOMETRY_KINDS,
    TagSpec,
)
from ..core.geocoding import (
    MAX_BOUNDARY_BYTES,
    NOMINATIM_HOST,
    NOMINATIM_MIN_INTERVAL_SECONDS,
    GeocodeError,
    boundary_geojson,
    build_lookup_url,
    build_search_url,
    parse_boundary,
    parse_geocode_results,
)
from ..core.postpass import POSTPASS_ENDPOINT, render_sql
from ..core.history import LEGACY_PRESET_ORDER
from ..core.places import (
    PlaceCandidate,
    build_place_query,
    parse_place_candidates,
)
from ..core.osm_areas import is_closed_ring, relation_is_area, way_is_area
from ..core.schema import FIELD_NAMES, FIELD_TYPES, feature_attributes
from ..core.query import (
    is_admin_boundary_spec,
    MAX_ADVANCED_FILTERS,
    MAX_BBOX_AREA_KM2,
    MAX_JOB_REQUESTS,
    MAX_RESPONSE_BYTES,
    MAX_TILE_AREA_KM2,
    MAX_TILES,
    OVERPASS_ENDPOINTS,
    OVERPASS_TIMEOUT_SECONDS,
    PLACE_SEARCH_TIMEOUT_SECONDS,
    TOO_MANY_OBJECTS_MESSAGE,
    ElementMerger,
    QueryError,
    TileRequest,
    advanced_specs,
    bbox_area_km2,
    excludes_tag,
    matching_specs,
    normalize_tag,
    normalized_specs,
    osm_timestamp,
    plan_requests,
    validate_area_id,
    validate_payload,
    with_overpass_header,
)

#: Sent as the Referer on every request.  QGIS replaces a plugin's own
#: User-Agent with its stock one but keeps this header, so the server
#: operators can tell this plugin's traffic apart and know whom to contact.
REFERER = "https://github.com/YusufEminoglu/02Agent-OSM-Downloader"
_PRODUCT = "02Agent-OSM-Downloader"
_CACHE_TTL_SECONDS = 60 * 60
_CACHE_LIMIT = 300
_QUERY_CACHE: Optional[QueryCache] = None
_QUERY_CACHE_LOCK = threading.RLock()
#: Nominatim answers in well under a second when it is up; a hung geocoder
#: should not hold a place search for the full transfer timeout.
_NOMINATIM_TIMEOUT_SECONDS = 15.0
#: Once data is arriving -- from a server or from the cache -- a download
#: stops after this many parts in a row that no server could deliver; before
#: any data, the first such part stops it.  A part too dense even at the
#: smallest size is not such a part: it is marked and skipped.
_LOST_PARTS_LIMIT = 2
#: A download stops sending requests after this many answers that ran out
#: of server time.  Each one cost a server up to its full time limit against
#: a quota of about ten minutes a day per address; past this, the servers
#: are overloaded more likely than the area is dense.
_TIMEOUTS_LIMIT = 6


def _cache() -> QueryCache:
    """Return the process-wide persistent Overpass response cache.

    Resolved lazily against the real QGIS profile directory rather than at
    import time, so importing this module never touches disk on its own —
    only an actual fetch does (see docs/TRAPS.md 4.6 on `QgsSettings`
    blocking against an uninitialised profile).  Opening it purges the rows
    that expired since the last session.
    """
    global _QUERY_CACHE
    with _QUERY_CACHE_LOCK:
        if _QUERY_CACHE is None:
            db_path = (
                Path(QgsApplication.qgisSettingsDirPath())
                / "zero2agent_osm_downloader"
                / "overpass_cache.sqlite3"
            )
            _QUERY_CACHE = QueryCache(
                db_path, ttl_seconds=_CACHE_TTL_SECONDS, limit=_CACHE_LIMIT
            )
        return _QUERY_CACHE


def _cached_entry(query: str) -> Optional[Tuple[Dict, str]]:
    """A validated cache entry and the host it came from; faults never block."""
    try:
        found = _cache().entry(query)
        if found is None:
            return None
        return validate_payload(found.payload), found.source
    except QueryError:
        with contextlib.suppress(OSError, sqlite3.Error):
            _cache().delete(query)
        return None
    except (OSError, sqlite3.Error):
        return None


def _cached(query: str) -> Optional[Dict]:
    """Return a validated cache entry; cache faults never block a download."""
    found = _cached_entry(query)
    return found[0] if found is not None else None


def _store_cached(query: str, payload: Dict, feedback, source: str = "") -> bool:
    """Best-effort cache write: storage trouble must not discard live data.

    Empty answers, answers the server marked incomplete and data more than a
    week old are never stored, so a retry asks again instead of replaying them.
    """
    reason = cache_refusal(payload)
    if reason:
        if reason != "it is empty":
            feedback.pushInfo(f"This answer is not kept in the local cache: {reason}.")
        return False
    try:
        _cache().set(query, payload, source=source)
    except (OSError, sqlite3.Error) as error:
        feedback.pushInfo(
            "The OSM response cache is unavailable; continuing without it "
            f"({type(error).__name__})."
        )
        return False
    return True


def _keep_for_continue(queries: Sequence[str]) -> None:
    """Keep the parts of an unfinished download for a day, for a re-run."""
    with contextlib.suppress(OSError, sqlite3.Error):
        _cache().keep_until(queries, UNFINISHED_JOB_TTL_SECONDS)


def _saved_below(request: TileRequest) -> bool:
    """True when a smaller part of `request` has a live row in the cache.

    A part that an earlier run of the same download split was saved as its
    quarters, never as itself, and a quarter too dense even at the smallest
    size left a marker.  Splitting it again without a request lets those
    rows answer, so a re-run (Continue) asks only for what is still missing
    instead of for the dense part first.  Cache faults read as nothing saved.
    """
    if not request.can_split:
        return False
    parts = list(request.split())
    try:
        cache = _cache()
        while parts:
            part = parts.pop()
            key = part.cache_key()
            if cache.has(key) or cache.is_too_dense(key):
                return True
            if part.can_split:
                parts.extend(part.split())
    except (OSError, sqlite3.Error):
        return False
    return False


def _marked_too_dense(request: TileRequest) -> bool:
    """True while an earlier run's too-dense marker for this part lives."""
    try:
        return _cache().is_too_dense(request.cache_key())
    except (OSError, sqlite3.Error):
        return False


def _mark_too_dense(request: TileRequest, source: str) -> None:
    """Skip this part for a day: it is too dense even at the smallest size."""
    with contextlib.suppress(OSError, sqlite3.Error):
        _cache().mark_too_dense(request.cache_key(), source)


def clear_cache() -> int:
    """Clear the persistent Overpass response cache and return the count removed."""
    try:
        return _cache().clear()
    except (OSError, sqlite3.Error):
        return 0


class _DownloadFailure(QgsProcessingException):
    """A download that ended without data, with its failure class attached.

    `kind` is the class of the deciding answer (core/transport.py), and
    `category` the plain name of the outcome the final message uses.
    """

    def __init__(
        self, message: str, kind: str = "", category: str = "", summary: str = ""
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.category = category
        self.summary = summary


def _canceled() -> _DownloadFailure:
    return _DownloadFailure(
        "The OSM download was canceled.", category=transport.CANCELED
    )


def _cancellable_wait(seconds: float, feedback) -> None:
    """Wait politely while checking cancellation at short intervals."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if feedback.isCanceled():
            raise _canceled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _event(
    feedback,
    phase: str,
    text: str,
    host: str = "",
    part: int = 0,
    parts: int = 0,
    kind: str = "",
    category: str = "",
    detail: str = "",
    **extra,
) -> None:
    """Report one step of a download to the live status and to the log.

    A feedback object with a callable `zero2agent_event` attribute receives a
    dict per step (called on the download's worker thread); every step is
    also written to the Processing log, so the log stays complete on its own.
    """
    event = {
        "phase": phase,
        "host": host,
        "part": int(part),
        "parts": int(parts),
        "kind": kind,
        "category": category,
        "detail": detail or text,
    }
    event.update(extra)
    hook = getattr(feedback, "zero2agent_event", None)
    if callable(hook):
        # The status display is a convenience; a fault in it must never
        # break the download it describes.
        with contextlib.suppress(Exception):
            hook(event)
    if text:
        feedback.pushInfo(text)


def _known_header(name: str):
    enum = getattr(QNetworkRequest, "KnownHeaders", None)
    return getattr(enum, name) if enum is not None else getattr(
        QNetworkRequest, name
    )


def _status_attribute():
    enum = getattr(QNetworkRequest, "Attribute", None)
    if enum is not None:
        return enum.HttpStatusCodeAttribute
    return QNetworkRequest.HttpStatusCodeAttribute


def _reply_status(reply) -> int:
    try:
        return int(reply.attribute(_status_attribute()))
    except (TypeError, ValueError):
        return 0


def _enum_int(value) -> int:
    """An enum's number on Qt 5 (int enums) and Qt 6 (Python enums)."""
    with contextlib.suppress(TypeError, ValueError):
        return int(getattr(value, "value", value))
    return 0


_VERSION: Optional[str] = None
_UA_ATTRIBUTE_PROBED = False
_UA_ATTRIBUTE = None


def _plugin_version() -> str:
    global _VERSION
    if _VERSION is None:
        version = ""
        with contextlib.suppress(OSError, KeyError, configparser.Error):
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(
                Path(__file__).resolve().parents[1] / "metadata.txt",
                encoding="utf-8",
            )
            version = parser["general"]["version"].strip()
        _VERSION = version
    return _VERSION


def _user_agent_suffix() -> str:
    version = _plugin_version()
    product = f"{_PRODUCT}/{version}" if version else _PRODUCT
    return f"{product} (+{REFERER})"


def _user_agent_attribute():
    """The request attribute QGIS appends to its User-Agent (QGIS 4.2+).

    Older QGIS has no such attribute and overwrites any User-Agent a plugin
    sets, so there the Referer is the only identification that arrives.
    """
    global _UA_ATTRIBUTE_PROBED, _UA_ATTRIBUTE
    if not _UA_ATTRIBUTE_PROBED:
        attribute = None
        with contextlib.suppress(Exception):
            raw = getattr(QgsNetworkRequestParameters, "AttributeUserAgentSuffix", None)
            if raw is None:
                enum = getattr(QgsNetworkRequestParameters, "RequestAttributes", None)
                raw = getattr(enum, "AttributeUserAgentSuffix", None)
            if raw is not None:
                attribute = QNetworkRequest.Attribute(int(getattr(raw, "value", raw)))
        _UA_ATTRIBUTE = attribute
        _UA_ATTRIBUTE_PROBED = True
    return _UA_ATTRIBUTE


def _identify(request: QNetworkRequest) -> None:
    """Name the plugin on a request: Referer always, User-Agent suffix where QGIS allows."""
    request.setRawHeader(b"Referer", REFERER.encode("ascii"))
    attribute = _user_agent_attribute()
    if attribute is not None:
        with contextlib.suppress(Exception):
            request.setAttribute(attribute, _user_agent_suffix())


def _transfer_seconds() -> float:
    """T = min(55 s, QGIS network timeout - 2 s), read from the user's QGIS."""
    timeout_ms = transport.DEFAULT_QGIS_TIMEOUT_MS
    with contextlib.suppress(Exception):
        timeout_ms = QgsNetworkAccessManager.timeout()
    return transport.transfer_seconds(timeout_ms)


def _form_body(query: str) -> bytes:
    # Standard form encoding: a literal "+" would reach the server as a
    # space, turning a regex such as ^[0-9]+$ into ^[0-9] $.
    return urlencode({"data": query}).encode("ascii")


def _host_name(url: str) -> str:
    return urlsplit(url).netloc or url


def _post_once(
    url: str, body: bytes, transfer_s: float, feedback, classify=transport.classify
) -> transport.Verdict:
    """Send one request to one host and classify what came back.

    The transfer runs under its own feedback object, cancelled either by the
    caller's Cancel or as soon as the answer passes 64 MB, so an oversized
    answer is never downloaded in full.  `classify` reads the answer: the
    Overpass rules by default, `postpass.classify` for Postpass.
    """
    request = QNetworkRequest(QUrl(url))
    if hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout(max(1000, int(round(transfer_s * 1000))))
    request.setHeader(
        _known_header("ContentTypeHeader"),
        "application/x-www-form-urlencoded",
    )
    request.setRawHeader(b"Accept", b"application/json")
    _identify(request)

    guard = QgsFeedback()
    oversize: List[int] = []

    def watch(received, _total) -> None:
        if received > MAX_RESPONSE_BYTES and not oversize:
            oversize.append(int(received))
            guard.cancel()

    client = QgsBlockingNetworkRequest()
    # For a POST, QGIS reports the bytes received through uploadProgress;
    # both signals are watched, and a direct connection lets the check run
    # on the thread doing the transfer even when the caller's is blocked.
    for name in ("downloadProgress", "uploadProgress"):
        signal = getattr(client, name, None)
        if signal is not None:
            signal.connect(watch, Qt.ConnectionType.DirectConnection)
    canceled = getattr(feedback, "canceled", None)
    linked = False
    if canceled is not None and hasattr(canceled, "connect"):
        canceled.connect(guard.cancel)
        linked = True
    try:
        if feedback.isCanceled():
            guard.cancel()
        code = client.post(request, QByteArray(body), False, guard)
    except Exception as error:  # noqa: BLE001 - one host's fault, not the job's
        return transport.Verdict(
            transport.NETWORK,
            f"could not be reached (the request failed: {type(error).__name__})",
        )
    finally:
        if linked:
            with contextlib.suppress(TypeError, RuntimeError):
                canceled.disconnect(guard.cancel)
    if feedback.isCanceled():
        raise _canceled()
    reply = client.reply()
    error_text = ""
    if code != QgsBlockingNetworkRequest.NoError:
        error_text = str(client.errorMessage() or "").strip()
    return classify(
        status=_reply_status(reply),
        body=b"" if oversize else bytes(reply.content()),
        error_code=_enum_int(reply.error()),
        error_text=error_text,
        oversize=bool(oversize),
        transfer_s=transfer_s,
    )


class _Job:
    """What one download has spent and heard so far."""

    def __init__(self, transfer_s: float, limit: int = MAX_JOB_REQUESTS) -> None:
        self.transfer_s = float(transfer_s)
        self.budget = mirrors.RequestBudget(limit)
        #: host -> the last failed answer it gave during this download
        self.failures: Dict[str, transport.Verdict] = {}
        #: Answers that ran out of server time, in this whole download.
        self.timeouts = 0
        #: The part being asked lies inside one split for timeouts: its own
        #: first timeout splits it, without asking another host.
        self.split_on_timeout = False

    def may_ask(self, answers: Sequence[Tuple[str, transport.Verdict]]) -> bool:
        """False once this download, with `answers` of the part being asked,
        has had _TIMEOUTS_LIMIT server timeouts."""
        current = sum(1 for _host, verdict in answers if verdict.kind == transport.SERVER_TIMEOUT)
        return self.timeouts + current < _TIMEOUTS_LIMIT


def _part_name(part: int, parts: int) -> str:
    return f"part {part} of {parts}" if parts > 1 else "the OSM data"


class _OverpassBackend:
    """The pinned Overpass hosts, asked one at a time in the board's order.

    `_fetch_all` asks each backend from `_backends()` in turn for a part and
    moves to the next only when this one returns mirrors.LOST.
    """

    name = "Overpass"
    #: How an answer from these hosts is read (core/transport.py).
    classify = staticmethod(transport.classify)

    def __init__(self, endpoints: Sequence[str]) -> None:
        self._urls = {_host_name(url): url for url in endpoints}
        self.hosts: Tuple[str, ...] = tuple(self._urls)

    def fetch(self, request: TileRequest, job: _Job, feedback, part: int, parts: int):
        query = request.render(transport.server_timeout_seconds(job.transfer_s))
        return self.fetch_text(
            query, job, feedback, part, parts, job.transfer_s, _part_name(part, parts)
        )

    def fetch_text(
        self,
        query: str,
        job: _Job,
        feedback,
        part: int,
        parts: int,
        transfer_s: float,
        what: str,
        announce: str = "",
    ) -> mirrors.HostResult:
        """Ask the hosts for one request; `announce` is said before the first."""
        body = _form_body(query)
        announced: List[str] = []
        switched: List[bool] = []

        def attempt(host: str) -> transport.Verdict:
            if announce and not switched:
                switched.append(True)
                _event(
                    feedback, "switch", announce,
                    host=host, part=part, parts=parts,
                )
            _event(
                feedback, "request", f"Asking {host} for {what} …",
                host=host, part=part, parts=parts,
            )
            return _post_once(
                self._urls[host], body, transfer_s, feedback, self.classify
            )

        def pause(seconds: float, host: str, why: str) -> None:
            if why == mirrors.RESTING:
                _event(
                    feedback, "waiting",
                    f"Waiting {transport.duration_text(seconds)} for {host} "
                    "to accept requests again.",
                    host=host, part=part, parts=parts,
                )
            elif why == mirrors.RESET:
                _event(
                    feedback, "waiting",
                    f"{host} closed the connection; asking it once more in "
                    f"{transport.duration_text(seconds)}.",
                    host=host, part=part, parts=parts, kind=transport.NETWORK,
                )
            elif why == mirrors.IN_USE and host not in announced:
                announced.append(host)
                _event(
                    feedback, "waiting",
                    f"Waiting for {host}: another download is using it.",
                    host=host, part=part, parts=parts,
                )
            _cancellable_wait(seconds, feedback)

        def notify(host: str, verdict: transport.Verdict) -> None:
            _event(
                feedback, "switch",
                f"{host} {verdict.detail}; trying another server.",
                host=host, part=part, parts=parts, kind=verdict.kind,
            )

        result = mirrors.ask_hosts(
            self.hosts, attempt, mirrors.BOARD, job.budget, pause, notify,
            timeout_ends=job.split_on_timeout, may_ask=job.may_ask,
        )
        for host, verdict in result.answers:
            if not verdict.ok:
                job.failures[host] = verdict
        return result


class _PostpassBackend(_OverpassBackend):
    """Geofabrik's Postpass, for a part no Overpass host could deliver.

    The same selection as SQL (core/postpass.py), sent under the same
    per-host rules: one request at a time, at least a second apart, a rest
    after a failure, one unit of the job's budget per request.  Its answer is
    adapted into Overpass elements before anything else sees it, and a part
    it delivers is cached under the same key as an Overpass part, so a later
    run finds it whichever server answered.
    """

    name = "Postpass"
    classify = staticmethod(postpass.classify)

    def fetch(self, request: TileRequest, job: _Job, feedback, part: int, parts: int):
        statement = render_sql(
            request.specs, request.bbox, request.match_mode,
            request.value_mode, request.area_id,
        )
        what = _part_name(part, parts)
        return self.fetch_text(
            statement, job, feedback, part, parts, job.transfer_s, what,
            announce=(
                "The OpenStreetMap servers are busy; asking Geofabrik's "
                f"Postpass service for {what}."
            ),
        )


def _backends() -> Tuple[_OverpassBackend, ...]:
    """The sources a part is asked from, in order; Overpass is always first.

    Postpass is asked for a part only when every Overpass host failed or is
    resting for it (`_fetch_part`), and only when the download allows it.
    """
    return (
        _OverpassBackend(OVERPASS_ENDPOINTS),
        _PostpassBackend((POSTPASS_ENDPOINT,)),
    )


def seconds_until_a_server_is_ready(allow_fallback: bool = False) -> float:
    """Seconds until a download could ask a server; 0.0 when one may be asked now.

    The servers are those a download would ask -- the Overpass hosts, and
    Postpass when `allow_fallback` is true -- and their rests come from the
    board every download in this QGIS session shares.  When every one of
    them rests after an earlier failure, the shortest rest left is returned,
    so a caller can offer Continue when it can actually fetch something.
    """
    backends = _backends()
    if not allow_fallback:
        backends = backends[:1]
    rests = [
        mirrors.BOARD.remaining(host)
        for backend in backends
        for host in backend.hosts
    ]
    if not rests or min(rests) <= 0.0:
        return 0.0
    return min(rests)


def _host_answers(job: _Job, hosts: Sequence[str]) -> List[transport.HostAnswer]:
    """How each host failed: during this download, or else the last time."""
    answers = []
    for host in dict.fromkeys(hosts):
        verdict = job.failures.get(host)
        if verdict is not None:
            answers.append(transport.HostAnswer(host, verdict.kind, verdict.detail))
            continue
        last = mirrors.BOARD.last_answer(host)
        if last is not None and last[0] != transport.OK:
            answers.append(
                transport.HostAnswer(host, last[0], last[1], asked=False, age_s=last[2])
            )
        else:
            answers.append(transport.HostAnswer(host, asked=False))
    return answers


def _job_failure(
    job: _Job,
    hosts: Sequence[str],
    subject: str = "download",
    note: str = "",
    category: str = "",
) -> _DownloadFailure:
    wait_s = min((mirrors.BOARD.remaining(host) for host in hosts), default=0.0)
    report = transport.failure_report(
        _host_answers(job, hosts), wait_s, subject, note, category
    )
    return _DownloadFailure(
        report.message, report.kind, report.category, report.summary
    )


def _fetch_json(query: str, feedback) -> Dict:
    """Fetch one pre-rendered Overpass request (the place-name search).

    It travels the same polite path as the download parts: cache first,
    then the hosts in the board's order, at most one attempt each.
    """
    hit = _cached_entry(query)
    if hit is not None:
        feedback.pushInfo("Using the saved copy of this request.")
        return hit[0]
    claimed = re.search(r"\[timeout:(\d+)\]", query)
    server_s = int(claimed.group(1)) if claimed else OVERPASS_TIMEOUT_SECONDS
    transfer_s = transport.transfer_for(server_s, _transfer_seconds())
    job = _Job(transfer_s)
    backend = _backends()[0]
    result = backend.fetch_text(
        query, job, feedback, 0, 0, transfer_s, "the place search"
    )
    if result.kind == transport.OK:
        _store_cached(query, result.payload, feedback, result.host)
        return result.payload
    raise _job_failure(job, backend.hosts, "place search")


_NOMINATIM_LOCK = threading.RLock()
_NOMINATIM_LAST_REQUEST = 0.0


def _prepared_request(url: str) -> QNetworkRequest:
    request = QNetworkRequest(QUrl(url))
    if hasattr(request, "setTransferTimeout"):
        seconds = min(_transfer_seconds(), _NOMINATIM_TIMEOUT_SECONDS)
        request.setTransferTimeout(int(seconds * 1000))
    request.setRawHeader(b"Accept", b"application/json")
    _identify(request)
    return request


def _nominatim_reply(url: str, feedback, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """The body of one Nominatim answer, under its one-request-per-second rule.

    The place search and the boundary lookup both come through here: one
    lock spaces their requests, each names the plugin and waits at most the
    short Nominatim timeout.  Raises GeocodeError when Nominatim could not
    answer, answered with an error, or sent more than `limit` bytes.
    """
    global _NOMINATIM_LAST_REQUEST

    with _NOMINATIM_LOCK:
        elapsed = time.monotonic() - _NOMINATIM_LAST_REQUEST
        if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
            _cancellable_wait(
                NOMINATIM_MIN_INTERVAL_SECONDS - elapsed, feedback
            )
        _NOMINATIM_LAST_REQUEST = time.monotonic()

    client = QgsBlockingNetworkRequest()
    code = client.get(_prepared_request(url), False, feedback)
    if feedback.isCanceled():
        raise _canceled()
    reply = client.reply()
    status = _reply_status(reply)
    if status and not 200 <= status < 300:
        raise GeocodeError(f"Nominatim answered with HTTP {status}.")
    if code != QgsBlockingNetworkRequest.NoError:
        detail = str(client.errorMessage() or "").strip() or "network request failed"
        raise GeocodeError(f"Nominatim could not be reached ({detail}).")
    content = bytes(reply.content())
    if not content:
        seconds = min(_transfer_seconds(), _NOMINATIM_TIMEOUT_SECONDS)
        raise GeocodeError(
            f"Nominatim did not answer within {transport.duration_text(seconds)}."
        )
    if len(content) > limit:
        raise GeocodeError("The Nominatim response was unexpectedly large.")
    return content


def _geocode(place: str, feedback) -> Tuple[PlaceCandidate, ...]:
    """Look a place up with Nominatim, honouring its one-request-per-second rule.

    Raises GeocodeError when Nominatim could not answer; an empty result is
    an answer ("no such place") and returns an empty tuple.
    """
    content = _nominatim_reply(build_search_url(place), feedback)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeocodeError("Nominatim returned an unreadable response.") from exc
    return parse_geocode_results(payload, place)


def _outline_geometry(geojson: Dict) -> Optional[QgsGeometry]:
    """A place outline (core.geocoding.boundary_geojson) as a valid multipolygon."""
    polygons = (
        geojson["coordinates"] if geojson["type"] == "MultiPolygon"
        else [geojson["coordinates"]]
    )
    geometry = QgsGeometry.fromMultiPolygonXY([
        [[QgsPointXY(lon, lat) for lon, lat in ring] for ring in polygon]
        for polygon in polygons
    ])
    if geometry.isNull() or geometry.isEmpty():
        return None
    if geometry.isGeosValid():
        return geometry
    return _polygonal(geometry.makeValid())


def _place_boundary(area_id: int, feedback) -> Tuple[Optional[QgsGeometry], str]:
    """(outline, "") of the place an Overpass area id names, or (None, why).

    The outline is Nominatim's (`lookup`, simplified to about 11 m), so the
    clip no longer depends on the area tables an Overpass server may have
    stopped updating, and it is the same whichever server answers.  It is
    kept in the persistent cache for a day under its lookup URL, so running
    the same download again cuts the same parts without asking again.
    """
    url = build_lookup_url(area_id)
    found = None
    with contextlib.suppress(OSError, sqlite3.Error):
        found = _cache().entry(url)
    geojson = (
        boundary_geojson(found.payload.get("boundary")) if found is not None else None
    )
    if geojson is not None:
        feedback.pushInfo("Using the saved outline of the place.")
    else:
        try:
            content = _nominatim_reply(url, feedback, MAX_BOUNDARY_BYTES)
        except GeocodeError as error:
            return None, str(error)
        geojson = parse_boundary(content, area_id)
        if geojson is None:
            return None, "Nominatim sent no outline for this place."
        with contextlib.suppress(OSError, sqlite3.Error):
            _cache().set(
                url, {"boundary": geojson},
                ttl_seconds=UNFINISHED_JOB_TTL_SECONDS, source=NOMINATIM_HOST,
            )
    outline = _outline_geometry(geojson)
    if outline is None:
        return None, "The outline Nominatim sent is not a usable area."
    return outline, ""


def _overlap(
    bbox: Tuple[float, float, float, float], box: QgsRectangle
) -> Optional[Tuple[float, float, float, float]]:
    """The part of a (south, west, north, east) extent inside a WGS84 box.

    None when they do not overlap: nothing of the place lies in the extent.
    """
    south, west, north, east = bbox
    south, west = max(south, box.yMinimum()), max(west, box.xMinimum())
    north, east = min(north, box.yMaximum()), min(east, box.xMaximum())
    if not (south < north and west < east):
        return None
    return south, west, north, east


def resolve_place(place: str, feedback) -> Tuple[PlaceCandidate, ...]:
    """Resolve a place name with Nominatim, searching Overpass only if it failed.

    Nominatim understands free-form text and ranks worldwide results.  A plain
    "no match" is an answer, so it is returned as such: the Overpass search
    would run a costly worldwide name match only to find nothing either.  Only
    when Nominatim could not answer does the Overpass administrative search
    run, once per server, with a short `[timeout:15]`.
    """
    try:
        candidates = _geocode(place, feedback)
    except GeocodeError as error:
        feedback.pushInfo(
            f"{error} Searching the OpenStreetMap administrative names instead."
        )
        query = with_overpass_header(
            build_place_query(place), PLACE_SEARCH_TIMEOUT_SECONDS
        )
        return parse_place_candidates(_fetch_json(query, feedback), place)
    if candidates:
        feedback.pushInfo(
            f"Nominatim matched {len(candidates)} place(s) for '{place}'."
        )
    else:
        feedback.pushInfo(f"Nominatim found no place called '{place}'.")
    return candidates


class FetchResult(NamedTuple):
    """The merged data of a download and what it could not get."""

    payload: Dict
    #: Parts the download was finally made of (a split part counts as four).
    total: int
    missing: Tuple[TileRequest, ...]
    #: Hosts that delivered data, in the order they first did.
    sources: Tuple[str, ...]
    #: Requests sent over the network.
    requests: int
    #: Why parts are missing, in plain words; "" when nothing is missing.
    gaps: str


def _fetch_part(backends, request, job, feedback, part, parts) -> mirrors.HostResult:
    """Ask each backend in turn; the next when this one could not deliver.

    A backend whose hosts lost the part, or all ran out of time on it, hands
    it to the next backend at the same size: a server timeout may only mean
    an overloaded host.  The part counts as too heavy (SERVER_TIMEOUT, so
    it is split) only when every host asked, of every backend, ran out of
    time; otherwise it is lost.  Either way it carries what every host
    answered, so the message about it weighs them all.  Inside a part split
    for timeouts (`job.split_on_timeout`) the first timeout decides, and no
    backend is asked once the download has had its _TIMEOUTS_LIMIT.
    """
    answers: List[Tuple[str, transport.Verdict]] = []
    for backend in backends:
        if job.timeouts >= _TIMEOUTS_LIMIT:
            break
        result = backend.fetch(request, job, feedback, part, parts)
        answers.extend(result.answers)
        job.timeouts += sum(
            1 for _host, verdict in result.answers
            if verdict.kind == transport.SERVER_TIMEOUT
        )
        if result.kind == transport.SERVER_TIMEOUT and job.split_on_timeout:
            return result
        if result.kind not in (mirrors.LOST, transport.SERVER_TIMEOUT):
            return result
    return mirrors.unanswered(answers)


def _heavy_hosts(result: mirrors.HostResult) -> str:
    """The hosts that found a part too heavy: "a", or "a and b"."""
    names = list(dict.fromkeys(
        host for host, verdict in result.answers if verdict.kind in transport.HEAVY
    )) or [result.host]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _remember(sources: List[str], source: str) -> None:
    if source and source not in sources:
        sources.append(source)


def _depth_proven(request: TileRequest, split_from: Dict, waiting: Dict) -> int:
    """`request` arrived: the depth its timeout split proves, or 0.

    A part split because every server asked ran out of time is proven too
    heavy only when all of its smaller parts arrive (from a server or the
    cache); then the parts after it start at their size, as after an
    out-of-memory answer.  One of them lost, out of time or skipped never
    arrives, so that split proves nothing.
    """
    parent = split_from.pop(request.bbox, None)
    if parent is None:
        return 0
    waiting[parent] -= 1
    if waiting[parent]:
        return 0
    del waiting[parent]
    return request.depth


def _gap_text(job: _Job, hosts, missing: int, total: int, heavy: int, stop: str) -> str:
    """How a download with gaps ended: what to do first, the hosts last."""
    sentences = [
        f"Finished with gaps: {missing} of {total} parts are missing.",
        "The parts that arrived are kept for 24 hours, so running the same "
        "download again fetches only the missing ones.",
    ]
    if stop == "budget":
        sentences.append(
            f"The download used all {job.budget.limit} of its requests."
        )
    if stop == "timeouts":
        sentences.append(transport.timeouts_note(job.timeouts))
    if heavy:
        sentences.append(transport.too_dense_note(heavy))
    hosts_said = ""
    if missing > heavy and stop not in ("budget", "timeouts"):
        others = "other missing parts" if heavy else "missing parts"
        sentences.append(f"No server could deliver the {others}.")
        hosts_said = f" ({transport.host_summary(_host_answers(job, hosts))})"
    return " ".join(sentences) + hosts_said


def _fetch_all(
    requests: Sequence[TileRequest],
    feedback,
    start_progress: float = 5.0,
    end_progress: float = 40.0,
    allow_fallback: bool = False,
) -> FetchResult:
    """Fetch every part of a download and merge what arrives.

    Each part is taken from the cache when it can be, otherwise from the
    backends in order: Postpass only after every Overpass host failed or is
    resting for that part, and only when `allow_fallback` is True (the
    fallback is optional and off by default).  A part
    is split into four (twice at most) when a server proves it too heavy
    (out of memory, over 64 MB or 150,000 objects) -- and the parts after it
    then start at that smaller size -- or when every server asked ran out of
    time on it, which proves the same only once all four of its smaller
    parts arrive.  A part an
    earlier run split is split again without a request when its smaller
    parts are saved.  A part still too heavy at the smallest size is marked
    for 24 hours and skipped, then and on any re-run, without counting as a
    lost part.  The job stops early when the first part fails on every
    server before anything arrived, after two lost parts in a row once data
    is flowing (from a server or from the cache), at the request ceiling, or
    at the object cap; parts already saved are still collected from the
    cache.  A request Postpass refuses as invalid costs only that part, and
    Postpass is not asked again during the job.  The result keeps everything
    that arrived and lists what is missing, so a download with gaps never
    looks complete.
    """
    pending = deque(requests)
    total = len(pending)
    if total > 1:
        feedback.pushInfo(
            f"The extent needs {total} bounded OSM requests. "
            "Downloading them in sequence."
        )
    job = _Job(_transfer_seconds())
    backends = _backends()
    if not allow_fallback:
        backends = backends[:1]
    hosts = tuple(host for backend in backends for host in backend.hosts)
    #: The fallback's own hosts: a request they refuse says nothing about
    #: the Overpass query, which is written differently.
    fallback_hosts = frozenset(
        host for backend in backends[1:] for host in backend.hosts
    ) - set(backends[0].hosts)
    merger = ElementMerger()
    missing: List[TileRequest] = []
    sources: List[str] = []
    kept: List[str] = []
    finished = delivered = heavy = 0
    learned_depth = 0
    lost_in_a_row = 0
    fetched = False
    stop = ""
    complete = False
    progress = start_progress
    #: Boxes of the parts inside one split because every server asked ran
    #: out of time: density is suspected there, so their first timeout
    #: splits them at once.
    suspected: set = set()
    #: The suspected part that timed out before any data arrived, set aside
    #: until another part shows whether the servers deliver at all; at most
    #: one per download, with the answer it gave.
    set_aside: set = set()
    set_aside_results: Dict[Tuple[float, float, float, float], mirrors.HostResult] = {}
    #: Timeout splits still waiting for their smaller parts: the box each
    #: smaller part came from, and how many have not arrived yet.
    timeout_split_from: Dict[
        Tuple[float, float, float, float], Tuple[float, float, float, float]
    ] = {}
    timeout_split_waiting: Dict[Tuple[float, float, float, float], int] = {}
    try:
        while pending:
            if feedback.isCanceled():
                raise _canceled()
            request = pending.popleft()
            part = finished + 1
            key = request.cache_key()
            hit = _cached_entry(key)
            if hit is not None:
                payload, source = hit
                merger.add(payload)
                kept.append(key)
                _remember(sources, source or "local cache")
                # Saved data is data: a re-run that meets a lost part after
                # collecting saved ones is not a job where nothing arrived.
                fetched = True
                lost_in_a_row = 0
                finished += 1
                delivered += 1
                learned_depth = max(learned_depth, _depth_proven(
                    request, timeout_split_from, timeout_split_waiting))
                _event(
                    feedback, "part_done",
                    f"Part {part} of {total}: using the saved copy "
                    f"({len(payload.get('elements', [])):,} objects).",
                    host=source, part=part, parts=total, cached=True,
                )
            elif _marked_too_dense(request):
                # Found too dense at the smallest size within a day: missing
                # without a request, and no reason to stop the job.
                missing.append(request)
                finished += 1
                heavy += 1
                _event(
                    feedback, "part_lost",
                    f"Part {part} of {total} is missing: it was found too dense "
                    "even at the smallest size in the last 24 hours, so it is "
                    "not asked again.",
                    part=part, parts=total,
                )
            elif _saved_below(request):
                # An earlier run split this part and saved some of its
                # smaller parts, or marked one too dense: take those, ask
                # only for the rest.
                children = request.split()
                if request.bbox in suspected:
                    suspected.update(child.bbox for child in children)
                pending.extendleft(reversed(children))
                total += len(children) - 1
                _event(
                    feedback, "split",
                    f"Part {part} is split into {len(children)} smaller parts, "
                    "as in the earlier download whose parts are saved.",
                    part=part, parts=total,
                )
                continue
            elif stop:
                missing.append(request)
                finished += 1
            elif request.depth < learned_depth and request.can_split:
                children = request.split()
                if request.bbox in suspected:
                    suspected.update(child.bbox for child in children)
                pending.extendleft(reversed(children))
                total += len(children) - 1
                _event(
                    feedback, "split",
                    f"Part {part} is split into {len(children)} smaller parts "
                    "in advance, like the dense part before it.",
                    part=part, parts=total,
                )
                continue
            else:
                # A part set aside comes back with the timeout it gave
                # before; any other part is asked now.
                result = set_aside_results.pop(request.bbox, None)
                revisited = result is not None
                if not revisited:
                    job.split_on_timeout = request.bbox in suspected
                    try:
                        result = _fetch_part(backends, request, job, feedback, part, total)
                    finally:
                        job.split_on_timeout = False
                    if not stop and job.timeouts >= _TIMEOUTS_LIMIT:
                        # Past the cap no request is sent; this answer is
                        # still read, the rest is collected from the cache.
                        stop = "timeouts"
                        feedback.pushInfo(
                            f"{transport.timeouts_note(job.timeouts)} Saved parts are "
                            "still collected."
                        )
                if (
                    result.kind == transport.SERVER_TIMEOUT
                    and request.bbox in suspected
                    and not fetched
                ):
                    if not revisited and not set_aside:
                        # Timeouts on smaller parts too, and nothing has
                        # arrived: overload or a dense spot.  Another part
                        # decides: if it arrives, this one is dense.
                        set_aside.add(request.bbox)
                        set_aside_results[request.bbox] = result
                        pending.append(request)
                        _event(
                            feedback, "switch",
                            f"{_heavy_hosts(result)} {result.detail}; part {part} "
                            "is set aside until another part arrives.",
                            host=result.host, part=part, parts=total, kind=result.kind,
                        )
                        continue
                    # A second such timeout before anything arrived: the
                    # servers are overloaded, not the area dense.  Nothing is
                    # marked; the rest waits for a later run.
                    stop = "overload"
                    missing.append(request)
                    finished += 1
                    _event(
                        feedback, "part_lost",
                        f"Part {part} of {total} is missing: {_heavy_hosts(result)} "
                        f"{result.detail} on a smaller part as well, before any "
                        "part arrived, so the servers seem overloaded.",
                        host=result.host, part=part, parts=total, kind=result.kind,
                    )
                    if pending:
                        feedback.pushInfo(
                            "No more requests are sent for this download; "
                            "saved parts are still collected."
                        )
                elif result.kind == transport.OK:
                    merger.add(result.payload)
                    if _store_cached(key, result.payload, feedback, result.host):
                        kept.append(key)
                    _remember(sources, result.host)
                    fetched = True
                    lost_in_a_row = 0
                    finished += 1
                    delivered += 1
                    learned_depth = max(learned_depth, _depth_proven(
                        request, timeout_split_from, timeout_split_waiting))
                    _event(
                        feedback, "part_done",
                        f"Part {part} of {total} arrived from {result.host} "
                        f"({len(result.payload.get('elements', [])):,} objects).",
                        host=result.host, part=part, parts=total, cached=False,
                    )
                elif result.kind in transport.HEAVY and request.can_split:
                    children = request.split()
                    if result.kind in transport.DENSE:
                        # Proven too heavy: the parts after it start smaller.
                        learned_depth = max(learned_depth, request.depth + 1)
                    elif request.bbox not in suspected:
                        # Every server asked ran out of time: proven too
                        # heavy only once all four smaller parts arrive (see
                        # _depth_proven).  A split at the first timeout
                        # inside a suspected part proves nothing.
                        timeout_split_waiting[request.bbox] = len(children)
                        timeout_split_from.update(
                            (child.bbox, request.bbox) for child in children
                        )
                    if result.kind == transport.SERVER_TIMEOUT or request.bbox in suspected:
                        # Density is suspected inside: a timeout there
                        # splits at once.
                        suspected.update(child.bbox for child in children)
                    pending.extendleft(reversed(children))
                    total += len(children) - 1
                    _event(
                        feedback, "split",
                        f"{_heavy_hosts(result)} {result.detail}; part {part} is "
                        f"split into {len(children)} smaller parts.",
                        host=result.host, part=part, parts=total, kind=result.kind,
                    )
                    continue
                else:
                    missing.append(request)
                    finished += 1
                    kind = result.kind
                    lost_limit = _LOST_PARTS_LIMIT if fetched else 1
                    if kind in transport.HEAVY:
                        # Too heavy even at the smallest size: marked for a
                        # day and skipped, here and on any re-run.  It does
                        # not count toward stopping, so the light parts
                        # after it are still fetched.
                        heavy += 1
                        _mark_too_dense(request, result.host)
                        why = (
                            f"{_heavy_hosts(result)} {result.detail}, even at the "
                            "smallest part size; it will not be asked again for "
                            "24 hours"
                        )
                    elif kind == transport.BAD_QUERY and result.host in fallback_hosts:
                        # Postpass refused its own statement: only this part
                        # is lost, and the Overpass hosts go on alone.
                        backends = backends[:1]
                        lost_in_a_row += 1
                        why = (
                            f"{result.host} {result.detail}, so it is not asked "
                            "again during this download"
                        )
                        if lost_in_a_row >= lost_limit:
                            stop = "lost"
                    elif kind == transport.BAD_QUERY:
                        stop = "bad_query"
                        why = f"{result.host} {result.detail}"
                    elif kind == mirrors.NO_BUDGET:
                        stop = "budget"
                        why = f"this download has used all {job.budget.limit} of its requests"
                    else:
                        lost_in_a_row += 1
                        answered = [verdict.kind for _host, verdict in result.answers]
                        kind = transport.category_for(answered)[1] if answered else ""
                        why = "no server could deliver it"
                        if lost_in_a_row >= lost_limit:
                            stop = "lost"
                    _event(
                        feedback, "part_lost",
                        f"Part {part} of {total} is missing: {why}.",
                        host=result.host, part=part, parts=total, kind=kind,
                    )
                    if stop and pending:
                        feedback.pushInfo(
                            "No more requests are sent for this download; "
                            "saved parts are still collected."
                        )
            if merger.over_limit:
                raise _DownloadFailure(
                    TOO_MANY_OBJECTS_MESSAGE, category=transport.TOO_MANY
                )
            # A split adds parts, so the share done can dip; the bar never does.
            progress = max(
                progress,
                start_progress
                + (end_progress - start_progress) * finished / max(1, total),
            )
            feedback.setProgress(progress)
        if not delivered:
            # Parts skipped as too dense are said so; when they are all that
            # is missing, the area is too dense whatever the hosts last said.
            # Timeouts that stopped the download mean busy servers.
            notes = []
            if stop == "timeouts":
                notes.append(transport.timeouts_note(job.timeouts))
            if heavy:
                notes.append(transport.too_dense_note(heavy))
            if stop in ("overload", "timeouts"):
                category = transport.BUSY_SERVERS
            elif heavy and heavy == len(missing):
                category = transport.TOO_DENSE
            else:
                category = ""
            raise _job_failure(job, hosts, note=" ".join(notes), category=category)
        gaps = (
            _gap_text(job, hosts, len(missing), total, heavy, stop) if missing else ""
        )
        if total > 1:
            feedback.pushInfo(
                f"Merged {delivered} of {total} parts into "
                f"{merger.count:,} OSM objects."
            )
        complete = not missing
        return FetchResult(
            merger.result(), total, tuple(missing), tuple(sources),
            job.budget.spent, gaps,
        )
    finally:
        if not complete and kept:
            _keep_for_continue(kept)


def _points(coordinates: object) -> List[QgsPointXY]:
    if not isinstance(coordinates, list):
        return []
    result: List[QgsPointXY] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, dict):
            return []
        try:
            result.append(
                QgsPointXY(
                    float(coordinate["lon"]),
                    float(coordinate["lat"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return []
    return result


#: Why a matched feature could not be built or written (FEATURES_DROPPED),
#: in the order the summary lists them.  A tag the user excluded
#: (EXCLUDE_KEY) was asked to be left out and is never counted here.
DROP_NO_GEOMETRY = "no geometry"
DROP_UNCLOSED = "unclosed area outline"
DROP_INVALID = "invalid and unrepairable"
DROP_TRANSFORM = "transform failed"
DROP_UNWRITABLE = "could not be written"
DROP_REASONS: Tuple[str, ...] = (
    DROP_NO_GEOMETRY,
    DROP_UNCLOSED,
    DROP_INVALID,
    DROP_TRANSFORM,
    DROP_UNWRITABLE,
)
#: Where a matched feature lies that was left out because it is outside the
#: chosen area (FEATURES_OUTSIDE).  Nothing is wrong with such a feature:
#: Overpass returns whatever touches a box, Postpass whatever box overlaps it.
DROP_OUTSIDE_EXTENT = "outside extent"
DROP_OUTSIDE_CIRCLE = "outside the around circle"
DROP_OUTSIDE_OUTLINE = "outside the place outline"
OUTSIDE_REASONS: Tuple[str, ...] = (
    DROP_OUTSIDE_EXTENT,
    DROP_OUTSIDE_CIRCLE,
    DROP_OUTSIDE_OUTLINE,
)
#: reason -> (phrase for one, phrase for several), after the count.
_DROP_PHRASES: Dict[str, Tuple[str, str]] = {
    DROP_NO_GEOMETRY: ("with no coordinates", "with no coordinates"),
    DROP_UNCLOSED: (
        "area outline that does not close", "area outlines that do not close",
    ),
    DROP_INVALID: (
        "invalid outline that could not be repaired",
        "invalid outlines that could not be repaired",
    ),
    DROP_TRANSFORM: (
        "that could not be projected to the output CRS",
        "that could not be projected to the output CRS",
    ),
    DROP_UNWRITABLE: (
        "that QGIS could not write to the output",
        "that QGIS could not write to the output",
    ),
    DROP_OUTSIDE_EXTENT: (
        "outside the requested extent", "outside the requested extent",
    ),
    DROP_OUTSIDE_CIRCLE: (
        "outside the search circle", "outside the search circle",
    ),
    DROP_OUTSIDE_OUTLINE: (
        "outside the boundary of the place", "outside the boundary of the place",
    ),
}


def _listed(counts: Dict[str, int], reasons: Sequence[str]) -> str:
    """The counts in words: "3 area outlines that do not close, 1 with no ...".

    Largest first; equal counts keep the order of `reasons`.
    """
    present = sorted(
        (reason for reason in reasons if counts.get(reason)),
        key=lambda reason: (-counts[reason], list(reasons).index(reason)),
    )
    return ", ".join(
        f"{counts[reason]:,} {_DROP_PHRASES[reason][counts[reason] != 1]}"
        for reason in present
    )


def _drop_summary(dropped: Dict[str, int], repaired: int = 0) -> str:
    """One plain sentence: what was not written and why; "" when nothing."""
    total = sum(dropped.get(reason, 0) for reason in DROP_REASONS)
    sentences = []
    if total:
        noun = "feature was" if total == 1 else "features were"
        sentences.append(
            f"{total:,} OSM {noun} not written: {_listed(dropped, DROP_REASONS)}."
        )
    if repaired:
        noun = "outline was" if repaired == 1 else "outlines were"
        sentences.append(f"{repaired:,} invalid {noun} repaired and kept.")
    return " ".join(sentences)


def _refusal_message(kind: str, count: int, sink) -> str:
    """The error for an output that refused every feature offered to it."""
    detail = ""
    with contextlib.suppress(Exception):
        detail = " ".join(str(sink.lastError() or "").split())
    what = f"the one {kind}" if count == 1 else f"any of the {count:,} {kind}s"
    reason = f" ({detail})" if detail else ""
    return (
        f"QGIS could not write {what} to the output{reason}. Check that the "
        "output file can be written, then run the download again."
    )


def _outside_summary(outside: Dict[str, int]) -> str:
    """One plain sentence on what lay outside the chosen area; "" when nothing."""
    total = sum(outside.get(reason, 0) for reason in OUTSIDE_REASONS)
    if not total:
        return ""
    head = (
        "1 matched OSM feature was left out because it lies" if total == 1
        else f"{total:,} matched OSM features were left out because they lie"
    )
    present = [reason for reason in OUTSIDE_REASONS if outside.get(reason)]
    if len(present) == 1:
        where = _DROP_PHRASES[present[0]][0]
        return f"{head} {where}."
    return f"{head} outside the chosen area: {_listed(outside, OUTSIDE_REASONS)}."


class _Built(NamedTuple):
    """The geometries one matched feature produced, or why there are none."""

    geometries: List[QgsGeometry]
    reason: str = ""
    #: Geometries that were invalid and kept after repair.
    repaired: int = 0
    #: What the extent test reads instead of each geometry: a relation's
    #: label point stands for the area it labels.
    footprint: Optional[QgsGeometry] = None


class _RelationArea(NamedTuple):
    geometry: Optional[QgsGeometry]
    reason: str = ""
    repaired: bool = False


def _closed_ring(coordinates: object) -> List[QgsPointXY]:
    """A closed way's ring; [] for an open way, which is never closed here."""
    if not is_closed_ring(coordinates):
        return []
    return _points(coordinates)


def _polygonal(geometry: Optional[QgsGeometry]) -> Optional[QgsGeometry]:
    """The polygon parts of a geometry as one multipolygon, or None.

    `makeValid` can return a collection that mixes the repaired area with
    the lines and points it collapsed to; only the area is a polygon.
    """
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    if geometry.type() == QgsWkbTypes.GeometryType.PolygonGeometry:
        result = QgsGeometry(geometry)
    else:
        parts = [
            part for part in geometry.asGeometryCollection()
            if part.type() == QgsWkbTypes.GeometryType.PolygonGeometry
            and not part.isEmpty()
        ]
        if not parts:
            return None
        result = QgsGeometry.unaryUnion(parts)
        if result.isNull() or result.isEmpty():
            return None
    result.convertToMultiType()
    return result


def _valid_polygon(polygon: QgsGeometry) -> Tuple[Optional[QgsGeometry], bool]:
    """(polygon, repaired): an invalid one is repaired with makeValid."""
    if polygon.isGeosValid():
        return polygon, False
    repaired = _polygonal(polygon.makeValid())
    if repaired is None or not repaired.isGeosValid():
        return None, True
    return repaired, True


#: Member roles whose ways outline a relation's area.  Which ring is a hole
#: is decided by nesting, so "outer" and "inner" are not trusted to say it.
_AREA_ROLES = frozenset(("", "outer", "inner", "exclave", "enclave"))
#: Relations that outline their area under a role of their own: a building
#: relation's "outline" (its "part"s lie inside it), a site's "perimeter".
_TYPE_AREA_ROLES = {
    "building": frozenset(("outline",)),
    "site": frozenset(("perimeter",)),
}


def _ring_segments(element: Dict) -> List[List[QgsPointXY]]:
    """The way geometries a relation's rings are made of, each way once.

    A way listed twice -- a common mapping slip -- would otherwise close a
    zero-area ring with itself, or nest inside its own copy as a hole.
    """
    members = element.get("members")
    if not isinstance(members, list):
        return []
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    roles = _TYPE_AREA_ROLES.get(
        str(tags.get("type") or "").strip().lower(), _AREA_ROLES
    )
    segments = []
    seen = set()
    for member in members:
        if (
            not isinstance(member, dict)
            or member.get("type") != "way"
            or str(member.get("role") or "").strip().lower() not in roles
        ):
            continue
        points = _points(member.get("geometry"))
        if len(points) < 2:
            continue
        key = tuple((point.x(), point.y()) for point in points)
        if key in seen or key[::-1] in seen:
            continue
        seen.add(key)
        segments.append(points)
    return segments


def _chain_rings(segments: List[List[QgsPointXY]]) -> List[List[QgsPointXY]]:
    """Join fragmented member ways into closed rings by their endpoints.

    Multipolygon relations commonly split one boundary across several open
    member ways. Treating every member as a standalone polygon silently drops
    those buildings and land-use areas, so assemble matching endpoints first.
    """
    endpoints: Dict[Tuple[float, float], List[int]] = {}
    for index, segment in enumerate(segments):
        for point in (segment[0], segment[-1]):
            key = (point.x(), point.y())
            endpoints.setdefault(key, []).append(index)
    unused = set(range(len(segments)))

    def take_segment(point: QgsPointXY) -> Optional[int]:
        candidates = endpoints.get((point.x(), point.y()), [])
        while candidates:
            candidate = candidates.pop()
            if candidate in unused:
                unused.remove(candidate)
                return candidate
        return None

    rings: List[List[QgsPointXY]] = []
    while unused:
        first = unused.pop()
        chain = list(segments[first])
        while chain[0] != chain[-1]:
            index = take_segment(chain[-1])
            attach_to_end = index is not None
            if index is None:
                index = take_segment(chain[0])
            if index is None:
                break
            segment = segments[index]
            if attach_to_end and chain[-1] == segment[0]:
                chain.extend(segment[1:])
            elif attach_to_end:
                chain.extend(reversed(segment[:-1]))
            elif chain[0] == segment[-1]:
                chain = segment[:-1] + chain
            else:
                chain = list(reversed(segment[1:])) + chain
        if len(chain) >= 4 and chain[0] == chain[-1]:
            rings.append(chain)
    return rings


def _nested_area(
    rings: List[List[QgsPointXY]],
) -> Tuple[Optional[QgsGeometry], bool]:
    """(area, repaired) from rings, holes decided by nesting (even/odd depth).

    A ring inside an even number of other rings is land, inside an odd
    number a hole -- what libosmium, osm2pgsql and OGR do -- so an island in
    a lake survives and swapped or missing roles cannot erase a feature.
    On the 3,491 area relations of Izmir and Istanbul, every result matches
    libosmium's assembly within 0.5%; reading roles, 12 did not.
    """
    polygons = []
    repaired = False
    for ring in rings:
        polygon = QgsGeometry.fromPolygonXY([ring])
        if polygon.isNull() or polygon.isEmpty():
            continue
        polygon, fixed = _valid_polygon(polygon)
        repaired = repaired or fixed
        if polygon is not None:
            polygons.append(polygon)
    if not polygons:
        return None, repaired
    polygons.sort(key=lambda item: -item.area())
    boxes = [polygon.boundingBox() for polygon in polygons]
    depths = []
    for index, polygon in enumerate(polygons):
        probe = polygon.pointOnSurface()
        depths.append(sum(
            1 for outer in range(index)
            if boxes[outer].contains(boxes[index])
            and polygons[outer].contains(probe)
        ))
    result: Optional[QgsGeometry] = None
    for level in sorted({depth - depth % 2 for depth in depths}):
        shells = [item for item, depth in zip(polygons, depths) if depth == level]
        holes = [item for item, depth in zip(polygons, depths) if depth == level + 1]
        part = QgsGeometry.unaryUnion(shells)
        if holes and not part.isEmpty():
            part = part.difference(QgsGeometry.unaryUnion(holes))
        if part.isNull() or part.isEmpty():
            continue
        result = part if result is None else QgsGeometry.unaryUnion([result, part])
    result = _polygonal(result)
    if result is None:
        return None, repaired
    result, fixed = _valid_polygon(result)
    return result, repaired or fixed


def _relation_area(element: Dict) -> _RelationArea:
    """A relation's area, or why it has none."""
    segments = _ring_segments(element)
    if not segments:
        return _RelationArea(None, DROP_NO_GEOMETRY)
    rings = _chain_rings(segments)
    if not rings:
        return _RelationArea(None, DROP_UNCLOSED)
    geometry, repaired = _nested_area(rings)
    if geometry is None:
        return _RelationArea(None, DROP_INVALID, repaired)
    return _RelationArea(geometry, "", repaired)


def _relation_polygon(element: Dict) -> Optional[QgsGeometry]:
    return _relation_area(element).geometry


def _relation_line(element: Dict) -> Optional[QgsGeometry]:
    """Join route relation member ways into a multi-line geometry."""
    members = element.get("members")
    if not isinstance(members, list):
        return None
    lines = []
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        points = _points(member.get("geometry"))
        if len(points) >= 2:
            lines.append(QgsGeometry.fromPolylineXY(points))
    if not lines:
        return None
    geometry = QgsGeometry.unaryUnion(lines)
    if geometry.isEmpty():
        return None
    geometry.convertToMultiType()
    return geometry


def _label_points(
    element: Dict, polygon: Optional[QgsGeometry]
) -> List[QgsGeometry]:
    members = element.get("members")
    if not isinstance(members, list):
        return []
    points = []
    for member in members:
        if (
            not isinstance(member, dict)
            or member.get("type") != "node"
            or str(member.get("role") or "") not in {"label", "admin_centre"}
        ):
            continue
        try:
            point = QgsPointXY(float(member["lon"]), float(member["lat"]))
        except (KeyError, TypeError, ValueError):
            continue
        geometry = QgsGeometry.fromPointXY(point)
        if polygon is not None and not polygon.isEmpty():
            if not polygon.contains(geometry):
                continue
        points.append(geometry)
    if not points and polygon is not None and not polygon.isEmpty():
        surface_point = polygon.pointOnSurface()
        if surface_point is not None and not surface_point.isEmpty():
            points.append(surface_point)
    return points


def _relation_points(element: Dict) -> List[QgsGeometry]:
    return _label_points(element, _relation_polygon(element))


def _relation_point_specs(
    specs: Tuple[TagSpec, ...], match_mode: str
) -> Tuple[TagSpec, ...]:
    """The point specs a relation can answer: administrative boundaries.

    Only an administrative-boundary point spec asks Overpass for relations
    (one selector per spec, or, matching all, when every point spec is one);
    every other point spec reads nodes.  A park or a bus route that arrived
    for its polygon or line therefore never becomes a point as well.
    """
    point_specs = tuple(spec for spec in specs if spec.geometry == "point")
    if str(match_mode or "").strip().casefold() == "all":
        if point_specs and all(is_admin_boundary_spec(spec) for spec in point_specs):
            return point_specs
        return ()
    return tuple(spec for spec in point_specs if is_admin_boundary_spec(spec))


def _kinds_for_element(
    element: Dict,
    specs: Tuple[TagSpec, ...],
    match_mode: str = "any",
    value_mode: str = "exact",
) -> Tuple[Tuple[str, Tuple[TagSpec, ...]], ...]:
    element_type = element.get("type")
    tags = element.get("tags")
    if element_type == "node":
        matches = matching_specs(tags, specs, "point", match_mode, value_mode)
        return (("point", matches),) if matches else ()
    if element_type == "relation":
        members = element.get("members")
        kinds = []
        point_specs = _relation_point_specs(specs, match_mode)
        point_matches = (
            matching_specs(
                tags, point_specs, "point", match_mode, value_mode,
                "relation", members,
            )
            if point_specs else ()
        )
        if point_matches:
            kinds.append(("point", point_matches))
        line_matches = matching_specs(
            tags, specs, "line", match_mode, value_mode, "relation", members
        )
        polygon_matches = matching_specs(
            tags, specs, "polygon", match_mode, value_mode, "relation", members
        )
        # A wildcard request matches a relation as both, so ask the tags which
        # one it is -- otherwise every multipolygon also arrives as a line.
        # A preset that names both geometries asked for both on purpose (the
        # Places presets want boundaries as areas *and* as lines), so only the
        # wildcard is disambiguated here.
        if (
            line_matches
            and polygon_matches
            and all(
                spec.key == "*" for spec in line_matches + polygon_matches
            )
        ):
            if relation_is_area(tags, members):
                line_matches = ()
            else:
                polygon_matches = ()
        if line_matches:
            kinds.append(("line", line_matches))
        if polygon_matches:
            kinds.append(("polygon", polygon_matches))
        return tuple(kinds)
    if element_type != "way":
        return ()
    line_matches = matching_specs(tags, specs, "line", match_mode, value_mode)
    polygon_matches = matching_specs(
        tags, specs, "polygon", match_mode, value_mode
    )
    # An administrative area is the relation, not the closed ways its ring is
    # assembled from.  The query already asks for admin polygons as relations
    # only; without the same rule here every closed fragment that arrived for
    # the *line* output also became a polygon, and one neighbourhood came back
    # as a pile of overlapping part-shapes.
    polygon_matches = tuple(
        spec for spec in polygon_matches if not is_admin_boundary_spec(spec)
    )
    if polygon_matches and not line_matches:
        # Asked for as an area only: an open way is dropped and counted when
        # its geometry is built, never closed into an invented polygon.
        return (("polygon", polygon_matches),)
    if line_matches and not polygon_matches:
        return (("line", line_matches),)
    # Both geometries were asked for -- a wildcard "OSM as-is" request asks for
    # every one of them -- so closure alone cannot decide.  OSM has no polygon
    # primitive: a closed way is an area only when its tags say so, and a
    # closed `highway=footway` is a loop path, not a plaza.  An open way is a
    # line whatever its tags say.
    if (
        polygon_matches
        and is_closed_ring(element.get("geometry"))
        and way_is_area(tags)
    ):
        return (("polygon", polygon_matches),)
    return (("line", line_matches),) if line_matches else ()


def _kind_for_element(
    element: Dict,
    specs: Tuple[TagSpec, ...],
    match_mode: str = "any",
    value_mode: str = "exact",
) -> Tuple[str, Tuple[TagSpec, ...]]:
    kinds = _kinds_for_element(element, specs, match_mode, value_mode)
    return kinds[0] if kinds else ("", ())


def _way_area(element: Dict) -> _Built:
    coordinates = element.get("geometry")
    ring = _closed_ring(coordinates)
    if not ring:
        if len(_points(coordinates)) < 2:
            return _Built([], DROP_NO_GEOMETRY)
        return _Built([], DROP_UNCLOSED)
    polygon, repaired = _valid_polygon(QgsGeometry.fromPolygonXY([ring]))
    if polygon is None:
        return _Built([], DROP_INVALID)
    polygon.convertToMultiType()
    return _Built([polygon], repaired=int(repaired))


def _build(element: Dict, kind: str, memo: Optional[Dict] = None) -> _Built:
    """Build one matched feature's geometries, or say why there are none.

    `memo` (one dict per element) keeps a relation's area between its point
    and polygon features, so the rings are assembled once.
    """
    memo = {} if memo is None else memo
    is_relation = element.get("type") == "relation"
    area = _RelationArea(None)
    if is_relation and kind in ("point", "polygon"):
        if "area" not in memo:
            memo["area"] = _relation_area(element)
        area = memo["area"]
    if kind == "point":
        if is_relation:
            points = _label_points(element, area.geometry)
            if not points:
                return _Built([], DROP_NO_GEOMETRY)
            return _Built(points, footprint=area.geometry)
        try:
            point = QgsPointXY(float(element["lon"]), float(element["lat"]))
        except (KeyError, TypeError, ValueError):
            return _Built([], DROP_NO_GEOMETRY)
        return _Built([QgsGeometry.fromPointXY(point)])
    if kind == "line":
        if is_relation:
            line = _relation_line(element)
        else:
            points = _points(element.get("geometry"))
            line = QgsGeometry.fromPolylineXY(points) if len(points) >= 2 else None
        if line is None or line.isEmpty():
            return _Built([], DROP_NO_GEOMETRY)
        # Route relations join into a multi-part line, so the line sink is
        # MultiLineString.  A single-part way must be promoted to match it or
        # the provider silently refuses the geometry.
        line.convertToMultiType()
        return _Built([line])
    if is_relation:
        if area.geometry is None:
            return _Built([], area.reason, int(area.repaired))
        return _Built([QgsGeometry(area.geometry)], repaired=int(area.repaired))
    return _way_area(element)


def _geometries(element: Dict, kind: str) -> List[QgsGeometry]:
    return _build(element, kind).geometries


class _RequestedArea:
    """What a written feature must touch: the job's box, its circle, or a
    place's outline.

    Overpass only returns objects that touch the box, so the box changes
    nothing for its answers; Postpass selects by bounding-box overlap, and a
    ferry line from Thessaloniki to Rhodes came back for a box in Konak.  A
    circle or an outline is prepared once by GEOS, because every feature of
    the download is tested against it.
    """

    def __init__(self, geometry: QgsGeometry, reason: str, is_box: bool) -> None:
        self.geometry = geometry
        self.box = geometry.boundingBox()
        self.reason = reason
        self.is_box = is_box
        self._engine = None
        if not is_box and not geometry.isEmpty():
            self._engine = QgsGeometry.createGeometryEngine(geometry.constGet())
            self._engine.prepareGeometry()

    def touches(self, geometry: QgsGeometry) -> bool:
        bounds = geometry.boundingBox()
        if not self.box.intersects(bounds):
            return False
        if self.is_box and self.box.contains(bounds):
            return True
        if self._engine is not None:
            return bool(self._engine.intersects(geometry.constGet()))
        return geometry.intersects(self.geometry)


def _project(geometry: QgsGeometry, transform: QgsCoordinateTransform) -> bool:
    """Transform in place; False when the output CRS cannot hold the geometry.

    A point outside a limited-domain projection (an orthographic view, say)
    raises; a line through one comes back with infinite coordinates.
    """
    try:
        geometry.transform(transform)
    except QgsCsException:
        return False
    return geometry.boundingBox().isFinite()


def _ellipsoid_measure(context) -> QgsDistanceArea:
    """Ground lengths and areas on the WGS84 ellipsoid (docs/TRAPS.md 5.8, 5.13).

    The ellipsoid is named, not read from the project: a scripted project
    reports "NONE", which silently measures in square degrees.
    """
    measure = QgsDistanceArea()
    measure.setSourceCrs(
        QgsCoordinateReferenceSystem("EPSG:4326"), context.transformContext()
    )
    measure.setEllipsoid("WGS84")
    return measure


_FIELD_TYPES: Optional[Dict[str, object]] = None


def _variant_field_types() -> Dict[str, object]:
    # Only reached on QGIS 3.28-3.36, where PyQt5 still has QVariant.Type.
    from qgis.PyQt.QtCore import QVariant

    return {
        "string": QVariant.String,
        "double": QVariant.Double,
        "integer": QVariant.Int,
        "integer64": QVariant.LongLong,
    }


def _field_types() -> Dict[str, object]:
    """The QgsField type for each schema type, on every supported QGIS.

    `QgsField(name, QMetaType.Type)` exists from QGIS 3.38 and importing
    QMetaType proves nothing, so one throwaway field is the probe (docs/
    TRAPS.md 3.2b); before 3.38 the QVariant types are used.  A raw integer
    is never passed: QGIS 4 refuses it.
    """
    global _FIELD_TYPES
    if _FIELD_TYPES is None:
        types = None
        with contextlib.suppress(Exception):
            candidate = {
                "string": QMetaType.Type.QString,
                "double": QMetaType.Type.Double,
                "integer": QMetaType.Type.Int,
                "integer64": QMetaType.Type.LongLong,
            }
            QgsField("probe", candidate["string"])
            types = candidate
        _FIELD_TYPES = types if types is not None else _variant_field_types()
    return _FIELD_TYPES


def _fields(types: Optional[Dict[str, object]] = None) -> QgsFields:
    types = types or _field_types()
    fields = QgsFields()
    for name in FIELD_NAMES:
        fields.append(QgsField(name, types[FIELD_TYPES[name]]))
    return fields


def _advanced_flag():
    """The flag that files a parameter under Advanced Parameters.

    QGIS 3.36 moved it to `Qgis.ProcessingParameterFlag`; 3.28 to 3.34 have
    only `QgsProcessingParameterDefinition.FlagAdvanced`.
    """
    flags = getattr(Qgis, "ProcessingParameterFlag", None)
    if flags is not None:
        return flags.Advanced
    return QgsProcessingParameterDefinition.FlagAdvanced


class _BaseDownloadAlgorithm(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
    AREA_ID = "AREA_ID"
    EXCLUDE_KEY = "EXCLUDE_KEY"
    EXCLUDE_VALUE = "EXCLUDE_VALUE"
    AROUND_X = "AROUND_X"
    AROUND_Y = "AROUND_Y"
    AROUND_METERS = "AROUND_METERS"
    ALLOW_FALLBACK = "ALLOW_FALLBACK"
    OSM_TIMESTAMP = "OSM_TIMESTAMP"
    TILES_TOTAL = "TILES_TOTAL"
    TILES_MISSING = "TILES_MISSING"
    DATA_SOURCE = "DATA_SOURCE"
    FEATURES_DROPPED = "FEATURES_DROPPED"
    FEATURES_OUTSIDE = "FEATURES_OUTSIDE"
    NOTICE = "NOTICE"
    OUTPUT_POINTS = "OUTPUT_POINTS"
    OUTPUT_LINES = "OUTPUT_LINES"
    OUTPUT_POLYGONS = "OUTPUT_POLYGONS"
    ALGORITHM_NAME = ""
    DISPLAY_NAME = ""

    def __init__(self) -> None:
        super().__init__()
        self._area_id = 0
        #: The place's name in NOTICE sentences, when the algorithm has one.
        self._place_label = ""

    def name(self) -> str:
        return self.ALGORITHM_NAME

    def displayName(self) -> str:
        return self.DISPLAY_NAME

    def group(self) -> str:
        return "OSM acquisition"

    def groupId(self) -> str:
        return "osm_acquisition"

    def tags(self) -> List[str]:
        return [
            "osm",
            "openstreetmap",
            "overpass",
            "agent",
            "smartmodeler",
            "download",
            "urban analysis",
            "temporary layers",
        ]

    def helpUrl(self) -> str:
        return (
            "https://github.com/YusufEminoglu/02Agent-OSM-Downloader/"
            "blob/main/AGENT_PROTOCOL.md"
        )

    def createInstance(self):
        return type(self)()

    def shortHelpString(self) -> str:
        hosts = " and ".join(
            _host_name(endpoint) for endpoint in OVERPASS_ENDPOINTS
        )
        return (
            "Downloads a bounded OSM request from the public Overpass servers "
            f"{hosts}, asked one at a time, the one that delivered most "
            "recently first. One request covers "
            f"{MAX_TILE_AREA_KM2:,.0f} km²; a wider extent is split into tiled "
            "requests and merged, up to "
            f"{MAX_BBOX_AREA_KM2:,.0f} km² and {MAX_TILES} tiles per download, "
            "and a tile the server finds too dense is split into four smaller "
            f"parts, at most twice. A download sends at most {MAX_JOB_REQUESTS} "
            "requests and gives up early when the servers are busy or refuse "
            "it. When every Overpass server has failed or is resting for a "
            "part and ALLOW_FALLBACK is on (optional, off by default), that "
            "part is asked from Geofabrik's Postpass service "
            f"({postpass.POSTPASS_HOST}) instead. If some parts cannot be "
            "fetched, the result keeps what "
            "arrived: TILES_MISSING says how many of TILES_TOTAL parts are "
            "missing, and running the same download again within a day "
            "fetches only those. DATA_SOURCE names the servers that delivered "
            "the data and OSM_TIMESTAMP the date of the OSM data they "
            "reported. FEATURES_DROPPED counts the matched features that could "
            "not be built or written, FEATURES_OUTSIDE those left out because "
            "they lie outside the chosen area, and NOTICE holds a sentence "
            "about the result when there is something to say. An optional "
            "resolved area id keeps only the features "
            "that touch the place's mapped boundary: its outline comes from "
            "Nominatim and the request covers the outline's bounding box "
            "within the extent; if the outline cannot be fetched, the "
            "OpenStreetMap server clips the request to its own copy of the "
            "boundary, and an extent that misses the outline returns empty "
            "layers. An optional exclude key/value drops "
            "matching features from the result after it is fetched. An "
            "optional around search (longitude, latitude in WGS84 and a radius "
            f"up to {MAX_RADIUS_M:,.0f} m) requests the circle's bounding box "
            "and keeps only the features that touch the circle. No raw "
            "query, URL, path, API key, or external dependency is accepted."
        )

    def _add_common_parameters(self) -> None:
        self.addParameter(
            QgsProcessingParameterExtent(self.EXTENT, "Download extent")
        )
        if self.parameterDefinition(self.AREA_ID) is None:
            self.addParameter(
                QgsProcessingParameterString(
                    self.AREA_ID,
                    "Resolved OSM area id (optional, clips to mapped boundary)",
                    defaultValue="",
                    optional=True,
                )
            )
        self.addParameter(
            QgsProcessingParameterString(
                self.EXCLUDE_KEY,
                "Exclude OSM tag key (optional)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.EXCLUDE_VALUE,
                "Exclude OSM tag value (blank or * means any value)",
                defaultValue="",
                optional=True,
            )
        )
        for name, label in (
            (self.AROUND_X, "Around search: centre longitude (WGS84, optional)"),
            (self.AROUND_Y, "Around search: centre latitude (WGS84, optional)"),
        ):
            self.addParameter(
                QgsProcessingParameterNumber(
                    name, label,
                    QgsProcessingParameterNumber.Type.Double,
                    defaultValue=0.0, optional=True,
                )
            )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.AROUND_METERS,
                f"Around search: radius in metres (0 = off, up to {MAX_RADIUS_M:,.0f})",
                QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, maxValue=MAX_RADIUS_M, optional=True,
            )
        )
        fallback = QgsProcessingParameterBoolean(
            self.ALLOW_FALLBACK,
            "If the OpenStreetMap servers are busy, use Geofabrik's Postpass",
            defaultValue=False,
            optional=True,
        )
        fallback.setFlags(fallback.flags() | _advanced_flag())
        self.addParameter(fallback)
        self.addOutput(
            QgsProcessingOutputString(
                self.OSM_TIMESTAMP, "OSM data timestamp (UTC, from the server)"
            )
        )
        self.addOutput(
            QgsProcessingOutputNumber(
                self.TILES_TOTAL, "Parts the download was made of"
            )
        )
        self.addOutput(
            QgsProcessingOutputNumber(
                self.TILES_MISSING, "Parts that could not be downloaded (0 = complete)"
            )
        )
        self.addOutput(
            QgsProcessingOutputString(
                self.DATA_SOURCE, "Servers that delivered the data"
            )
        )
        self.addOutput(
            QgsProcessingOutputNumber(
                self.FEATURES_DROPPED,
                "Matched OSM features that could not be built or written (0 = none)",
            )
        )
        self.addOutput(
            QgsProcessingOutputNumber(
                self.FEATURES_OUTSIDE,
                "Matched OSM features left out because they lie outside the "
                "chosen area (0 = none)",
            )
        )
        self.addOutput(
            QgsProcessingOutputString(
                self.NOTICE, "A note on the result (empty when there is none)"
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINTS, "OSM points"
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_LINES, "OSM lines"
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POLYGONS, "OSM polygons"
            )
        )

    def _request(
        self, parameters, context
    ) -> Tuple[str, str, Tuple[TagSpec, ...], str]:
        raise NotImplementedError

    def _value_mode(self, parameters, context) -> str:
        """The tag-value matching mode for this run; only Advanced Query offers regex."""
        return "exact"

    def _exclude_tag(self, parameters, context) -> Tuple[str, str]:
        key = self.parameterAsString(parameters, self.EXCLUDE_KEY, context).strip()
        if not key:
            return "", ""
        value = self.parameterAsString(
            parameters, self.EXCLUDE_VALUE, context
        ).strip()
        try:
            return normalize_tag(key, value)
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc

    def _resolved_area_id(self, parameters, context) -> int:
        if self._area_id:
            return self._area_id
        resolved = self.parameterAsString(
            parameters, self.AREA_ID, context
        ).strip()
        if not resolved:
            return 0
        try:
            return validate_area_id(resolved)
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc

    def _plan_requests(
        self,
        specs: Tuple[TagSpec, ...],
        bbox: Tuple[float, float, float, float],
        match_mode: str,
        feedback,
        value_mode: str = "exact",
        area_id: int = 0,
        outline: bool = False,
    ) -> Tuple[TileRequest, ...]:
        """Return every bounded request needed to cover the extent.

        `outline` says the features are kept against a place's outline here;
        `area_id` has the server clip to its own copy of the boundary, when
        that outline could not be fetched.
        """
        area = bbox_area_km2(*bbox)
        requests = plan_requests(
            specs, bbox, match_mode, value_mode, area_id=area_id
        )
        plural = "s" if len(requests) != 1 else ""
        if outline:
            feedback.pushInfo(
                "Keeping only what touches the mapped boundary of the place "
                f"({area:,.0f} km² around it, {len(requests)} bounded "
                f"OSM request{plural})."
            )
        elif area_id:
            feedback.pushInfo(
                "Clipping the request to the mapped boundary of the place "
                f"({area:,.0f} km² extent, {len(requests)} bounded "
                f"OSM request{plural})."
            )
        else:
            feedback.pushInfo(
                f"Request extent: {area:,.1f} km² "
                f"({len(requests)} bounded OSM request{plural})."
            )
        return requests

    def _around(self, parameters, context):
        """(lon, lat, radius) of an around search, or None when it is off."""
        radius = self.parameterAsDouble(parameters, self.AROUND_METERS, context)
        if not radius:
            return None
        try:
            radius = validate_radius(radius)
            lon, lat = validate_centre(
                self.parameterAsDouble(parameters, self.AROUND_X, context),
                self.parameterAsDouble(parameters, self.AROUND_Y, context),
            )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc
        return lon, lat, radius

    def processAlgorithm(self, parameters, context, feedback):
        """Thin outer frame: every failure leaves as a fresh exception.

        PyQGIS keeps an exception that escapes `processAlgorithm` alive with
        all its frames, so a failed or canceled download pinned its whole
        response (about 0.5 GB per failed city job) until QGIS restarted.
        The work runs in `_run`; only the message crosses this frame, and the
        exception raised here references nothing but this frame.
        """
        message = kind = category = summary = ""
        try:
            return self._run(parameters, context, feedback)
        except QgsProcessingException as error:
            message = str(error).strip() or "The OSM download failed."
            kind = str(getattr(error, "kind", "") or "")
            category = str(getattr(error, "category", "") or "")
            summary = str(getattr(error, "summary", "") or "")
        except Exception as error:  # noqa: BLE001 - see the docstring
            message = (
                "The OSM download stopped on an unexpected error: "
                f"{type(error).__name__}: {error}"
            )
            category = transport.ERROR
            feedback.reportError(traceback.format_exc(), fatalError=False)
        if not category:
            category = (
                transport.CANCELED if feedback.isCanceled() else transport.ERROR
            )
        _event(
            feedback, "failed", f"Download failed: {summary or message}",
            kind=kind, category=category, detail=summary or message,
            message=message,
        )
        raise QgsProcessingException(message)

    def _run(self, parameters, context, feedback):
        preset_id, theme, specs, match_mode = self._request(parameters, context)
        value_mode = self._value_mode(parameters, context)
        exclude_key, exclude_value = self._exclude_tag(parameters, context)
        allow_fallback = self.parameterAsBoolean(
            parameters, self.ALLOW_FALLBACK, context
        )
        try:
            area_id = self._resolved_area_id(parameters, context)
        finally:
            self._area_id = 0
        place = self._place_label or "this place"
        #: One sentence about the result for whoever ran it (NOTICE).
        notice = ""
        extent = self.parameterAsExtent(parameters, self.EXTENT, context)
        extent_crs = self.parameterAsExtentCrs(
            parameters, self.EXTENT, context
        )
        project = context.project() or QgsProject.instance()
        if not extent_crs.isValid() and project is not None:
            extent_crs = project.crs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if not extent_crs.isValid():
            extent_crs = wgs84
        try:
            if extent_crs != wgs84:
                transform = QgsCoordinateTransform(
                    extent_crs, wgs84, context.transformContext()
                )
                wgs_extent = transform.transformBoundingBox(extent)
            else:
                wgs_extent = extent
            bbox = (
                wgs_extent.yMinimum(), wgs_extent.xMinimum(),
                wgs_extent.yMaximum(), wgs_extent.xMaximum(),
            )
            around = self._around(parameters, context)
            circle = None
            if around is not None:
                # The circle's own bounding box replaces the extent, so every
                # size ceiling and tiling rule below applies to what is
                # actually requested.
                lon, lat, radius = around
                bbox = circle_bbox(lon, lat, radius)
                circle = QgsGeometry.fromPolygonXY([[
                    QgsPointXY(x, y) for x, y in circle_ring(lon, lat, radius)
                ]])
                feedback.pushInfo(
                    f"Around search: {radius:,.0f} m from {lat:.5f}, {lon:.5f}."
                )
            outline = None
            inside: Optional[Tuple[float, float, float, float]] = bbox
            if area_id:
                # The place's own outline: the parts cover its bounding box
                # (within the extent) and features are kept against it below,
                # with no area filter for the server to resolve.
                outline, why = _place_boundary(area_id, feedback)
                if outline is None:
                    feedback.pushInfo(
                        f"Could not fetch the outline of the place: {why} The "
                        "OpenStreetMap server clips the request to its own "
                        "copy of the boundary instead."
                    )
                    notice = (
                        f"The outline of {place} could not be fetched, so the "
                        "server clipped the request to its own copy of the "
                        "boundary."
                    )
                else:
                    inside = _overlap(bbox, outline.boundingBox())
            if inside is None:
                # Nothing of the place lies in the extent: empty layers, as
                # 1.5.1 gave, rather than a failed run in a batch of extents.
                requests = ()
                notice = (
                    f"The download extent does not overlap the boundary of "
                    f"{place}, so nothing was downloaded."
                )
                feedback.pushWarning(notice)
            else:
                bbox = inside
                requests = self._plan_requests(
                    specs, bbox, match_mode, feedback, value_mode,
                    area_id=area_id if outline is None else 0,
                    outline=outline is not None,
                )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc

        fields = _fields()
        sink_specs = {
            "point": (
                self.OUTPUT_POINTS, QgsWkbTypes.Type.Point,
            ),
            "line": (
                self.OUTPUT_LINES, QgsWkbTypes.Type.MultiLineString,
            ),
            "polygon": (
                self.OUTPUT_POLYGONS, QgsWkbTypes.Type.MultiPolygon,
            ),
        }
        sinks = {}
        destinations = {}
        for kind, (name, wkb_type) in sink_specs.items():
            sink, destination = self.parameterAsSink(
                parameters, name, context, fields, wkb_type, extent_crs
            )
            if sink is None:
                raise QgsProcessingException(
                    f"QGIS could not create the {kind} output."
                )
            sinks[kind] = sink
            destinations[name] = destination

        feedback.setProgress(5)
        if requests:
            fetched = _fetch_all(requests, feedback, allow_fallback=allow_fallback)
        else:
            fetched = FetchResult({"elements": []}, 0, (), (), 0, "")
        payload = fetched.payload
        feedback.setProgress(40)
        data_date = osm_timestamp([payload])
        if data_date:
            feedback.pushInfo(f"OSM data as of {data_date} (UTC).")
        to_target = (
            QgsCoordinateTransform(
                wgs84, extent_crs, context.transformContext()
            )
            if extent_crs != wgs84 else None
        )
        # Every written feature touches what was asked for: the around circle
        # when there is one, otherwise the job's WGS84 box; and a place's
        # outline, where it lies inside that box, when there is one.
        south, west, north, east = bbox
        box = QgsGeometry.fromRect(QgsRectangle(west, south, east, north))
        if circle is not None:
            areas = [_RequestedArea(circle, DROP_OUTSIDE_CIRCLE, False)]
        elif outline is None:
            areas = [_RequestedArea(box, DROP_OUTSIDE_EXTENT, True)]
        else:
            areas = []
        if outline is not None and requests:
            areas.append(
                _RequestedArea(outline.intersection(box), DROP_OUTSIDE_OUTLINE, False)
            )
        # Lengths and areas are taken on the ellipsoid from the WGS84
        # geometry, before it is projected into the output CRS.
        measure = _ellipsoid_measure(context)
        counts = {"point": 0, "line": 0, "polygon": 0}
        dropped = dict.fromkeys(DROP_REASONS + OUTSIDE_REASONS, 0)
        #: Features a sink refused, per output: one is a counted drop, all of
        #: them a broken output.
        refused = {"point": 0, "line": 0, "polygon": 0}
        repaired = 0
        elements = payload.get("elements", [])
        for index, element in enumerate(elements):
            if feedback.isCanceled():
                raise QgsProcessingException("The OSM download was canceled.")
            if exclude_key and excludes_tag(
                element.get("tags"), exclude_key, exclude_value
            ):
                continue
            memo: Dict = {}
            for kind, matches in _kinds_for_element(
                element, specs, match_mode, value_mode
            ):
                if not kind or not matches:
                    continue
                built = _build(element, kind, memo)
                if not built.geometries:
                    dropped[built.reason] += 1
                    continue
                written = 0
                for geometry in built.geometries:
                    outside = next(
                        (
                            area for area in areas
                            if not area.touches(built.footprint or geometry)
                        ),
                        None,
                    )
                    if outside is not None:
                        dropped[outside.reason] += 1
                        continue
                    length_m = area_m2 = None
                    if kind == "line":
                        length_m = measure.measureLength(geometry)
                    elif kind == "polygon":
                        area_m2 = measure.measureArea(geometry)
                    if to_target is not None and not _project(geometry, to_target):
                        dropped[DROP_TRANSFORM] += 1
                        continue
                    feature = QgsFeature(fields)
                    feature.setGeometry(geometry)
                    feature.setAttributes(
                        feature_attributes(
                            element, preset_id, theme, matches, length_m, area_m2
                        )
                    )
                    if not sinks[kind].addFeature(
                        feature, QgsFeatureSink.Flag.FastInsert
                    ):
                        # One value the output cannot hold costs that
                        # feature, not the whole download.
                        dropped[DROP_UNWRITABLE] += 1
                        refused[kind] += 1
                        continue
                    counts[kind] += 1
                    written += 1
                if written:
                    repaired += built.repaired
            if index % 250 == 0:
                feedback.setProgress(
                    40 + int(55 * (index + 1) / max(1, len(elements)))
                )
        for kind, count in refused.items():
            if count and not counts[kind]:
                # Every feature refused: the output itself is broken.
                raise QgsProcessingException(
                    _refusal_message(kind, count, sinks[kind])
                )
        missing = len(fetched.missing)
        if missing:
            # A download with gaps must never read as a clean finish.
            feedback.pushWarning(fetched.gaps)
        total_dropped = sum(dropped[reason] for reason in DROP_REASONS)
        total_outside = sum(dropped[reason] for reason in OUTSIDE_REASONS)
        summary = _drop_summary(dropped, repaired)
        if total_dropped:
            feedback.pushWarning(summary)
        elif summary:
            feedback.pushInfo(summary)
        if total_outside:
            # Leaving out what lies outside the chosen area is the request
            # working, not a fault.
            feedback.pushInfo(_outside_summary(dropped))
        if area_id and outline is None and requests and not elements and not missing:
            # Clipped by the server's own copy of the boundary, and nothing
            # came back: that copy may be missing or out of date.
            notice = (
                f"Nothing came back from inside the boundary of {place}; the "
                "server may not hold it. Choose Rectangular extent and press "
                "Download again."
            )
            feedback.pushWarning(notice)
        elif (
            requests and not any(counts.values()) and not missing
            and not total_dropped and not total_outside
        ):
            # An empty result is a successful request, so say why it can
            # happen rather than handing back three blank layers in silence.
            feedback.pushInfo(
                "The request succeeded but matched nothing. Either this area "
                "has none of these tags mapped, or the boundary used for the "
                "request covers no data."
            )
        _event(
            feedback, "done",
            "Created "
            f"{counts['point']:,} points, {counts['line']:,} lines and "
            f"{counts['polygon']:,} polygons.",
            host=", ".join(fetched.sources),
            part=fetched.total - missing, parts=fetched.total,
            category="gaps" if missing else "",
            detail=fetched.gaps,
            missing=missing,
            dropped=total_dropped,
            outside=total_outside,
            notice=notice,
        )
        feedback.setProgress(100)
        destinations[self.OSM_TIMESTAMP] = data_date
        destinations[self.TILES_TOTAL] = fetched.total
        destinations[self.TILES_MISSING] = missing
        destinations[self.DATA_SOURCE] = ", ".join(fetched.sources)
        destinations[self.FEATURES_DROPPED] = total_dropped
        destinations[self.FEATURES_OUTSIDE] = total_outside
        destinations[self.NOTICE] = notice
        return destinations


#: Presets added to the catalogue after 1.5.1, in the order they were added.
#: Append a new preset's id here; never insert, re-sort or remove an entry.
ADDED_PRESET_IDS: Tuple[str, ...] = ("everyday_amenities",)
#: The PRESET parameter's options, in this order.  Models, scripts and Smart
#: Modeler proposals store PRESET as indexes, so an index has to name the same
#: dataset in every release: the catalogue order of 1.5.1 first, then each
#: addition at the end.  The dock's own catalogue order is free to change.
PRESET_ENUM_ORDER: Tuple[str, ...] = LEGACY_PRESET_ORDER + ADDED_PRESET_IDS


def _preset_option(preset_id: str) -> str:
    preset = PRESETS_BY_ID.get(preset_id)
    if preset is None:
        # A removed dataset keeps its place, so later indexes do not move.
        return f"{preset_id} (no longer available)"
    return preset.processing_label


class DownloadPresetAlgorithm(_BaseDownloadAlgorithm):
    PRESET = "PRESET"
    ALGORITHM_NAME = "download_preset"
    DISPLAY_NAME = "Download curated OSM thematic preset"

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PRESET,
                "Thematic preset",
                options=[_preset_option(item) for item in PRESET_ENUM_ORDER],
                allowMultiple=True,
                defaultValue=[0],
            )
        )
        self._add_common_parameters()

    def _request(self, parameters, context):
        raw_indexes = self.parameterAsEnums(parameters, self.PRESET, context)
        indexes = tuple(dict.fromkeys(int(index) for index in raw_indexes))
        if not indexes:
            fallback = self.parameterAsEnum(parameters, self.PRESET, context)
            indexes = (fallback,)
        if any(index < 0 or index >= len(PRESET_ENUM_ORDER) for index in indexes):
            raise QgsProcessingException("The selected preset is not valid.")
        presets = tuple(
            PRESETS_BY_ID.get(PRESET_ENUM_ORDER[index]) for index in indexes
        )
        if any(preset is None for preset in presets):
            raise QgsProcessingException(
                "The selected preset is no longer available."
            )
        specs = tuple(tag for preset in presets for tag in preset.tags)
        return "+".join(preset.preset_id for preset in presets), presets[0].group_title, specs, "any"


class DownloadPlaceAlgorithm(DownloadPresetAlgorithm):
    PLACE = "PLACE"
    CLIP = "CLIP"
    AREA_ID = "AREA_ID"
    ALGORITHM_NAME = "download_place"
    DISPLAY_NAME = "Download curated OSM datasets for a named place"
    CLIP_OPTIONS = (
        "Exact mapped boundary of the place",
        "Rectangular extent of the place",
    )

    def __init__(self) -> None:
        super().__init__()
        self._area_id = 0

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterString(
                self.PLACE,
                "Place or administrative name",
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.CLIP,
                "Download boundary",
                options=list(self.CLIP_OPTIONS),
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.AREA_ID,
                "Resolved OSM area id (leave blank to geocode the name)",
                defaultValue="",
                optional=True,
            )
        )
        super().initAlgorithm(_configuration)

    def shortHelpString(self) -> str:
        return (
            "Resolves a place name with the OpenStreetMap Nominatim geocoder "
            "and downloads the selected datasets for it. When AREA_ID is "
            "blank, the name is geocoded and EXTENT is replaced by the place's "
            "extent; when AREA_ID holds an id already resolved, the name is "
            "not geocoded again and EXTENT is used as given. When the place "
            "is a mapped boundary and the exact boundary is chosen, its "
            "outline is fetched from Nominatim and only the features that "
            "touch it are kept; the request covers the outline's bounding box "
            "within the extent. Otherwise the rectangular extent is used. "
            "Either way the area is split into bounded tiles as needed. When "
            "every Overpass server is busy or resting for a part and "
            "ALLOW_FALLBACK is on (optional, off by default), Geofabrik's "
            "Postpass service is asked for it instead."
        )

    def _run(self, parameters, context, feedback):
        place = self.parameterAsString(parameters, self.PLACE, context).strip()
        if not place:
            raise QgsProcessingException("Enter a place or administrative name.")
        clip_index = self.parameterAsEnum(parameters, self.CLIP, context)
        if clip_index < 0 or clip_index >= len(self.CLIP_OPTIONS):
            raise QgsProcessingException("The download boundary is not valid.")
        resolved = self.parameterAsString(
            parameters, self.AREA_ID, context
        ).strip()
        working = dict(parameters)

        if resolved:
            # The caller already chose a specific place, so re-geocoding the
            # name could silently pick a different one.
            try:
                self._area_id = (
                    validate_area_id(resolved) if clip_index == 0 else 0
                )
            except QueryError as exc:
                raise QgsProcessingException(str(exc)) from exc
            feedback.pushInfo(f"Using the boundary already resolved for '{place}'.")
            self._place_label = place
        else:
            try:
                candidates = resolve_place(place, feedback)
            except (QueryError, ValueError) as exc:
                raise QgsProcessingException(str(exc)) from exc
            if not candidates:
                raise QgsProcessingException(
                    f"No place matched '{place}'. Try a fuller name, for "
                    "example 'Konak, Izmir, Turkey'."
                )
            candidate = candidates[0]
            south, west, north, east = candidate.bbox
            detail = f"Resolved place: {candidate.label}"
            if candidate.admin_level:
                detail = f"{detail} (admin level {candidate.admin_level})"
            feedback.pushInfo(f"{detail} — via {candidate.source}.")
            self._area_id = candidate.area_id if clip_index == 0 else 0
            self._place_label = candidate.name or place
            if not self._area_id and clip_index == 0:
                feedback.pushInfo(
                    "This place has no mapped boundary; using its rectangular "
                    "extent instead."
                )
            working[self.EXTENT] = QgsReferencedRectangle(
                QgsRectangle(west, south, east, north),
                QgsCoordinateReferenceSystem("EPSG:4326"),
            )

        working[self.AREA_ID] = str(self._area_id) if self._area_id else ""
        try:
            return super()._run(working, context, feedback)
        finally:
            self._area_id = 0
            self._place_label = ""


class DownloadCustomTagAlgorithm(_BaseDownloadAlgorithm):
    KEY = "KEY"
    VALUE = "VALUE"
    GEOMETRY = "GEOMETRY"
    ALGORITHM_NAME = "download_custom_tag"
    DISPLAY_NAME = "Download custom OSM key/value tag"

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(QgsProcessingParameterString(self.KEY, "OSM tag key"))
        self.addParameter(
            QgsProcessingParameterString(
                self.VALUE,
                "OSM tag value (blank or * means any value)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.GEOMETRY,
                "Geometry type",
                options=["Point", "Line", "Polygon"],
                defaultValue=0,
            )
        )
        self._add_common_parameters()

    def _request(self, parameters, context):
        try:
            key, value = normalize_tag(
                self.parameterAsString(parameters, self.KEY, context),
                self.parameterAsString(parameters, self.VALUE, context),
            )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc
        index = self.parameterAsEnum(parameters, self.GEOMETRY, context)
        if index < 0 or index >= len(GEOMETRY_KINDS):
            raise QgsProcessingException("The geometry type is not valid.")
        spec = TagSpec(key, value, GEOMETRY_KINDS[index])
        return "custom", "Custom tag", normalized_specs((spec,)), "any"


class DownloadAdvancedQueryAlgorithm(_BaseDownloadAlgorithm):
    MATCH_MODE = "MATCH_MODE"
    GEOMETRY = "GEOMETRY"
    VALUE_MODE = "VALUE_MODE"
    ALGORITHM_NAME = "download_advanced"
    DISPLAY_NAME = "Download structured advanced OSM query"
    GEOMETRY_OPTIONS = (
        "All geometries",
        "Points",
        "Lines",
        "Polygons",
    )

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterEnum(
                self.MATCH_MODE,
                "Tag match mode",
                options=["Match any tag (OR)", "Match all tags (AND)"],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.GEOMETRY,
                "Geometry scope",
                options=list(self.GEOMETRY_OPTIONS),
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.VALUE_MODE,
                "Match values as regex (~) instead of exact equality",
                defaultValue=False,
            )
        )
        for index in range(1, MAX_ADVANCED_FILTERS + 1):
            self.addParameter(
                QgsProcessingParameterString(
                    f"KEY_{index}",
                    f"OSM tag key {index}",
                    optional=index > 1,
                )
            )
            self.addParameter(
                QgsProcessingParameterString(
                    f"VALUE_{index}",
                    f"OSM tag value {index} (blank or * means any value)",
                    defaultValue="",
                    optional=True,
                )
            )
        self._add_common_parameters()

    def _value_mode(self, parameters, context) -> str:
        return (
            "regex"
            if self.parameterAsBoolean(parameters, self.VALUE_MODE, context)
            else "exact"
        )

    def _request(self, parameters, context):
        mode_index = self.parameterAsEnum(parameters, self.MATCH_MODE, context)
        if mode_index not in (0, 1):
            raise QgsProcessingException("The tag match mode is not valid.")
        match_mode = ("any", "all")[mode_index]
        value_mode = self._value_mode(parameters, context)

        geometry_index = self.parameterAsEnum(parameters, self.GEOMETRY, context)
        geometry_options = (
            GEOMETRY_KINDS,
            ("point",),
            ("line",),
            ("polygon",),
        )
        if geometry_index < 0 or geometry_index >= len(geometry_options):
            raise QgsProcessingException("The geometry scope is not valid.")

        filters = []
        for index in range(1, MAX_ADVANCED_FILTERS + 1):
            key = self.parameterAsString(
                parameters, f"KEY_{index}", context
            ).strip()
            value = self.parameterAsString(
                parameters, f"VALUE_{index}", context
            ).strip()
            if not key:
                if value:
                    raise QgsProcessingException(
                        f"OSM tag value {index} has no corresponding key."
                    )
                continue
            filters.append((key, value))
        try:
            specs = advanced_specs(
                filters,
                geometry_options[geometry_index],
                match_mode,
                value_mode,
            )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc
        return "advanced", "Advanced query", specs, match_mode
