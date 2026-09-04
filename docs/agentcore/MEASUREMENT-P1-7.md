# MEASUREMENT-P1-7 — the false-positive rate over real PRs (D-5)

**Status: READY — runway dry-run executed 2026-09-04 on the archived pr-129
artifacts; 0 of 3 real PRs labelled yet. The toolchain is proven; what remains
is human.**

AUDIT.md calls this **P1.7** ("Collect 3 human-labelled PRs for a real
false-positive rate", row status `ongoing`); BENCHMARK-DELTA.md calls it **D-5**
("3 real human PRs → the FP rate"). Same item, two names.

## WHY

Phase 1's false-positive rate is the number that decides whether the agent's
comment is worth reading. It is currently **unproven on real PRs** — the seeded
corpus measures false *negatives* (S-1..S-4, where the answer is known because
you planted the bug); it cannot measure false *positives*, because a run that
cries wolf on healthy code and a run that reports nothing are indistinguishable
without a human who knows the code.

Only a human can label a finding true-positive / false-positive / by-design —
deciding whether something is a defect is exactly the judgement the agent is
imitating, so a scorer that decided it automatically would be marking its own
homework (`agents/qa/harness/score.py` docstring).

**What is NOT a valid sample:** agent-authored fix PRs (e.g. vesselAI #134) and
scratch demo PRs (e.g. #129). Corpus semantics exclude both — the PRs that feed
this rate must be written by humans and reviewed by a human who labels the
agent's findings on them. See BENCHMARK-DELTA.md §4.

## Gates to check before starting

- [x] `score.py --findings/--corpus/--board` path works end-to-end (dry-ran
  2026-09-04 on `s3://pipelineguard-qa-dev-reports-149751500899/reports/pr-129/latest/`
  — board parsed, unlabelled rows correctly excluded, a corpus label flips
  "not measured" into a computed rate).
- [x] `corpus.json` labels are human-authored and current (they are; the seeds
  are untouched by the dry run).
- [ ] Three real, human-authored PRs on amdhd/vesselAI main carrying the
  `agent-qa` label.

## Procedure — per real PR (the human part is ~2 minutes)

1. **Open a real PR** the way you would anyway (a fix, a feature, a refactor you
   actually want). Add the `agent-qa` label. The QA job runs and posts a
   comment (label gate in `ui-qa-agent.yml`).
2. **Read each finding row in the comment** and decide, one per row:
   - `true-positive` — a real defect, worth fixing.
   - `false-positive` — not a defect; the agent misread the page. This usually
     means a rubric problem.
   - `by-design` — not a defect by the app's own documentation, but the agent
     reported it anyway. This means the rubric has fallen out of sync with the
     target. (Counts as a false positive for the RATE — the reviewer's time went
     the same way either way — but stays a separate label because the fix is
     different.)
3. **Record the label** in `agents/qa/harness/corpus.json` under `labels`. The
   key is the finding's stable fingerprint. Two ways to get the exact string:
   - the finding's own `/page::SEVERITY::summary` fingerprint, or
   - run `score.py --findings <run findings.json>` and copy from the
     *awaiting review* list it prints (it prints the normalized form — copy
     verbatim; a re-typed key silently never matches).
   ```json
   "labels": {
     "/page::SEV::the summary exactly as score.py prints it": "true-positive"
   }
   ```
4. **Optional but ideal on a fix-chain board:** when the finding came back
   through a D-4 board row (status `fixed`, `label` null), add the human label
   to that board row instead. Board labels join the corpus at scoring time and
   an existing corpus label always wins (`score.py` `effective_corpus`).

## What "done" looks like

- [ ] Three real, human-authored PRs each got a QA run and every finding on
  them got a human label in `corpus.json`.
- [ ] For the three PRs, the FP rate is computed:
  ```
  .venv/bin/python agents/qa/harness/score.py \
    --findings reports/pr-<n>/latest/findings.json \
    --corpus agents/qa/harness/corpus.json
  ```
- [ ] The three rates (or one aggregate) are recorded in `AUDIT.md` under the
  P1.7 row and in `EVIDENCE.md` alongside the FN record, so Phase 1 criterion 4
  is quoted from measurement rather than asserted.

An unlabelled finding is counted as unlabelled, never as correct — `score.py`
prints `not measured` until every finding on the sample has a label. A rate
over a partially-labelled run is not a rate.

## Honesty guards

- Never label a PR you (or the agent) authored *and* then count it in this
  sample. Genuinely-human only.
- Scratch/demo branches (pr-129 and friends) never count; they are for
  demonstrating the loop, not for measuring it.
- If a real PR is clean (agent finds nothing), that PR still counts toward the
  three — a clean run on healthy human code is exactly what the false-positive
  question needs, and it costs one label each: "nothing to label".
