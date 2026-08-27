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
