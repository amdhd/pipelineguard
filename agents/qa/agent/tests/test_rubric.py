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
