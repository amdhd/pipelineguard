"""
Phase 3's decision layer -- does the loop run another round, or stop?

NAMED convergence.py, and the package is `converge`, for the same reason
`agents/fix/harness.py` is not main.py: source module names are global in the
shared pytest session and --import-mode=importlib only rescues identically-named
TEST files. `state`, `report`, `summary`, `main` and `harness` are all taken.

WHAT THIS PROGRAM IS
--------------------
Three programs already exist: the agent (drives a browser, in AgentCore), the QA
harness (invokes it, prices it, renders the comment) and the fix harness
(patches what the QA run found). This is the fourth, and it owns exactly one
question: given every round so far, what happens next?

It makes no model calls, drives no browser, and writes to no repository. It
reads two artefacts a round leaves behind -- the findings JSON and the fix
result JSON -- and returns a state. That narrowness is the point: the loop's
stopping rule is the part of Phase 3 that can burn money silently, so it lives
in a program that can be unit-tested exhaustively without spending a cent.

THE RULE, AND ONE DELIBERATE REFINEMENT OF IT
---------------------------------------------
PLAN.md Phase 3 states it as:

    Finding set shrinking            -> continue
    Finding set identical or growing -> stall, hand to a human
    Zero blocking findings           -> done, PASS

This module implements that, with one refinement it is worth being explicit
about because it is a deviation from the letter of the plan.

The plan's rule is about the SIZE of the set. Size alone cannot see a fix that
trades one defect for another: {A, B, C} -> {A, D} is a set that shrank, and
under a size-only reading the loop would call that progress and keep going --
while the agent's own patch has just introduced D. So a blocking finding present
in this round and absent from the previous one stops the loop as `REGRESSED`,
whatever happened to the count.

This is a strict refinement, not a contradiction. Where the plan says stall, so
does this: a set that grew necessarily contains something new, and an identical
set has nothing resolved. `REGRESSED` only splits the sharpest case out of
`STALL` and names it, because the two want different things from the human who
reads the report -- a stall means the agent cannot fix this, while a regression
means the agent broke something and the branch it produced should be treated
accordingly.

Two things must be true before either verdict can fire, because both accuse the
code.

First, a round that patched is never stopped on its own findings. The round
records its finding set BEFORE its fix runs, so STALL ("the agent cannot fix
this") or REGRESSED ("the patch is the suspect") computed from a round's own
pre-fix set would pass sentence on a patch that has never been measured. A
round that changed the code always earns the next round, which exists to
measure that change; the stop verdicts belong to a round that found blocking
findings and produced no patch -- the one state where another round would
re-measure the same code and cost the same money.

Second, `REGRESSED` requires a patch to have run between the two measured
states. When neither round patched, the two finding sets describe the same
code, so a blocker that only appears in the second measurement is the
run-to-run variance repeated runs exist to absorb -- it cannot be a regression,
because nothing changed to regress.

A ROUND THAT DID NOT FINISH DECIDES NOTHING
-------------------------------------------
`INCONCLUSIVE` exists because of a failure mode this repo has already been bitten
by once: an agent run that reports nothing looks exactly like a healthy
application (report.py's `unauthenticated` hint says so in as many words). The
convergence check makes that worse, because a truncated round's finding set is
SMALLER than the previous round's -- so a run that died halfway reads as
convergence, twice over: as progress on the way in, and as a PASS at the end.

So a round carrying `error` or `incomplete` is never compared and never passes.
It stops the loop and says why.

BACKSTOPS STOP THE NEXT ROUND; THEY NEVER OVERTURN A VERDICT
-------------------------------------------------------------
The hard round cap, the cumulative token budget and the two wall-clock caps are
checked only on the path that would otherwise CONTINUE. A round that passed
still passed even if it was the third; a stall is still a stall even if the
budget also ran out. Reporting `MAX_ROUNDS` for a run that actually converged
would be a lie in the direction that makes the loop look worse than it is, and
`TOKEN_BUDGET` for a run that stalled hides the finding that stalled it.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa" / "harness"))

import pricing  # noqa: E402
import schema  # noqa: E402

# --- The states -----------------------------------------------------------
#
# One string per reason the loop can be in, because "stopped" on its own tells
# the reader nothing about whether to celebrate, review a branch, or go and look
# at CloudWatch.

CONTINUE = "CONTINUE"        # progress, or a round that patched needs verifying
PASS = "PASS"                # zero blocking findings -- the loop's goal
STALL = "STALL"              # nothing resolved this round
REGRESSED = "REGRESSED"      # a blocking finding appeared that was not there before
INCONCLUSIVE = "INCONCLUSIVE"  # the round failed or was truncated; it proves nothing
MAX_ROUNDS = "MAX_ROUNDS"
TOKEN_BUDGET = "TOKEN_BUDGET"
WALL_CLOCK = "WALL_CLOCK"
ROUND_TIMEOUT = "ROUND_TIMEOUT"

# Everything except CONTINUE ends the loop. Derived rather than listed, so a
# state added above cannot be forgotten here and silently keep the loop running.
def stops(state: str) -> bool:
    return state != CONTINUE


# Only one of the stop states is a success. `PASS` is not "the loop finished" --
# it is "there are no blocking findings left", which is the only outcome that
# should leave a green check on a PR.
def succeeded(state: str) -> bool:
    return state == PASS


# PLAN.md Phase 3: "Hard max rounds (3)". The other three are not given numbers
# in the plan, so these are derived from measurement rather than invented: a
# round is a QA run (~$0.03, ~50s of session, measured in EVIDENCE.md) plus a
# fix run (~$0.11 per finding, 5 findings max). Three rounds of that sits far
# under these caps, which is what a backstop should do -- catch a run that has
# gone wrong, not shape a run that has not.
DEFAULT_BUDGETS = {
    "max_rounds": 3,
    "token_budget": 2_000_000,
    "wall_clock_seconds": 3600,
    "round_timeout_seconds": 1500,
}


class Decision:
    """A state, the sentence explaining it, and whether the loop ends here."""

    def __init__(self, state: str, reason: str):
        self.state = state
        self.reason = reason

    @property
    def stop(self) -> bool:
        return stops(self.state)

    @property
    def ok(self) -> bool:
        return succeeded(self.state)

    def as_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason, "stop": self.stop}

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"Decision({self.state}, {self.reason!r})"


# --- Recording a round ----------------------------------------------------


def _fingerprints(findings: dict) -> dict:
    """
    Map each finding to its cross-round identity, keeping the fields the ledger
    needs to render a row.

    `schema.finding_fingerprint` is deliberately reused rather than reimplemented:
    it already excludes `id` (renumbered every run) and `evidence` (free text
    that varies between runs describing the same defect), and a second
    implementation that drifted from it would make every round look like
    progress -- which is precisely the failure test_schema.py warns about.
    """
    out = {}
    for f in findings.get("findings", []):
        out[schema.finding_fingerprint(f)] = {
            "id": f.get("id", "?"),
            "severity": f.get("severity", "?"),
            "page": f.get("page", "?"),
            "summary": f.get("summary", ""),
        }
    return out


def _patched_fingerprints(fix_result: dict, by_id: dict) -> list:
    """
    Which of THIS round's findings the fix agent actually patched.

    Attribution happens here, at record time, because it is only possible here:
    `applied[].finding_id` is a per-run id (F-001), which means nothing outside
    the round that produced it. Resolving it to a fingerprint while both
    artefacts are in hand is what lets the final ledger say "patched in round 2,
    gone in round 3" instead of "gone".
    """
    patched = []
    for applied in fix_result.get("applied", []):
        fp = by_id.get(applied.get("finding_id"))
        if fp and fp not in patched:
            patched.append(fp)
    return patched


# --- Aggregating repeated QA runs into one round (P1.3) ------------------
#
# One QA run is not a measurement: EVIDENCE.md measured S-1 caught in five runs
# and missed in the sixth, route coverage varying between seven and eight, on
# identical code. A convergence check that compares single-run finding sets
# therefore reads noise as progress, or progress as noise. The audit's P1.3 fix
# -- "repeated runs per round" -- is to run QA K times per round and fold the
# runs into one findings JSON by majority vote: a finding counts only when more
# than half the runs report it. It lives here, in the deterministic layer, so
# the workflow and the session are unchanged: the aggregate is a drop-in for the
# findings file they already read.

# The severityless, prose-tolerant identity. `finding_fingerprint` embeds both
# severity and the summary verbatim -- which would split one defect graded HIGH
# by one run and MEDIUM by another into two rows, AND split the same defect
# whose summary a model phrased differently into two rows. Live run 33586824290
# proved the second split is the real one: all three QA runs found the same two
# defects, each phrasing them differently, so an exact-summary key dropped every
# one of them and the round PASSED on a dirty corpus. So: group by page, then by
# Dice similarity over normalized significant tokens -- prose about the same
# defect shares its stable core (voyage/typeerror/tofixed) even when no sentence
# matches, while two genuinely different defects on one page share far less than
# half their tokens. Grade the group at its most severe -- the blocking-safe
# direction, so a HIGH in any majority of runs stays blocking.
_SEVERITY_RANK = {s: i for i, s in enumerate(schema.SEVERITIES)}

# Function words that carry no defect identity, plus the short numerals that
# slip into prose. Tokenizing to this set is what makes "TypeError on .toFixed()
# of undefined" and "TypeError: Cannot read properties of undefined (reading
# 'toFixed')" the same signal.
_FINDING_STOPWORDS = frozenset(
    """a an the and or but is are was were be been with without on of for to in
    from at by as it its this that these those has have had every all each any
    some than then when which will would can could may might not no one two""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _finding_tokens(finding: dict) -> frozenset:
    """The significant tokens of a finding's summary, as a set."""
    text = finding.get("summary", "").lower()
    return frozenset(
        w for w in _TOKEN_RE.findall(text) if len(w) > 1 and w not in _FINDING_STOPWORDS
    )


def _dice(a: frozenset, b: frozenset) -> float:
    """Sorensen-Dice similarity: twice the shared tokens over the two sizes."""
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _similarity_clusters(findings, sim_threshold: float = 0.5):
    """
    Group (run_index, finding) pairs into defects.

    Same defect = same page and summaries whose significant tokens overlap by
    >= sim_threshold (Dice). Findings from the SAME run are never merged -- a
    run reports a given defect at most once -- so a page with two distinct
    defects stays two clusters even when every run reports both.
    """
    by_page: dict = {}
    for run_index, finding in findings:
        by_page.setdefault(finding.get("page", "?"), []).append((run_index, finding))

    clusters = []
    for page, items in by_page.items():
        tokens = [_finding_tokens(f) for _, f in items]
        n = len(items)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(n):
            for j in range(i + 1, n):
                if items[i][0] == items[j][0]:
                    continue  # never merge a run with itself
                if _dice(tokens[i], tokens[j]) >= sim_threshold:
                    parent[find(i)] = find(j)

        comps: dict = {}
        for i in range(n):
            comps.setdefault(find(i), []).append(items[i])
        clusters.extend(comps.values())
    return clusters


def _observed_at_commit(runs: list):
    """Every run in a round is stamped with the same commit, so any one works."""
    for r in runs:
        if r.get("observed_at_commit"):
            return r["observed_at_commit"]
    return None


def _sum_costs(runs: list) -> dict:
    """
    Sum every run's tokens and turns.

    The cumulative TOKEN_BUDGET backstop measures spend through the round's
    meters, so if the aggregate dropped the other K-1 runs' cost, the cap would
    silently stop enforcing K times the real spend.
    """
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    turns = 0
    model = None
    for r in runs:
        cost = r.get("cost") or {}
        tokens = cost.get("model_tokens") or {}
        for key in total:
            total[key] += int(tokens.get(key, 0))
        turns += int(cost.get("turns", 0))
        model = model or cost.get("model")
    return {"model": model or "unknown", "turns": turns, "model_tokens": total}


def aggregate_findings(runs: list, *, threshold: int | None = None) -> dict:
    """
    Fold K QA findings JSONs into one, by strict-majority vote.

    A finding counts only when more than half the runs report it, so a defect
    one run's model hallucinated is dropped and one a single run missed is not
    lost to a coin flip. The round is comparable only when a majority of the
    runs completed; otherwise the aggregate carries `error`/`incomplete`, so the
    round records INCONCLUSIVE and never passes.

    K=1 returns the run unchanged: repeated runs are opt-in, and the single-run
    behaviour is byte-for-byte what it was before this function existed.
    """
    if not runs:
        raise ValueError("aggregate_findings needs at least one run")
    if len(runs) == 1:
        return runs[0]

    k = len(runs)
    # Strict majority: k//2+1. (ceil(k/2) would count a finding present in 1 of
    # 2 runs, which is no agreement at all.)
    need = threshold if threshold is not None else k // 2 + 1
    completed = [r for r in runs if not r.get("error") and not r.get("incomplete")]

    # The round proves nothing. Prefer the error over the truncation: both stop
    # the loop, but the error names the cause.
    if len(completed) < need:
        errored = next((r for r in runs if r.get("error")), None)
        agg = {
            "findings": [],
            "observed_at_commit": _observed_at_commit(runs),
            "session_seconds": sum(int(r.get("session_seconds", 0)) for r in runs),
            "cost": _sum_costs(runs),
            "repeats": {"total": k, "completed": len(completed), "threshold": need},
        }
        if errored is not None:
            agg["error"] = errored["error"]
            agg["detail"] = f"{k - len(completed)} of {k} repeated QA runs failed"
        else:
            agg["incomplete"] = True
            agg["stop_reason"] = "fewer than a majority of repeated QA runs completed"
        return agg

    # Majority over similarity clusters; a cluster grades at its most severe.
    all_findings = [
        (run_index, finding)
        for run_index, run in enumerate(completed)
        for finding in run.get("findings", [])
    ]
    merged = []
    for cluster in _similarity_clusters(all_findings):
        # A cluster is a vote per run: how many DIFFERENT runs report it.
        if len({run_index for run_index, _ in cluster}) < need:
            continue
        # Most severe = smallest rank (CRITICAL=0 < HIGH=1 < ...). `min` picks
        # it; `max` would pick the mildest grade, the wrong direction.
        best = min(
            (finding for _, finding in cluster),
            key=lambda f: _SEVERITY_RANK.get(f.get("severity"), 9),
        )
        merged.append(dict(best))

    # ids are renumbered every run, so the merged set gets its own F-001.. run,
    # and the fix result's applied[].finding_id resolves 1:1 through it.
    merged.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.get("severity"), 9),
            f.get("page", "?"),
            f.get("summary", ""),
        )
    )
    for i, finding in enumerate(merged, 1):
        finding["id"] = f"F-{i:03d}"

    return {
        # Kept schema-plausible (a blocking finding forces FAIL), though only
        # round_record and the fix harness actually read the result.
        "overall": (
            "FAIL" if any(f["severity"] in schema.BLOCKING for f in merged) else "PASS"
        ),
        "pages_tested": max((r.get("pages_tested", 0) for r in completed), default=0),
        "findings": merged,
        "session_seconds": sum(int(r.get("session_seconds", 0)) for r in runs),
        "cost": _sum_costs(runs),
        "observed_at_commit": _observed_at_commit(runs),
        # Self-describing and inert to every consumer; tells a reader of the
        # artefact that it is a vote, not a single run.
        "repeats": {"total": k, "completed": len(completed), "threshold": need},
    }


def round_record(
    number: int,
    findings: dict,
    fix_result: dict | None = None,
    *,
    seconds: int = 0,
    prices: dict | None = None,
) -> dict:
    """
    Everything the loop needs to remember about one round.

    Both artefacts are folded in at once because a round is one unit: the QA run
    and the fix run that consumed its findings. Keeping them apart would leave
    the attribution above impossible to do later.
    """
    prices = prices if prices is not None else pricing.load_prices()
    fps = _fingerprints(findings)
    by_id = {meta["id"]: fp for fp, meta in fps.items()}
    fix = fix_result or {}
    budget = fix.get("budget") or {}
    cost = pricing.summarise(findings, prices)

    # The fix half reports UNITS and its model name; dollars are computed here,
    # from the same price table the QA half uses. agent.py states the rule this
    # follows -- "converting units to money in two places is how the two
    # disagree" -- and it applies across programs, not just across a runtime
    # boundary.
    fix_usd = (
        pricing.model_cost_usd(
            budget.get("model", ""),
            int(budget.get("input_tokens", 0)),
            int(budget.get("output_tokens", 0)),
            prices,
            cache_read=int(budget.get("cache_read", 0)),
            cache_write=int(budget.get("cache_write", 0)),
        )
        if budget.get("calls")
        else None
    )

    return {
        "round": number,
        # A failed or truncated round is flagged, not silently folded in. See
        # the INCONCLUSIVE note at the top of this module.
        "error": findings.get("error"),
        "incomplete": bool(findings.get("incomplete")),
        "stop_reason": findings.get("stop_reason"),
        "observed_at_commit": findings.get("observed_at_commit"),
        "findings": fps,
        "blocking": sorted(
            fp for fp, meta in fps.items() if meta["severity"] in schema.BLOCKING
        ),
        "patched": _patched_fingerprints(fix, by_id),
        "fix_skipped": [
            {"finding_id": s.get("finding_id", "?"), "reason": s.get("reason", "")}
            for s in fix.get("skipped", [])
        ],
        "fix_errors": list(fix.get("errors") or []),
        "seconds": int(seconds),
        "meters": {
            "qa_tokens": cost["total_input_tokens"] + cost["output_tokens"],
            "qa_usd": cost["estimated_total_usd"],
            "qa_model": cost["model"],
            "session_seconds": cost["session_seconds"],
            "fix_tokens": int(budget.get("spent", 0)),
            "fix_calls": int(budget.get("calls", 0)),
            "fix_model": budget.get("model", ""),
            "fix_usd": fix_usd,
            # A fix ran but reported no budget: its tokens are UNKNOWN, not zero.
            # Same honesty rule as pricing.py's `unpriced` -- a silent zero here
            # would understate the cumulative total, which is the one number the
            # token backstop is enforcing against.
            "fix_tokens_known": bool(budget) or not fix,
        },
    }


def totals(rounds: list) -> dict:
    """
    Cumulative meters across every round.

    Dollars go to None the moment ANY round is unpriced, rather than summing the
    rounds that happened to have a price table. A total that quietly covers two
    rounds out of three is the kind of number PRICING.md exists to prevent.
    """
    usd_parts = []
    unpriced = 0
    for r in rounds:
        for key in ("qa_usd", "fix_usd"):
            value = r["meters"].get(key)
            if value is None:
                # A fix run that never happened is not an unpriced fix run.
                if key == "fix_usd" and not r["meters"].get("fix_calls"):
                    continue
                unpriced += 1
            else:
                usd_parts.append(value)

    unknown_fix_tokens = sum(1 for r in rounds if not r["meters"].get("fix_tokens_known", True))
    return {
        "rounds": len(rounds),
        "tokens": sum(r["meters"]["qa_tokens"] + r["meters"]["fix_tokens"] for r in rounds),
        "tokens_known": unknown_fix_tokens == 0,
        "unknown_fix_token_rounds": unknown_fix_tokens,
        "usd": sum(usd_parts) if unpriced == 0 else None,
        "unpriced_meters": unpriced,
        "seconds": sum(r["seconds"] for r in rounds),
        "session_seconds": sum(r["meters"]["session_seconds"] for r in rounds),
    }


# --- The decision ---------------------------------------------------------


def _backstop(rounds: list, budgets: dict) -> Decision | None:
    """
    The four caps, checked only on the path that would otherwise continue.

    The per-round timeout is enforced by the workflow's own step timeout -- a
    hung round cannot be stopped by a program that only runs after it. What
    happens here is the other half: a round that overran is not allowed to start
    another one, and the report says which cap was hit rather than leaving the
    reader to infer it from a job that simply ended.
    """
    current = rounds[-1]
    agg = totals(rounds)
    limits = {**DEFAULT_BUDGETS, **(budgets or {})}

    if len(rounds) >= limits["max_rounds"]:
        return Decision(
            MAX_ROUNDS,
            f"the finding set is still shrinking, but this was round "
            f"{len(rounds)} of a hard maximum of {limits['max_rounds']}.",
        )
    if current["seconds"] > limits["round_timeout_seconds"]:
        return Decision(
            ROUND_TIMEOUT,
            f"round {current['round']} took {current['seconds']}s, over the "
            f"{limits['round_timeout_seconds']}s per-round cap.",
        )
    if agg["seconds"] > limits["wall_clock_seconds"]:
        return Decision(
            WALL_CLOCK,
            f"{agg['seconds']}s spent across {agg['rounds']} round(s), over the "
            f"{limits['wall_clock_seconds']}s cumulative cap.",
        )
    if agg["tokens"] >= limits["token_budget"]:
        return Decision(
            TOKEN_BUDGET,
            f"{agg['tokens']:,} tokens spent across {agg['rounds']} round(s), at "
            f"or over the {limits['token_budget']:,} cumulative budget.",
        )
    # An unknown cannot be compared against a cap, and a budget that cannot be
    # enforced must not be reported as enforced -- PLAN.md 1d. So the loop stops
    # rather than continuing on an accounting hole.
    if not agg["tokens_known"]:
        return Decision(
            TOKEN_BUDGET,
            f"the cumulative token budget cannot be enforced: "
            f"{agg['unknown_fix_token_rounds']} round(s) ran the fix agent but "
            "reported no token usage, so the total above is a floor, not a count.",
        )
    return None


def _cross_round_blocking(previous: dict, current: dict):
    """
    Match consecutive rounds' blocking findings by the prose-tolerant identity.

    `round_record` stores each round's blocking set as fingerprints, and the
    summary inside a fingerprint is fresh prose every run -- the exact variance
    that made live run 33588304974 end REGRESSED: round 2 reported the same
    /voyage crash round 1 patched, only rephrased, so the exact-fingerprint set
    difference read a still-unfixed defect as a brand-new one and blamed the
    patch. Matching by page + Dice over significant tokens instead -- the same
    identity `aggregate_findings` already uses within a round -- a rephrased
    blocker matches, and the verdict falls out of what the sets actually did: a
    blocker that persisted after a patch is STALL, not REGRESSED.

    Severity is deliberately not part of this identity, for the same reason it
    is not part of `aggregate_findings`': a defect graded HIGH by one run and
    MEDIUM by the next is the same defect, and splitting it would turn round-to-
    round grading noise into a false regression.

    Returns (resolved, appeared): the previous round's blocking fingerprints
    with no match in the current round, and the current round's with no match in
    the previous.
    """
    prev_meta = previous.get("findings", {}) or {}
    curr_meta = current.get("findings", {}) or {}
    prev_fps = list(previous.get("blocking", []))
    curr_fps = list(current.get("blocking", []))
    prev_findings = [prev_meta.get(fp, {}) for fp in prev_fps]
    curr_findings = [curr_meta.get(fp, {}) for fp in curr_fps]
    # `_similarity_clusters` returns the finding objects it was given, so map
    # each back to its fingerprint by identity. `_fingerprints` builds a fresh
    # dict per finding, so id() is unique within a round.
    fp_of = {
        **{id(f): prev_fps[i] for i, f in enumerate(prev_findings)},
        **{id(f): curr_fps[i] for i, f in enumerate(curr_findings)},
    }

    pairs = [(0, f) for f in prev_findings] + [(1, f) for f in curr_findings]
    resolved, appeared = [], []
    for cluster in _similarity_clusters(pairs):
        prev_members = [(rn, f) for rn, f in cluster if rn == 0]
        curr_members = [(rn, f) for rn, f in cluster if rn == 1]
        if prev_members and not curr_members:
            resolved.extend(fp_of[id(f)] for _, f in prev_members)
        elif curr_members and not prev_members:
            appeared.extend(fp_of[id(f)] for _, f in curr_members)
    return resolved, appeared


def decide(rounds: list, budgets: dict | None = None) -> Decision:
    """
    The whole stopping rule, in the order the reasons take precedence.

    `rounds` is every round so far, oldest first, as built by `round_record`.
    """
    if not rounds:
        raise ValueError("decide() needs at least one round")

    current = rounds[-1]
    previous = rounds[-2] if len(rounds) > 1 else None

    # 1. A round that did not finish is not evidence of anything.
    if current["error"]:
        return Decision(
            INCONCLUSIVE,
            f"round {current['round']} failed ({current['error']}), so its finding "
            "set cannot be compared and cannot be a PASS.",
        )
    if current["incomplete"]:
        return Decision(
            INCONCLUSIVE,
            f"round {current['round']} was truncated "
            f"({current.get('stop_reason') or 'budget exhausted'}). A partial run "
            "reports a smaller finding set for reasons that have nothing to do "
            "with the application, so it reads as convergence when it is not.",
        )

    # 2. The goal.
    if not current["blocking"]:
        return Decision(
            PASS,
            f"round {current['round']} reported no blocking findings.",
        )

    # 3. The comparison. Nothing to compare against on the first round.
    if previous is not None:
        resolved, appeared = _cross_round_blocking(previous, current)

        # A round that patched changed the code, so its patch has not been
        # measured yet -- the next round exists to do that. STALL and REGRESSED
        # would end the loop on this round's PRE-fix findings and discard the
        # patch it just made. Live runs did exactly that: 33588304974 and
        # 33590568978 each applied the correct backend cause in their final
        # round and were stopped a round before any QA could verify it. A round
        # that changed the code falls through to CONTINUE below; the stop
        # verdicts belong to a round that found blockers and produced no patch.
        if not current["patched"]:
            if appeared:
                if previous["patched"]:
                    return Decision(
                        REGRESSED,
                        f"round {current['round']} reports {len(appeared)} blocking "
                        f"finding(s) that round {previous['round']} did not "
                        f"({len(resolved)} resolved). A set that gained a member is not "
                        "converging, and when the only thing that changed between the "
                        "rounds was the agent's own patch, the patch is the suspect.",
                    )
                # Neither round patched, so the two sets describe the same code.
                # A blocker only the second measurement sees is the run-to-run
                # variance repeated runs exist to absorb -- not a regression,
                # because nothing changed to regress. Still a stop: with no fix
                # applied in either round, another round changes nothing.
                return Decision(
                    STALL,
                    f"round {current['round']} reports {len(appeared)} more blocking "
                    f"finding(s) than round {previous['round']}, on code that has not "
                    "changed (neither round patched). A blocker that only shows up in "
                    "the second measurement of identical code is re-measurement "
                    "variance, not a regression -- but the loop cannot converge "
                    "without a fix.",
                )
            if not resolved:
                return Decision(
                    STALL,
                    f"round {current['round']} reports the same "
                    f"{len(current['blocking'])} blocking finding(s) as round "
                    f"{previous['round']}. Another round would run the same agent "
                    "against the same defects and cost the same money.",
                )

    # 4. Continue, unless a cap says otherwise. The continue paths: the first
    # round, a set that shrank, or a round that patched and must be verified.
    hit = _backstop(rounds, budgets or {})
    if hit is not None:
        return hit

    if previous is None:
        return Decision(
            CONTINUE,
            f"round {current['round']} found {len(current['blocking'])} blocking "
            "finding(s); nothing to compare against yet.",
        )
    if current["patched"]:
        return Decision(
            CONTINUE,
            f"round {current['round']} patched {len(current['patched'])} blocking "
            f"finding(s); the next round measures whether the fixes took.",
        )
    return Decision(
        CONTINUE,
        f"round {current['round']} resolved {len(resolved)} of round "
        f"{previous['round']}'s blocking finding(s) and introduced none.",
    )


# --- The reconciliation ledger --------------------------------------------

# PLAN.md Phase 3: "a per-finding ledger of what was fixed, what was by-design,
# and what was agent noise". Two of those three are human judgements, so this
# splits the question in half and refuses to answer the half it cannot:
#
#   * WHAT HAPPENED is observable from the artefacts, and is computed here.
#   * WHAT IT MEANS -- by-design, false positive, real defect -- is a label a
#     human writes, in score.py's vocabulary. Unlabelled stays unlabelled.
#
# score.py already states the rule this follows: "An unlabelled finding is
# counted as unlabelled, never as correct."

FIXED = "fixed"                       # patched, and gone in the next round
PATCH_INEFFECTIVE = "patch did not fix it"
PATCH_UNVERIFIED = "patch unverified (run ended before a re-measure)"
NOT_REPRODUCED = "not reproduced"     # went away without a patch
OUTSTANDING = "outstanding"           # still there in the final round
INTRODUCED = "introduced"             # first seen after round 1


def reconcile(rounds: list, labels: dict | None = None) -> list:
    """
    The per-finding ledger, across every round, in one table.

    A finding is followed by the same prose-tolerant identity the stopping rule
    uses, so a defect the model rephrased between rounds -- and whose id the
    fix loop renumbered -- is one row rather than two. The exact-fingerprint
    ledger would render live run 33588304974 as the /voyage crash "fixed" in
    round 1 and a NEW /voyage crash "introduced" in round 2, contradicting the
    STALL the stopping rule now reports.
    """
    labels = labels or {}
    final = rounds[-1]
    patched_by_round = {r["round"]: set(r.get("patched", [])) for r in rounds}

    # Every (round, finding) in the run. `_fingerprints` builds a fresh dict per
    # finding, so object identity maps a cluster member back to its fingerprint.
    pairs = []
    fp_of = {}
    for r in rounds:
        for fp, meta in (r.get("findings") or {}).items():
            pairs.append((r["round"], meta))
            fp_of[id(meta)] = fp

    rows = []
    for cluster in _similarity_clusters(list(pairs)):
        members = sorted(
            ((rn, fp_of[id(meta)], meta) for rn, meta in cluster),
            key=lambda m: m[0],
        )
        first_round, fp, meta = members[0]
        rounds_seen = sorted({rn for rn, _, _ in members})
        patched_in = sorted(
            {
                rn
                for rn, member_fp, _ in members
                if member_fp in patched_by_round.get(rn, set())
            }
        )
        present_at_end = any(member_fp in final["findings"] for _, member_fp, _ in members)
        patched = bool(patched_in)

        if patched and patched_in[-1] == final["round"]:
            # The finding's last patch was applied in the final round, whose
            # findings were recorded BEFORE that patch ran (QA measures the
            # code the previous round's patch produced). No round exists that
            # could have measured it, so "patch did not fix it" would sentence
            # a patch no QA ever saw -- the same overclaim #59 removed from the
            # stopping rule, restated for the ledger. Live run 33593197606
            # patched its round-3 blocker (SAVINGS CRITICAL) in round 3 and the
            # loop hit its 3-round cap: unverified, not ineffective.
            outcome = PATCH_UNVERIFIED
        elif present_at_end and patched:
            outcome = PATCH_INEFFECTIVE
        elif present_at_end and first_round > rounds[0]["round"]:
            outcome = INTRODUCED
        elif present_at_end:
            outcome = OUTSTANDING
        elif patched:
            outcome = FIXED
        else:
            # Gone, and nothing patched it. Either the first round's report was
            # noise or the defect is intermittent -- both are the agent's
            # problem rather than the application's, and neither can be told
            # apart from here.
            outcome = NOT_REPRODUCED

        rows.append(
            {
                "fingerprint": fp,
                "severity": meta["severity"],
                "page": meta["page"],
                "summary": meta["summary"],
                "first_round": first_round,
                "rounds_seen": rounds_seen,
                "patched_in": patched_in,
                "outcome": outcome,
                "label": labels.get(fp),
            }
        )

    # Blocking severities first, then by the round they appeared, so the rows a
    # human has to act on are the rows at the top.
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    return sorted(
        rows,
        key=lambda r: (order.get(r["severity"], 9), r["first_round"], r["page"]),
    )


# --- PR re-verify (D-2): the one-before/after ledger -----------------------
#
# `reconcile` walks a whole multi-round loop. The PR path asks a narrower
# question: a report found defects, a fix was attempted, QA ran again -- which
# of the earlier findings is still there? It reuses the same identity the
# stopping rule and the round ledger use (page + Dice over significant tokens),
# so a defect the model rephrased between the two measurements is one row, not
# two. Two statuses are the round ledger's (same meaning); two belong to
# having exactly one re-measurement.

STILL_FAILING = "still failing"  # present again in the re-measurement
UNVERIFIED = "unverified"  # no usable re-measurement exists to judge it by


def verify_report(prior: dict, current: dict, *, fix_intervened: bool = False) -> list:
    """
    Classify every finding in `prior` against a single re-measurement `current`.

    Both arguments are QA findings JSONs in the schema's shape (top-level
    `findings` list). Returns one row per prior finding, blocking severities
    first, each keyed by `schema.finding_fingerprint` so a human can later
    label it in score.py's vocabulary:

      * **STILL_FAILING** -- the defect is present again in `current`.
      * **FIXED** -- absent from `current` AND `fix_intervened` is true. Never
        inferred from silence alone: absent-with-no-known-fix is the round
        ledger's NOT_REPRODUCED (the first report was noise, or the defect is
        intermittent -- neither deserves credit as fixed).
      * **UNVERIFIED** -- `current` is an error or a truncated run, so it
        cannot re-measure anything. "Fixed" by an empty or partial re-report
        is exactly the overclaim fix #60 removed from the round ledger.

    Findings new in `current` are not returned -- the findings list above the
    block already shows them, and this ledger answers "what happened to what
    was reported before".
    """
    prior_findings = prior.get("findings") or []

    if current.get("error") or current.get("incomplete"):
        # A run that errored or stopped early did not necessarily revisit the
        # route a prior defect lives on. Absence from it proves nothing.
        return [
            {
                "fingerprint": schema.finding_fingerprint(f),
                "severity": f["severity"],
                "page": f["page"],
                "summary": f["summary"],
                "status": UNVERIFIED,
            }
            for f in prior_findings
        ]
    if not prior_findings:
        return []

    # Two measurements, two run labels: 0 = prior, 1 = current.
    # `_similarity_clusters` never merges two items with the same label and
    # merges across labels only when the page agrees and Dice >= 0.5.
    pairs = [(0, f) for f in prior_findings] + [
        (1, f) for f in (current.get("findings") or [])
    ]

    rows = []
    for cluster in _similarity_clusters(pairs):
        prior_member = next((m for m in cluster if m[0] == 0), None)
        if prior_member is None:
            continue  # new since the prior report
        _, f = prior_member
        still_present = any(m[0] == 1 for m in cluster)
        if still_present:
            status = STILL_FAILING
        else:
            status = FIXED if fix_intervened else NOT_REPRODUCED
        rows.append(
            {
                "fingerprint": schema.finding_fingerprint(f),
                "severity": f["severity"],
                "page": f["page"],
                "summary": f["summary"],
                "status": status,
            }
        )

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    return sorted(
        rows, key=lambda r: (order.get(r["severity"], 9), r["page"], r["summary"])
    )
