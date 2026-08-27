"""
Minimal Chrome DevTools Protocol client.

WHY NOT PLAYWRIGHT
------------------
Playwright is the obvious choice and was the first implementation. It cannot
ship here. Its wheel is tagged `py3-none-any` and declares Root-Is-Purelib, but
it bundles a `node` driver binary that is selected for the INSTALL platform --
packaging on a Mac yields a Mach-O arm64 binary, which will not execute on the
managed Linux runtime. The wheel tag means `pip --platform` cannot correct it
either. That is the same silent-rejection failure as shipping an amd64 container
image to an arm64 runtime, just relocated into a dependency: it would surface as
an import error at invoke time, after a browser session had already been paid
for.

Playwright is also 134 MB. This module plus websocket-client is under 100 KB.

We talk to a REMOTE browser over a websocket AgentCore hands us, and the tool
surface is five operations. CDP covers them directly, so the abstraction was
buying us very little to begin with.
"""

import json
import logging
import threading
import time

import websocket
from websocket import WebSocketTimeoutException

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class CDPError(RuntimeError):
    pass


class CDPSession:
    """
    One attached page target.

    Runs a reader thread because CDP interleaves command responses with events,
    and the events are half of what makes this agent useful -- console errors and
    failed requests are how "the page silently broke" becomes visible.
    """

    def __init__(self, ws_url: str, headers: dict):
        header_list = [f"{k}: {v}" for k, v in headers.items()]
        self._ws = websocket.create_connection(ws_url, header=header_list, timeout=DEFAULT_TIMEOUT)
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._cv = threading.Condition(self._lock)
        self._closed = False
        self._reader_stopped: str | None = None
        self.session_id: str | None = None

        # Event state. Guarded by _lock because the reader thread writes it.
        self.console_errors: list[str] = []
        self.failed_requests: list[str] = []
        self._inflight = 0
        self._last_activity = time.monotonic()

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # --- plumbing ---

    def _read_loop(self) -> None:
        reason = "reader thread exited"
        while not self._closed:
            try:
                raw = self._ws.recv()
            except WebSocketTimeoutException:
                # A QUIET SOCKET IS NORMAL. This used to end the run.
                #
                # create_connection(timeout=...) calls settimeout(), which applies
                # to recv as well as to connect, so recv raises after
                # DEFAULT_TIMEOUT seconds of SILENCE -- not of failure. CDP is
                # silent for exactly as long as the model is thinking, and a
                # multi-turn Converse call on a large history, or a single
                # adaptive-retry backoff, routinely runs past 30s.
                #
                # The previous `except Exception: break` read that as a dead
                # socket and killed the reader for good. Nothing restarted it, so
                # every later command failed with "timeout waiting for
                # Page.navigate", and is_authenticated() fell back to None --
                # which also disarms the false-PASS guard. One idle gap took out
                # the run AND the check that would have caught it.
                #
                # Resuming is safe at the frame level: websocket-client appends
                # each partial read to frame_buffer.recv_buffer BEFORE the call
                # that raises, and recomputes the shortage on re-entry, so a
                # timeout mid-frame resumes rather than desynchronising the
                # stream.
                continue
            except Exception as e:  # noqa: BLE001 -- a closed socket ends the thread
                reason = f"{type(e).__name__}: {e}"[:200]
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            with self._cv:
                if "id" in msg:
                    self._pending[msg["id"]] = msg
                    self._cv.notify_all()
                else:
                    self._on_event(msg)

        # Wake anyone blocked in send(). With the reader gone no reply can ever
        # arrive, so waiting out the full timeout only delays a failure that has
        # already happened -- and reports it as the wrong one.
        with self._cv:
            self._reader_stopped = "session closed" if self._closed else reason
            self._cv.notify_all()

    def _on_event(self, msg: dict) -> None:
        """Called with _lock held."""
        method = msg.get("method", "")
        params = msg.get("params", {})
        self._last_activity = time.monotonic()

        if method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            text = detail.get("exception", {}).get("description") or detail.get("text", "")
            self.console_errors.append(f"uncaught: {text}"[:500])
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning"):
            args = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
            self.console_errors.append(f"{params['type']}: {args}"[:500])
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") in ("error", "warning"):
                self.console_errors.append(f"{entry['level']}: {entry.get('text','')}"[:500])
        elif method == "Network.requestWillBeSent":
            self._inflight += 1
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            self._inflight = max(0, self._inflight - 1)
            if method == "Network.loadingFailed":
                self.failed_requests.append(
                    f"{params.get('errorText','failed')}: {params.get('type','')}"[:500]
                )

    def send(self, method: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        with self._cv:
            self._id += 1
            msg_id = self._id
        payload = {"id": msg_id, "method": method, "params": params or {}}
        if self.session_id:
            payload["sessionId"] = self.session_id
        try:
            self._ws.send(json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            # A dead socket must surface as a CDPError, which browser_tools turns
            # into data for the model, not as a raw websocket exception escaping
            # into the tool loop.
            raise CDPError(f"{method}: send failed ({type(e).__name__}: {e})"[:200]) from e

        deadline = time.monotonic() + timeout
        with self._cv:
            while msg_id not in self._pending:
                if self._reader_stopped is not None:
                    # Distinguishable from a slow command, and it names the cause.
                    raise CDPError(f"{method}: CDP reader stopped -- {self._reader_stopped}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CDPError(f"timeout waiting for {method}")
                self._cv.wait(remaining)
            reply = self._pending.pop(msg_id)

        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result", {})

    # --- lifecycle ---

    def attach_to_page(self) -> None:
        """Find a page target and attach. Flat mode keeps everything on one socket."""
        targets = self.send("Target.getTargets")["targetInfos"]
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise CDPError("no page target available in the browser session")
        result = self.send("Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True})
        self.session_id = result["sessionId"]

        for domain in ("Page", "Runtime", "Log", "Network"):
            self.send(f"{domain}.enable")

    def close(self) -> None:
        self._closed = True
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 -- teardown must not mask a real error
            logger.warning("cdp socket close raised", exc_info=True)

    # --- helpers ---

    def evaluate(self, expression: str, timeout: float = DEFAULT_TIMEOUT):
        """Evaluate JS and return the value. await-capable."""
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise CDPError(detail.get("exception", {}).get("description") or detail.get("text", "JS error"))
        return result.get("result", {}).get("value")

    def wait_for_network_idle(self, quiet_ms: int = 500, timeout: float = 15.0) -> None:
        """
        Approximate Playwright's networkidle: no in-flight requests, and nothing
        new for quiet_ms. An SPA fires load long before its data has arrived, so
        waiting on the load event alone reads a half-rendered page and produces
        false "blank chart" findings.
        """
        deadline = time.monotonic() + timeout
        quiet = quiet_ms / 1000.0
        while time.monotonic() < deadline:
            with self._lock:
                idle = self._inflight == 0
                since = time.monotonic() - self._last_activity
            if idle and since >= quiet:
                return
            time.sleep(0.1)
        logger.info("network did not settle within %.1fs; continuing", timeout)

    def drain_events(self) -> dict:
        with self._lock:
            errors, failed = self.console_errors[:], self.failed_requests[:]
            self.console_errors.clear()
            self.failed_requests.clear()
        return {"console_errors": errors[:20], "failed_requests": failed[:20]}
