# Price changes (prices with a start date)

Since 0.5.0, `prices.json` can hold several prices for the same model, each one with the day it starts. When Anthropic changes a price, you add the new one with its date and the spend you already had keeps its old price.

## How it works
claude-spend never stores a frozen cost: the USD columns are recalculated from the tokens every time you open the report. Before 0.5.0 that meant that changing a price in `prices.json` changed the whole history too.

Now each record keeps its tokens split by **UTC day** (`days` inside each model in the log), and every day is charged with the price that was in effect that day. Adding a new price with a later start date therefore does not change the cost of anything used before that date.

## The `prices.json` format
A model's entry can be one of two things:

- **A plain object**, as before: a single price that always applies. Existing `prices.json` files keep working without changes.
- **A list of periods.** Each period is a price object with a `"from"` day.

Example with two periods:

```json
{
  "_price_date": "2026-12-01",
  "claude-opus-5-5": [
    {"input": 5.0, "output": 25.0, "cache_read": 0.25, "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    {"from": "2026-12-01", "input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write_5m": 5.0, "cache_write_1h": 8.0}
  ],
  "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write_5m": 1.25, "cache_write_1h": 2.0}
}
```

Here Opus 5.5 usage up to 2026-11-30 (UTC) costs 5.0 per million input tokens, and usage from 2026-12-01 on costs 4.0. Haiku 4.5 has a single price.

Rules:

- `"from"` is a day in `YYYY-MM-DD` format, in **UTC**, and **inclusive**: the period starts at 00:00 UTC that day.
- Each period lasts until the `"from"` of the next one; the last one has no end. The order in the file does not matter: periods are sorted by `"from"`.
- Only one period may leave out `"from"`. It becomes the first one and applies since always.
- `tiers` (a different price for long prompts) go inside each period, and can be different in each one.
- If a model's entry is invalid (a date that is not `YYYY-MM-DD` or does not exist, two periods with the same `"from"`, more than one period without `"from"`, an empty list or a period that is not an object), that model is ignored: it counts as having no price (0 USD) and the report explains the problem under the tables. The other models are not affected.

## Adding a price change
1. If you don't have your own `prices.json` yet, copy the bundled one to `~/.claude/claude-spend/prices.json` (see the README).
2. Find the model and turn its object into a list: keep the current price as the first element, without `"from"`.
3. Add the new price as a second element, with `"from"` set to the day the change took effect.
4. Update `"_price_date"`.

Don't overwrite the old price: if you do, the old usage is recalculated with the new one.

## Old records and `--import`
- Records written before 0.5.0 have no split by day. Each one is charged as a whole with the price in effect on the day it ended (UTC day of `end`). For a session that crossed a price change, that is an approximation.
- `claude-spend --import` reads the transcripts that are still on disk again and writes new records with the split by day, which replace the old ones in the report (nothing is counted twice). Transcripts that Claude Code has already deleted keep the approximation.
- The log is never rewritten: it stays append-only.

## Limits
- **UTC, by day.** A change is placed on a UTC day, not at an exact time. Usage in the hours around midnight UTC of the change day may be charged with the price of the neighbouring period. It is fine for an estimate.
- **Usage before the first period.** If every period has a `"from"` and there is usage before the earliest one, that usage is charged with the oldest period.
- **Tiers are fixed when recording.** Whether a request falls in a tier is decided when it is recorded, with the tiers of that day's period. If you add or change a tier of a past period, run `claude-spend --import` again (it only helps for transcripts still on disk).
- **The "Spend by day" table and `--since`** still place each record on the day it ended, even though its USD is calculated day by day.
- The bundled `prices.json` keeps one plain price per model: it does not include past price changes.
