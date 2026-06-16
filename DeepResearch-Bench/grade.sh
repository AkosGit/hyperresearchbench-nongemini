#!/bin/bash
# Run RACE + FACT evaluation on harness output JSONL using the upstream
# DeepResearch-Bench evaluator, scored by MISTRAL models.
#
#   RACE  — report quality (comprehensiveness, insight, instruction-following,
#           readability), pairwise vs the reference article. Judge: mistral-large-latest.
#   FACT  — citation accuracy (valid citations / total). Checker: mistral-small-latest.
#           FACT scrapes citation URLs via Jina, so it also needs JINA_API_KEY.
#
# Prerequisites:
#   1. bash setup.sh                          (clones upstream DRB + installs the
#                                              Mistral client + deps)
#   2. python harness.py --query <id>         (generates results/<output>.jsonl)
#   3. export MISTRAL_API_KEY=<your-key>      (RACE + FACT judge)
#   4. export JINA_API_KEY=<your-key>         (FACT only; omit and use --skip-fact)
#
# Usage:
#   bash grade.sh                              # Grade results/claude-research.jsonl (RACE+FACT)
#   bash grade.sh my-experiment                # Grade results/my-experiment.jsonl
#   bash grade.sh --skip-fact                  # RACE only (no citation web-scraping)
#   bash grade.sh --only-en                    # Grade English queries only
#   bash grade.sh --limit 5                    # Grade only first 5 queries
#   bash grade.sh fork-research --only-en      # Single-language grade of a named run
#
# Model overrides (optional):
#   RACE_MODEL=mistral-medium-latest FACT_MODEL=mistral-small-latest bash grade.sh ...
#
# Mistral free tier (1 req/s): add --free-tier to serialize calls (sets
# N_WORKERS=1 + LLM_MIN_INTERVAL=1.1). The client retries on 429/5xx regardless,
# so RACE and FACT finish without dropping scores or citations either way.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# Load secrets (MISTRAL_API_KEY, JINA_API_KEY) from a gitignored .env if present.
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

DRB_REPO="$HERE/deep_research_bench"
RESULTS_DIR="$HERE/results"
RESULTS_NAME="claude-research"
SKIP_FACT=false
LIMIT_ARG=""
LANG_ARG=""
FORCE_ARG=""
FREE_TIER=false
N_WORKERS="${N_WORKERS:-4}"

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-fact) SKIP_FACT=true ;;
        --limit)     LIMIT_ARG="--limit $2"; shift ;;
        --only-en)   LANG_ARG="--only_en" ;;
        --only-zh)   LANG_ARG="--only_zh" ;;
        --force)     FORCE_ARG="--force" ;;
        --free-tier) FREE_TIER=true ;;
        --help|-h)
            sed -n '2,33p' "$0"
            exit 0
            ;;
        *) RESULTS_NAME="$1" ;;
    esac
    shift
done

# Free tier: serialize to ~1 req/s (client also retries on 429 regardless)
if [ "$FREE_TIER" = true ]; then
    N_WORKERS=1
    export LLM_MIN_INTERVAL="${LLM_MIN_INTERVAL:-1.1}"
fi

RESULTS_FILE="$RESULTS_DIR/${RESULTS_NAME}.jsonl"

echo "=== DeepResearch-Bench grading (Mistral) ==="
echo "Results file: $RESULTS_FILE"
echo "RACE model:   ${RACE_MODEL:-mistral-large-latest}"
echo "FACT model:   ${FACT_MODEL:-mistral-small-latest}"
echo ""

# ── Sanity checks ──────────────────────────────────────────────────
if [ ! -d "$DRB_REPO" ]; then
    echo "[ERROR] Upstream DRB not cloned. Run: bash setup.sh"
    exit 1
fi

if [ ! -f "$DRB_REPO/utils/api.py" ] || ! grep -q "mistral" "$DRB_REPO/utils/api.py"; then
    echo "[ERROR] Mistral client not installed into the evaluator."
    echo "        Re-run: bash setup.sh   (it copies patches/api_mistral.py)"
    exit 1
fi

if [ ! -f "$RESULTS_FILE" ]; then
    echo "[ERROR] Results file not found: $RESULTS_FILE"
    echo "        Run: python harness.py [--query <id>]"
    exit 1
fi

if [ -z "${MISTRAL_API_KEY:-}" ]; then
    echo "[ERROR] Set MISTRAL_API_KEY before grading."
    echo "        Get one at https://console.mistral.ai/api-keys"
    exit 1
fi

if [ "$SKIP_FACT" = false ] && [ -z "${JINA_API_KEY:-}" ]; then
    echo "[WARN] JINA_API_KEY not set — FACT scrapes citation URLs via Jina."
    echo "       Either export JINA_API_KEY or re-run with --skip-fact."
    echo "       Proceeding with RACE only."
    SKIP_FACT=true
fi

# ── Environment: point the evaluator at Mistral ────────────────────
export LLM_BACKEND="mistral"
export RACE_MODEL="${RACE_MODEL:-mistral-large-latest}"
export FACT_MODEL="${FACT_MODEL:-mistral-small-latest}"

# Place the results file where the upstream evaluator expects raw target data
RAW_DATA_DIR="$DRB_REPO/data/test_data/raw_data"
mkdir -p "$RAW_DATA_DIR"
cp "$RESULTS_FILE" "$RAW_DATA_DIR/${RESULTS_NAME}.jsonl"

QUERY_FILE="data/prompt_data/query.jsonl"
cd "$DRB_REPO"

# ── Step 1: RACE — report quality (cleans articles, then pairwise scores) ──
echo "=== RACE evaluation (mistral-large-latest pairwise judge) ==="
RACE_OUT="results/race/${RESULTS_NAME}"
mkdir -p "$RACE_OUT"
python -u deepresearch_bench_race.py "${RESULTS_NAME}" \
    --raw_data_dir "data/test_data/raw_data" \
    --cleaned_data_dir "data/test_data/cleaned_data" \
    --query_file "$QUERY_FILE" \
    --output_dir "$RACE_OUT" \
    --max_workers "$N_WORKERS" \
    $LIMIT_ARG $LANG_ARG $FORCE_ARG

# ── Step 2: FACT — citation accuracy (extract → dedup → scrape → validate → stat) ──
FACT_OUT="results/fact/${RESULTS_NAME}"
if [ "$SKIP_FACT" = false ]; then
    echo ""
    echo "=== FACT evaluation (mistral-small-latest citation grader, scrapes URLs via Jina) ==="
    mkdir -p "$FACT_OUT"
    RAW_DATA_PATH="data/test_data/raw_data/${RESULTS_NAME}.jsonl"

    # FACT must NEVER abort the run. A single bad model response (e.g. a
    # truncated-JSON citation extraction) previously killed grade.sh under
    # `set -e`, which aborted compare.sh before the other version ran. Relax
    # errexit across the chain, record success, and let Step 3 always run so
    # RACE scores are preserved regardless.
    set +e
    fact_ok=1
    echo "[FACT 1/5] Extract citations"
    python -u -m utils.extract     --raw_data_path "$RAW_DATA_PATH"          --output_path "$FACT_OUT/extracted.jsonl"    --query_data_path "$QUERY_FILE" --n_total_process "$N_WORKERS" || fact_ok=0
    echo "[FACT 2/5] Deduplicate citations"
    python -u -m utils.deduplicate --raw_data_path "$FACT_OUT/extracted.jsonl"    --output_path "$FACT_OUT/deduplicated.jsonl" --query_data_path "$QUERY_FILE" --n_total_process "$N_WORKERS" || fact_ok=0
    echo "[FACT 3/5] Scrape webpages"
    python -u -m utils.scrape      --raw_data_path "$FACT_OUT/deduplicated.jsonl" --output_path "$FACT_OUT/scraped.jsonl"      --n_total_process "$N_WORKERS" || fact_ok=0
    echo "[FACT 4/5] Validate citations"
    python -u -m utils.validate    --raw_data_path "$FACT_OUT/scraped.jsonl"      --output_path "$FACT_OUT/validated.jsonl"    --query_data_path "$QUERY_FILE" --n_total_process "$N_WORKERS" || fact_ok=0
    echo "[FACT 5/5] Collect statistics"
    python -u -m utils.stat        --input_path "$FACT_OUT/validated.jsonl"       --output_path "$FACT_OUT/fact_result.txt" || fact_ok=0
    set -e
    [ "$fact_ok" = 1 ] || echo "[WARN] FACT evaluation incomplete — RACE scores below are still valid."
else
    echo ""
    echo "[SKIP] FACT evaluation skipped"
fi

# ── Step 3: Collect + print scores, copy back to the benchmark results dir ──
echo ""
echo "=== Score: ${RESULTS_NAME} ==="
DEST="$RESULTS_DIR/${RESULTS_NAME}_score"
mkdir -p "$DEST"

if [ -f "$RACE_OUT/race_result.txt" ]; then
    echo "--- RACE ---"
    cat "$RACE_OUT/race_result.txt"
    cp "$RACE_OUT/race_result.txt" "$DEST/race_result.txt"
    [ -f "$RACE_OUT/raw_results.jsonl" ] && cp "$RACE_OUT/raw_results.jsonl" "$DEST/race_raw_results.jsonl"
else
    echo "[WARN] No race_result.txt produced — check the RACE log above."
fi

if [ "$SKIP_FACT" = false ] && [ -f "$FACT_OUT/fact_result.txt" ]; then
    echo "--- FACT ---"
    cat "$FACT_OUT/fact_result.txt"
    cp "$FACT_OUT/fact_result.txt" "$DEST/fact_result.txt"
fi

echo ""
echo "[DONE] Scores saved under $DEST/"
