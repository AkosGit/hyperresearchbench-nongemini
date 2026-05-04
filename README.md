# hyperresearchbench

**[DeepResearch-Bench](https://github.com/Ayanami0730/deep_research_bench) runner for Claude Code.**

Test any Claude Code research workflow against the same 100-query benchmark used to score Grep Deep Research, OpenAI Deep Research, Gemini Deep Research, and the public DRB leaderboard. Agent-agnostic — works with [hyperresearch](https://github.com/jordan-gibbs/hyperresearch), custom skills, or vanilla Claude Code.

The harness:

- Pulls the upstream 100-query benchmark (50 zh + 50 en, 22 fields, PhD-level prompts)
- Runs each query through `claude -p` in an isolated per-query subdir
- Mirrors your project's `.claude/` and `CLAUDE.md` into each subdir, so whatever skills + agents + hooks you have installed run for every query
- Captures `research/notes/final_report*.md` as the deliverable
- Writes a JSONL ready for the upstream RACE + FACT evaluator
- Resume-safe; supports per-query limits, language filters, model pinning

## Install

```bash
git clone https://github.com/jordan-gibbs/hyperresearchbench
cd hyperresearchbench/DeepResearch-Bench
bash setup.sh
```

Setup clones the upstream evaluator into `DeepResearch-Bench/deep_research_bench/`, installs the Gemini eval deps, and downloads the 100 benchmark queries.

You'll also need:

- **Claude Code CLI** authenticated (`claude --version` should work). Get it at [claude.com/claude-code](https://claude.com/claude-code).
- **Gemini API key** for grading. Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Set it as `GEMINI_API_KEY` or `GOOGLE_API_KEY`.

## Single-query smoke test (recommended first run)

```bash
cd DeepResearch-Bench

# 1. Run query #67 (English, RL exploration → trajectory planning) on Sonnet
python harness.py --query 67 --model sonnet

# 2. Grade it (RACE only, no web-scrape)
export GEMINI_API_KEY=<your-key>
bash grade.sh --skip-fact --limit 1

# 3. Result: results/claude-research_score.json
cat results/claude-research_score.json
```

Expected wall-clock: 3–8 min for the harness run, 1–2 min for grading. Total cost: ~$1–2 per query (depends on agent workflow).

A successful smoke test confirms:

- Claude Code is authenticated and reachable
- Your project's research workflow writes `research/notes/final_report*.md` correctly
- Gemini API key is valid
- The grading pipeline returns a score

## Full 100-query run

```bash
cd DeepResearch-Bench

# 1. Generate results (resume-safe; restartable on crash)
python harness.py --resume                       # ~1.5–2.5 hours per query for hyperresearch full tier
                                                 # ~3–8 min per query for vanilla Claude Code

# 2. Grade (RACE + FACT)
export GEMINI_API_KEY=<your-key>
bash grade.sh                                    # both RACE and FACT (FACT scrapes citation URLs — slow)
bash grade.sh --skip-fact                        # RACE only (faster, no web-scrape)
```

Expected total cost ranges:
- **vanilla Claude Code**: ~$50–100 (1× $0.50–1 per query)
- **hyperresearch full tier**: ~$6,000–12,000 (100× $60–120 per query) — full pipeline with adversarial review
- **hyperresearch light tier**: ~$500–1,500 (100× $5–15 per query)

Results land in `results/claude-research.jsonl` (raw outputs) and `results/claude-research_score.json` (RACE/FACT scores).

## Configuring the agent under test

The harness is **agent-agnostic** — it just runs `claude -p <prompt>` and reads the resulting `research/notes/final_report*.md`. Whatever your `.claude/skills/`, `.claude/agents/`, `.claude/settings.json`, and `CLAUDE.md` contain at `--project-root` (default: parent of this repo) is what the harness mirrors into each per-query subdir.

**Three ways to use it:**

### Option A — Test hyperresearch (recommended)

Install [hyperresearch](https://github.com/jordan-gibbs/hyperresearch) globally:

```bash
pip install hyperresearch
hyperresearch install --global
```

Then point the harness at any project that has hyperresearch installed:

```bash
# From a project where you've run `hyperresearch install`
cd /path/to/your-project
python /path/to/hyperresearchbench/DeepResearch-Bench/harness.py \
    --project-root . \
    --query 67
```

The harness mirrors `.claude/` from your project (which has the hyperresearch skill files) into each per-query subdir. Each subdir auto-creates its own `research/` vault on first run because hyperresearch's bootstrap step handles it.

### Option B — Test a custom Claude Code skill

If you have your own research skill at `.claude/skills/my-research/SKILL.md`, point the harness at that project:

```bash
python harness.py --project-root /path/to/your-project --limit 1
```

The harness will copy `.claude/` and `CLAUDE.md` from `your-project` into each per-query subdir. Your skill needs to write the final report to `research/notes/final_report*.md`.

### Option C — Test vanilla Claude Code

Point the harness at any project (or a project root with no `.claude/` at all). Claude Code will use only the built-in tools (WebSearch, WebFetch, etc.). Results will be much faster but less rigorous than a hyperresearch run.

```bash
python harness.py --project-root /tmp/empty-dir --limit 1
```

## Output format

`results/claude-research.jsonl` — one JSON object per query:

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

`results/claude-research_score.json` — RACE + FACT aggregate scores after grading.

## Harness CLI reference

```
python harness.py --setup                       # Download benchmark queries (~50 KB)
python harness.py                               # Run all 100 queries
python harness.py --limit N                     # Run only the first N
python harness.py --query <id>                  # Run a specific query (1–100)
python harness.py --lang en | --lang zh         # Filter by language (50 each)
python harness.py --resume                      # Skip queries already in JSONL
python harness.py --model sonnet | opus | haiku # Model alias (default: opus)
python harness.py --timeout 1800                # Per-query timeout in seconds (default: 3600)
python harness.py --output my-experiment        # Write to results/my-experiment.jsonl
python harness.py --runs-dir /path/to/runs      # Where to put per-query workdirs
python harness.py --project-root /path/to/proj  # Project whose .claude/ to mirror in
```

## Troubleshooting

**"No final_report*.md found for query N"** — the agent didn't write a report. Check the run subdir at `runs/query_N/` to see what happened. Common causes: timeout (try `--timeout 7200`), the agent's research workflow doesn't write to `research/notes/`, or a skill is broken.

**"Pre-rename architecture leaks"** in the report — your project has an old hyperresearch version. Upgrade with `pip install --upgrade hyperresearch && hyperresearch install`.

**Gemini rate limits during grading** — the upstream evaluator pauses and retries automatically. If it fails permanently, run `bash grade.sh --limit 10` repeatedly until done; results are append-only.

**Out-of-quota mid-run** — `--resume` is your friend. The harness skips queries already in the output JSONL, so you can fix billing and continue.

## License

MIT. Same as the parent project [hyperresearch](https://github.com/jordan-gibbs/hyperresearch).

The upstream [DeepResearch-Bench](https://github.com/Ayanami0730/deep_research_bench) repo (cloned by `setup.sh` into `DeepResearch-Bench/deep_research_bench/`) is licensed separately by its authors.
