# Fix-harness fixtures

## `findings-33140664097.json`

The negative-check run of 2026-08-28 (`main`, healthy, sonnet) — recorded in
`docs/agentcore/EVIDENCE.md` under the fifth pass. Seven candidates fired and
every one was refuted; one real finding survived.

It is here because it is **Phase 2's first input**, and because the reports
bucket expires everything after `report_retention_days` (7). Left in S3 it would
have stopped existing on ~2026-09-04, and the smoke test would have had no
source. A fixture in the repo is a reference that keeps resolving.

Two properties make it the right first target, and one makes it the wrong thing
to tune on:

- **F-001 is real and reproducible.** `{greeting}, Captain {firstName}` against
  a seeded name of *"Captain Ahmad Fauzi"* renders "Good afternoon, Captain
  Captain". Trivially patchable, in `frontend/src/**`, inside the allow-list.
- **`suspected_source` is `null`** — confirmed, not assumed. The rubric tells the
  agent to prefer null over a guess, so the first smoke test exercises the
  **grep fallback** in file selection, not the happy path. That is worth knowing
  before reading the result.
- **The correct patch is ambiguous.** The template's hardcoded "Captain" may be
  wrong, or the name split may be; nothing in vesselAI's tests encodes which.
  Judge a run against this fixture on whether the loop worked end to end, never
  on whether the agent chose right.

The `screenshots/…` URLs inside point at the private reports bucket. They are
unsigned, they 403 for everyone, and the objects behind them have expired. They
are kept because the JSON is a verbatim record of a real run, not an edited one.

## `findings-33137979741.json`

**Two findings, which is the point.** Every Phase 2 result so far rests on the
one-finding fixture above, and the defect fixed in PR #33 -- a single failing
finding discarding every good patch -- was invisible to it by construction. A
multi-finding input is the cheapest way to find what else is hiding.

A real run against `main` on 2026-08-28, not a synthetic file:

| id | severity | page | what |
|---|---|---|---|
| F-001 | MEDIUM | `/sire` | SIRE readiness shows 0 GREEN chapters |
| F-002 | LOW | `/` | the "Captain Captain" greeting duplicate |

Both carry `suspected_source: null`, so both exercise the grep fallback.

**F-002 is expected to fail to apply, and that is a feature of this fixture.**
The greeting defect was fixed on `main` by the agent's own PR
(amdhd/vesselAI#102), so the text an edit would quote is no longer there. A run
against current `main` should therefore report F-002 as skipped with
`old_string not found` -- an honest "already fixed" -- while F-001 is attempted
normally. That is exactly the partial-run shape PR #33 rewrote `exit_code` to
survive, so this fixture tests the fix rather than merely the happy path.

Pulled out of S3 before `report_retention_days` (7) expired it, same as the
fixture above.
