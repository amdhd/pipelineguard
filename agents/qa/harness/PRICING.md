# Price table

The harness reports **units** (tokens, seconds) and **dollars** separately, on
purpose. Units are measured and always correct. Dollars come from
`prices.json`, and a price table goes stale.

An unknown model renders as **`unpriced`**, never `$0.00`. A silent zero reads
as "this run was free" when it actually means "nobody told me the price" —
which is the sort of quietly wrong number a FinOps project should not ship.

## Where the model prices come from

**Not from the AWS Price List API, and the reason is structural.** This file
used to say the API "does not currently expose" current-generation Anthropic
models, and left it there as if it might turn up later. It will not, and the
actual cause tells you where to look instead.

Re-verified 2026-08-27:

```bash
aws pricing get-attribute-values --region us-east-1 \
  --service-code AmazonBedrock --attribute-name model
```

still returns only `Claude 2.0`, `Claude 2.1`, `Claude 3 Haiku`,
`Claude 3 Sonnet`, `Claude Instant`. So does the bulk offer file — and that is
the telling part, because the bulk file is not sparse:

```bash
curl -s --compressed \
  https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/us-east-1/index.json \
  | python3 -c "import json,sys; print(sorted({v['attributes'].get('model','?') for v in json.load(sys.stdin)['products'].values()}))"
```

lists **80 models** — Nova 2.0, Grok 4.6, Qwen3, Llama 4, Mistral Large 3,
DeepSeek, GLM 5 — and, of Anthropic's, only the same five legacy entries. The
`ap-southeast-1` file carries no Anthropic model at all.

**Current-generation Claude on Bedrock is billed through AWS Marketplace**, not
through the Bedrock service meter, which is why it is absent from a service
price list that is otherwise complete. The authoritative price is therefore the
**AWS Marketplace listing for the model**, and every Bedrock model card prints
the Marketplace product ID needed to find it.

### Refreshing a price

1. Open the model card:
   `https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-<model>.html`
   Take the **Marketplace product ID** and the **Global inference ID**. The
   inference ID is the key in `prices.json` — it must match the `modelId` the
   agent actually invokes, or the run reports `unpriced`, which is the intended
   failure rather than a wrong number.
2. Open that product's AWS Marketplace listing and read the **Standard Context**
   row of its pricing table.
3. Write the figure into `prices.json` with `read_on`, the `tier` it applies to,
   and a `source` naming where it came from.

### Provenance of the two entries, stated plainly

The two rungs are not equally well sourced, and the difference is recorded in
each entry's `source` field rather than smoothed over:

| Rung | Price | How it was read |
|---|---|---|
| `global.anthropic.claude-sonnet-4-6` | $3.00 / $15.00 per MTok | **Directly**, from the Marketplace listing's own pricing table (`prodview-o6w4hyizv7g64`), Standard Context, scope Global |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` | $1.00 / $5.00 per MTok | **Indirectly.** The Haiku listing page was not reachable; the figure comes from a published Bedrock price table quoting $0.00100/1K in and $0.00500/1K out for global cross-region inference, and is corroborated by Sonnet 4.6 billing at exact parity with Anthropic's first-party rate |

Re-read the Haiku figure from its Marketplace listing when you next have console
access, and drop the qualifier here once you have.

Two things that change the number and are **not** modelled:

- **Geo inference is not priced the same as global.** These entries are for the
  `global.` profiles, which is what `infra/modules/qa_agent/variables.tf` grants
  and what the agent invokes. A `jp.` profile, for one, bills about 10% more.
- **Prompt caching is not modelled**, because the agent does not use it. Bedrock
  supports it on both rungs (4 checkpoints; 4,096 minimum tokens per checkpoint
  on Haiku 4.5, 1,024 on Sonnet 4.6). Since every turn re-sends the whole
  history, a cache point is the single largest cost lever available here -- and
  the moment it is added, cache-read and cache-write rates become two more
  meters this table has to carry.
## Compute meter

`compute` covers the AgentCore vCPU/GB meter, which **both** the runtime session
and the browser session bill against — so a run is charged two concurrent
sessions for roughly the same wall-clock, not one.

Memory bills for every second a session is alive **including idle**. That is why
the agent enforces a wall-clock deadline rather than only a token budget: a
wedged run costs money while generating nothing.

CPU is consumption-based, so `compute_cost_usd` assumes full vCPU utilisation
and therefore **over**-estimates. Deliberate — a cost report that flatters itself
is worse than one that does not.

## Overriding

`QA_PRICES_PATH` points at a different file. A missing file is not an error: the
harness still reports units and marks dollars unpriced.
