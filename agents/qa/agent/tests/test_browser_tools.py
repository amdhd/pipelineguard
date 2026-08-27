"""
Browser tool tests against a fake CDP session.

These do not need a browser. What they pin down is the layer between the model
and CDP: that tool arguments map to the right calls, that a browser-level failure
becomes DATA for the model rather than a crashed run, and -- most importantly --
that typing goes through React's native setter.

That last one is not a detail. Assigning el.value directly is swallowed by
React's synthetic event system: state never updates and the form submits empty.
The agent would appear to type the login credentials, fail to log in, and report
a broken login page. A tool bug would be reported as an application CRITICAL,
which is the worst possible failure for a QA agent.
"""

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_tools  # noqa: E402
from cdp import CDPError  # noqa: E402


class FakeCDP:
    """Records what the tools ask of CDP and returns scripted values."""

    def __init__(self, eval_results=None, raise_on_eval=None):
        self.sent: list[tuple[str, dict]] = []
        self.evaluated: list[str] = []
        self._eval_results = eval_results or {}
        self._raise = raise_on_eval
        self.idle_waits = 0
        self.events = {"console_errors": [], "failed_requests": []}

    def send(self, method, params=None, timeout=None):
        self.sent.append((method, params or {}))
        if method == "Page.captureScreenshot":
            # 1x1 transparent PNG
            return {
                "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
            }
        return {}

    def evaluate(self, expression, timeout=None):
        self.evaluated.append(expression)
        if self._raise:
            raise self._raise
        for needle, value in self._eval_results.items():
            if needle in expression:
                return value
        # Match on the exact read expressions, not a loose substring -- the click
        # script also mentions innerText, and a sloppy fake made this file fail
        # against correct code.
        if expression.startswith("document.body"):
            return "Fleet Overview"
        if expression.startswith("location.href"):
            return "https://example.test/voyage"
        return {"ok": True}

    def wait_for_network_idle(self, **kwargs):
        self.idle_waits += 1

    def drain_events(self):
        return dict(self.events)


def _session(fake):
    keys = []
    s = browser_tools.BrowserSession("https://example.test/", lambda label, png: (keys.append(label), f"screenshots/{label}.png")[1])
    s.cdp = fake
    return s, keys


def test_tool_specs_are_wellformed():
    specs = browser_tools.tool_specs()
    names = [t["toolSpec"]["name"] for t in specs]
    assert names == ["navigate", "read_page", "click", "type_text", "screenshot"]
    for spec in specs:
        ts = spec["toolSpec"]
        assert ts["description"].strip()
        assert ts["inputSchema"]["json"]["type"] == "object"


def test_navigate_builds_url_and_waits_for_idle():
    fake = FakeCDP()
    session, _ = _session(fake)
    result = session.navigate("/voyage")
    assert ("Page.navigate", {"url": "https://example.test/voyage"}) in fake.sent
    # An SPA fires load long before its data arrives; without the idle wait the
    # agent reads a half-rendered page and invents "blank chart" findings.
    assert fake.idle_waits == 1
    assert result["text"] == "Fleet Overview"


def test_navigate_rejects_absolute_urls():
    """The route allow-list is meaningless if the model can pass a full URL."""
    fake = FakeCDP()
    session, _ = _session(fake)
    result = session.navigate("https://elsewhere.test/admin")
    assert "error" in result
    assert fake.sent == []


def test_type_text_uses_the_react_native_setter():
    fake = FakeCDP()
    session, _ = _session(fake)
    session.type_text("#email", "demo@petronas.com")
    script = fake.evaluated[-1]
    # The native setter, not a direct assignment.
    assert "Object.getOwnPropertyDescriptor" in script
    assert ".set" in script
    assert "dispatchEvent" in script
    assert "new Event('input'" in script


def test_type_text_json_escapes_its_arguments():
    """A password with a quote in it must not break out of the JS expression."""
    fake = FakeCDP()
    session, _ = _session(fake)
    session.type_text("#pw", 'pa"ss\'word\\')
    script = fake.evaluated[-1]
    assert json.dumps('pa"ss\'word\\') in script


def test_click_supports_text_selector():
    fake = FakeCDP()
    session, _ = _session(fake)
    session.click("text=Sign in")
    # click() evaluates the click script and THEN reads page state, so the click
    # is the first evaluation, not the last.
    click_script = fake.evaluated[0]
    assert "startsWith('text=')" in click_script
    assert json.dumps("text=Sign in") in click_script


def test_click_reports_a_missing_element_as_data_not_a_crash():
    fake = FakeCDP(eval_results={"el.click()": {"ok": False, "error": "no element matching #nope"}, "document.body": "Login"})
    session, _ = _session(fake)
    result = session.click("#nope")
    assert "no element matching" in result["error"]
    # Still returns page state, so the model can see where it actually is.
    assert "url" in result


def test_screenshot_persists_and_does_not_return_the_image():
    fake = FakeCDP()
    session, labels = _session(fake)
    result = session.screenshot("blank-chart")
    assert result["saved"] is True
    assert labels == ["blank-chart"]
    assert result["bytes"] > 0
    # Echoing the image back would double the cost of the most expensive tool
    # for information the model already has.
    assert "data" not in result
    assert "image" not in result


def test_state_includes_console_errors_and_failed_requests():
    """
    The signal that catches vesselAI's actual bug class: most breakage there did
    not 404, it rendered NaN or crashed the React tree.
    """
    fake = FakeCDP()
    fake.events = {
        "console_errors": ["uncaught: TypeError: t.map is not a function"],
        "failed_requests": ["net::ERR_FAILED: XHR"],
    }
    session, _ = _session(fake)
    state = session.read_page()
    assert state["console_errors"]
    assert state["failed_requests"]


def test_long_text_is_truncated_with_a_flag():
    fake = FakeCDP(eval_results={"document.body": "x" * (browser_tools.MAX_TEXT_CHARS + 500)})
    session, _ = _session(fake)
    state = session.read_page()
    assert len(state["text"]) == browser_tools.MAX_TEXT_CHARS
    assert state["text_truncated"] is True


class TestDispatch:
    def test_routes_each_tool(self):
        fake = FakeCDP()
        session, _ = _session(fake)
        assert "url" in session.dispatch("navigate", {"path": "/"})
        assert "url" in session.dispatch("read_page", {})
        assert session.dispatch("screenshot", {"label": "x"})["saved"] is True

    def test_unknown_tool_is_an_error_not_an_exception(self):
        session, _ = _session(FakeCDP())
        assert "unknown tool" in session.dispatch("hack_the_mainframe", {})["error"]

    def test_missing_argument_is_reported_not_raised(self):
        session, _ = _session(FakeCDP())
        assert "missing argument" in session.dispatch("navigate", {})["error"]

    def test_browser_error_becomes_data(self):
        """A CDP failure may itself be the finding; it must not kill the run."""
        session, _ = _session(FakeCDP(raise_on_eval=CDPError("Target crashed")))
        result = session.dispatch("read_page", {})
        assert "browser error" in result["error"]
        assert "Target crashed" in result["error"]


def test_requirements_exclude_playwright():
    """
    Guards the reason cdp.py exists.

    Note what this does NOT assert: that dependencies are pure Python. They are
    not -- bedrock-agentcore requires pydantic (Rust core) and pulls in
    websockets (C speedups), and that is fine, because both publish honestly
    tagged manylinux wheels that scripts/package-qa-agent.sh installs for the
    target platform.

    Playwright fails precisely that test. Its wheel claims py3-none-any while
    bundling a node driver chosen at install time, so pip has nothing to select
    and --platform cannot correct it. If anyone re-adds it, fail here rather than
    at invoke time inside the runtime.
    """
    reqs = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    packages = [
        line.split(">=")[0].split("==")[0].strip().lower()
        for line in reqs.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "playwright" not in packages
    assert set(packages) == {"bedrock-agentcore", "websocket-client"}


class TestRouteBudget:
    """
    The explore-cap is listed as a COST CONTROL. Stated only in the prompt it was
    not one: a local run with max_routes=3 visited seven distinct routes -- the
    model read the limit, agreed with it, and kept going. Enforced here it cannot
    be declined.
    """

    def _capped(self, n):
        fake = FakeCDP()
        s = browser_tools.BrowserSession("https://x.test", lambda l, p: f"k/{l}", max_routes=n)
        s.cdp = fake
        return s, fake

    def test_navigation_is_refused_past_the_cap(self):
        s, fake = self._capped(2)
        assert "error" not in s.navigate("/voyage")
        assert "error" not in s.navigate("/ports")
        blocked = s.navigate("/sire")
        assert blocked.get("budget_exhausted") is True
        assert "route budget exhausted" in blocked["error"]
        # And it never actually navigated.
        assert ("Page.navigate", {"url": "https://x.test/sire"}) not in fake.sent

    def test_the_refusal_tells_the_model_what_to_do_next(self):
        """A refusal the model cannot act on just burns turns."""
        s, _ = self._capped(1)
        s.navigate("/voyage")
        assert "Emit your findings report now" in s.navigate("/ports")["error"]

    def test_revisiting_a_route_is_free(self):
        """Re-reading a page already counted must not consume budget."""
        s, _ = self._capped(2)
        s.navigate("/voyage")
        s.navigate("/voyage")
        assert "error" not in s.navigate("/ports")
        assert s.visited == ["/voyage", "/ports"]

    def test_login_does_not_consume_budget(self):
        """
        Reaching the app requires /login. Charging it would silently cost one of
        the routes the caller asked for.
        """
        s, _ = self._capped(1)
        s.navigate("/login")
        assert "error" not in s.navigate("/voyage")
        assert s.visited == ["/voyage"]

    def test_no_cap_means_no_enforcement(self):
        s, _ = self._capped(None)
        for r in ("/a", "/b", "/c", "/d", "/e"):
            assert "error" not in s.navigate(r)
