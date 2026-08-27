# Price table

The harness reports **units** (tokens, seconds) and **dollars** separately, on
purpose. Units are measured and always correct. Dollars come from
`prices.json`, and a price table goes stale.

An unknown model renders as **`unpriced`**, never `$0.00`. A silent zero reads
as "this run was free" when it actually means "nobody told me the price" —
which is the sort of quietly wrong number a FinOps project should not ship.

## Why `models` is empty

It is not an oversight, and it should not be filled in from memory.

The AWS Price List API does not currently expose the current-generation
Anthropic models for `ap-southeast-1`. Verified by paginating both:

```bash
aws pricing get-attribute-values --region us-east-1 \
  --service-code AmazonBedrock --attribute-name model
```

which returns only `Claude 2.0`, `Claude 2.1`, `Claude 3 Haiku`,
`Claude 3 Sonnet`, `Claude Instant`; and the full regional product list, which
carries no `Haiku4-5` / `Sonnet4-5` usage types.

So the two benchmark rungs must be entered by hand from the Bedrock pricing
page, **with the date they were read**:

```json
"models": {
  "global.anthropic.claude-haiku-4-5-20251001-v1:0": {
    "input_usd_per_mtok": 0.00,
    "output_usd_per_mtok": 0.00,
    "read_on": "YYYY-MM-DD"
  }
}
```

Until then every run reports exact token counts and an explicitly incomplete
total. That is the intended degraded state.

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
