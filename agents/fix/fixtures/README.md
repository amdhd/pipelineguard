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
