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

One thing that changes the number and is **not** modelled:

- **Geo inference is not priced the same as global.** These entries are for the
  `global.` profiles, which is what `infra/modules/qa_agent/variables.tf` grants
  and what the agent invokes. A `jp.` profile, for one, bills about 10% more.
## Prompt caching — four meters, not two

The agent places one rolling cache checkpoint at the end of its history, so
input now arrives on **three** meters: uncached, read-from-cache, and
written-to-cache. Each entry therefore carries `cache_read_usd_per_mtok` and
`cache_write_usd_per_mtok`, and `model_cost_usd` charges all four rates.

Getting this wrong is not a rounding error, and this is now measured rather
than modelled. Two real runs on 2026-08-27 (agent runtime v7, 8 routes each)
served **74% and 82% of their input from cache**:

| Run | Uncached in | Cache read | Cache write | Out | Total |
|---|---|---|---|---|---|
| clean `main` | 8,304 | 53,657 | 10,485 | 1,145 | **$0.03** |
| seeded corpus | 8,301 | 90,647 | 12,213 | 1,754 | **$0.04** |

Price those cache reads at the full input rate and the bill reads ~4x high;
treat them as free and it reads low. Both are worse than the arithmetic here.

Note also what the measurement corrected: an earlier *simulation* of the same
8-route run assumed 32 turns and 390 seconds and put a run at $0.23. The real
agent batches tool calls, finishes in 11–15 turns and under 50 seconds, and
costs about a tenth of that. The token-growth model in `agent.py` is still the
right shape for sizing a budget — it is deliberately an upper bound — but it is
not a cost forecast.

An entry that carries base rates but no cache rates falls back to the documented
multipliers — **0.1x input for a read, 1.25x for a 5-minute write** — rather
than to zero. Those are not assumed: Sonnet 4.6's published cache rates
($0.30 and $3.75 against a $3.00 input rate) are exactly 0.1x and 1.25x, which
is what licenses using them for Haiku 4.5 until its own listing is readable.

`cache_min_tokens_per_checkpoint` is recorded per entry because it changes
behaviour rather than price: **Haiku 4.5 needs 4,096 tokens before a checkpoint
caches at all** (Sonnet 4.6 needs 1,024). A checkpoint under the minimum is not
an error — inference succeeds and simply does not cache — which is why the first
turns of a Haiku run cost what they always did and only the later ones get cheap.

The **1-hour TTL is not used.** It costs more to write ($6.00/M on Sonnet 4.6 vs
$3.75) and buys nothing here: turns are seconds apart, and a 5-minute cache
refreshed inside its own window is free to keep alive.

Also worth knowing, because it is invisible when it breaks: `pricing.summarise`
reports a **cache hit rate**, and the PR comment calls out a run that wrote to
cache and never read back. That means something volatile is changing the prompt
prefix between turns, and it costs several times what the run should.

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
