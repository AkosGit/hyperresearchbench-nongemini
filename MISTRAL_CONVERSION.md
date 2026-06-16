# Mistral conversion + fork-vs-original benchmark — handoff

The RACE + FACT evaluation now runs on **Mistral** instead of Gemini, and the harness can benchmark **your fork vs. the original** hyperresearch on a chosen set of queries.

## What changed

| File | Change |
|---|---|
| `DeepResearch-Bench/patches/api_mistral.py` | **New.** Mistral-backed drop-in for the upstream `utils/api.py`. Adds a `mistral` backend (base `https://api.mistral.ai/v1`), `RACE_MODEL=mistral-large-latest`, `FACT_MODEL=mistral-small-latest`. Uses `max_tokens` (not `max_completion_tokens`), drops `reasoning_effort`, normalizes `model_length`→`length`. Retries on `429`/`5xx` with backoff (honors `Retry-After`) + optional client throttle, so rate limits never drop a RACE score or FACT citation. Preserves the full surface (`AIClient`, `call_model`, `scrape_url`, `Model`, `FACT_Model`). |
| `DeepResearch-Bench/setup.sh` | Copies the Mistral client over the cloned upstream's `utils/api.py`; drops `google-generativeai`; Mistral-oriented next steps. |
| `DeepResearch-Bench/grade.sh` | Rewritten to drive the **real** upstream interface — RACE via `deepresearch_bench_race.py`, FACT via `utils.extract → deduplicate → scrape → validate → stat`. Requires `MISTRAL_API_KEY`; `JINA_API_KEY` for FACT. (The old script called nonexistent `race.eval`/`fact.eval` modules.) |
| `DeepResearch-Bench/compare.sh` | **New.** Installs each version globally, generates the reports, grades with Mistral, prints a side-by-side fork-vs-original table + verdict. `--fresh` wipes prior run dirs so every report starts from an empty vault/DB. |
| `DeepResearch-Bench/patches/verify_mistral.py` | **New.** Offline self-test (no API calls). |
| `README.md` | Updated for Mistral + the comparison workflow. |

Models chosen per your selection: **RACE = `mistral-large-latest` (Large 3), FACT = `mistral-small-latest` (Small 4)**. Override anytime with `RACE_MODEL` / `FACT_MODEL`.

## Run it on your Mac (2-report comparison)

```bash
cd hyperresearchbench-nongemini/DeepResearch-Bench
bash setup.sh

export MISTRAL_API_KEY=HFp7Z75kROZDaMxJrZ4zdhEdf9iY6f2r
export JINA_API_KEY=<your-jina-key>      # FACT citation scraping; omit + use --skip-fact

# Benchmark your fork vs. jordan-gibbs/hyperresearch on queries 67 + 52
# --fresh wipes prior run dirs; --free-tier throttles to 1 req/s for the Mistral free plan
bash compare.sh /Users/akos/Documents/GitHub/hyperresearch --queries "67 52" --fresh --free-tier
```

On the Mistral **free tier** (1 req/s, 1B tokens/month) one comparison is only a few million tokens — well within quota. The client retries on `429` with backoff so nothing is dropped; `--free-tier` (`N_WORKERS=1` + `LLM_MIN_INTERVAL=1.1`) keeps you under the rate limit and avoids wasted retries.

This generates 4 full-tier reports (Q67 + Q52 × fork + original), grades each, and prints the RACE/FACT delta. **Cost ≈ $240–480, ~6–10 h** (the generations, not the Mistral grading). Add `--skip-fact` if you don't have a Jina key; add `--yes` to skip the confirmation prompt.

To grade reports you already have (no generation):

```bash
bash grade.sh <run-name> --only-en        # reads results/<run-name>.jsonl
```

## Verified offline (in this session)

- `patches/verify_mistral.py` — payload shape, base URL, auth, model routing, finish-reason normalization. ✓
- End-to-end: the **real** upstream RACE scorer (`process_single_item` → `extract_json_from_markdown` → `calculate_weighted_scores`) run through the actual `AIClient` against a localhost mock returning a valid Q67 scoring JSON → correct normalized score (0.5714 for an 8-vs-6 grading). ✓
- Mistral client imports cleanly into the real upstream package; RACE CLI accepts exactly the args `grade.sh` passes; FACT-chain modules import. ✓
- `bash -n` on all three scripts. ✓

## Constraints worth knowing

- **Couldn't execute the real run here.** This environment's proxy blocks `api.mistral.ai` (and all non-Anthropic LLM endpoints), command calls cap at 45 s, and report generation needs unrestricted web crawling + a `hyperresearch` install — so the actual scoring must run on your Mac. The code is verified against the real upstream with the network mocked.
- **Fork vs. original is only meaningful on freshly generated reports.** The committed Q67 example report is byte-identical between your fork and the original; the fork's "accuracy levers" only show up in new runs — which `compare.sh` produces.
- **Re-runs reuse stale vault state unless you pass `--fresh`.** Each query generates in its own dir with its own SQLite vault DB (no cross-contamination within a clean run), but the harness reuses an existing run dir rather than wiping it. On a re-run or after an interrupted query, leftover sources/notes/drafts in that DB carry over. `--fresh` wipes `runs/<label>/` + the results JSONL (and forces re-grading) so every report starts empty.
- **The provided Mistral key is in plaintext above.** Rotate it if this repo is shared.
