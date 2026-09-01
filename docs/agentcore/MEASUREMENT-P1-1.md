# P1.1 measurement — does the rubric carve-out close S-3?

**Status: DRAFT — the runbook for measuring PR #49.** This is not a claim. Recall
is a *measured* number; this document is how the measurement gets taken.

PR #49 (`fix/s3-recall-rubric-carveout`) narrows the rubric's fixture-surface
exemption so S-3's render breaks — a raw `"OPEN"` leaking into the text, the `0
open` badge while the rows are on screen, the lost `text-status-red` — are
reportable even on a fixture surface. Whether that actually lands S-3 at 3/3 —
without a false-positive spike — is what this measures.

## Gates to check before running

- [ ] **PR #49 merged into pipelineguard `main`.** The vesselAI workflow checks
      the harness out at `PIPELINEGUARD_REF: main`, so the new rubric is only
      live once the PR is on `main`. Dispatching against the PR branch would
      measure the old rubric.
- [ ] **`agents/qa/harness/corpus.example.json` is current.** The S-3 seed row
      must read `"keywords": ["status", "open finding", "colour", "color",
      "styling", "uncategorised"]` with `"page": "/sire"`. The ninth pass
      corrected the seed's description; the corpus file is what `score.py`
      matches against, so verify it against the seed's full diff first.

## The two legs

The carve-out has two failure modes, each measured separately.

### Leg A — recall (S-3 becomes reportable)

Three `workflow_dispatch` runs on vesselAI `qa-corpus-1` at the **default
(sonnet) rung** — the rung the corpus recall is stated at (EVIDENCE criterion 6).

```bash
# from anywhere with gh authenticated to amdhd/vesselAI
for i in 1 2 3; do
  gh workflow run ui-qa-agent.yml \
    --repo amdhd/vesselAI \
    --ref qa-corpus-1 \
    --field model=global.anthropic.claude-sonnet-4-6 \
    --field max_routes=8
done
```

**Getting `findings.json` out.** The workflow writes it in the runner but never
uploads it (no `actions/upload-artifact`), and the PR comment is a rendered
summary, not the raw JSON `score.py` needs. Two options:

- **Recommended:** add an `actions/upload-artifact` step for `findings.json` to
  `ui-qa-agent.yml` (a small vesselAI PR) *before* running, so each run's raw
  findings survive.
- **Without it:** the run's step summary (`Report outside a PR` prints
  `comment.md`) shows the findings detail, but scoring against the corpus needs
  the JSON. Do not hand-score from the comment — that is exactly the "written
  before it is measured" failure the audit forbids.

Scoring each run:

```bash
.venv/bin/python agents/qa/harness/score.py \
  --findings findings.json \
  --corpus agents/qa/harness/corpus.example.json
```

**Pass = S-3 detected in ≥2 of 3 runs** (combined recall ≥8/9 with S-1 3/3 and
S-2 2/3 standing — and re-check S-1/S-2 did not regress). Record each run's
turns, wall, cost, and which seeds landed, in the EVIDENCE eleventh-pass entry.

### Leg B — false positives (the carve-out did not reopen the floodgates)

The exemption now names three render-break shapes as reportable on *any* fixture
surface. The risk is that the agent reports the seeded statistical anomalies the
exemption exists to suppress. One run on **healthy `main`** (no seeds) and label
every finding:

```bash
gh workflow run ui-qa-agent.yml \
  --repo amdhd/vesselAI \
  --ref main \
  --field model=global.anthropic.claude-sonnet-4-6 \
  --field max_routes=8
```

**Pass = 0 findings on `/sire` (and the other fixture surfaces).** A finding that
says "the numbers look odd" is a regression from the carve-out and must go back
to the rubric. (EVIDENCE criterion 4's last measured healthy-`main` run was
already 0 FPs; this confirms the carve-out kept it.)

## What "done" looks like

- [ ] Three corpus runs scored; S-3 ≥2/3, S-1 and S-2 not regressed.
- [ ] One healthy-`main` run with 0 false positives on fixture surfaces.
- [ ] Eleventh-pass entry in `EVIDENCE.md` with run IDs, tables, and verdicts.
- [ ] Then — and only then — `AUDIT.md` P1.1 row flips to **DONE** per its
      acceptance criterion (≥8/9 *measured*, or a written decision).

## Non-goals

- Changing the seed (the ninth pass showed it was never the problem).
- Re-litigating PR #49's wording — if Leg B fails, the rubric goes back, but the
  question this runbook answers is whether it works as written.
