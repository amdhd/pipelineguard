"""
Rubric tests.

The rubric is prose, so these do not test that it is *good* -- only that the
facts it depends on are present and correct. Every assertion here corresponds to
a known false-positive generator documented in DISCOVERY.md section 8. If one of
these regresses, the report fills with noise and the human stops reading it,
which is the failure mode that kills the whole project.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rubric  # noqa: E402


def test_routes_match_the_app_router():
    """
    Straight from frontend/src/App.tsx. If vesselAI adds a module and this is not
    updated, the agent silently never tests it -- a false negative, which no
    amount of reading the report reveals.
    """
    assert rubric.PUBLIC_ROUTES == ("/login", "/register")
    assert rubric.PRIVATE_ROUTES == (
        "/",
        "/voyage",
        "/maintenance",
        "/compliance",
        "/ports",
        "/knowledge",
        "/sire",
        "/analytics",
    )


def test_prompt_lists_every_private_route():
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    for route in rubric.PRIVATE_ROUTES:
        assert route in prompt


def test_prompt_states_that_404_is_not_observable():
    """
    App.tsx routes "*" to <Navigate to="/">, so a bad URL lands on the dashboard.
    Without this, "no 404 page" is a guaranteed finding on every single run.
    """
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "404 IS NOT OBSERVABLE" in prompt
    assert "redirect" in prompt.lower()


def test_prompt_names_every_fixture_surface():
    """Seeded anomalies in fixture data are the demo working, not defects."""
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    for surface in rubric.FIXTURE_SURFACES:
        module = surface.split(" (")[0]
        assert module in prompt
    assert "seeded anomalies" in prompt.lower()


def test_fallback_mode_tells_the_agent_ai_answers_are_expected():
    """
    Default for pull_request and schedule runs is a dummy AI key, so every AI
    surface returns its canned fallback. Getting this wrong turns all six AI
    modules into false positives at once.
    """
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "FALLBACK MODE" in prompt
    assert "NOT a finding" in prompt


def test_live_mode_inverts_the_fallback_signal():
    """With a real key, an X-AI-Fallback flag means the live call failed."""
    prompt = rubric.build_system_prompt(ai_fallback_mode=False, max_routes=8)
    assert "AI SURFACES ARE LIVE" in prompt
    assert "FAILED" in prompt


def test_in_memory_db_fallback_is_an_inverted_signal():
    """
    Most degradation is by design here. This one is not: under compose the
    database is healthy, so the in-memory fallback should never fire, and seeing
    it is genuinely CRITICAL.
    """
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "in-memory fallback" in prompt
    assert "CRITICAL" in prompt


def test_prompt_forbids_reporting_the_unobserved():
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "Report ONLY what you OBSERVE" in prompt
    assert "Missing features" in prompt


def test_prompt_asks_for_a_fenced_block_last_and_warns_about_narrating():
    """
    Changed from "bare JSON, no fence". A real run showed the model narrating
    for a paragraph and THEN emitting a correct fenced object -- so the parser
    now reads the last fence, and the prompt asks for the shape that actually
    occurs rather than one models keep failing to produce.
    """
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "```json" in prompt
    assert "LAST thing" in prompt
    assert "discarded, never read" in prompt
    assert "FAILED" in prompt


def test_prompt_limits_screenshots_to_findings():
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "ONLY when you have a finding" in prompt


def test_route_cap_is_stated():
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=3)
    assert "at most 3 routes" in prompt
    assert "Do not crawl" in prompt


def test_prompt_prefers_null_over_a_guessed_source():
    """A wrong guess sends the Phase 2 fix agent to the wrong file."""
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "Prefer null" in prompt


def test_empty_findings_is_described_as_a_valid_result():
    prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
    assert "valid, good result" in prompt


class TestReadTheValues:
    """
    The rubric change the first real measurement earned.

    Against a three-bug seeded corpus the agent caught the LOUD failure -- a
    thrown TypeError behind an error boundary -- and missed the QUIET one, a
    number silently absent from every card. That is backwards for this project:
    the target's own audit found most of its breakage does not 404, it renders
    NaN or a blank where a figure belongs.
    """

    def test_the_prompt_asks_the_agent_to_read_rendered_values(self):
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "READ THE VALUES" in prompt
        assert "A page that renders without throwing is not necessarily working" in prompt

    def test_a_label_with_no_value_is_named_as_reportable(self):
        """The exact shape of the miss: a slot for a figure, and nothing in it."""
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "LABEL WITH NO VALUE" in prompt
        assert "A missing number is as much a" in prompt  # wraps across lines

    def test_broken_value_literals_are_enumerated(self):
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        for literal in ("NaN", "undefined", "[object Object]", "Invalid Date"):
            assert literal in prompt, literal

    def test_every_route_is_checked_not_only_broken_looking_ones(self):
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "EVERY route you visit" in prompt

    def test_looking_harder_is_fenced_against_speculation(self):
        """
        The risk of this change is a false-positive spike. The guard has to be
        in the prompt, not just in the reviewer's hopes.
        """
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "do NOT turn this into guesswork" in prompt
        assert "merely SURPRISING" in prompt
        assert "designed EMPTY STATE" in prompt

    def test_the_absent_from_a_slot_distinction_is_stated(self):
        """
        The line that keeps 'read the values' from colliding with 'missing
        features are product decisions'.
        """
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "ABSENT FROM A PLACE THAT HAS ONE" in prompt

    def test_repetition_across_a_list_resolves_the_hint_guard(self):
        """
        S-2's shape: a field dropped from a list response leaves the SAME blank
        in every card. The agent was told an empty slot is "a HINT, not a finding
        on its own -- confirm what the slot is for", and it cannot confirm what a
        blank is FOR without the source, so it declined -- the guard was
        suppressing the report it was meant to enable. Repetition across every
        item is the evidence that resolves the ambiguity.
        """
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "A lone blank is a HINT" in prompt
        assert "repeats across EVERY item in a list" in prompt
        assert "`count` above 1" in prompt
        assert "evidence in itself" in prompt

    def test_repeated_slots_is_named_as_the_visible_aggregation(self):
        """
        The relaxation said a `count` above 1 is evidence -- but the harvest
        dedups by context, and each card's header differs, so the S-2 blanks
        arrived as twelve count:1s and read as lone hints. `repeated_slots` is
        the fix: the group count is the repetition, even when the per-item
        entries each show count 1.
        """
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "repeated_slots" in prompt
        assert "grouped by" in prompt
        assert "each show `count: 1`" in prompt

    def test_svg_adjacent_is_named_as_the_loudest_blank_shape(self):
        """
        A blank beside an icon/chart/ring is a figure that arrived missing --
        the strongest form of the shape, and the discriminator that keeps
        decorative dots (plain spans, no svg) from aggregating with it.
        """
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "svg-adjacent" in prompt
        assert "beside an icon, chart or ring" in prompt

    def test_text_kind_repetition_is_not_evidence(self):
        """
        The first version told the model any repeated_slots group with a count
        above 1 is evidence -- but the badge dots aggregate exactly that way on
        every page, and the model reported a phantom blank CII column off pure
        decoration. Repetition is evidence only for the svg-adjacent kind, the
        one that means "a figure slot that renders nothing"; text-kind groups
        are dots and separators.
        """
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        assert "in an `svg-adjacent` group is evidence in itself" in prompt
        assert "A text-kind group with a count above 1 is usually decoration" in prompt
        assert "NOT a finding on its own" in prompt

    def test_it_does_not_name_the_seeded_bugs(self):
        """
        Teaching to the test would make the next measurement meaningless. The
        instruction must be general -- no route names, no field names from the
        corpus.
        """
        prompt = rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)
        lowered = prompt.lower()
        for leak in ("healthscore", "health score", "actualfuel", "actual_fuel", "tofixed"):
            assert leak not in lowered, f"rubric leaks the corpus: {leak}"


class TestFixtureSurfaceExemption:
    """
    The carve-out the P1.1 measurement withdrew (PR #49) made THREE render-break
    shapes reportable on fixture surfaces; Leg B showed that was too wide -- the
    'count reads zero' shape regressed the healthy-`main` FP baseline (F-002).
    What the measurement did NOT implicate is the narrowest shape: a status
    VALUE leaking raw into the text. That is kept reportable here, because it is
    the one half of S-3 that is mechanical, and without it the
    `status_case_leak` candidate would be refuted as "the demo working as
    intended" and S-3 could never land.
    """

    def _prompt(self):
        return rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)

    def test_status_value_render_break_is_reportable_on_fixture_surfaces(self):
        prompt = self._prompt()
        assert "ONE RENDER BREAK IS REPORTABLE EVEN ON A FIXTURE SURFACE" in prompt
        assert "status VALUE" in prompt
        assert "leaking raw into the text" in prompt
        assert "Report the leaked value" in prompt

    def test_the_leaked_value_is_anchored_to_casing_not_to_numbers(self):
        """
        The line between this exception and the FP it must not invite: it names
        a casing mismatch (the stored spelling showing), never a surprising
        figure. 'Report the leaked value; never a figure' is what keeps F-001
        (numbers) and F-002 (count-zero) out.
        """
        prompt = self._prompt()
        assert "casing it is stored in" in prompt
        assert "storage is a defect" in prompt
        assert "not a seeded anomaly" in prompt

    def test_the_exception_does_not_leak_the_seed_vocabulary(self):
        """
        The exception names the SHAPE, not the seed. Teaching to the test would
        make the next measurement meaningless. 'open' is intentionally NOT a
        leak term -- the empty-state line already says "No open findings" -- so
        the guard uses the quoted/value forms and the class names.
        """
        lowered = self._prompt().lower()
        for leak in ("'open'", '"open"', "uncategorised", "text-status-red", "status-red"):
            assert leak not in lowered, f"rubric leaks the S-3 seed: {leak}"


class TestCandidates:
    """
    The mandatory-verdict contract. The discriminator run proved the model rung
    is not the quiet-blank bottleneck -- sonnet was shown the pristine repeated
    svg-adjacent signal and still reported nothing -- so the rubric must make it
    IMPOSSIBLE to walk past a mechanically-detected signal: every candidate gets
    a verdict, and no unconfirmed candidate becomes a finding.
    """

    def _prompt(self):
        return rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)

    def test_the_prompt_requires_candidate_assessment(self):
        prompt = self._prompt()
        for token in ("candidate_findings", "candidate_assessments", "confirmed", "refuted"):
            assert token in prompt, token

    def test_unassessed_candidates_are_called_incomplete(self):
        assert "an unassessed candidate makes the run's output incomplete" in self._prompt()

    def test_unconfirmed_candidates_are_forbidden_as_findings(self):
        assert "Never report an unconfirmed candidate as a finding" in self._prompt()

    def test_a_confirmed_candidate_must_produce_a_real_finding(self):
        """The single-source-of-truth rule the schema enforces, stated up front."""
        prompt = self._prompt()
        # Wraps across a line break in the prompt, so assert the fragments.
        assert "Produce" in prompt
        assert "a finding for it" in prompt
        assert "finding_id" in prompt

    def test_warnings_are_excluded_from_console_candidates(self):
        assert "warnings are excluded" in self._prompt()

    def test_the_candidate_section_does_not_leak_seeded_bugs(self):
        """
        Same guard as TestReadTheValues: the candidate instruction must stay
        general or the next corpus measurement is meaningless. Route names are
        intentionally NOT leak terms -- the route allow-list always contains
        them -- only the field-level vocabulary of the seeded bugs.
        """
        lowered = self._prompt().lower()
        for leak in ("healthscore", "health score", "actualfuel", "actual_fuel", "tofixed"):
            assert leak not in lowered, f"rubric leaks the corpus: {leak}"


class TestInteraction:
    """
    The coverage half of recall, which the "read harder" work never touched.

    S-1 in the seeded corpus is click-triggered. The run that missed it read the
    route and never used the control, so the crash never fired -- zero console
    errors and zero failed requests across the whole session. The detectors were
    in place and had nothing to see. No amount of instruction about reading
    values fixes a defect that has not been provoked, which is why this section
    exists and why it is bounded: an unbounded one would spend the turn budget on
    the first route and never reach the rest.
    """

    def _prompt(self):
        return rubric.build_system_prompt(ai_fallback_mode=True, max_routes=8)

    def test_reading_a_route_is_stated_to_be_half_a_test(self):
        assert "half-tested" in self._prompt()

    def test_the_interaction_pass_is_capped_at_two_controls(self):
        prompt = self._prompt()
        assert "UP TO TWO in-page" in prompt
        assert "Two controls, not more" in prompt

    def test_the_cap_is_justified_by_the_turn_budget(self):
        """
        A cap the agent does not understand is a cap it rationalises around. The
        reason has to travel with the number.
        """
        assert "never reaches the rest of the list" in self._prompt()

    def test_writes_are_forbidden(self):
        """
        One database backs the whole run, so a write invalidates every later
        observation -- and PLAN.md Phase 3 raises the same problem across rounds.
        Interaction must stay read-only or the findings stop being comparable.
        """
        prompt = self._prompt()
        assert "Anything that WRITES" in prompt
        for control in ("submit", "save", "create", "delete", "send", "upload"):
            assert control in prompt

    def test_interaction_does_not_reopen_crawling(self):
        """
        The route allow-list is a cost control. "Use the controls" must not be
        read as "follow whatever they lead to".
        """
        prompt = self._prompt()
        assert "Anything that leaves the route list above" in prompt
        assert "controls INSIDE a route" in prompt

    def test_a_route_without_controls_is_not_a_finding(self):
        """The missing-feature rule, restated where it would otherwise be lost."""
        prompt = self._prompt()
        assert "offers no such control" in prompt
        assert "The absence" in prompt and "is a missing feature" in prompt

    def test_a_broken_control_is_tied_back_to_the_candidate_contract(self):
        """
        The interaction pass and the candidate layer are one mechanism: clicking
        is what makes console_error and failed_request fire at all.
        """
        assert "candidate you must assess" in self._prompt()

    def test_opening_every_tab_is_mandatory_reading(self):
        """
        P1.2 run record: S-3's leak renders only on a non-default tab, and the
        agent read the default view and moved on -- the status_case_leak
        candidate is correct but never had its signal in the DOM. The pass must
        make opening every view REQUIRED, not optional, or the detectors never
        see the defect. General rule; no route or tab name may appear.
        """
        prompt = self._prompt()
        assert "open EVERY view" in prompt
        assert "a view you never opened" in prompt
        assert "Switching views is READING" in prompt
        assert "does not count against the budget" in prompt

    def test_it_does_not_leak_the_seeded_bugs(self):
        """
        The strongest temptation in this section is to name the control that
        crashes. Doing so would make the next corpus run measure nothing.

        Route and fixture-surface names are NOT leak terms -- the allow-list and
        the known-by-design list have always contained them. What must not
        appear is the seeded defect's own vocabulary.
        """
        lowered = self._prompt().lower()
        for leak in ("history tab", "crashes when clicked", "healthscore", "health score"):
            assert leak not in lowered, f"rubric leaks the corpus: {leak}"
