# Review — `ui-qa-agent.yml` (lives in `amdhd/vesselAI`)

The workflow that invokes the QA agent lives in the target repo, not this one.
That is the right place for it — it needs no cross-repo token because the app
and the workflow ship together — but it means **the control that protects this
repo's IAM role is defined somewhere this repo cannot see.**

That is worth writing down rather than assuming, because of what the role
unlocks. `pipelineguard-qa-dev-github` is assumable by a workflow in a **public**
repo, and the trust policy **cannot** distinguish a fork PR from a same-repo
one: GitHub mints the token with `sub = repo:amdhd/vesselAI:pull_request` for
both. The enumerated subject list is doing less work than it looks like it is.
The guard is in the workflow file, so this file records that it was checked.

**Reviewed against `main`, 2026-08-27.**

---

## Verified present

Each of these was checked in the file itself, not inferred from the plan:

| Control | Status |
|---|---|
| **Fork guard** — `github.event.pull_request.head.repo.fork == false` on the credentialed job | ✅ present |
| `agent-qa` label gate on PR runs | ✅ present |
| `QA_SCHEDULE_ENABLED` kill switch, cron defaults off | ✅ present |
| Workflow-level `permissions:` — `id-token`/`contents: read`/`pull-requests: write`, no push | ✅ present |
| `concurrency` keyed on `event_name` *and* `ref` | ✅ present |
| Every `uses:` pinned to a commit SHA | ✅ present |
| `paths:` filter scoped to `frontend/**`, `backend/**` | ✅ present |
| Health gate against the **tunnel**, asserting JSON, a seeded login and an authenticated read | ✅ present |
| `timeout-minutes: 25` job backstop | ✅ present |
| Teardown in `always()` | ✅ present |
| Unique session label — `gh-${{ github.run_id }}` | ✅ present |

**The critical control holds.** The role is safe to keep deployed.

One consequence to keep in view: a `pull_request` workflow runs the workflow file
**from the PR's own merge ref**, so a fork PR could edit the guard out of the
copy that runs. What actually stops that today is GitHub capping fork-PR
permissions at read on a public repo, so `id-token: write` is never granted and
no OIDC token is minted. That is a **platform default, not a control this
project owns**, and the admin setting that lifts it applies to private repos —
which the plan contemplates elsewhere. Inheriting a protection while documenting
it as one you enforce is how this becomes a real finding later.

---

## Defects found

Six, none of them the critical control. Ordered by what I would fix first.
Defect 6 was found by actually running the workflow rather than by reading it.

### 1. Script injection via `workflow_dispatch` input — MEDIUM

```yaml
${{ inputs.model && format('--model {0}', inputs.model) || '' }}
```

interpolated directly into a `run:` block. `${{ }}` is substituted *before* the
shell sees the script, so the input is not an argument — it is source code, in a
job holding `id-token: write`. Only someone with write access can dispatch the
workflow, which caps the blast radius, but this is the textbook Actions
anti-pattern and it sits in a repo whose whole argument is least privilege.

**Fix:** pass through `env:` and let the shell quote it.

```yaml
        env:
          QA_MODEL: ${{ inputs.model }}
        run: |
          python .pipelineguard/agents/qa/harness/main.py \
            ${QA_MODEL:+--model "$QA_MODEL"} \
            ...
```

### 2. The tunnel binary is unpinned, and executed — MEDIUM

```yaml
curl -fsSL -o cloudflared .../releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared && ./cloudflared ...
```

The file SHA-pins **every** action with the argument that "tags are mutable; a
compromised or retagged action runs inside a job holding `id-token: write`" —
and then downloads an unpinned 40 MB binary from `latest` and executes it in
that same job. The reasoning applies with more force to the binary, not less.

Cloudflare publishes no checksum asset, so pin the version and record a digest
you computed yourself. Verified for `2026.8.2`:

```yaml
env:
  CLOUDFLARED_VERSION: "2026.8.2"
  CLOUDFLARED_SHA256: "fcfb02b575a52ca1af2e3267af4e1517bcdeb30ac48c834c69abaed3c0576ad2"
run: |
  curl -fsSL -o cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
  echo "${CLOUDFLARED_SHA256}  cloudflared" | sha256sum -c -
  chmod +x cloudflared
```

### 3. `--runner-minutes` is never passed — blocks an exit criterion

Phase 1's exit criteria require a comment carrying "token cost, session seconds,
**and runner minutes**". The harness renders that row only when the flag is
given, and the workflow does not give it, so criterion 1 cannot currently be
met. Runner minutes are also the one figure the agent cannot know for itself.

**Fix:** stamp the start, compute at report time.

```yaml
      - name: Mark start
        run: echo "JOB_STARTED=$(date +%s)" >> "$GITHUB_ENV"
      # ... then, on the harness invocation:
            --runner-minutes "$(( ( $(date +%s) - JOB_STARTED + 59 ) / 60 ))" \
```

### 4. The tunnel URL is written into the job log — LOW, but it is a stated rule

```yaml
echo "::notice::target $URL"
```

PLAN.md 1e: no reason to write the tunnel URL "anywhere durable — not into the
PR comment, not into the findings JSON, **not into logs**." A `::notice::` on a
public repo is all three of durable, public and indexed. The tunnel dies with
the job so the link is stale within minutes, but the rule is this project's own.

**Fix:** `echo "::notice::tunnel is up"`. The URL is already in `$GITHUB_ENV`
for the steps that need it.

*(The PR-comment half of this rule was being broken by the harness, in this
repo. Fixed — `report.render` no longer takes or prints the target URL.)*

### 5. The health gate hardcodes credentials the agent reads from Secrets Manager — LOW

```yaml
        env:
          QA_EMAIL: demo@petronas.com
          QA_PASSWORD: demo123
```

These are published in the target's README, so this is not an exposure. It is a
**drift** risk: the gate proves that *these* credentials work while the agent
logs in with whatever `pipelineguard/qa-agent/dev` holds. Rotate the secret and
the gate goes on passing while every agent run fails to authenticate — and the
agent's own auth probe reports that as "never authenticated", pointing the
reader at the login page rather than at the secret.

**Fix:** read the secret in the gate. The CI role already has
`GetSecretValue` on exactly that ARN — the plan justifies the statement on the
grounds that "the health gate has to prove a real login works", which is only
true once the gate uses it.

```yaml
run: |
  creds=$(aws secretsmanager get-secret-value --secret-id "${{ vars.QA_SECRET_ARN }}" \
    --query SecretString --output text)
  QA_EMAIL=$(echo "$creds" | python3 -c 'import json,sys; print(json.load(sys.stdin)["QA_TARGET_EMAIL"])')
  QA_PASSWORD=$(echo "$creds" | python3 -c 'import json,sys; print(json.load(sys.stdin)["QA_TARGET_PASSWORD"])')
```

### 6. The label gate cannot start a run on an existing PR — MEDIUM *(found by running it)*

```yaml
  pull_request:
    paths: [...]        # no `types:`
```

With no `types:`, `pull_request` defaults to **`opened`, `synchronize`,
`reopened`** — and **not `labeled`**. So adding `agent-qa` to a PR that already
exists does nothing, and the label that 1d calls "the single most effective cost
control" cannot actually *start* anything.

This is not theoretical. Opening PR #95 with `--label agent-qa` produced a
**skipped** job: the label was attached moments after creation, so the `opened`
payload carried no labels and the `if:` evaluated false. It took an empty commit
to trigger a run.

The failure mode is quiet in the worst way — a skipped job is green, so the
reviewer sees a passing PR and concludes the agent found nothing, when the agent
never ran.

**Fix:** declare the types explicitly.

```yaml
  pull_request:
    types: [opened, synchronize, reopened, labeled]
    paths:
      - "frontend/**"
      - "backend/**"
```

`labeled` fires on *any* label, so the existing `contains(...'agent-qa')` guard
is what keeps unrelated labels from starting a paid run — it is already there
and already correct.

---

## Not defects — checked and deliberate

- **`PIPELINEGUARD_REF: main`** checks the harness out from a mutable ref. That
  is the same class of risk as an unpinned action, and it is the right call
  anyway: the harness and the agent share `schema.py`, and a pinned copy that
  fell behind would accept findings the deployed agent can no longer produce.
  Pin it the day the two repos need to release independently, not before.
- **`continue-on-error: true` on the agent step**, with the verdict applied at
  the end, is what lets the comment be posted before the job fails. Removing it
  would lose the report on exactly the runs that most need one.
- **The comment-not-found fallback step** exists because a real run failed on
  `cat: No such file`, three steps from the actual cause. Keep it.
