"""
CDP client tests -- the reader thread, and what a caller sees when it stops.

The bug these exist for: `create_connection(timeout=...)` calls settimeout(),
which applies to recv as well as to connect, so recv raises
WebSocketTimeoutException after 30s of SILENCE. CDP is silent for exactly as
long as the model is thinking. The reader treated that as a dead socket, broke,
and never restarted -- so a single slow Converse call ended the run several tool
calls later with a message ("timeout waiting for Page.navigate") that pointed at
the browser rather than at the socket.

These run against a fake websocket. Nothing here needs a browser.
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest
import websocket
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cdp  # noqa: E402


class FakeWS:
    """
    Replays a script of recv outcomes.

    Each entry is either bytes/str to hand back, or an exception INSTANCE to
    raise. Once the script is spent, recv keeps raising a timeout after a short
    sleep -- which is precisely what a healthy-but-quiet CDP socket does, and
    what the reader must survive indefinitely.
    """

    def __init__(self, script=None, send_error=None):
        self.script = list(script or [])
        self.sent: list[dict] = []
        self.closed = False
        self._send_error = send_error

    def recv(self):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        time.sleep(0.01)
        raise WebSocketTimeoutException("Connection timed out waiting for data")

    def send(self, payload):
        if self._send_error:
            raise self._send_error
        self.sent.append(json.loads(payload))

    def close(self):
        self.closed = True


@pytest.fixture
def fake_ws(monkeypatch):
    """Hand CDPSession a FakeWS instead of a real socket."""
    holder = {}

    def _make(script=None, send_error=None):
        ws = FakeWS(script, send_error)
        holder["ws"] = ws
        monkeypatch.setattr(
            websocket, "create_connection", lambda *a, **k: ws
        )
        return ws

    return _make


def _reply(msg_id, result=None):
    return json.dumps({"id": msg_id, "result": result or {"ok": True}})


class TestReaderSurvivesIdleSocket:
    """
    THE REGRESSION. A quiet socket is not a broken one.
    """

    def test_recv_timeout_does_not_kill_the_reader(self, fake_ws):
        fake_ws(
            [
                WebSocketTimeoutException("Connection timed out"),
                WebSocketTimeoutException("Connection timed out"),
                _reply(1, {"value": "still here"}),
            ]
        )
        session = cdp.CDPSession("ws://fake", {})
        try:
            assert session.send("Page.navigate", timeout=5) == {"value": "still here"}
            assert session._reader_stopped is None
        finally:
            session.close()

    def test_many_consecutive_timeouts_are_survivable(self, fake_ws):
        """
        A run can idle across several model turns. The reader must not degrade
        after the first gap -- the old code failed on exactly one.
        """
        script = [WebSocketTimeoutException("timed out")] * 25 + [_reply(1)]
        fake_ws(script)
        session = cdp.CDPSession("ws://fake", {})
        try:
            assert session.send("Runtime.evaluate", timeout=5) == {"ok": True}
        finally:
            session.close()

    def test_events_still_arrive_after_an_idle_gap(self, fake_ws):
        """
        Console errors and failed requests are half the value of this agent. A
        reader that survived the gap but stopped collecting events would be a
        silent downgrade to "visible text only".
        """
        event = json.dumps(
            {
                "method": "Runtime.exceptionThrown",
                "params": {"exceptionDetails": {"text": "TypeError: x is not a function"}},
            }
        )
        fake_ws([WebSocketTimeoutException("timed out"), event, _reply(1)])
        session = cdp.CDPSession("ws://fake", {})
        try:
            session.send("Page.enable", timeout=5)
            drained = session.drain_events()
            assert any("TypeError" in e for e in drained["console_errors"])
        finally:
            session.close()


class TestReaderStopIsDiagnosable:
    """
    When the socket really is gone, the caller should be told that -- promptly,
    and by name. Blocking for the full command timeout and then reporting
    "timeout waiting for Page.navigate" sends the reader to the browser logs to
    debug a dead websocket.
    """

    def test_closed_socket_surfaces_as_a_named_reader_stop(self, fake_ws):
        fake_ws([WebSocketConnectionClosedException("socket is already closed.")])
        session = cdp.CDPSession("ws://fake", {})
        try:
            with pytest.raises(cdp.CDPError, match="reader stopped"):
                session.send("Page.navigate", timeout=5)
        finally:
            session.close()

    def test_a_dead_reader_does_not_wait_out_the_full_timeout(self, fake_ws):
        fake_ws([WebSocketConnectionClosedException("gone")])
        session = cdp.CDPSession("ws://fake", {})
        try:
            started = time.monotonic()
            with pytest.raises(cdp.CDPError):
                session.send("Page.navigate", timeout=10)
            assert time.monotonic() - started < 2.0
        finally:
            session.close()

    def test_send_failure_is_a_cdp_error_not_a_websocket_exception(self, fake_ws):
        """
        browser_tools.dispatch catches CDPError and turns it into data for the
        model. A raw websocket exception escapes that and crashes the run.
        """
        fake_ws(send_error=WebSocketConnectionClosedException("socket is already closed."))
        session = cdp.CDPSession("ws://fake", {})
        try:
            with pytest.raises(cdp.CDPError, match="send failed"):
                session.send("Page.navigate", timeout=5)
        finally:
            session.close()

    def test_normal_close_is_labelled_as_such(self, fake_ws):
        fake_ws()
        session = cdp.CDPSession("ws://fake", {})
        session.close()
        # The reader notices the close on its next wake-up.
        deadline = time.monotonic() + 3
        while session._reader_stopped is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session._reader_stopped == "session closed"


def test_a_timed_out_command_does_not_wedge_later_ones(fake_ws):
    """
    The reply to a command that already timed out must not be mistaken for the
    reply to the next one -- ids are matched, not positions.
    """
    fake_ws([_reply(2, {"second": True})])
    session = cdp.CDPSession("ws://fake", {})
    try:
        session._id = 1  # pretend command 1 was sent and timed out
        assert session.send("Runtime.evaluate", timeout=5) == {"second": True}
    finally:
        session.close()


class TestHeldEventsAreBounded:
    """
    Each entry was already truncated to 500 chars; the LIST was not bounded at
    all, and a drain only happens on the next read -- however long the model
    spends thinking. A page in a React error loop emits thousands a second:
    50,000 entries measured at 23.8 MB resident, which is exactly the "the page
    silently broke" case this agent exists to visit.

    The cap cannot change what the model sees. drain_events() already truncates
    to 20, so this is a memory bound and nothing else -- which is why the tests
    below assert the drain output is unchanged.
    """

    def _session(self, fake_ws):
        fake_ws()
        return cdp.CDPSession("ws://fake", {})

    def _flood(self, session, n, method="Runtime.exceptionThrown"):
        for _ in range(n):
            session._on_event({"method": method,
                               "params": {"exceptionDetails": {"text": "boom"}}})

    def test_console_errors_stop_accumulating_at_the_cap(self, fake_ws):
        session = self._session(fake_ws)
        try:
            self._flood(session, 50_000)
            assert len(session.console_errors) == cdp.MAX_HELD_EVENTS
        finally:
            session.close()

    def test_failed_requests_are_capped_too(self, fake_ws):
        session = self._session(fake_ws)
        try:
            for _ in range(5_000):
                session._on_event({"method": "Network.loadingFailed",
                                   "params": {"errorText": "net::ERR", "type": "XHR"}})
            assert len(session.failed_requests) == cdp.MAX_HELD_EVENTS
        finally:
            session.close()

    def test_the_earliest_entries_are_the_ones_kept(self, fake_ws):
        """
        On a page repeating one error, the FIRST occurrences are the diagnostic
        ones and drain_events reads from the front -- so the cap drops the
        newest rather than rotating the oldest out.
        """
        session = self._session(fake_ws)
        try:
            for i in range(cdp.MAX_HELD_EVENTS + 50):
                session._on_event({"method": "Log.entryAdded",
                                   "params": {"entry": {"level": "error", "text": f"e{i}"}}})
            assert "e0" in session.console_errors[0]
            assert not any("e250" in e for e in session.console_errors)
        finally:
            session.close()

    def test_what_the_model_sees_is_unchanged(self, fake_ws):
        """The drain already truncated to 20; the cap must not alter that."""
        session = self._session(fake_ws)
        try:
            self._flood(session, 50_000)
            drained = session.drain_events()
            assert len(drained["console_errors"]) == 20
        finally:
            session.close()

    def test_a_drain_frees_the_hold_so_a_long_run_can_keep_collecting(self, fake_ws):
        session = self._session(fake_ws)
        try:
            self._flood(session, 1_000)
            session.drain_events()
            assert session.console_errors == []
            self._flood(session, 5)
            assert len(session.console_errors) == 5
        finally:
            session.close()


class TestSettleTimeoutsAreCounted:
    """
    `_inflight` rises on requestWillBeSent and falls on loadingFinished/Failed.
    A connection that stays open -- EventSource, websocket, polling interval --
    gives the first and never the second, so wait_for_network_idle can never
    settle and every navigate/click pays the full timeout instead of ~0.5s.
    Twenty such calls is 300s of a 600s deadline.

    The only trace was an INFO log, and nothing reads the runtime's logs when a
    report comes back. Counting it is the same argument paced_seconds already
    makes for the other clock: time spent waiting is indistinguishable from a
    slow agent, and the two want opposite responses.
    """

    def _session(self, fake_ws):
        fake_ws()
        return cdp.CDPSession("ws://fake", {})

    def test_a_settled_network_returns_true_and_counts_nothing(self, fake_ws):
        session = self._session(fake_ws)
        try:
            session._last_activity = time.monotonic() - 5
            assert session.wait_for_network_idle(quiet_ms=10, timeout=2.0) is True
            assert session.settle_timeouts == 0
            assert session.settle_timeout_seconds == 0.0
        finally:
            session.close()

    def test_a_request_that_never_finishes_times_out_and_is_counted(self, fake_ws):
        session = self._session(fake_ws)
        try:
            session._on_event({"method": "Network.requestWillBeSent", "params": {}})
            assert session.wait_for_network_idle(quiet_ms=10, timeout=0.3) is False
            assert session.settle_timeouts == 1
            assert session.settle_timeout_seconds >= 0.3
        finally:
            session.close()

    def test_a_stream_that_keeps_talking_never_settles(self, fake_ws):
        """
        The OTHER shape, and the one a polling SPA actually produces: inflight
        does reach zero, but activity keeps refreshing so the quiet period never
        elapses. Modelled by asking for a quiet window longer than the timeout,
        which is what a page talking every few hundred ms amounts to -- no
        patching of time, so the test cannot outlive its own assumptions.
        """
        session = self._session(fake_ws)
        try:
            session._last_activity = time.monotonic()
            assert session._inflight == 0
            assert session.wait_for_network_idle(quiet_ms=5_000, timeout=0.3) is False
            assert session.settle_timeouts == 1
        finally:
            session.close()

    def test_the_cost_accumulates_across_calls(self, fake_ws):
        """It is the TOTAL that eats the deadline, not any one wait."""
        session = self._session(fake_ws)
        try:
            session._on_event({"method": "Network.requestWillBeSent", "params": {}})
            session.wait_for_network_idle(quiet_ms=10, timeout=0.2)
            session.wait_for_network_idle(quiet_ms=10, timeout=0.2)
            assert session.settle_timeouts == 2
            assert session.settle_timeout_seconds >= 0.4
        finally:
            session.close()
