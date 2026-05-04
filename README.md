# hyperresearchbench

**Reproduction harness for hyperresearch's [DeepResearch-Bench](https://github.com/Ayanami0730/deep_research_bench) score.**

Runs the 100-query DRB benchmark against [hyperresearch](https://github.com/jordan-gibbs/hyperresearch) on Claude Code, full tier (16-step pipeline with adversarial review), Opus 4.7. Outputs a JSONL file ready for the upstream RACE + FACT evaluator.

## Stack

| Layer | Model | Role |
|---|---|---|
| Orchestrator | Opus 4.7 | Tier classification, pipeline routing, synthesis planning |
| Critics (4×) | Opus 4.7 | Dialectic, depth, width, instruction adversarial review |
| Synthesizer | Opus 4.7 | Two-pass write from 3 angle-specific drafts |
| Patcher + polish auditor | Opus 4.7 | Tool-locked `[Read, Edit]` — surgical hunks only |
| Loci-analysts, depth-investigators, draft sub-orchestrators, source-analyst | Sonnet 4.6 | Parallel reading + position-committing |
| Fetchers | Haiku 4.5 | URL fetching via crawl4ai, 8–12 in parallel per wave |

Per-query wall-clock: **~1.5–2.5 hours** for full tier. Per-query cost: **~$60–120**.

## Reproduce

```bash
git clone https://github.com/jordan-gibbs/hyperresearchbench
cd hyperresearchbench/DeepResearch-Bench

# 1. One-command setup. Installs hyperresearch globally, clones upstream
#    DRB, installs Gemini eval deps, downloads the 100 queries.
bash setup.sh

# 2. Smoke test — one query end-to-end (~1.5-2.5 hours, ~$60-120)
python harness.py --query 67          # English, "RL exploration → trajectory planning"
export GEMINI_API_KEY=<your-key>      # Get one at https://aistudio.google.com/apikey
bash grade.sh --limit 1

# 3. Full run (resume-safe; checkpoints to results/claude-research.jsonl)
python harness.py                     # ~6-10 days wall-clock for 100 queries
bash grade.sh                         # RACE + FACT
```

Reproduction expectations:

- **`hyperresearch --version`** must report `0.8.5` or later
- **`claude --version`** must work and be authenticated
- **Python 3.11, 3.12, or 3.13** (3.14 unsupported pending upstream Crawl4AI fix; setup.sh refuses to run on 3.14)
- A Gemini API key (Gemini-2.5-Pro for RACE, Gemini-2.5-Flash for FACT)

## What the harness does per query

1. Mirrors your project's `.claude/` and `CLAUDE.md` into a fresh `runs/query_<id>/` subdir
2. Runs `claude -p "Use the /hyperresearch skill on the FULL tier..."` with the benchmark prompt
3. The agent in that subdir auto-bootstraps a hyperresearch vault (step 0), then walks the 16-step pipeline:

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

The schema matches the upstream DRB evaluator's expected format. `grade.sh` copies the file into `deep_research_bench/data/results/` and invokes `python -m race.eval` and `python -m fact.eval` (FACT scrapes citation URLs to verify them).

## Single-query smoke test details

```bash
python harness.py --query 67 --timeout 10800
```

`--query 67` is the canonical English query for smoke-testing — it's the one we used as our public sample report at https://github.com/jordan-gibbs/hyperresearch/blob/main/example-reports/rl-exploration-trajectory-planning.md (graded 58.3/100 in the V8.3 stratified pilot). Anyone reproducing should expect to land within ±2 points of that score.

`--timeout 10800` is 3 hours — defensive, since full tier on a research-heavy query can run long when fetcher waves hit slow sources.

After the run, `runs/query_67/research/notes/final_report_*.md` is the deliverable. `results/claude-research.jsonl` has the JSONL entry.

## Resuming after interruption

The harness is resume-safe via `--resume`. Reads `results/claude-research.jsonl`, builds a set of completed query IDs, skips them on the next run.

```bash
python harness.py --resume
```

Worth knowing:

- **API rate limits / quotas** — if Anthropic billing pauses mid-run, fix the billing, then `python harness.py --resume`. Already-completed queries are not re-run.
- **Timeouts** — if a query hits `--timeout`, it's NOT recorded as completed. The next `--resume` will retry it. Bump `--timeout` if certain queries consistently hit the cap.
- **Manual recovery** — every per-query subdir at `runs/query_<id>/` is a complete hyperresearch project (vault, sources, scaffold, drafts, critic findings). If a query crashed mid-pipeline (e.g., during step 15), you can `cd` in and inspect / manually finish.

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

bash grade.sh                            # RACE + FACT on results/claude-research.jsonl
bash grade.sh my-run                     # Grade results/my-run.jsonl
bash grade.sh --skip-fact                # RACE only (no citation web-scraping)
bash grade.sh --limit 5                  # Grade first 5 entries only
```

## Hyperresearch's published score

V8.3 stratified pilot (n = 9 queries, full reference-strength distribution): **57.77 average overall**, beating xiaoyi (DRB #1 on the public leaderboard at the time) by 0.77 points.

Full 100-query reproduction is what this harness exists to enable.

## License

MIT. The upstream [DeepResearch-Bench](https://github.com/Ayanami0730/deep_research_bench) repo (cloned by `setup.sh` into `DeepResearch-Bench/deep_research_bench/`) is licensed separately by its authors.
