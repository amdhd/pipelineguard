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

The `screenshots/…` URLs inside point at the private reports bucket, and the
objects behind them have expired. The JSON is otherwise a verbatim record of a
real run -- with one deliberate edit, in both fixtures.

**The query strings were stripped, and must stay stripped.** As emitted, these
URLs were *presigned*: `_presign` in `agents/qa/agent/agent.py` signs each
screenshot so a PR reviewer can open evidence in a block-public-access bucket,
so every URL carried `AWSAccessKeyId`, `Signature`, and the runtime role's
`x-amz-security-token`. Committing that verbatim into a public repo published
the QA runtime's STS session tokens; secret scanning flagged it. The signature
is scoped to one object and dies at `Expires`, and a presigned URL never carries
the secret access key -- so the blast radius was one GET per link, for a few
hours -- but it is credential material and does not belong in git. What is left
is the bare object URL: it still identifies the evidence, and it 403s for
everyone.

Reports archived after that PR no longer need this: `_archive` strips presigned
URLs before writing, so a fixture pulled fresh out of S3 arrives clean. The rule
survives for anything older -- if you refresh a fixture from a report predating
that change, strip the query string before committing.

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
