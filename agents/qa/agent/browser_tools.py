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
import os

import candidates
from cdp import CDPError, CDPSession

logger = logging.getLogger(__name__)

# Cap on returned text. A full page of innerText can be tens of thousands of
# tokens, and the model needs enough to judge the page, not all of it.
MAX_TEXT_CHARS = 6000

# WHY A STRUCTURAL READ EXISTS, AND WHAT IT COST NOT TO HAVE ONE
#
# The agent's whole model of a page was `document.body.innerText`, and that
# representation is lossy in one specific, expensive way: IT DOES NOT CARRY
# VALUES THAT ARE NOT TEXT NODES.
#
# Both of the agent's measured error modes trace to exactly that:
#
#   * A FALSE NEGATIVE. A field dropped from an API response renders as
#     `<span></span>`, which contributes nothing to innerText. There is no gap to
#     notice -- the number is simply absent from the agent's input -- so telling
#     the rubric to "read the values" asked it to read something it cannot see.
#   * A FALSE POSITIVE, from the same hole. `<input value={650}>` also
#     contributes nothing to innerText, because an input's value is a DOM
#     PROPERTY, not text content. The agent saw two labels with no adjacent text
#     and reported "empty input fields" on a form that was working correctly.
#
# One blind spot, both directions. So this harvests what innerText drops:
# form-control values, labelled values, and empty leaf elements that sit inside
# a container that does have text -- the shape a missing figure actually takes.
#
# Output is capped hard. This rides on every navigate and read_page, and the
# whole point of MAX_TEXT_CHARS is that page content is the dominant input cost.
MAX_VALUES = 50
MAX_EMPTY_SLOTS = 20

_HARVEST = r"""
(function () {
  const MAX = %d, MAX_EMPTY = %d;
  const values = [], empty = [];

  function labelFor(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l && l.innerText.trim()) return l.innerText.trim();
      } catch (e) {}
    }
    const wrap = el.closest('label');
    if (wrap && wrap.innerText.trim()) return wrap.innerText.trim();
    let prev = el.previousElementSibling;
    while (prev) {
      const t = (prev.innerText || '').trim();
      if (t && t.length < 60) return t;
      prev = prev.previousElementSibling;
    }
    if (el.placeholder) return '(placeholder) ' + el.placeholder;
    return '(unlabelled)';
  }

  // 1. Form controls. Their values are invisible to innerText -- this is the
  //    half that stops a populated form being reported as empty.
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (values.length >= MAX) break;
    if (el.type === 'hidden' || el.type === 'password') continue;
    values.push({
      label: labelFor(el).slice(0, 60),
      value: String(el.value == null ? '' : el.value).slice(0, 60),
      kind: el.tagName.toLowerCase()
    });
  }

  // 2. Anything carrying an explicit accessible name, value included when the
  //    element renders no text at all.
  for (const el of document.querySelectorAll('[aria-label]')) {
    if (values.length >= MAX) break;
    if (el.matches('input, select, textarea')) continue;
    values.push({
      label: el.getAttribute('aria-label').slice(0, 60),
      value: (el.innerText || '').trim().slice(0, 60),
      kind: 'labelled'
    });
  }

  // 3. Empty leaves inside a container that DOES have text. This is the shape a
  //    missing figure takes: a slot with nothing in it, sitting beside the name
  //    it belongs to. Decorative empties are excluded by requiring surrounding
  //    text, and svg/img/icon nodes are skipped outright.
  for (const el of document.querySelectorAll('span, div, td, dd, p, h1, h2, h3, h4, strong, b')) {
    if (empty.length >= MAX_EMPTY) break;
    if (el.children.length) continue;
    if ((el.textContent || '').trim()) continue;
    if (el.getAttribute('aria-hidden') === 'true') continue;
    if (el.closest('svg, img, button[disabled]')) continue;
    // A decorative marker -- a badge dot, a separator, a status indicator --
    // renders small but POSITIVE. A genuinely missing figure has NO laid-out
    // box at all (an empty span is 0x0), so requiring a small positive size in
    // both dimensions drops the dot while keeping the slot. Without this the
    // status-badge dots aggregated into a text-kind repeated_slots group on
    // every page, and the model was told that repetition is evidence -- which
    // manufactured a phantom "empty column" finding from pure decoration.
    if (el.offsetWidth > 0 && el.offsetWidth <= 8 && el.offsetHeight > 0 && el.offsetHeight <= 8) continue;
    // Climb for context rather than reading the immediate parent only. The
    // shape this exists to catch defeated the parent-only version: a score
    // rendered as <span/> inside a <div> holding an <svg> and nothing else, so
    // the PARENT's innerText is empty too and the slot was dropped -- by the
    // very filter meant to find it. Walk up until some ancestor has text.
    //
    // Along the way, note whether the blank sits beside an <svg> -- a ring,
    // chart or icon whose number/label slot renders nothing. That is a figure
    // that arrived missing, and it is also the key that lets "the same blank
    // in every item" aggregate even when each item's text differs (see
    // repeated_slots below): the context-based dedup splits on the differing
    // text, so without this kind the repetition stays invisible as count:1s.
    let node = el.parentElement, ctx = '', hops = 0, svgAdjacent = false;
    while (node && hops < 4) {
      if (!svgAdjacent && node.querySelector('svg')) svgAdjacent = true;
      const t = (node.innerText || '').trim().replace(/\s+/g, ' ');
      if (t) { ctx = t; break; }
      node = node.parentElement;
      hops++;
    }
    if (!ctx) continue;
    const key = ctx.slice(0, 80);
    // Deduplicated with a count: "every card" is a stronger signal than one
    // card, and twenty copies of it would otherwise eat the whole cap.
    const hit = empty.find(e => e.context === key);
    if (hit) { hit.count++; } else { empty.push({ context: key, count: 1, kind: svgAdjacent ? 'svg-adjacent' : 'text' }); }
  }

  // Group the blanks by kind so a blank repeated across list items is visible
  // even when each item's surrounding text differs. A kind with a count above
  // 1 is the systematic-repetition signal: the same figure slot missing on
  // every card, which is what a dropped list field looks like.
  const byKind = {};
  for (const e of empty) {
    const k = e.kind;
    byKind[k] = byKind[k] || { kind: k, count: 0, sample: [] };
    byKind[k].count += e.count;
    if (byKind[k].sample.length < 3) byKind[k].sample.push(e.context.slice(0, 80));
  }
  const repeated_slots = Object.values(byKind).filter(g => g.count >= 2);

  return { values: values, empty_slots: empty, repeated_slots: repeated_slots };
})()
""" % (MAX_VALUES, MAX_EMPTY_SLOTS)


def tool_specs() -> list[dict]:
    """Bedrock Converse toolConfig entries."""
    return [
        {
            "toolSpec": {
                "name": "navigate",
                "description": (
                    "Navigate to a path on the target application, e.g. '/voyage'. "
                    "Waits for the network to settle. Returns the final URL and the "
                    "page's visible text, plus any console errors, failed requests, "
                    "and `candidate_findings` the page mechanically triggered."
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
                    "errors since the last read, failed network requests, and any "
                    "`candidate_findings` the page mechanically triggered. Use "
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


def _log_page_state_enabled() -> bool:
    """
    Whether the full-page-state diagnostic is ON.

    Value-aware rather than presence-aware: Python's truthiness would treat
    `LOG_PAGE_STATE=0` as enabled, so a stray "0" in an env file would silently
    start emitting page state on every read. Only '1', 'true', or 'yes' enable
    it; the key being absent -- the Terraform default -- or any other value
    leaves it off.
    """
    return os.environ.get("LOG_PAGE_STATE", "").strip().lower() in ("1", "true", "yes")


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
        # Candidate-findings bookkeeping (candidates.py). The session owns the
        # id space and the (type, url) dedup set: re-reading a buggy page must
        # not spawn a new candidate for the same observation, or the rubric's
        # mandatory-assessment contract becomes untractable.
        self._candidate_counter = 0
        self._seen_candidate_slots: set[tuple[str, str]] = set()
        self.seen_candidates: dict[str, dict] = {}
        self.candidate_screenshots: dict[str, str] = {}

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
        except Exception:  # noqa: BLE001
            # Deliberately broad. This probe is a CHECK, and a check that can
            # crash the run it is checking is worse than no check. The first
            # version caught only CDPError and a closed websocket raised
            # straight through it, failing runs that had otherwise succeeded.
            logger.warning("auth probe failed; reporting unknown", exc_info=True)
            return None

    def _harvest(self) -> dict:
        """
        Structured values innerText cannot carry. Never fails the read: a page
        that breaks the harvester is exactly the kind of page worth reporting on,
        so losing the text as well would be the wrong trade.
        """
        try:
            got = self.cdp.evaluate(_HARVEST) or {}
        except Exception:  # noqa: BLE001
            logger.warning("value harvest failed; continuing with text only", exc_info=True)
            return {}
        return {
            "values": got.get("values", []),
            "empty_slots": got.get("empty_slots", []),
            "repeated_slots": got.get("repeated_slots", []),
        }

    def _state(self) -> dict:
        text = self.cdp.evaluate("document.body ? document.body.innerText : ''") or ""
        state = {
            "url": self.cdp.evaluate("location.href"),
            "text": text[:MAX_TEXT_CHARS],
            "text_truncated": len(text) > MAX_TEXT_CHARS,
            **self._harvest(),
            **self.cdp.drain_events(),
        }
        # Deterministic candidates ride on every read. Always present (empty
        # list on a clean page) -- the rubric promises the field and requires
        # the model to assess every entry, so an absent key would be a broken
        # contract rather than a missing signal.
        state["candidate_findings"] = self._emit_candidates(state, candidates.detect(state))
        # DIAGNOSTIC LOOK. When LOG_PAGE_STATE is set, every read emits the full
        # page state -- including empty_slots, the structure innerText cannot
        # carry. This is how a run's blindness is diagnosed: a QA agent that
        # misses a blank score might be failing to SEE the blank, and that is
        # visible here without re-running. Gated so it costs nothing in normal
        # operation; 20k chars holds the entire state including the 6k text cap.
        if _log_page_state_enabled():
            logger.info("page state: %s", json.dumps(state, default=str)[:20000])
        return state

    def _emit_candidates(self, state: dict, detected: list[dict]) -> list[dict]:
        """
        Assign ids to newly-detected candidates and auto-capture a screenshot
        for each.

        Emitted once per (type, url): re-reading a buggy page must not spawn
        cand-2 of the same observation, or the rubric's mandatory-assessment
        contract becomes untractable and the bounded retry can never converge.
        The id is the stable handle the report uses to reference a candidate.

        The auto-captured bytes go to S3 only, never back to the model --
        matching screenshot()'s existing contract (it knows what it just looked
        at). A capture failure must not kill the read, so it is guarded here:
        screenshot() is not itself exception-guarded the way dispatch() is.
        """
        url = state.get("url", "")
        emitted: list[dict] = []
        for c in detected:
            slot = (c["type"], url)
            if slot in self._seen_candidate_slots:
                continue
            self._seen_candidate_slots.add(slot)
            self._candidate_counter += 1
            cid = f"cand-{self._candidate_counter}"
            self.seen_candidates[cid] = {
                "type": c["type"],
                "count": c["count"],
                "evidence": c["evidence"],
            }
            emitted.append({**c, "id": cid})
            try:
                shot = self.screenshot(f"candidate-{cid}")
                self.candidate_screenshots[cid] = shot.get("key")
            except Exception:  # noqa: BLE001
                logger.warning("candidate screenshot failed for %s", cid, exc_info=True)
        return emitted

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

        try:
            self.cdp.send("Page.navigate", {"url": f"{self.base_url}{path}"})
            self.cdp.wait_for_network_idle()
        except CDPError as e:
            # A navigation that produced nothing must not cost a route: charging
            # it would silently burn one of the slots the caller asked for, and
            # the model could be refused a real route because a broken one failed
            # first. wait_for_network_idle never raises, so the send is the only
            # realistic failure point -- but catching the pair costs nothing.
            if new_route:
                logger.warning(
                    "navigation to %s failed; route not charged", path, exc_info=True
                )
            return {"error": str(e)[:300], **self._state()}
        if new_route:
            self.visited.append(path)
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
