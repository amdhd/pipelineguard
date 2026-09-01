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
    # The same events must have fired deterministic candidates -- this is the
    # whole point of the layer: the model is required to assess them.
    cands = state["candidate_findings"]
    assert {c["type"] for c in cands} == {"console_error", "failed_request"}


def test_long_text_is_truncated_with_a_flag():
    fake = FakeCDP(eval_results={"document.body": "x" * (browser_tools.MAX_TEXT_CHARS + 500)})
    session, _ = _session(fake)
    state = session.read_page()
    assert len(state["text"]) == browser_tools.MAX_TEXT_CHARS
    assert state["text_truncated"] is True


def test_harvest_forwards_repeated_slots_and_kind():
    """
    The JS computes the repeated_slots rollup; the Python wrapper must forward
    it. Dropping it is exactly the bug that cost a corpus run: the model saw
    twelve svg-adjacent blanks (kind passed through) but never the aggregate
    count that makes repetition evidence, so each still read as a lone hint.
    """
    fake = FakeCDP(eval_results={
        "document.querySelectorAll": {
            "values": [{"label": "Speed", "value": "12", "kind": "input"}],
            "empty_slots": [
                {"context": "Main Engine MAN Energy Solutions", "count": 1, "kind": "svg-adjacent"},
                {"context": "Turbocharger #1 MAN Energy Solutions", "count": 1, "kind": "svg-adjacent"},
            ],
            "repeated_slots": [
                {"kind": "svg-adjacent", "count": 2, "sample": ["Main Engine…", "Turbocharger…"]}
            ],
        }
    })
    session, _ = _session(fake)
    got = session._harvest()
    assert got["repeated_slots"] == [
        {"kind": "svg-adjacent", "count": 2, "sample": ["Main Engine…", "Turbocharger…"]}
    ]
    assert got["empty_slots"][0]["kind"] == "svg-adjacent"
    assert got["values"][0]["value"] == "12"
    # And the model-facing read carries it, not just the internal helper.
    assert session.read_page()["repeated_slots"][0]["count"] == 2


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

    def test_a_failed_navigation_does_not_charge_the_route(self):
        """
        A navigation that produced nothing must not consume a budget slot.
        Charging it would silently cost one of the routes the caller asked for,
        and the model could be refused a real route because a broken one failed
        first.
        """
        class _Broken(FakeCDP):
            def send(self, method, params=None, timeout=None):
                if method == "Page.navigate" and params.get("url", "").endswith("/voyage"):
                    raise CDPError("navigation failed: net::ERR_CONNECTION_REFUSED")
                return super().send(method, params, timeout)

        s = browser_tools.BrowserSession("https://x.test", lambda l, p: f"k/{l}", max_routes=1)
        s.cdp = _Broken()
        failed = s.navigate("/voyage")
        assert "error" in failed
        assert s.visited == []
        # The slot is still available, so a real route can still be visited.
        assert "error" not in s.navigate("/ports")
        assert s.visited == ["/ports"]

    def test_a_failed_navigation_reports_the_browser_error(self):
        """The model needs the CDP reason, not an empty failure."""
        class _Broken(FakeCDP):
            def send(self, method, params=None, timeout=None):
                if method == "Page.navigate" and params.get("url", "").endswith("/voyage"):
                    raise CDPError("net::ERR_CONNECTION_REFUSED")
                return super().send(method, params, timeout)

        s = browser_tools.BrowserSession("https://x.test", lambda l, p: f"k/{l}", max_routes=3)
        s.cdp = _Broken()
        result = s.navigate("/voyage")
        assert "ERR_CONNECTION_REFUSED" in result["error"]


class TestAuthProbe:
    """
    A false PASS is the worst failure a QA agent has. An agent stuck on the login
    page and an agent exploring a healthy app both produce "no findings", so the
    answer must come from something OBSERVABLE rather than the model's account of
    itself. The frontend writes its JWT to localStorage on login.
    """

    def test_token_present_means_authenticated(self):
        fake = FakeCDP(eval_results={"localStorage": True})
        s, _ = _session(fake)
        assert s.is_authenticated("vm_token") is True
        assert "vm_token" in fake.evaluated[-1]

    def test_token_absent_means_not_authenticated(self):
        fake = FakeCDP(eval_results={"localStorage": False})
        s, _ = _session(fake)
        assert s.is_authenticated("vm_token") is False

    def test_no_key_configured_declines_to_answer(self):
        """
        None, not False. A different target may authenticate differently, and
        guessing would turn "we cannot tell" into "it failed".
        """
        s, _ = _session(FakeCDP())
        assert s.is_authenticated("") is None

    def test_a_failed_probe_declines_rather_than_accusing(self):
        s, _ = _session(FakeCDP(raise_on_eval=CDPError("Target crashed")))
        assert s.is_authenticated("vm_token") is None


def test_auth_probe_never_crashes_the_run():
    """
    Regression: the probe caught only CDPError, so a closed websocket raised
    straight through and failed a run that had otherwise completed. A check that
    can kill the run it is checking is worse than no check.
    """
    class DeadSocket:
        def evaluate(self, *a, **k):
            raise RuntimeError("socket is already closed.")

    s = browser_tools.BrowserSession("https://x.test", lambda l, p: "k")
    s.cdp = DeadSocket()
    assert s.is_authenticated("vm_token") is None


class TestStructuralRead:
    """
    innerText is lossy in one specific way, and it cost an error in BOTH
    directions: a dropped field renders as an empty element and never reaches
    the agent (false negative), while a populated <input> also never reaches it,
    because a value is a DOM property rather than text (false positive -- the
    agent reported a working form as empty).
    """

    def test_state_carries_values_and_empty_slots(self):
        fake = FakeCDP(eval_results={
            "values": {"values": [{"label": "Fuel Price ($/MT)", "value": "650", "kind": "input"}],
                       "empty_slots": [{"context": "Main Engine Wartsila", "count": 8}]},
        })
        session, _ = _session(fake)
        state = session.read_page()
        assert state["values"][0]["value"] == "650"
        assert state["empty_slots"] == [{"context": "Main Engine Wartsila", "count": 8}]

    def test_a_populated_input_is_visible_even_though_text_is_not(self):
        """The false positive this exists to stop."""
        fake = FakeCDP(eval_results={
            "values": {"values": [{"label": "Voyage Distance (nm)", "value": "1500", "kind": "input"}],
                       "empty_slots": []},
        })
        session, _ = _session(fake)
        values = session.read_page()["values"]
        assert any(v["value"] == "1500" for v in values)

    def test_a_failed_harvest_does_not_lose_the_page(self):
        """
        A page that breaks the harvester is exactly the kind worth reporting on,
        so losing the text with it would be the wrong trade.
        """
        class Broken(FakeCDP):
            def evaluate(self, expression, timeout=None):
                if "empty_slots" in expression or "labelFor" in expression:
                    raise CDPError("harvest blew up")
                return super().evaluate(expression, timeout)

        session, _ = _session(Broken())
        state = session.read_page()
        assert state["text"] == "Fleet Overview"
        assert "values" not in state or state.get("values") == []

    def test_context_is_found_by_climbing_not_by_reading_the_parent(self):
        """
        The regression that made the first version useless: the slot it exists
        to catch sits inside a container whose own innerText is empty, so a
        parent-only lookup dropped it.
        """
        assert "hops < 4" in browser_tools._HARVEST
        assert "node = node.parentElement" in browser_tools._HARVEST

    def test_repeated_slots_are_counted_not_repeated(self):
        """"Every card" is a stronger signal than one card, and cheaper."""
        assert "e.context === key" in browser_tools._HARVEST

    def test_decorative_dots_are_not_empty_slots(self):
        """
        The CII phantom's mechanism: a 5x5 badge dot renders small but POSITIVE,
        so it passed the empty-leaf filter and aggregated into a text-kind
        repeated_slots group that the rubric then called evidence -- and the
        model reported a column as blank that was not. A genuinely missing
        figure has no laid-out box at all (an empty span is 0x0), so requiring
        a small positive size in BOTH dimensions drops the dot and keeps the
        slot.
        """
        assert "el.offsetWidth > 0" in browser_tools._HARVEST
        assert "el.offsetHeight > 0" in browser_tools._HARVEST
        assert "<= 8" in browser_tools._HARVEST

    def test_the_harvest_is_capped(self):
        """It rides on every read; page content is the dominant input cost."""
        assert browser_tools.MAX_VALUES <= 50
        assert browser_tools.MAX_EMPTY_SLOTS <= 20
        assert f"{browser_tools.MAX_VALUES}" in browser_tools._HARVEST

    def test_passwords_are_never_harvested(self):
        assert "el.type === 'password'" in browser_tools._HARVEST


class TestCandidateFindings:
    """
    The deterministic-candidate layer. The discriminator run proved the model
    rung is not the S-2 bottleneck -- sonnet saw the pristine repeated_svg_empty
    signal and still reported nothing -- so the runtime now emits the mechanical
    signals itself and REQUIRES the model to assess each one. These pin the
    session-side bookkeeping: ids, (type, url) dedup, and the auto-captured
    screenshot that becomes the confirmed finding's evidence.
    """

    _SVG_HARVEST = {
        "repeated_slots": [
            {"kind": "svg-adjacent", "count": 13,
             "sample": ["Main Engine…", "Turbocharger…", "Shaft Generator…"]}
        ]
    }

    def test_repeated_svg_empty_fires_a_candidate(self):
        fake = FakeCDP(eval_results={"document.querySelectorAll": self._SVG_HARVEST})
        session, _ = _session(fake)
        cands = session.read_page()["candidate_findings"]
        assert len(cands) == 1
        assert cands[0]["type"] == "repeated_svg_empty"
        assert cands[0]["count"] == 13
        assert cands[0]["id"] == "cand-1"

    def test_candidate_findings_are_deduplicated_per_url(self):
        """
        Re-reading a buggy page must not spawn cand-2 of the same observation,
        or the mandatory-assessment contract becomes untractable and the bounded
        retry can never converge.
        """
        fake = FakeCDP(eval_results={"document.querySelectorAll": self._SVG_HARVEST})
        session, _ = _session(fake)
        assert session.read_page()["candidate_findings"][0]["id"] == "cand-1"
        assert session.read_page()["candidate_findings"] == []

    def test_candidate_screenshot_is_captured_once(self):
        """Evidence for the confirmed finding, deterministically attached later."""
        fake = FakeCDP(eval_results={"document.querySelectorAll": self._SVG_HARVEST})
        session, labels = _session(fake)
        session.read_page()
        assert labels == ["candidate-cand-1"]
        assert session.candidate_screenshots["cand-1"] == "screenshots/candidate-cand-1.png"
        session.read_page()
        assert labels == ["candidate-cand-1"]  # not captured again

    def test_clean_page_has_an_empty_candidate_list(self):
        """
        The field is ALWAYS present -- the rubric requires the model to assess
        every entry, so an absent key would be a broken contract on the page
        where nothing fired.
        """
        session, _ = _session(FakeCDP())
        assert session.read_page()["candidate_findings"] == []

    def test_warning_console_errors_do_not_fire_a_candidate(self):
        """Warnings are decoration noise; never ask the model to explain them."""
        fake = FakeCDP()
        fake.events = {"console_errors": ["warning: manifest icon failed"], "failed_requests": []}
        session, _ = _session(fake)
        assert session.read_page()["candidate_findings"] == []

    def test_candidate_screenshot_failure_does_not_kill_the_read(self):
        """A CDP failure mid-_state is data for the read, not a crash for it."""
        class ScreenshotBroken(FakeCDP):
            def send(self, method, params=None, timeout=None):
                if method == "Page.captureScreenshot":
                    raise CDPError("target crashed")
                return super().send(method, params, timeout)

        fake = ScreenshotBroken(eval_results={"document.querySelectorAll": self._SVG_HARVEST})
        session, _ = _session(fake)
        state = session.read_page()
        assert state["candidate_findings"][0]["type"] == "repeated_svg_empty"
        assert session.candidate_screenshots == {}


class TestLogPageStateGate:
    """
    The full-page-state diagnostic must be OFF unless explicitly enabled.

    The Terraform default omits LOG_PAGE_STATE entirely, which this helper
    reads as off. The value-awareness exists because Python truthiness would
    treat a stray LOG_PAGE_STATE=0 as ON, silently emitting page state on
    every read -- the exact metered tax the gate exists to avoid.
    """

    def test_absent_means_off(self, monkeypatch):
        monkeypatch.delenv("LOG_PAGE_STATE", raising=False)
        assert browser_tools._log_page_state_enabled() is False

    def test_zero_and_false_are_off_not_on(self, monkeypatch):
        for value in ("0", "false", "False", "no", ""):
            monkeypatch.setenv("LOG_PAGE_STATE", value)
            assert browser_tools._log_page_state_enabled() is False, value

    def test_one_true_and_yes_are_on(self, monkeypatch):
        for value in ("1", "true", "True", "yes", " 1 "):
            monkeypatch.setenv("LOG_PAGE_STATE", value)
            assert browser_tools._log_page_state_enabled() is True, value
