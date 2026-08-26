"""
The browser tools exposed to the model, and their Playwright implementations.

AgentCore's Browser tool is a REMOTE Chromium. `BrowserClient.generate_ws_headers()`
returns a CDP websocket endpoint plus SigV4 headers; Playwright connects to that
over CDP. Nothing runs a browser locally, which is why the deployment zip needs
the Playwright client library but none of its bundled browsers.

Tool surface is deliberately small. Every tool is more schema in every request
and more ways for a run to wander, and PLAN.md 1d counts both tokens and
wall-clock as cost.
"""

import base64
import logging

logger = logging.getLogger(__name__)

# Cap on returned text. A full page of innerText can be tens of thousands of
# tokens, and the model needs enough to judge the page, not all of it.
MAX_TEXT_CHARS = 6000


def tool_specs() -> list[dict]:
    """Bedrock Converse toolConfig entries."""
    return [
        {
            "toolSpec": {
                "name": "navigate",
                "description": (
                    "Navigate to a path on the target application, e.g. '/voyage'. "
                    "Waits for the network to settle. Returns the final URL, HTTP "
                    "status, and the page's visible text (truncated)."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path beginning with '/', not a full URL.",
                            }
                        },
                        "required": ["path"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "read_page",
                "description": (
                    "Read the current page without navigating: visible text, any "
                    "console errors since the last read, and any failed network "
                    "requests. Use this to check whether something rendered."
                ),
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            }
        },
        {
            "toolSpec": {
                "name": "click",
                "description": (
                    "Click the first element matching a CSS selector or visible "
                    "text. Returns the page state afterwards."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": "CSS selector, or text=... for visible text.",
                            }
                        },
                        "required": ["selector"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "type_text",
                "description": "Type text into the field matching a CSS selector.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["selector", "text"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "screenshot",
                "description": (
                    "Capture the current viewport as evidence for a finding. "
                    "ONLY call this when you have found something to report -- it "
                    "is the most expensive tool available."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Short slug identifying the finding.",
                            }
                        },
                        "required": ["label"],
                    }
                },
            }
        },
    ]


class BrowserSession:
    """
    Playwright over AgentCore's remote CDP endpoint, plus the passive collectors
    (console errors, failed requests) that make 'the page silently broke' visible.

    Those collectors matter more than they look: vesselAI's own contract audit
    found that most of its breakage did NOT 404 -- it rendered NaN, blank charts,
    or crashed the React tree. A QA agent looking only at rendered text would
    miss exactly the class of bug this project exists to catch.
    """

    def __init__(self, base_url: str, screenshot_sink):
        self.base_url = base_url.rstrip("/")
        self._sink = screenshot_sink
        self._console_errors: list[str] = []
        self._failed_requests: list[str] = []
        self._page = None
        self._browser = None
        self._pw = None
        self.screenshots: list[dict] = []

    def attach(self, ws_url: str, headers: dict) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(ws_url, headers=headers)
        context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self._page = context.pages[0] if context.pages else context.new_page()

        self._page.on("console", self._on_console)
        self._page.on("requestfailed", self._on_request_failed)
        # An uncaught exception is the "crashed the React tree" case; it does not
        # always surface as a console error.
        self._page.on("pageerror", lambda e: self._console_errors.append(f"pageerror: {e}"))

    def close(self) -> None:
        for closer in (self._browser, self._pw):
            try:
                if closer is not None:
                    closer.close() if closer is self._browser else closer.stop()
            except Exception:  # noqa: BLE001 -- teardown must never mask a real error
                logger.warning("browser teardown raised", exc_info=True)

    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            self._console_errors.append(f"{msg.type}: {msg.text}"[:500])

    def _on_request_failed(self, request) -> None:
        self._failed_requests.append(f"{request.method} {request.url}"[:500])

    def _drain(self) -> dict:
        errors, failed = self._console_errors, self._failed_requests
        self._console_errors, self._failed_requests = [], []
        return {"console_errors": errors[:20], "failed_requests": failed[:20]}

    def _state(self, status: int | None = None) -> dict:
        text = self._page.inner_text("body")
        truncated = len(text) > MAX_TEXT_CHARS
        state = {
            "url": self._page.url,
            "text": text[:MAX_TEXT_CHARS],
            "text_truncated": truncated,
            **self._drain(),
        }
        if status is not None:
            state["status"] = status
        return state

    # --- tool implementations ---

    def navigate(self, path: str) -> dict:
        if not path.startswith("/"):
            return {"error": f"path must start with '/', got {path!r}"}
        response = self._page.goto(f"{self.base_url}{path}", wait_until="networkidle")
        return self._state(status=response.status if response else None)

    def read_page(self) -> dict:
        return self._state()

    def click(self, selector: str) -> dict:
        try:
            self._page.click(selector, timeout=10_000)
        except Exception as e:  # noqa: BLE001 -- a failed click is data, not a crash
            return {"error": str(e)[:300], **self._state()}
        self._page.wait_for_load_state("networkidle", timeout=10_000)
        return self._state()

    def type_text(self, selector: str, text: str) -> dict:
        try:
            self._page.fill(selector, text, timeout=10_000)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:300], **self._state()}
        return {"ok": True, "url": self._page.url}

    def screenshot(self, label: str) -> dict:
        png = self._page.screenshot(full_page=False)
        key = self._sink(label, png)
        record = {"key": key, "label": label, "url": self._page.url}
        self.screenshots.append(record)
        # The image is NOT returned to the model. It already knows what it just
        # looked at; sending the bytes back would double the cost of the most
        # expensive tool for no added information.
        return {"saved": True, "key": key, "bytes": len(png)}

    def dispatch(self, name: str, args: dict) -> dict:
        handler = {
            "navigate": lambda: self.navigate(args["path"]),
            "read_page": self.read_page,
            "click": lambda: self.click(args["selector"]),
            "type_text": lambda: self.type_text(args["selector"], args["text"]),
            "screenshot": lambda: self.screenshot(args["label"]),
        }.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        return handler()


def encode_png(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")
