# hyperresearchbench

**Reproduction harness for hyperresearch's [DeepResearch-Bench](https://github.com/Ayanami0730/deep_research_bench) score — RACE + FACT scored on Mistral.**

Runs the 100-query DRB benchmark against [hyperresearch](https://github.com/jordan-gibbs/hyperresearch) on Claude Code, full tier (16-step pipeline with adversarial review), Opus 4.7. Outputs a JSONL file, then grades it with the upstream RACE + FACT evaluator **driven by Mistral models** (not Gemini).

It also ships a `compare.sh` driver that benchmarks **your fork of hyperresearch against the original** on a chosen set of queries and diffs the scores.

## Stack

### Generation (hyperresearch under test)

| Layer | Model | Role |
|---|---|---|
| Orchestrator | Opus 4.7 | Tier classification, pipeline routing, synthesis planning |
| Critics (4×) | Opus 4.7 | Dialectic, depth, width, instruction adversarial review |
| Synthesizer | Opus 4.7 | Two-pass write from 3 angle-specific drafts |
| Patcher + polish auditor | Opus 4.7 | Tool-locked `[Read, Edit]` — surgical hunks only |
| Loci-analysts, depth-investigators, draft sub-orchestrators, source-analyst | Sonnet 4.6 | Parallel reading + position-committing |
| Fetchers | Haiku 4.5 | URL fetching via crawl4ai, 8–12 in parallel per wave |

Per-query wall-clock: **~1.5–2.5 hours** for full tier. Per-query cost: **~$60–120**.

### Evaluation (RACE + FACT) — **Mistral**

| Stage | Model (default) | Role |
|---|---|---|
| RACE | `mistral-large-latest` | Report-quality pairwise judge (comprehensiveness, insight, instruction-following, readability), target vs. reference |
| FACT | `mistral-small-latest` | Citation accuracy — extract → dedup → scrape → validate. Scrapes citation URLs via Jina. |

Both are overridable via `RACE_MODEL` / `FACT_MODEL`. The swap is implemented by copying `patches/api_mistral.py` over the upstream `deep_research_bench/utils/api.py` during `setup.sh`; Mistral's API is OpenAI-compatible, so only the LLM client changes. The judge defaults to deterministic decoding (`MISTRAL_TEMPERATURE=0.0`).

## Reproduce

```bash
git clone <this-repo>
cd hyperresearchbench/DeepResearch-Bench

# 1. One-command setup. Installs hyperresearch globally, clones upstream DRB,
#    installs the Mistral evaluator client + deps, downloads the 100 queries.
bash setup.sh

# 2. Smoke test — one query end-to-end (~1.5-2.5 hours, ~$60-120 to generate)
python harness.py --query 67          # English, "RL exploration → trajectory planning"
export MISTRAL_API_KEY=<your-key>     # https://console.mistral.ai/api-keys
export JINA_API_KEY=<your-key>        # FACT only; https://jina.ai/reader (or use --skip-fact)
bash grade.sh --limit 1

# 3. Full run (resume-safe; checkpoints to results/claude-research.jsonl)
python harness.py                     # ~6-10 days wall-clock for 100 queries
bash grade.sh                         # RACE + FACT
```

Reproduction expectations:

- **`hyperresearch --version`** must report `0.8.5` or later
- **`claude --version`** must work and be authenticated
- **Python 3.11, 3.12, or 3.13** (3.14 unsupported pending upstream Crawl4AI fix; setup.sh refuses to run on 3.14). The eval half alone runs on 3.10+.
- A **Mistral API key** (RACE = `mistral-large-latest`, FACT = `mistral-small-latest`)
- A **Jina API key** for FACT citation scraping (skip with `--skip-fact`)

## Fork-vs-original comparison

`compare.sh` benchmarks **your fork** of hyperresearch against the **original** and diffs the RACE + FACT scores. The pipeline under test is whatever package is installed *globally* (`hyperresearch install --global` copies the skill + agent prompts into `~/.claude/`), so the driver installs each version, generates the reports, grades them, then prints a side-by-side table. Because it mutates global `~/.claude` state, the two versions run **strictly sequentially**.

```bash
export MISTRAL_API_KEY=<your-key>
export JINA_API_KEY=<your-key>        # or pass --skip-fact

# Defaults: queries "67 52", original = jordan-gibbs/hyperresearch
bash compare.sh /path/to/your/hyperresearch

# Custom queries / original / RACE-only
bash compare.sh /path/to/your/hyperresearch \
    --original https://github.com/jordan-gibbs/hyperresearch.git \
    --queries "67 52" --skip-fact

# Re-running? Wipe prior run dirs so every report starts from an empty vault/DB
bash compare.sh /path/to/your/hyperresearch --fresh
```

Each version's scores land in `results/fork-research_score/` and `results/original-research_score/`, and the driver prints:

```
RACE metric                   fork  original   Δ (fork-orig)
------------------------------------------------------------
Comprehensiveness          ...      ...        ...
Insight                    ...      ...        ...
Instruction Following      ...      ...        ...
Readability                ...      ...        ...
Overall Score              ...      ...        ...
Verdict (RACE overall): FORK wins  (... vs ...)
```

> **Cost:** the default is 2 queries × 2 versions = 4 full-tier generations ≈ **6–10 h** and **~$240–480** of Anthropic API. `compare.sh` prompts for confirmation (bypass with `--yes`). The Mistral grading itself is cheap.
>
> **Note:** the fork's example report for Q67 is byte-identical to the original's — the fork's differences ("accuracy levers") only manifest in *freshly generated* reports, which is exactly what `compare.sh` produces.
>
> **Run isolation:** each query generates in its own `runs/<label>/query_<id>/` dir with its own fresh hyperresearch vault + SQLite DB, so fork/original and Q67/Q52 never share state *within* a run. The harness does not wipe an existing run dir, though — so if you re-run (or resume after an interrupted query), leftover vault state from a prior run is reused and can influence the result. Pass **`--fresh`** to wipe each version's `runs/` dir and results JSONL first, guaranteeing every report starts from an empty DB.
>
> **Rate limits:** the Mistral client retries on `429`/`5xx` with exponential backoff (honoring `Retry-After`), so RACE and FACT both finish without dropping scores or citations on any tier — it just runs slower under throttling. On the **Mistral free tier** (1 request/second) add **`--free-tier`** to `compare.sh` (or `grade.sh`), which sets `N_WORKERS=1` and `LLM_MIN_INTERVAL=1.1` so you stay under the limit and avoid wasted retries. One comparison is only a few million tokens — far under the free tier's monthly cap.

### Compare from scratch — step by step

Full runbook from a clean shell. Assumes this repo and your fork are checked out, the Claude Code CLI is authenticated, and you have Python 3.11–3.13.

```bash
# 0. Prerequisites
python3 --version            # 3.11, 3.12, or 3.13 (NOT 3.10 / 3.14)
claude --version             # must be authenticated — this is what generates the reports
git --version

# 1. (Recommended) isolate in a venv so the install churn never touches system Python
cd /path/to/hyperresearchbench/DeepResearch-Bench
python3 -m venv .venv        # use python3.12 -m venv .venv if python3 isn't 3.11–3.13
source .venv/bin/activate

# 2. One-time setup: installs eval deps, clones the upstream evaluator, copies the
#    Mistral client over its api.py, and downloads the 100 benchmark queries
bash setup.sh
python patches/verify_mistral.py     # offline sanity check, no API calls

# 3. Keys
export MISTRAL_API_KEY=<your-key>    # https://console.mistral.ai/api-keys
export JINA_API_KEY=<your-key>       # FACT only — omit it and add --skip-fact below

# 4. Compare your fork vs. the original on Q67 + Q52 (RACE + FACT)
bash compare.sh /path/to/your/hyperresearch --fresh
#   add --skip-fact if you have no Jina key
#   add --yes to skip the cost-confirmation prompt (~$240–480, 6–10 h)

# 5. Read results — the console prints the side-by-side table; per-query detail lives in
#    results/fork-research_score/ and results/original-research_score/

# 6. compare.sh leaves the ORIGINAL installed. Restore your fork as the active version:
pip install -e /path/to/your/hyperresearch && hyperresearch install --global
#   (or, if you used the throwaway venv above: deactivate && rm -rf .venv)
```

**Why this is a fair comparison.** `compare.sh` installs each version's *entire package* with `pip install --force-reinstall` — both the Python code (fetcher, search, DB, crawl4ai provider, …) and the bundled skill/agent prompts — then runs `hyperresearch install --global`, then generates and grades. The complete version is swapped each time, not just the `.md` prompts. Two details make this robust and are handled for you: both forks report the same version (`0.8.6`), so `--force-reinstall` is **required** for the code to actually swap — a plain `pip install` would see "already installed" and skip it; and because the versions differ in `db.py` / `migrations.py`, `--fresh` ensures each vault's SQLite DB is created and consumed by a single version (no cross-version schema mismatch). Both versions share global `~/.claude` and site-packages, so they run strictly back-to-back — don't use hyperresearch for other work while a comparison is running.

## What the harness does per query

1. Mirrors your project's `.claude/` and `CLAUDE.md` into a fresh `runs/query_<id>/` subdir
2. Runs `claude -p "Use the /hyperresearch skill on the FULL tier..."` with the benchmark prompt
3. The agent auto-bootstraps a hyperresearch vault (step 0), then walks the 16-step pipeline:

   ```
   1. decompose                    9.  evidence digest
   2. width sweep (40-100 sources) 10. triple-draft (3 parallel)
   3. contradiction graph          11. synthesize (Opus, two-pass)
   4. loci analysis                12. 4 adversarial critics (parallel)
   5. depth investigation (K par.) 13. gap-fetch
   6. cross-locus reconcile        14. patcher (Read+Edit only)
   7. source tensions              15. polish (Read+Edit only)
   8. corpus critic                16. readability audit
   ```

4. Reads the resulting `research/notes/final_report_<vault_tag>.md`
5. Appends one JSON record per query to `results/claude-research.jsonl`

## Output JSONL schema

```json
{
  "id": 67,
  "language": "en",
  "prompt": "Summarize recent research progress in reinforcement learning...",
  "article": "<the full final_report.md content>",
  "model": "opus",
  "prompt_tokens": 8423,
  "completion_tokens": 12017,
  "duration_seconds": 9182.4,
  "timestamp": "2026-04-29T19:32:45Z"
}
```

The schema matches the upstream DRB evaluator's expected `{id, prompt, article}` raw-data format. `grade.sh` copies the file into `deep_research_bench/data/test_data/raw_data/<name>.jsonl` and then:

- **RACE** — `python deepresearch_bench_race.py <name> --raw_data_dir … --cleaned_data_dir … --query_file … --output_dir …`. Cleans each article, then scores it against the pre-baked reference article (`data/test_data/cleaned_data/reference.jsonl`) using the pre-baked per-query criteria (`data/criteria_data/criteria.jsonl`), matched by prompt. Overall = target / (target + reference). Writes `race_result.txt`.
- **FACT** — the `utils.extract → utils.deduplicate → utils.scrape → utils.validate → utils.stat` chain (citation extraction with `mistral-small-latest`, URL scraping with Jina). Writes `fact_result.txt` with `valid_rate`.

## Single-query smoke test details

```bash
python harness.py --query 67 --timeout 10800
export MISTRAL_API_KEY=<your-key>
bash grade.sh --limit 1 --only-en          # add --skip-fact if you have no Jina key
```

`--query 67` is the canonical English query for smoke-testing — it's the public sample report at https://github.com/jordan-gibbs/hyperresearch/blob/main/example-reports/rl-exploration-trajectory-planning.md.

`--timeout 10800` is 3 hours — defensive, since full tier on a research-heavy query can run long when fetcher waves hit slow sources.

## Verify the Mistral client offline (no API calls)

```bash
# Config banner
LLM_BACKEND=mistral MISTRAL_API_KEY=$MISTRAL_API_KEY python deep_research_bench/utils/api.py

# Payload / surface self-test (mocks the network)
python patches/verify_mistral.py
```

## Resuming after interruption

The harness is resume-safe via `--resume`. Reads `results/claude-research.jsonl`, builds a set of completed query IDs, skips them on the next run.

```bash
python harness.py --resume
```

Worth knowing:

- **API rate limits / quotas** — if Anthropic billing pauses mid-run, fix the billing, then `python harness.py --resume`. Already-completed queries are not re-run.
- **Timeouts** — if a query hits `--timeout`, it's NOT recorded as completed. The next `--resume` will retry it.
- **Manual recovery** — every per-query subdir at `runs/query_<id>/` is a complete hyperresearch project (vault, sources, scaffold, drafts, critic findings).

## CLI reference

```
python harness.py --setup                # Download benchmark queries
python harness.py                        # Run all 100 queries
python harness.py --limit N              # Run only the first N
python harness.py --query <id>           # Run a specific query (1-100)
python harness.py --lang en | --lang zh  # Filter by language (50 each)
python harness.py --resume               # Skip queries already in JSONL
python harness.py --model opus           # Default; can also pass sonnet or haiku
python harness.py --timeout 10800        # Per-query timeout in seconds (default 3600)
python harness.py --output run-name      # Write to results/run-name.jsonl

bash grade.sh                            # RACE + FACT on results/claude-research.jsonl (Mistral)
bash grade.sh my-run                     # Grade results/my-run.jsonl
bash grade.sh --skip-fact                # RACE only (no citation web-scraping / Jina)
bash grade.sh --only-en | --only-zh      # Grade one language only
bash grade.sh --limit 5                  # Grade first 5 entries only
bash grade.sh --force                    # Re-grade even if results exist

bash compare.sh /path/to/fork            # Fork-vs-original on queries "67 52", diff scores
bash compare.sh /path/to/fork --queries "67 52" --skip-fact --yes
bash compare.sh /path/to/fork --fresh    # Wipe prior run dirs first (clean vault/DB per report)
bash compare.sh /path/to/fork --free-tier # Throttle to 1 req/s for the Mistral free tier
```

Environment variables: `MISTRAL_API_KEY` (required), `JINA_API_KEY` (FACT), `RACE_MODEL` / `FACT_MODEL` (model overrides), `MISTRAL_TEMPERATURE` (default 0.0), `MAX_OUTPUT_TOKENS` (default 8192), `N_WORKERS` (grade.sh parallelism, default 4). Rate-limit knobs: `LLM_MAX_RETRIES` (default 8), `LLM_RETRY_BASE` / `LLM_RETRY_CAP` (backoff seconds), `LLM_MIN_INTERVAL` (min seconds between requests, default 0 — set ~1.1 for the free tier, or just use `--free-tier`).

## Hyperresearch's published score

V8.3 stratified pilot (n = 9 queries, full reference-strength distribution): **57.77 average overall**, beating xiaoyi (DRB #1 on the public leaderboard at the time) by 0.77 points.

Full 100-query reproduction is what this harness exists to enable.

## License

MIT. The upstream [DeepResearch-Bench](https://github.com/Ayanami0730/deep_research_bench) repo (cloned by `setup.sh` into `DeepResearch-Bench/deep_research_bench/`) is licensed separately by its authors.
