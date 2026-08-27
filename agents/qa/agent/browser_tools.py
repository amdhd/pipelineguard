"""
The browser tools exposed to the model.

AgentCore's Browser tool is a REMOTE Chromium. `BrowserClient.generate_ws_headers()`
returns a CDP websocket endpoint plus SigV4 headers; cdp.py speaks that protocol
directly. Nothing runs a browser locally and no native binary ships in the zip --
see the note at the top of cdp.py for why Playwright could not be used.

The tool surface is deliberately small. Every tool is more schema in every
request and more ways for a run to wander, and PLAN.md 1d counts both tokens and
wall-clock as cost.
"""

import json
import logging

from cdp import CDPError, CDPSession

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
                    "Waits for the network to settle. Returns the final URL and the "
                    "page's visible text, plus any console errors or failed requests."
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
                    "Read the current page without navigating: visible text, console "
                    "errors since the last read, and failed network requests. Use "
                    "this to check whether something actually rendered."
                ),
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            }
        },
        {
            "toolSpec": {
                "name": "click",
                "description": (
                    "Click the first element matching a CSS selector, or use "
                    "'text=Some Label' to match visible text. Returns page state after."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {"selector": {"type": "string"}},
                        "required": ["selector"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "type_text",
                "description": "Type text into the input matching a CSS selector.",
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
                    "Capture the viewport as evidence for a finding. ONLY call this "
                    "when you have found something to report -- it is the most "
                    "expensive tool available."
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


# React controls its inputs, so assigning `el.value` directly is swallowed --
# React's synthetic event system never sees it, state never updates, and the form
# submits empty. The value must go through the NATIVE setter with an input event
# dispatched after it. Without this the agent appears to type the login
# credentials successfully and then fails to log in, which reads as a broken
# login page rather than a broken tool.
_REACT_SET_VALUE = """
(function(sel, value) {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, error: 'no element matching ' + sel};
  const proto = el instanceof HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true};
})(%s, %s)
"""

_CLICK = """
(function(sel) {
  let el = null;
  if (sel.startsWith('text=')) {
    const wanted = sel.slice(5).trim().toLowerCase();
    const candidates = document.querySelectorAll('button, a, [role=button], input[type=submit]');
    for (const c of candidates) {
      if ((c.innerText || c.value || '').trim().toLowerCase().includes(wanted)) { el = c; break; }
    }
  } else {
    el = document.querySelector(sel);
  }
  if (!el) return {ok: false, error: 'no element matching ' + sel};
  el.click();
  return {ok: true};
})(%s)
"""


class BrowserSession:
    """
    Drives the remote browser and collects the passive signals.

    Those signals matter more than they look: vesselAI's own contract audit found
    that most of its breakage did NOT 404 -- it rendered NaN, blank charts, or
    crashed the React tree. An agent reading only visible text would miss exactly
    the class of bug this project exists to catch.
    """

    def __init__(self, base_url: str, screenshot_sink, max_routes: int | None = None):
        self.base_url = base_url.rstrip("/")
        self._sink = screenshot_sink
        self.cdp: CDPSession | None = None
        self.screenshots: list[dict] = []
        # The explore-cap is a COST CONTROL, so it is enforced here rather than
        # only stated in the prompt. A local run with max_routes=3 visited seven
        # distinct routes: the model read "visit at most 3", agreed, and kept
        # going. A budget the model can decline is not a budget.
        self.max_routes = max_routes
        self.visited: list[str] = []

    def attach(self, ws_url: str, headers: dict) -> None:
        self.cdp = CDPSession(ws_url, headers)
        self.cdp.attach_to_page()

    def close(self) -> None:
        if self.cdp is not None:
            self.cdp.close()

    def is_authenticated(self, token_key: str) -> bool | None:
        """
        Did the app actually authenticate? MEASURED, not asked.

        A QA agent that never got past the login page and a QA agent that
        explored a healthy app both produce "no findings". Those are opposite
        outcomes and a false PASS is the worst failure this system can have, so
        the answer has to come from something observable rather than from the
        model's account of itself.

        The frontend writes its JWT to localStorage on a successful login
        (AuthContext.tsx). Presence of that key is the signal. Returns None when
        no key is configured -- a different target may authenticate differently,
        and guessing would be worse than declining to answer.
        """
        if not token_key:
            return None
        try:
            return bool(self.cdp.evaluate(f"!!window.localStorage.getItem({json.dumps(token_key)})"))
        except CDPError:
            logger.warning("auth probe failed", exc_info=True)
            return None

    def _state(self) -> dict:
        text = self.cdp.evaluate("document.body ? document.body.innerText : ''") or ""
        return {
            "url": self.cdp.evaluate("location.href"),
            "text": text[:MAX_TEXT_CHARS],
            "text_truncated": len(text) > MAX_TEXT_CHARS,
            **self.cdp.drain_events(),
        }

    # --- tool implementations ---

    def navigate(self, path: str) -> dict:
        if not path.startswith("/"):
            return {"error": f"path must start with '/', got {path!r}"}

        # /login and /register do not count -- reaching the app requires them,
        # and charging them to the budget would silently cost two of the routes
        # the caller asked for.
        counts = path not in ("/login", "/register")
        new_route = counts and path not in self.visited

        if new_route and self.max_routes is not None and len(self.visited) >= self.max_routes:
            return {
                "error": (
                    f"route budget exhausted: {len(self.visited)} of "
                    f"{self.max_routes} routes already visited "
                    f"({', '.join(self.visited)}). Do not navigate anywhere new. "
                    "Emit your findings report now."
                ),
                "routes_visited": list(self.visited),
                "budget_exhausted": True,
            }

        if new_route:
            self.visited.append(path)

        self.cdp.send("Page.navigate", {"url": f"{self.base_url}{path}"})
        self.cdp.wait_for_network_idle()
        return self._state()

    def read_page(self) -> dict:
        return self._state()

    def click(self, selector: str) -> dict:
        try:
            result = self.cdp.evaluate(_CLICK % json.dumps(selector))
        except CDPError as e:
            return {"error": str(e)[:300], **self._state()}
        if not result.get("ok"):
            return {"error": result.get("error", "click failed"), **self._state()}
        self.cdp.wait_for_network_idle()
        return self._state()

    def type_text(self, selector: str, text: str) -> dict:
        try:
            result = self.cdp.evaluate(_REACT_SET_VALUE % (json.dumps(selector), json.dumps(text)))
        except CDPError as e:
            return {"error": str(e)[:300]}
        if not result.get("ok"):
            return {"error": result.get("error", "type failed")}
        return {"ok": True}

    def screenshot(self, label: str) -> dict:
        import base64

        result = self.cdp.send("Page.captureScreenshot", {"format": "png"})
        png = base64.b64decode(result["data"])
        key = self._sink(label, png)
        self.screenshots.append({"key": key, "label": label})
        # The image is NOT returned to the model. It already knows what it just
        # looked at; sending the bytes back would double the cost of the most
        # expensive tool for no added information.
        return {"saved": True, "key": key, "bytes": len(png)}

    def dispatch(self, name: str, args: dict) -> dict:
        handlers = {
            "navigate": lambda: self.navigate(args["path"]),
            "read_page": self.read_page,
            "click": lambda: self.click(args["selector"]),
            "type_text": lambda: self.type_text(args["selector"], args["text"]),
            "screenshot": lambda: self.screenshot(args["label"]),
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return handler()
        except KeyError as e:
            return {"error": f"tool {name} missing argument {e}"}
        except CDPError as e:
            # A browser-level failure is data for the model, not a crash for the
            # run -- it may itself be the finding.
            return {"error": f"browser error: {e}"[:300]}
