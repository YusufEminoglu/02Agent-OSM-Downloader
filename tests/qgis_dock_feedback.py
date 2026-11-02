"""The real dock against fake Overpass servers: live status, closing messages, Continue.

Run under a QGIS Python (python-qgis-ltr.bat / python-qgis.bat), from the
directory that contains the plugin folder, with QGIS_CUSTOM_CONFIG_PATH set to
a throwaway directory (TRAPS 4.6):

    python -m zero2agent_osm_downloader.tests.qgis_dock_feedback

Every download here is real: the dock's own buttons, the Processing task on
its worker thread, the network client and two Overpass hosts on 127.0.0.1
(the fake servers of qgis_fetch_resilience, whose error bodies are copied
from the Overpass source and a live capture). QGIS's network timeout is set
to 4 s, so a host that never answers is given up after T = 2 s. The dock sits
in a QGIS-shaped window, tabbed with Browser, with a real message bar. No
public host is contacted.

Covers U1 and U2 of the v1.6 development set:
- busy servers: the status moves while the download runs (elapsed time, the
  server asked, the reason for a switch), then gives the failure sentence
  and where the details are; the failure class is kept for Report a problem;
- a wait (a dropped connection, asked once more) and a split (a part too
  dense for one request) appear in the servers' own words;
- parts missing: "Finished with gaps", the Recent entry marked incomplete,
  and Continue (or Run again on that entry) asks only for the missing parts;
  while the servers rest after the errors, Continue waits with a countdown
  in its place (REV-UX UX-2);
- Cancel: one sentence, and nothing is added;
- success: the counts, the data date and the server that delivered; matched
  objects outside the chosen area are not called failures (UX-1) and the
  algorithm's NOTICE appears under the closing message (UX-5);
- a dock out of sight: the QGIS message bar repeats the closing message.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import unquote

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsNetworkAccessManager,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsVectorLayer,
)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
QGIS_TIMEOUT_MS = 4000
HISTORY_KEY = "zero2agent_osm_downloader/recent_downloads"
#: Off here: these checks are about Overpass alone; qgis_fallback_boundary
#: drives the dock with the Postpass fallback on.
FALLBACK_KEY = "zero2agent_osm_downloader/allow_fallback"
SETTINGS = (
    "zero2agent_osm_downloader/save_mode",
    "zero2agent_osm_downloader/geopackage_path",
    HISTORY_KEY,
    FALLBACK_KEY,
)
DETAILS = "Details: View > Panels > Log Messages > Processing."
CANCELED = "Download canceled. Nothing was added."
_ELAPSED_RE = re.compile(r"— (\d+:\d\d)")


class CheckFailed(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise CheckFailed(message)


def pump(seconds=0.05):
    app = QgsApplication.instance()
    end = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.005)


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        pump(0.02)
    return predicate()


class _Iface:
    """The iface calls the plugin makes, on a real main window."""

    def __init__(self, window, canvas, bar):
        self._window = window
        self._canvas = canvas
        self._bar = bar

    def mainWindow(self):  # noqa: N802 - QGIS naming
        return self._window

    def mapCanvas(self):  # noqa: N802
        return self._canvas

    def messageBar(self):  # noqa: N802
        return self._bar

    def activeLayer(self):  # noqa: N802
        return None

    def addDockWidget(self, area, dock):  # noqa: N802
        self._window.addDockWidget(area, dock)

    def removeDockWidget(self, dock):  # noqa: N802
        self._window.removeDockWidget(dock)

    def addPluginToMenu(self, _name, _action):  # noqa: N802
        return None

    def removePluginMenu(self, _name, _action):  # noqa: N802
        return None

    def addToolBarIcon(self, _action):  # noqa: N802
        return None

    def removeToolBarIcon(self, _action):  # noqa: N802
        return None


class Rig:
    """A QGIS-shaped window: a message bar over the canvas, Browser on the left.

    The plugin opens its dock as it does in QGIS, tabbed with Browser. The
    canvas is not bridged to the project, so nothing is rendered.
    """

    def __init__(self, servers):
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QMainWindow, QVBoxLayout, QWidget
        from qgis.gui import QgsMapCanvas, QgsMessageBar

        from zero2agent_osm_downloader.main_plugin import AgentOsmDownloaderPlugin

        self.servers = servers
        self.window = QMainWindow()
        self.window.setObjectName("QgisApp")
        central = QWidget()
        column = QVBoxLayout(central)
        self.bar = QgsMessageBar()
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        column.addWidget(self.bar)
        column.addWidget(self.canvas, 1)
        self.window.setCentralWidget(central)
        self.browser = QDockWidget("Browser", self.window)
        self.browser.setObjectName("Browser")
        self.browser.setWidget(QLabel("Browser panel"))
        self.window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.browser)
        self.window.resize(1366, 728)
        self.window.show()
        pump(0.1)
        self.iface = _Iface(self.window, self.canvas, self.bar)
        self.plugin = AgentOsmDownloaderPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.action.trigger()
        pump(0.3)
        self.dock = self.plugin.dock
        self.log = []
        # QGIS 4.2 reports the text through messageReceivedWithFormat; there
        # the three-argument messageReceived never fires (measured).
        messages = QgsApplication.messageLog()
        signal = getattr(messages, "messageReceivedWithFormat", None) or messages.messageReceived
        signal.connect(self._logged)

    def _logged(self, message, tag, *_level_and_format):
        self.log.append((str(message), str(tag)))

    def fresh(self, **modes):
        """Servers in the given modes, no cache, and no host resting."""
        from zero2agent_osm_downloader.core import mirrors
        from zero2agent_osm_downloader.processing import osm_algorithms

        mirrors.BOARD.reset()
        osm_algorithms.clear_cache()
        for name, mode in modes.items():
            if isinstance(mode, tuple):
                self.servers.set(name, mode[0], **mode[1])
            else:
                self.servers.set(name, mode)

    def rest_is_over(self, **modes):
        """The hosts' rest has passed (the cache is kept)."""
        from zero2agent_osm_downloader.core import mirrors

        mirrors.BOARD.reset()
        for name, mode in modes.items():
            self.servers.set(name, mode)

    def ask_for_cafes(self, box):
        """amenity=cafe over a drawn rectangle (south, west, north, east)."""
        dock = self.dock
        dock.tabs.setCurrentWidget(dock.download_tab)
        dock.custom_check.setChecked(True)
        dock.key_edit.setText("amenity")
        dock.value_edit.setText("cafe")
        dock.geometry_combo.setCurrentIndex(0)
        dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
        dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("drawn"))
        south, west, north, east = box
        dock._extent_drawn(QgsRectangle(west, south, east, north))

    def drive(self, start, cancel_after=None, limit=90.0):
        """Start a download with `start` and watch the dock until it ends.

        "statuses" is what a person could see, sampled every 10 ms; "shown"
        is every text the dock put in the status, in order, including ones
        replaced within the same event cycle (a local server answers in
        milliseconds).
        """
        dock = self.dock
        self.servers.take_hits()
        statuses, labels, shown = [], set(), []
        show = dock._set_status
        dock._set_status = lambda text: (shown.append(str(text)), show(text))[-1]
        started = time.monotonic()
        clicked = None
        try:
            start()
            check(dock._task is not None, f"The download did not start: {dock.status.text()!r}")
            while time.monotonic() - started < limit:
                QgsApplication.processEvents()
                text = dock.status.text()
                if not statuses or statuses[-1] != text:
                    statuses.append(text)
                if dock._task is None:
                    break
                labels.add((dock.download_button.text(), dock.download_button.isEnabled()))
                if (cancel_after is not None and clicked is None
                        and time.monotonic() - started >= cancel_after):
                    dock.cancel_button.click()
                    clicked = time.monotonic()
                time.sleep(0.01)
            check(dock._task is None, f"The download did not end within {limit:.0f} s.")
            pump(0.1)
        finally:
            del dock._set_status
        final = dock.status.text()
        if statuses[-1] != final:
            statuses.append(final)
        return {
            "statuses": statuses, "running": statuses[:-1], "final": final, "shown": shown,
            "labels": labels, "elapsed": time.monotonic() - started,
            "after_cancel": (time.monotonic() - clicked) if clicked else None,
            "hits": self.servers.take_hits(),
        }


def _record_submits(dock):
    """Note every request the dock submits, then submit it as usual."""
    calls = []
    original = dock._submit

    def recording(algorithm_id, parameters, extent):
        calls.append((algorithm_id, dict(parameters),
                      (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(),
                       extent.yMaximum(), extent.crs().authid())))
        return original(algorithm_id, parameters, extent)

    dock._submit = recording
    return calls


def _boxes(hits):
    from zero2agent_osm_downloader.tests.qgis_fetch_resilience import _BOX_RE

    return [tuple(_BOX_RE.findall(hit["query"])[0]) for hit in hits]


def _new_points(before):
    """osm ids of the Points layer the last download added."""
    added = [layer for layer_id, layer in QgsProject.instance().mapLayers().items()
             if layer_id not in before and isinstance(layer, QgsVectorLayer)
             and layer.name().endswith("— Points")]
    check(len(added) == 1, f"Expected one new Points layer, found {[layer.name() for layer in added]}")
    return sorted(int(feature["osm_id"]) for feature in added[0].getFeatures())


def _stored_history():
    from zero2agent_osm_downloader.core import history

    return history.loads(QgsSettings().value(HISTORY_KEY, "[]"))


def _no_old_copy(statuses):
    from zero2agent_osm_downloader.core.transport import JARGON

    for text in statuses:
        check("Check the Processing log" not in text and "failed or was canceled" not in text,
              f"The old catch-all message is back: {text!r}")
        lowered = text.casefold()
        check(not any(word in lowered for word in JARGON), f"The status uses jargon: {text!r}")


# --------------------------------------------------------------------- checks


def check_busy_servers(rig):
    """(a) Every server hangs: a moving status, then the reason and the hint."""
    dock = rig.dock
    host_a, host_b = rig.servers.host("a"), rig.servers.host("b")
    rig.fresh(a="hang", b="hang")
    rig.ask_for_cafes((38.400, 27.100, 38.430, 27.140))
    idle_label = dock.download_button.text()
    dock._last_failure_kind = ""
    rig.log.clear()
    rig.bar.clearWidgets()
    run = rig.drive(dock.download_button.click)
    running, final = run["running"], run["final"]
    check(len(run["hits"]) == 2, f"Two hung hosts took {len(run['hits'])} requests.")
    check(len(running) >= 3, f"The status changed {len(running) - 1} times while running: {running}")
    clocks = {match for text in running for match in _ELAPSED_RE.findall(text)}
    check(len(clocks) >= 3, f"The elapsed time did not move: {sorted(clocks)}")
    check(any(text.startswith("Downloading amenity=cafe — 0:0") and text.endswith(f"· asking {host_a}")
              for text in running), f"Never showed the first server being asked: {running}")
    switched = f"{host_a} did not answer within 2 seconds; trying another server."
    check(any(f"· asking {host_b}\n{switched}" in text for text in running),
          f"Never showed the switch to the second server with its reason: {running}")
    # The advice first, the hosts last (transport.failure_report, REV-UX UX-4).
    sentence = ("The OpenStreetMap servers are busy. Nothing was downloaded. Try again in about "
                f"2 minutes. ({host_a}, {host_b}: did not answer within 2 seconds)")
    check(final == f"{sentence}. {DETAILS}", f"Final status: {final!r}")
    check(any(tag == "Processing" and sentence in message for message, tag in rig.log),
          f"The Processing log does not hold the sentence the status points to: {rig.log[-4:]}")
    check(dock._last_failure_kind == "busy", f"Failure class: {dock._last_failure_kind!r}")
    check("Last failure: busy" in unquote(dock.problem_report_url()),
          "Report a problem does not carry the failure class.")
    check(run["labels"] == {("Downloading…", False)}, f"Download button while running: {run['labels']}")
    check(dock.download_button.text() == idle_label and dock.download_button.isEnabled(),
          f"Download button after: {dock.download_button.text()!r}")
    check(dock.continue_button.isHidden() and not dock.continue_button.isEnabled(),
          "Continue is offered after a failure.")
    check(not rig.bar.items(), "A visible dock also pushed to the message bar.")
    _no_old_copy(run["statuses"])
    return (f"{len(running) - 1} live changes in {run['elapsed']:.1f} s (clock {min(clocks)}–{max(clocks)}, "
            "both servers named, the switch explained); closes with the busy sentence and the Log Messages hint")


def check_wait_and_split(rig):
    """The plain sentence of a wait and of a split reaches the status line."""
    dock = rig.dock
    host_a = rig.servers.host("a")
    # A dropped connection: one more try on the same host, 5 s later.
    rig.fresh(a=("rst_for", {"seconds": 2}), b="ok")
    rig.ask_for_cafes((38.400, 27.100, 38.430, 27.140))
    run = rig.drive(dock.download_button.click)
    wait = f"{host_a} closed the connection; asking it once more in 5 seconds."
    # The wait lasts 5 s, so it was on screen; the retry is answered in
    # milliseconds, so what follows it is read from every text shown.
    check(any(text.endswith("\n" + wait) for text in run["running"]), f"The wait was not shown: {run['running']}")
    waiting = [index for index, text in enumerate(run["shown"]) if text.endswith("\n" + wait)]
    after = run["shown"][waiting[-1] + 1:]
    check(after and after[0].endswith(f"· asking {host_a}"),
          f"Asking again still showed the wait: {after[:2]}")
    check(run["final"] == f"Finished — Points: 2. OSM data as of 2026-11-02 06:58:39 (UTC) from {host_a}.",
          f"After the retry: {run['final']!r}")
    # A part too dense for one request: split into four.
    rig.fresh(a="dense", b="dense")
    rig.ask_for_cafes((38.400, 27.100, 38.490, 27.210))
    run = rig.drive(dock.download_button.click)
    # A timeout alone proves nothing: both hosts are asked before the split.
    split = (f"{host_a} and {rig.servers.host('b')} ran out of time on this query; "
             "part 1 is split into 4 smaller parts.")
    # Parts of a local server arrive within milliseconds: read every text shown.
    check(any("· part 1 of 4" in text and text.endswith("\n" + split) for text in run["shown"]),
          f"The split was not shown: {run['shown']}")
    check(any("· part 4 of 4 · asking" in text for text in run["shown"]),
          f"Parts after the split: {run['shown']}")
    check(len(run["hits"]) == 6 and run["final"].startswith("Finished — Points: 5."),
          f"Dense part: {len(run['hits'])} requests, {run['final']!r}")
    _no_old_copy(run["statuses"])
    return "a wait and a split are shown in the servers' own words; the wait clears once the server is asked"


def check_gaps_and_continue(rig):
    """(b) Six of nine parts lost: gaps said, Recent incomplete, Continue fetches six."""
    from qgis.PyQt.QtCore import Qt

    from zero2agent_osm_downloader.core import history
    from zero2agent_osm_downloader.processing import osm_algorithms

    dock = rig.dock
    host_a = rig.servers.host("a")
    box9 = (38.200, 26.900, 38.460, 27.230)
    QgsSettings().setValue(HISTORY_KEY, "[]")
    dock._populate_recent()
    submitted = _record_submits(dock)
    # The network code tells how long the servers rest (UX-2). The fake
    # hosts answered 502, so they rest 45 s; Continue waits for that.
    rests = getattr(osm_algorithms, "seconds_until_a_server_is_ready", None) is not None
    tail = f" Points: 4. OSM data as of 2026-11-02 06:58:39 (UTC) from {host_a}."
    gaps = ("Finished with gaps: 6 of 9 parts are missing, so some data is missing. "
            "Press Continue to fetch only those." + tail)
    resting = ("Finished with gaps: 6 of 9 parts are missing, so some data is missing. The "
               "servers are resting after errors; Continue fetches only those once they are "
               "ready." + tail)
    try:
        rig.fresh(a=("ok", {"seq": "ok,ok,ok,h502"}), b="h502")
        rig.ask_for_cafes(box9)
        before = set(QgsProject.instance().mapLayers())
        first = rig.drive(dock.download_button.click)
        check(len(first["hits"]) == 5, f"The gap run sent {len(first['hits'])} requests, not 5.")
        check(first["final"] == (resting if rests else gaps), f"Gap status: {first['final']!r}")
        arrived = _boxes([hit for hit in first["hits"] if hit["host"] == "a"][:3])
        first_ids = _new_points(before)
        check(len(first_ids) == 4 and first_ids.count(1) == 1, f"Gap run points: {first_ids}")
        check(dock._last_failure_kind == "gaps", f"Failure class: {dock._last_failure_kind!r}")
        if rests:
            check(dock.continue_button.isHidden() and not dock.continue_button.isEnabled()
                  and re.fullmatch(r"Continue becomes available in about \d+ s, then fetches "
                                   r"only the missing parts\.", dock.continue_wait.text())
                  and not dock.continue_wait.isHidden(),
                  f"Continue while the servers rest: {dock.continue_wait.text()!r}")
        else:
            check(not dock.continue_button.isHidden() and dock.continue_button.isEnabled(),
                  "Continue is not offered after gaps.")
        stored = _stored_history()
        check(len(stored) == 1 and stored[0].get("incomplete") is True
              and stored[0]["algorithm"] == "zero2agentosm:download_custom_tag",
              f"Recent after gaps: {stored}")
        row = dock.recent_combo.itemText(0)
        tip = dock.recent_combo.itemData(0, Qt.ItemDataRole.ToolTipRole)
        check(row.startswith("(incomplete) amenity=cafe"), f"Recent row: {row!r}")
        check("Run again within a day to fetch only the missing parts." in str(tip), f"Tooltip: {tip!r}")
        _no_old_copy(first["statuses"])

        rig.rest_is_over(a="ok", b="ok")
        check(wait_for(lambda: dock.continue_button.isEnabled() and not dock.continue_button.isHidden(), 3)
              and dock.continue_wait.isHidden(), "Continue did not come back once the rest was over.")
        check(dock.status.text() == gaps, f"Gap status once the rest is over: {dock.status.text()!r}")
        before = set(QgsProject.instance().mapLayers())
        second = rig.drive(dock.continue_button.click)
        asked = _boxes(second["hits"])
        check(len(asked) == 6 and len(set(asked)) == 6, f"Continue sent {len(asked)} requests: {asked}")
        check(not set(asked) & set(arrived) and len(set(asked) | set(arrived)) == 9,
              f"Continue asked for parts that had arrived: {sorted(set(asked) & set(arrived))}")
        check(len(submitted) == 2 and submitted[1] == submitted[0],
              f"Continue did not repeat the stored request: {submitted}")
        done = f"Finished — Points: 10. OSM data as of 2026-11-02 06:58:39 (UTC) from {host_a}."
        check(second["final"] == done, f"Continue status: {second['final']!r}")
        second_ids = _new_points(before)
        check(len(second_ids) == 10 and set(first_ids) <= set(second_ids),
              f"Continue points: {second_ids} (first run {first_ids})")
        stored = _stored_history()
        check(len(stored) == 1 and "incomplete" not in stored[0], f"Recent after Continue: {stored}")
        check(not dock.recent_combo.itemText(0).startswith("(incomplete)"), "Recent still says incomplete.")
        check(dock.continue_button.isHidden() and not dock.continue_button.isEnabled(),
              "Continue is still offered after a clean finish.")
        check(dock._last_failure_kind == "", f"Failure class after Continue: {dock._last_failure_kind!r}")
        _no_old_copy(second["statuses"])

        # Run again on an incomplete Recent entry does the same as Continue.
        rig.fresh(a=("ok", {"seq": "ok,ok,ok,h502"}), b="h502")
        third = rig.drive(dock.download_button.click)
        check(third["final"] == (resting if rests else gaps), f"Second gap run: {third['final']!r}")
        check(len(_stored_history()) == 1 and _stored_history()[0].get("incomplete") is True,
              "The gap run did not mark the same Recent entry incomplete.")
        rig.rest_is_over(a="ok", b="ok")
        dock.recent_combo.setCurrentIndex(0)
        check(dock.recent_combo.currentText().startswith("(incomplete)"), "Recent lost the mark.")
        fourth = rig.drive(dock.rerun_button.click)
        check(len(fourth["hits"]) == 6 and fourth["final"] == done,
              f"Run again on the incomplete entry: {len(fourth['hits'])} requests, {fourth['final']!r}")
        check(len(submitted) == 4 and submitted[3] == submitted[0],
              f"Run again did not repeat the stored request: {submitted[3:]}")
        check("incomplete" not in _stored_history()[0], "Run again left the entry incomplete.")
    finally:
        del dock._submit
    # The stored flag survives a reload; history itself is pure Python.
    entry = history.loads(json.dumps([{**_stored_history()[0], "incomplete": True}]))[0]
    check(entry.get("incomplete") is True, "The incomplete flag does not survive a reload.")
    return ("6 of 9 parts missing: gaps said, Recent marked incomplete; "
            + ("Continue waited out the servers' rest, then " if rests else "")
            + "Continue and Run again each sent exactly the 6 missing parts and finished with 10 points")


def check_cancel(rig):
    """(c) Cancel: exactly one sentence, no layers, no Recent entry."""
    dock = rig.dock
    rig.fresh(a="hang", b="hang")
    rig.ask_for_cafes((38.400, 27.100, 38.430, 27.140))
    dock._last_failure_kind = "busy"
    recent = QgsSettings().value(HISTORY_KEY)
    before = set(QgsProject.instance().mapLayers())
    run = rig.drive(dock.download_button.click, cancel_after=0.8)
    check(run["final"] == CANCELED, f"Cancel status: {run['final']!r}")
    # Read from every text shown: a download that stops at once replaces
    # "Canceling download …" within the same event cycle.
    shown = run["shown"]
    canceling = [index for index, text in enumerate(shown) if text == "Canceling download …"]
    check(canceling and shown[-1] == CANCELED
          and all(text in (CANCELED, "Canceling download …") for text in shown[canceling[0]:]),
          f"Statuses from the click on: {shown[canceling[0] if canceling else 0:]}")
    check(run["after_cancel"] is not None and run["after_cancel"] < 3.0,
          f"Cancel took {run['after_cancel']} s to end the download.")
    check(set(QgsProject.instance().mapLayers()) == before, "A canceled download added layers.")
    check(QgsSettings().value(HISTORY_KEY) == recent, "A canceled download joined Recent.")
    check(dock._last_failure_kind == "busy", "Canceling replaced the last real failure class.")
    check(dock.continue_button.isHidden(), "Continue is offered after Cancel.")
    check(dock.download_button.isEnabled() and dock.download_button.text() != "Downloading…",
          "The Download button did not come back after Cancel.")
    _no_old_copy(run["statuses"])
    return f"'{CANCELED}' {run['after_cancel']:.2f} s after the click; no layer, no Recent entry"


def check_success(rig):
    """(d) Success: counts, data date and server; the failure class cleared."""
    dock = rig.dock
    host_a = rig.servers.host("a")
    rig.fresh(a="ok", b="ok")
    rig.ask_for_cafes((38.400, 27.100, 38.430, 27.140))
    dock._last_failure_kind = "busy"
    before = set(QgsProject.instance().mapLayers())
    run = rig.drive(dock.download_button.click)
    expected = f"Finished — Points: 2. OSM data as of 2026-11-02 06:58:39 (UTC) from {host_a}."
    check(run["final"] == expected, f"Success status: {run['final']!r}")
    ids = _new_points(before)
    check(len(ids) == 2 and ids.count(1) == 1, f"Success points: {ids}")
    check(dock._last_failure_kind == "", f"Failure class after success: {dock._last_failure_kind!r}")
    check("Last failure: none recorded" in unquote(dock.problem_report_url()),
          "Report a problem still names an old failure.")
    _no_old_copy(run["statuses"])
    # Objects that could not be built are named once the algorithm reports
    # FEATURES_DROPPED; a run whose outputs lack it says nothing about them.
    dock._finished(True, {"OSM_TIMESTAMP": "2026-11-02T06:58:39Z", "TILES_TOTAL": 1, "TILES_MISSING": 0,
                          "DATA_SOURCE": "overpass.private.coffee", "FEATURES_DROPPED": 3})
    check(dock.status.text() == "Finished — no matching features. OSM data as of 2026-11-02 06:58:39 "
          "(UTC) from overpass.private.coffee. 3 objects could not be built (details in View > "
          "Panels > Log Messages > Processing).",
          f"FEATURES_DROPPED status: {dock.status.text()!r}")
    dock._finished(True, {"OSM_TIMESTAMP": "2026-11-02T06:58:39Z", "DATA_SOURCE": "local cache",
                          "FEATURES_DROPPED": 0})
    check(dock.status.text() == "Finished — no matching features. OSM data as of 2026-11-02 06:58:39 "
          "(UTC) from the local cache.", f"Cache-only status: {dock.status.text()!r}")
    return "'" + expected + "'; the failure class is cleared; dropped objects are named when reported"


_CLOSE_BASE = {"OSM_TIMESTAMP": "2026-11-02T06:58:39Z", "TILES_TOTAL": 1, "TILES_MISSING": 0,
               "DATA_SOURCE": "overpass.private.coffee"}
_CLOSE_HEAD = ("Finished — no matching features. OSM data as of 2026-11-02 06:58:39 (UTC) from "
               "overpass.private.coffee.")


def _close(dock, **outputs):
    """The dock's closing message for a run that returned these outputs."""
    dock._feedback, dock._context, dock._canceling = None, None, False
    dock._finished(True, {**_CLOSE_BASE, **outputs})
    pump(0.05)
    return dock.status.text()


def check_left_out(rig):
    """UX-1: matched objects outside the chosen area are not called failures."""
    dock = rig.dock
    head = _CLOSE_HEAD
    outside_4 = " 4 matched objects were left out because they lie outside the chosen area."
    built_3 = " 3 objects could not be built (details in View > Panels > Log Messages > Processing)."

    def close(**outputs):
        return _close(dock, **outputs)

    cases = (
        ({"FEATURES_OUTSIDE": 4, "FEATURES_DROPPED": 0}, head + outside_4),
        ({"FEATURES_OUTSIDE": 1}, head + " 1 matched object was left out because it lies outside "
         "the chosen area."),
        ({"FEATURES_OUTSIDE": 4, "FEATURES_DROPPED": 3}, head + outside_4 + built_3),
        ({"FEATURES_DROPPED": 1}, head + " 1 object could not be built (details in View > Panels > "
         "Log Messages > Processing)."),
        ({"FEATURES_OUTSIDE": 12345.0}, head + " 12,345 matched objects were left out because they "
         "lie outside the chosen area."),
        # A Processing run of an older algorithm: neither output, nothing said.
        ({}, head),
    )
    for outputs, expected in cases:
        text = close(**outputs)
        check(text == expected, f"{outputs}: {text!r}")
    check("could not be built" not in close(FEATURES_OUTSIDE=4, FEATURES_DROPPED=0),
          "Objects outside the area are still called objects that could not be built.")
    dock._set_status("Ready.")
    return "outside the area: 'N matched objects were left out…', never 'could not be built'"


def check_notice_under_the_status(rig):
    """UX-5: the algorithm's NOTICE is shown under the closing message."""
    from qgis.PyQt.QtWidgets import QLabel

    dock, bar = rig.dock, rig.bar
    head = _CLOSE_HEAD

    def close(**outputs):
        return _close(dock, **outputs)

    notice = ("Nothing came back from inside the boundary of Konak; the server may not hold it. "
              "Choose Rectangular extent and press Download again.")
    text = close(NOTICE=notice)
    shown = [label for label in dock.findChildren(QLabel)
             if notice in label.text() and label.isVisibleTo(dock)]
    check(shown, f"The NOTICE is nowhere in the dock; the status says {text!r}")
    check(text == head, f"The notice changed the closing message: {text!r}")
    check(dock.status_notice.text() == notice and dock.status_notice.isVisibleTo(dock),
          f"NOTICE not shown: {dock.status_notice.text()!r}")
    status, under = dock.status.geometry(), dock.status_notice.geometry()
    check(dock.footer.isAncestorOf(dock.status_notice) and under.top() >= status.bottom()
          and dock.status_notice.parentWidget() is dock.status.parentWidget(),
          f"The notice is not under the status card: {status} / {under}")
    check(dock.status_notice.wordWrap() and dock.status_notice.height()
          >= dock.status_notice.heightForWidth(dock.status_notice.width()) - 1,
          "The notice is cut off.")
    dock._set_status("Searching for “Konak” …")
    check(dock.status_notice.isHidden() and not dock.status_notice.text(),
          "The notice outlived its closing message.")
    for empty in ("", "   "):
        close(NOTICE=empty)
        check(dock.status_notice.isHidden(), f"An empty NOTICE {empty!r} was shown.")
    # With gaps the notice is shown too.
    close(TILES_TOTAL=9, TILES_MISSING=6, NOTICE=notice)
    check(dock.status.text().startswith("Finished with gaps: 6 of 9 parts are missing")
          and dock.status_notice.text() == notice, f"Gaps and notice: {dock.status.text()!r}")
    dock._offer_continue(None)
    # Out of sight, the message bar carries the notice as well.
    rig.browser.raise_()
    pump(0.2)
    bar.clearWidgets()
    text = close(NOTICE=notice)
    pushed = [item.text() for item in bar.items()]
    check(pushed == [f"{text} {notice}"], f"Message bar with a notice: {pushed}")
    dock.raise_()
    pump(0.2)
    bar.clearWidgets()
    dock._set_status("Ready.")
    return ("NOTICE on its own line under the status, gone with the next status, and in the "
            "message bar")


def check_left_out_and_notice_end_to_end(rig):
    """The same two closing lines from real downloads, once the algorithm
    reports FEATURES_OUTSIDE and NOTICE: a cafe outside an around circle,
    and a boundary download whose outline lookup fails and finds nothing."""
    from zero2agent_osm_downloader.core.geocoding import parse_geocode_results

    dock = rig.dock
    host_a = rig.servers.host("a")
    algorithm = QgsApplication.processingRegistry().algorithmById(
        "zero2agentosm:download_custom_tag")
    reports = {name: algorithm is not None and algorithm.outputDefinition(name) is not None
               for name in ("FEATURES_OUTSIDE", "NOTICE")}
    notes = []
    captured = []
    finished = dock._finished
    dock._finished = lambda successful, results: (captured.append(dict(results or {})),
                                                  finished(successful, results))[-1]
    try:
        if reports["FEATURES_OUTSIDE"]:
            # The fake answers a cafe at the centre of the requested box and
            # one at 38.41, 27.11: inside the circle's box, 552 m from its
            # centre, outside a 500 m circle.
            rig.fresh(a="ok", b="ok")
            rig.ask_for_cafes((38.400, 27.100, 38.430, 27.140))
            dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("around"))
            dock.around_centre_combo.setCurrentIndex(dock.around_centre_combo.findData("map"))
            dock.around_radius.setValue(500)
            rig.canvas.setExtent(QgsRectangle(27.1095, 38.4085, 27.1195, 38.4185))
            pump(0.1)
            run = rig.drive(dock.download_button.click)
            outputs = captured[-1] if captured else {}
            check(run["final"] == f"Finished — Points: 1. OSM data as of 2026-11-02 06:58:39 (UTC) "
                  f"from {host_a}. 1 matched object was left out because it lies outside the "
                  "chosen area.", f"Around with a cafe outside the circle: {run['final']!r} {outputs}")
            notes.append("a cafe outside the circle is 'left out', not 'could not be built'")
        else:
            print("    [SKIP] the algorithm does not report FEATURES_OUTSIDE yet")
        if reports["NOTICE"]:
            konak = parse_geocode_results(json.loads(
                (PLUGIN_DIR / "tests" / "fixtures" / "nominatim_konak.json").read_text(encoding="utf-8")),
                "Konak")

            class Done:  # a finished place search, as the dock's task hands it over
                candidates, error_text, error_detail, place = tuple(konak), "", "", "Konak"

            rig.fresh(a="empty", b="ok")
            rig.ask_for_cafes((38.400, 27.100, 38.430, 27.140))
            dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("place"))
            task = Done()
            dock._place_task = task
            dock._place_resolved(task, True)
            check(dock._uses_exact_boundary(), "Konak is not downloaded by its boundary.")
            run = rig.drive(dock.download_button.click)
            notice = str((captured[-1] if captured else {}).get("NOTICE") or "").strip()
            check(run["final"].startswith("Finished — no matching features.") and notice
                  and dock.status_notice.text() == notice and not dock.status_notice.isHidden(),
                  f"Boundary fallback that found nothing: {run['final']!r}, NOTICE {notice!r}, "
                  f"shown {dock.status_notice.text()!r}")
            notes.append(f"the boundary fallback's NOTICE is shown: {notice!r}")
            dock._clear_place()
        else:
            print("    [SKIP] the algorithm does not report NOTICE yet")
    finally:
        del dock._finished
        dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("drawn"))
    return "; ".join(notes) or "skipped: this build reports neither FEATURES_OUTSIDE nor NOTICE"


class _Rest:
    """Stands in for osm_algorithms.seconds_until_a_server_is_ready."""

    def __init__(self):
        self.seconds = 0.0
        self.asked = []

    def __call__(self, allow_fallback):
        self.asked.append(allow_fallback)
        return self.seconds


def check_continue_waits_for_the_servers(rig):
    """UX-2: Continue stays disabled while the servers rest, a countdown
    stands in its place, and Continue comes back when one is ready."""
    from qgis.core import QgsReferencedRectangle

    from zero2agent_osm_downloader.dialogs import dock as dockmod
    from zero2agent_osm_downloader.processing import osm_algorithms

    dock = rig.dock
    name = "seconds_until_a_server_is_ready"
    missing = object()
    original = getattr(osm_algorithms, name, missing)
    rest = _Rest()
    setattr(osm_algorithms, name, rest)
    history = QgsSettings().value(HISTORY_KEY)
    # What Continue would send; nothing is downloaded here.
    submitted = []
    dock._submit = lambda algorithm_id, parameters, extent: submitted.append(
        (algorithm_id, dict(parameters)))
    host = "overpass.private.coffee"

    def gaps(action):
        return (f"Finished with gaps: 6 of 9 parts are missing, so some data is missing. {action} "
                f"No matching features. OSM data as of 2026-11-02 06:58:39 (UTC) from {host}.")

    resting = gaps("The servers are resting after errors; Continue fetches only those once they "
                   "are ready.")
    ready = gaps("Press Continue to fetch only those.")

    def finish_with_gaps():
        extent = QgsReferencedRectangle(QgsRectangle(26.9, 38.2, 27.23, 38.46),
                                        QgsCoordinateReferenceSystem("EPSG:4326"))
        dock._pending_request = ("zero2agentosm:download_custom_tag",
                                 {"KEY": "amenity", "VALUE": "cafe", "GEOMETRY": 0}, extent)
        dock._current_label, dock._current_style_scope, dock._current_where = "amenity=cafe", "", ""
        dock._feedback, dock._context, dock._canceling = None, None, False
        dock._finished(True, {"OSM_TIMESTAMP": "2026-11-02T06:58:39Z", "DATA_SOURCE": host,
                              "TILES_TOTAL": 9, "TILES_MISSING": 6})
        pump(0.05)

    def counting(wait):
        return (dock.continue_button.isHidden() and not dock.continue_button.isEnabled()
                and not dock.continue_wait.isHidden()
                and dock.continue_wait.text() == "Continue becomes available in about "
                f"{wait}, then fetches only the missing parts.")

    def offered():
        return (not dock.continue_button.isHidden() and dock.continue_button.isEnabled()
                and dock.continue_wait.isHidden())

    try:
        rest.seconds = 40.0
        finish_with_gaps()
        check(dock.continue_button.isHidden() and not dock.continue_button.isEnabled(),
              f"Continue is offered while the servers rest (hidden={dock.continue_button.isHidden()}, "
              f"enabled={dock.continue_button.isEnabled()}); status {dock.status.text()!r}")
        check(counting("40 s"), f"While the servers rest, the countdown reads {dock.continue_wait.text()!r}")
        check(dock.status.text() == resting, f"Gap message while resting: {dock.status.text()!r}")
        check(rest.asked and rest.asked[-1] == dock.fallback_check.isChecked(),
              f"The rest was asked for allow_fallback={rest.asked[-1:]}")
        dock.continue_button.click()
        pump(0.1)
        check(not submitted and dock._task is None, "Continue ran while the servers rested.")
        # The Postpass option changes which servers count: asked again at once.
        before = len(rest.asked)
        dock.fallback_check.setChecked(not dock.fallback_check.isChecked())
        check(len(rest.asked) > before and rest.asked[-1] == dock.fallback_check.isChecked(),
              f"Toggling the Postpass option did not ask again: {rest.asked[before:]}")
        dock.fallback_check.setChecked(not dock.fallback_check.isChecked())
        rest.seconds = 12.2
        check(wait_for(lambda: counting("15 s"), 3), f"The countdown did not move: {dock.continue_wait.text()!r}")
        rest.seconds = 0.0
        check(wait_for(offered, 3), "Continue did not come back when a server was ready.")
        check(dock.status.text() == ready, f"Gap message once ready: {dock.status.text()!r}")
        # Something else on the status meanwhile: it is left alone.
        rest.seconds = 40.0
        finish_with_gaps()
        dock._set_status("Searching for “Konak” …")
        rest.seconds = 0.0
        check(wait_for(offered, 3) and dock.status.text() == "Searching for “Konak” …",
              f"A later status was overwritten: {dock.status.text()!r}")
        # A new download takes the countdown away with Continue.
        rest.seconds = 40.0
        finish_with_gaps()
        dock._offer_continue(None)
        check(dock.continue_button.isHidden() and dock.continue_wait.isHidden()
              and not dock._continue_timer.isActive(), "The countdown outlived Continue.")
        # A build without the answer never waits, as before.
        delattr(osm_algorithms, name)
        finish_with_gaps()
        check(offered() and dock.status.text() == ready,
              f"Without the rest answer: {dock.status.text()!r}")
        dock.continue_button.click()
        pump(0.1)
        check(submitted == [("zero2agentosm:download_custom_tag",
                             {"KEY": "amenity", "VALUE": "cafe", "GEOMETRY": 0})],
              f"Continue did not send the stored request once offered: {submitted}")
        for seconds, text in ((0.2, "5 s"), (40, "40 s"), (43, "45 s"), (55, "55 s"),
                              (56, "1 min"), (61, "2 min"), (3600, "60 min")):
            check(dockmod._wait_text(seconds) == text,
                  f"{seconds} s reads {dockmod._wait_text(seconds)!r}")
    finally:
        del dock._submit
        if original is missing:
            if hasattr(osm_algorithms, name):
                delattr(osm_algorithms, name)
        else:
            setattr(osm_algorithms, name, original)
        dock._offer_continue(None)
        if history is None:
            QgsSettings().remove(HISTORY_KEY)
        else:
            QgsSettings().setValue(HISTORY_KEY, history)
        dock._populate_recent()
        dock._set_status("Ready.")
    return ("resting servers: Continue disabled, 'Continue becomes available in about 40 s, then "
            "fetches only the missing parts.' in its place, counting down; back when one is ready")


def _level(value) -> int:
    """A message level as a number: 3.44 hands back ints, 4.2 enum members."""
    return int(getattr(value, "value", value))


def check_out_of_sight(rig):
    """(e) Behind Browser or closed: the message bar repeats the closing message."""
    dock, bar = rig.dock, rig.bar
    host_a, host_b = rig.servers.host("a"), rig.servers.host("b")
    small = (38.400, 27.100, 38.430, 27.140)
    title = "02Agent OSM Downloader"
    success, warning, critical = (
        _level(Qgis.MessageLevel.Success), _level(Qgis.MessageLevel.Warning),
        _level(Qgis.MessageLevel.Critical),
    )

    def pushed():
        return [(item.title(), item.text(), _level(item.level()), item.duration())
                for item in bar.items()]

    def closing(run):
        """The closing message as the bar repeats it: the status, then any notice."""
        notice = dock.status_notice.text() if not dock.status_notice.isHidden() else ""
        return " ".join([run["final"], *notice.splitlines()])

    def seen():
        # Behind Browser a tabbed dock still reports isVisible(); what shows
        # is its visible region (measured on 3.44 and 4.2).
        return dock.isVisible() and not dock.visibleRegion().isEmpty()

    rig.browser.raise_()
    pump(0.2)
    check(not seen(), "Raising Browser did not cover the dock.")
    bar.clearWidgets()
    rig.fresh(a="ok", b="ok")
    rig.ask_for_cafes(small)
    run = rig.drive(dock.download_button.click)
    items = pushed()
    check(len(items) == 1 and items[0][:3] == (title, closing(run), success),
          f"Behind Browser, success pushed {items}")

    dock.setVisible(False)
    pump(0.1)
    bar.clearWidgets()
    rig.fresh(a="h502", b="h502")
    run = rig.drive(dock.download_button.click)
    sentence = ("The OpenStreetMap servers are busy. Nothing was downloaded. Try again in about a "
                f"minute. ({host_a}, {host_b}: was at capacity (HTTP 502))")
    check(run["final"] == f"{sentence}. {DETAILS}", f"Closed dock, failure status: {run['final']!r}")
    check(pushed() == [(title, run["final"], critical, 0)], f"Closed dock, failure pushed {pushed()}")

    bar.clearWidgets()
    rig.fresh(a=("ok", {"seq": "ok,ok,ok,h502"}), b="h502")
    rig.ask_for_cafes((38.200, 26.900, 38.460, 27.230))
    run = rig.drive(dock.download_button.click)
    check(run["final"].startswith("Finished with gaps: 6 of 9 parts are missing"), f"Gaps: {run['final']!r}")
    check(pushed() == [(title, closing(run), warning, 0)], f"Closed dock, gaps pushed {pushed()}")

    bar.clearWidgets()
    rig.fresh(a="hang", b="hang")
    rig.ask_for_cafes(small)
    run = rig.drive(dock.download_button.click, cancel_after=0.5)
    check(run["final"] == CANCELED and not pushed(),
          f"Closed dock, cancel: {run['statuses']}, pushed {pushed()}")

    dock.setVisible(True)
    dock.raise_()
    pump(0.2)
    check(seen(), "The raised dock is not on screen.")
    bar.clearWidgets()
    rig.fresh(a="ok", b="ok")
    run = rig.drive(dock.download_button.click)
    check(run["final"].startswith("Finished — Points: 2.") and not pushed(),
          f"A visible dock pushed {pushed()}")
    return "behind Browser: Success; closed: Critical (failure) and Warning (gaps) kept until dismissed; " \
           "nothing for Cancel or a visible dock"


def check_closing_without_a_step(rig):
    """Closing messages when no "failed" step arrived, and when adding fails."""
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback

    dock = rig.dock

    def close(feedback, successful=False, results=None, context=None):
        dock._feedback, dock._context, dock._canceling = feedback, context, False
        dock._finished(successful, results or {})
        return dock.status.text()

    # The task's log is the fallback: QGIS writes the exception there last.
    logged = QgsProcessingFeedback(False)
    logged.pushInfo("Asking a server …")
    logged.reportError("The selected extent is outside WGS84 bounds")
    dock._last_failure_kind = ""
    text = close(logged)
    check(text == f"The selected extent is outside WGS84 bounds. {DETAILS}", f"Log fallback: {text!r}")
    check(dock._last_failure_kind == "error", f"Log fallback class: {dock._last_failure_kind!r}")
    # Canceled before it started (QGIS's task list): nothing ran, nothing logged.
    unstarted = QgsProcessingFeedback(False)
    unstarted.cancel()
    check(close(unstarted) == CANCELED, f"Canceled before the start: {dock.status.text()!r}")
    # A canceled feedback that carries an error is a failure, not a cancel.
    refused = QgsProcessingFeedback(False)
    refused.reportError("The selected preset is not valid.")
    refused.cancel()
    text = close(refused)
    check(text == f"The selected preset is not valid. {DETAILS}", f"Canceled with an error: {text!r}")

    # Data that arrived but could not be added: the reason, and where the
    # traceback is (this dock's own Log Messages tab).
    def broken(_results, _context):
        raise RuntimeError("the project refused the layer")

    dock._add_result_layers = broken
    rig.log.clear()
    try:
        text = close(None, True, {"TILES_MISSING": 0}, QgsProcessingContext())
    finally:
        del dock._add_result_layers
    check(text == "The data arrived but could not be added to the project (RuntimeError: the project "
          "refused the layer). Details: View > Panels > Log Messages > 02Agent OSM Downloader.",
          f"Layers not added: {text!r}")
    check(any(tag == "02Agent OSM Downloader" and "the project refused the layer" in message
              for message, tag in rig.log), "The traceback is not in the tab the status names.")
    check(dock._last_failure_kind == "error", f"Not-added class: {dock._last_failure_kind!r}")
    return "the task log stands in for a missing step; a task canceled unstarted says canceled; " \
           "an unadded result names its log tab"


def check_history_flag():
    """The incomplete flag in the stored history: round trip and older entries."""
    from zero2agent_osm_downloader.core import history

    extent = {"xmin": 27.1, "ymin": 38.4, "xmax": 27.2, "ymax": 38.5, "crs": "EPSG:4326"}
    gap = history.make_entry("Buildings", "zero2agentosm:download_preset", {"PRESET": [3]}, extent,
                             "2026-11-02T09:00:00", "Polygons: 40", presets=["buildings"], incomplete=True)
    whole = history.make_entry("Buildings", "zero2agentosm:download_preset", {"PRESET": [3]}, extent,
                               "2026-11-02T09:05:00", "Polygons: 52", presets=["buildings"])
    check(gap["incomplete"] is True and "incomplete" not in whole, f"Flag: {gap} {whole}")
    check(history.loads(history.dumps([gap])) == [gap], "The flag does not round-trip.")
    # 1.5.1 wrote PRESET indexes and no flag: such an entry is complete.
    legacy = {"label": "Buildings", "algorithm": "zero2agentosm:download_preset",
              "parameters": {"PRESET": [3]}, "extent": extent, "when": "2026-10-28T09:12:00",
              "summary": "Polygons: 110", "style_scope": ""}
    loaded = history.loads(json.dumps([legacy]))
    check(len(loaded) == 1 and "incomplete" not in loaded[0] and loaded[0]["presets"] == ["buildings"],
          f"A 1.5.1 entry: {loaded}")
    for value in ("true", 1, "yes", None, False):
        damaged = history.loads(json.dumps([{**gap, "incomplete": value}]))
        check(len(damaged) == 1 and "incomplete" not in damaged[0],
              f"incomplete={value!r} was read as {damaged}")
    # Same request: the complete run replaces the incomplete entry, and back.
    check(history.add([gap], whole) == [whole], "A complete run did not replace the gap entry.")
    check(history.add([whole], gap) == [gap], "A gap run did not replace the complete entry.")
    return "stored flag round-trips; 1.5.1, current and damaged entries read as complete; same request replaces"


def check_copy():
    """The catch-all failure sentence is gone from the dock."""
    from zero2agent_osm_downloader.dialogs import dock as dockmod

    source = Path(dockmod.__file__).read_text(encoding="utf-8")
    for old in ("Check the Processing log", "Download failed or was canceled"):
        check(old not in source, f"The dock still says {old!r}.")
    return "no 'Check the Processing log' and no 'failed or was canceled' left in the dock"


def run_checks():
    from zero2agent_osm_downloader.processing import osm_algorithms
    from zero2agent_osm_downloader.tests._offline import isolate_network
    from zero2agent_osm_downloader.tests.qgis_fetch_resilience import FakeServers

    # Every host at a closed local port before anything runs; the fake
    # Overpass hosts below are on 127.0.0.1 too.
    isolate_network()
    snapshot = {key: QgsSettings().value(key) for key in SETTINGS}
    for key in SETTINGS:
        QgsSettings().remove(key)
    QgsSettings().setValue(FALLBACK_KEY, False)
    servers = FakeServers()
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    original = osm_algorithms.OVERPASS_ENDPOINTS
    osm_algorithms.OVERPASS_ENDPOINTS = (servers.url("a"), servers.url("b"))
    results = []
    try:
        rig = Rig(servers)
        steps = (
            ("busy servers", lambda: check_busy_servers(rig)),
            ("wait and split", lambda: check_wait_and_split(rig)),
            ("gaps and Continue", lambda: check_gaps_and_continue(rig)),
            ("cancel", lambda: check_cancel(rig)),
            ("success", lambda: check_success(rig)),
            ("left out", lambda: check_left_out(rig)),
            ("notice under the status", lambda: check_notice_under_the_status(rig)),
            ("Continue waits for the servers", lambda: check_continue_waits_for_the_servers(rig)),
            ("dock out of sight", lambda: check_out_of_sight(rig)),
            ("left out and notice, end to end", lambda: check_left_out_and_notice_end_to_end(rig)),
            ("closing without a step", lambda: check_closing_without_a_step(rig)),
            ("history flag", check_history_flag),
            ("copy", check_copy),
        )
        for name, step in steps:
            started = time.monotonic()
            try:
                results.append((name, True, step()))
            except Exception as error:  # noqa: BLE001 - one check, one verdict
                traceback.print_exc()
                results.append((name, False, f"{type(error).__name__}: {error}"))
            print(f"  ({name}: {time.monotonic() - started:.1f} s)", flush=True)
    finally:
        osm_algorithms.OVERPASS_ENDPOINTS = original
        for key, value in snapshot.items():
            if value is None:
                QgsSettings().remove(key)
            else:
                QgsSettings().setValue(key, value)
    return results


def main():
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    plugin_parent = str(PLUGIN_DIR.parent)
    if plugin_parent not in sys.path:
        sys.path.insert(0, plugin_parent)
    profile = tempfile.TemporaryDirectory(prefix="zero2agent-dock-feedback-")
    app = QgsApplication([], True, profile.name, "external")
    app.initQgis()
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)
    from processing.core.Processing import Processing
    from qgis.analysis import QgsNativeAlgorithms

    QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())
    Processing.initialize()
    from zero2agent_osm_downloader.processing.provider import AgentOsmProvider

    # A provider only the registry holds is garbage-collected (qgis_v15_smoke).
    provider = AgentOsmProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    print("=" * 62)
    print(f" 02Agent OSM Downloader - dock live status and Continue ({Qgis.version()})")
    print("=" * 62, flush=True)
    results = []
    try:
        results = run_checks()
    except Exception as error:  # noqa: BLE001 - a broken rig is a failed test
        traceback.print_exc()
        results = [("rig", False, f"{type(error).__name__}: {error}")]
    passed = sum(1 for _name, ok, _note in results if ok)
    for name, ok, note in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {note}")
    print("-" * 62)
    print(f"  {passed}/{len(results)} passed")
    ok = bool(results) and passed == len(results)
    sys.stdout.flush()
    sys.stderr.flush()
    # TRAPS 4.6: exit from the frame that holds the application.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
