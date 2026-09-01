# P1.2 measurement — does the `status_case_leak` candidate close S-3?

**Status: MEASURED — verdict: NO. Leg A fails, Leg B passes. P1.1 closes via the
written-decision branch of its acceptance criterion (2026-09-02).** Three passes
ran against the PR #50 candidate layer. Pass one caught a deterministic detector
bug (PR #51 fixed it); pass two showed S-3's leak renders only on a non-default
tab, a coverage miss (PR #52 made opening every view required); pass three
measured the coverage rule: **Leg A recall 6/9, S-3 0/3 — identical to pass two —
and Leg B 0 findings on healthy `main`.** The candidate is correct and the FP side
is clean, but the signal is unreachable: the agent visits `/sire` and never
materializes the Findings tab the leak renders on, so `status_case_leak` has never
fired (0/7 corpus runs). The human decision scopes S-3's non-default-tab symptom
out of the browser-driving agent and owns the miss; the detector stays live so any
future layer that materializes all views closes it. The twelfth-pass entry in
[EVIDENCE.md](EVIDENCE.md) is the record.

PR #50 (`feat/s3-candidate-layer`) replaces the withdrawn carve-out (PR #49)
with the measured lever: S-3's raw-enum half becomes a **deterministic
candidate**. `_HARVEST` pass 4 walks text nodes (the SOURCE case, which CSS
`text-transform` never reaches) and collects all-caps words; `detect()` emits a
`status_case_leak` candidate only when one of those is a status-vocabulary word
AND a lowercase status renders on the same page (uppercase-everywhere is a
convention, not a leak). The rubric keeps ONE narrow fixture-surface exception —
a status VALUE leaking raw — the single shape P1.1 did not implicate in any
false positive. F-002 (count-zero) and F-001 (odd numbers) stay excluded.

## Gates to check before running

- [x] **PR #50 merged into pipelineguard `main`** (merge `bc2682e`). The
      vesselAI workflow checks the harness out at `PIPELINEGUARD_REF: main`, so
      the candidate layer is only live once the PR is on `main`.
- [x] **`agents/qa/harness/corpus.example.json` is current** — S-3 row reads
      `"page": "/sire"`, `"keywords": ["status", "open finding", "colour",
      "color", "styling", "uncategorised"]`; the two P1.1 false positives are
      labelled `false-positive`.
- [x] **`qa-corpus-1` seed is the measured shape.** `backend/src/mock/findings.ts`
      widens the status type to `'OPEN' | 'closed'` and sets four findings'
      status to `'OPEN'`; the SIRE route compares `=== 'OPEN'`. The /sire page
      renders raw `OPEN` (all-caps textContent) beside lowercase `closed` — the
      exact `status_case_leak` shape.
- [x] **`findings.json` is downloadable.** `qa-corpus-1` head `de96b34`
      ("ci(qa): upload raw findings.json as an artifact") added the
      `actions/upload-artifact` step.

## The two legs

### Leg A — recall (S-3 surfaces and is confirmed)

Three `workflow_dispatch` runs on vesselAI `qa-corpus-1` at the **default
(sonnet) rung** — the rung the corpus recall is stated at (EVIDENCE criterion 6).

```bash
for i in 1 2 3; do
  gh workflow run ui-qa-agent.yml \
    --repo amdhd/vesselAI \
    --ref qa-corpus-1 \
    --field model=global.anthropic.claude-sonnet-4-6 \
    --field max_routes=8
done
```

Scoring each run (`findings.json` from the run's artifacts):

```bash
.venv/bin/python agents/qa/harness/score.py \
  --findings findings.json \
  --corpus agents/qa/harness/corpus.example.json
```

**Pass = S-3 confirmed as a finding in ≥2 of 3 runs** — the `status_case_leak`
candidate surfaced and the agent assessed it `confirmed` (combined recall ≥8/9
with S-1 3/3 and S-2 2/3 standing; re-check S-1/S-2 did not regress). If the
candidate surfaces but the agent refutes it every time, that is a rubric
failure and S-3 stays open — a candidate the model cannot confirm is not a
catch. Record each run's turns, wall, cost, candidate ids, and verdicts in the
EVIDENCE twelfth-pass entry.

### Leg B — false positives (the detector + exception did not reopen the floodgates)

One run on healthy `main` (no seeds) and label every finding:

```bash
gh workflow run ui-qa-agent.yml \
  --repo amdhd/vesselAI \
  --ref main \
  --field model=global.anthropic.claude-sonnet-4-6 \
  --field max_routes=8
```

**Pass = 0 findings on `/sire` and the other fixture surfaces.** Healthy `main`
stores every status lowercase, so the detector must not emit a
`status_case_leak` candidate anywhere; if it does, or if the agent confirms a
candidate that healthy `main` produced, Leg B fails.

## Run record

### First pass (merged PR #50 code) — caught a deterministic detector bug

| Run | Code | Result |
|---|---|---|
| Leg A run 1 (`33529291729`) | merged | **2/3** — S-1 ✓ (`/voyage` TypeError), S-2 ✓ (`/maintenance` 12 blank rings), **S-3 ✗** |
| Leg B (`33529796841`) | merged | Valid (not throttled). **0 `status_case_leak` candidates** anywhere on healthy `main`; 1 finding, the **known `/voyage` savings fixture FP** (eleventh-pass F-001 shape, already labelled `false-positive` in the corpus; see below) |

**S-3 was missed at the DETECTOR level, not the coverage level.** The `/sire`
status span carries a `capitalize` class (`SirePage.tsx` on `qa-corpus-1`):

```jsx
<span className={cn('text-xs capitalize', {
  'text-status-green': finding.status === 'closed' || finding.status === 'verified',
  'text-status-amber': finding.status === 'in_progress',
  'text-status-red': finding.status === 'open',
})}>
  {finding.status.replace('_', ' ')}
</span>
```

so a lowercase source renders as `Closed` in innerText. The twin check tokenised
only `[a-z]+` runs and required the fully-lowercase form, so it could **never**
pass on the page it exists for — the corpus data is `'OPEN'` (4×) and `'closed'`,
whose innerText is `OPEN` and `Closed`. PR #51 (`fix/s3-twin-check`) makes the
twin **any non-uppercase rendering** of a status word; the all-caps-convention
guard is preserved. 210 tests pass.

**The `/voyage` Leg B finding is not a candidate-layer FP.** It is a true
rendering of the seeded fixture anomaly (`backend/src/mock/voyages.ts`:
v-001-03 has `plannedFuel: 3200, actualFuel: 3245, savings: 29500`, rendered by
`VoyageHistory.tsx` as `savings * 650` → `+$19.2M`), which the rubric explicitly
forbids reporting on a fixture surface. Same shape as eleventh-pass F-001. Not a
`status_case_leak`; 0 status candidates fired on healthy `main`.

### Second pass (fixed code, PR #51 merged as `e5ba1b0`) — S-3 is now a coverage miss

Leg A runs (corpus `qa-corpus-1`, sequential — same-ref concurrency cancels, and
Bedrock throttles two concurrent sonnet sessions):

| Run | S-1 `/voyage` | S-2 `/maintenance` | S-3 `/sire` |
|---|---|---|---|
| run 1 (`33530930117`) | ✅ | ✅ | ❌ |
| run 2 (`33531621415`) | ✅ | ✅ | ❌ |
| run 3 (`33532131019`) | ✅ | ✅ | ❌ |

**Fixed-code recall 6/9, S-3 0/3. Leg A FAILS.** The detector fix is on `main`
and unit-tested to fire on the exact `OPEN`/`Closed` innerText shape, yet **no
`status_case_leak` candidate fired in any of the three runs** — `/sire` produced
zero candidates of any kind across all three. The leak renders only on the
`/sire` **Findings** tab (a non-default view: `/sire` tabs are Readiness Score,
Documents, Findings, Chat; `activeTab` defaults to `readiness`, and the Findings
tab is conditionally rendered). The agent reads the default Readiness tab and
moves on, so the all-caps `OPEN` text nodes never enter the DOM and no candidate
can fire.

This is a **coverage miss, not a detection miss.** The candidate layer is
correct; its signal sits behind an unopened tab. The next lever is the
interaction pass: make opening every tab/segmented/sub-nav view on a route
REQUIRED reading (a view you never opened is a route you never saw), which the
current pass only makes optional ("up to two controls" includes tabs). Drafted
and 211 tests green; not yet merged.

### Third pass (coverage rule, PR #52 merged as `5454e922`) — in progress

The coverage rule is verified live: `main` is `5454e922`, `rubric.py` on `main`
contains "open EVERY view the route itself offers... a view you never opened is a
route you never saw... Switching views is READING", and the workflow checks the
harness out at `PIPELINEGUARD_REF: main` (`ui-qa-agent.yml` line 74), so every
run below executes it.

Leg A runs (corpus `qa-corpus-1`, sequential):

| Run | S-1 `/voyage` | S-2 `/maintenance` | S-3 `/sire` |
|---|---|---|---|
| run 1 (`33533100509`) | ✅ | ✅ | ❌ |
| run 2 (`33533958227`) | ✅ | ✅ | ❌ |
| run 3 (`33534532566`) | ✅ | ✅ | ❌ |
| Leg B (healthy `main`) | `33535141706` — **0 findings, PASS** | | |

**Leg A verdict: recall 6/9, S-3 0/3 — Leg A FAILS, same as the second pass.** The
coverage rule changed nothing measurable: S-1 and S-2 held (3/3 and 3/3, no
regression) and S-3 is again 0/3. The mandatory open-every-view rule did not make
the agent render the `/sire` Findings tab in any of the three runs. Across the
second and third passes combined, S-3 is **0/6** on the fixed candidate layer.

**Run 1 is still 2/3 — S-3 still missed, still at the coverage level.** The run
visited `/sire` and produced 9 candidates, NONE a `status_case_leak`. The
`_HARVEST` pass-4 walk (`browser_tools.py` lines 162-181) is provably live in
production — the same harvest's `repeated_slots` fired elsewhere in this run —
and it reads `nodeValue`, the SOURCE case that the `capitalize` CSS class never
touches, so `OPEN` enters `case_words` if and only if the `/sire` Findings tab is
in the DOM. It was not. The mandatory open-every-view rule did not make the agent
open the Findings tab in run 1.

## What "done" looks like

- [x] Three corpus runs scored: S-3 **0/3** (6/9 recall), S-1 and S-2 not
      regressed. **Leg A fails** the ≥2/3 bar.
- [x] One healthy-`main` run with **0 findings** on fixture surfaces and no
      `status_case_leak` candidates anywhere. **Leg B passes.**
- [x] Twelfth-pass entry in `EVIDENCE.md` with run IDs, tables, and verdicts.
- [x] `AUDIT.md` P1.1 row **flips to DONE via the written-decision branch**
      (2026-09-02). Leg A fails the ≥2/3 measured bar, so the closure rests on
      the written decision that S-3's non-default-tab symptom is out of scope for
      the browser-driving agent, with the miss documented and owned and the
      detector retained for any future layer that materializes all views.

## Non-goals

- Changing the seed (the ninth pass showed it was never the problem).
- Re-litigating the carve-out (already withdrawn on measurement).
- Expanding the `STATUS_WORDS` vocabulary to chase a miss — a missed value is a
  vocabulary gap to close deliberately, not by stretching the rubric.
