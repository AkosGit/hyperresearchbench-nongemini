#!/bin/bash
# Run RACE + FACT evaluation on harness output JSONL using the upstream
# DeepResearch-Bench evaluator.
#
# Prerequisites:
#   1. bash setup.sh   (clones upstream DRB + installs deps)
#   2. python harness.py --limit N   (generates results/<output>.jsonl)
#   3. export GEMINI_API_KEY=<your-key>   (Gemini-2.5-Pro for RACE, 2.5-Flash for FACT)
#
# Usage:
#   bash grade.sh                              # Grade results/claude-research.jsonl
#   bash grade.sh my-experiment                # Grade results/my-experiment.jsonl
#   bash grade.sh --skip-fact                  # Skip FACT (no web scraping for citations)
#   bash grade.sh --limit 5                    # Grade only first 5 queries
#   bash grade.sh my-experiment --limit 1      # Single-query grade for smoke test

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DRB_REPO="$HERE/deep_research_bench"
RESULTS_DIR="$HERE/results"
RESULTS_NAME="claude-research"
SKIP_FACT=false
LIMIT_ARG=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-fact) SKIP_FACT=true ;;
        --limit)     LIMIT_ARG="--limit $2"; shift ;;
        --help|-h)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *) RESULTS_NAME="$1" ;;
    esac
    shift
done

RESULTS_FILE="$RESULTS_DIR/${RESULTS_NAME}.jsonl"

echo "=== DeepResearch-Bench grading ==="
echo "Results file: $RESULTS_FILE"
echo ""

# Sanity checks
if [ ! -d "$DRB_REPO" ]; then
    echo "[ERROR] Upstream DRB not cloned. Run: bash setup.sh"
    exit 1
fi

if [ ! -f "$RESULTS_FILE" ]; then
    echo "[ERROR] Results file not found: $RESULTS_FILE"
    echo "        Run: python harness.py [--limit N]"
    exit 1
fi

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
    echo "[ERROR] Set GEMINI_API_KEY (or GOOGLE_API_KEY) before grading."
    echo "        Get one at https://aistudio.google.com/apikey"
    exit 1
fi

# Place the results file where the upstream evaluator expects it
TARGET_NAME="${RESULTS_NAME}.jsonl"
DRB_RESULTS_DIR="$DRB_REPO/data/results"
mkdir -p "$DRB_RESULTS_DIR"
cp "$RESULTS_FILE" "$DRB_RESULTS_DIR/$TARGET_NAME"

# Step 1: RACE — report quality (comprehensiveness, insight, instruction-following, readability)
echo "=== RACE evaluation (Gemini-2.5-Pro pairwise judge) ==="
cd "$DRB_REPO"
python -m race.eval \
    --target "$TARGET_NAME" \
    $LIMIT_ARG

# Step 2: FACT — citation accuracy (effective citations / total citations)
if [ "$SKIP_FACT" = false ]; then
    echo ""
    echo "=== FACT evaluation (Gemini-2.5-Flash citation grader, web-scrapes URLs) ==="
    python -m fact.eval \
        --target "$TARGET_NAME" \
        $LIMIT_ARG
else
    echo "[SKIP] FACT evaluation skipped (--skip-fact)"
fi

# Step 3: Pull the score back into our results dir
SCORE_FILE="$DRB_REPO/data/results/${RESULTS_NAME}_score.json"
if [ -f "$SCORE_FILE" ]; then
    cp "$SCORE_FILE" "$RESULTS_DIR/${RESULTS_NAME}_score.json"
    echo ""
    echo "=== Score ==="
    python -c "
import json, sys
with open('$RESULTS_DIR/${RESULTS_NAME}_score.json', encoding='utf-8') as f:
    s = json.load(f)
print(f'Overall:               {s.get(\"overall\", \"?\"):.2f}')
print(f'Comprehensiveness:     {s.get(\"comprehensiveness\", \"?\"):.2f}')
print(f'Insight:               {s.get(\"insight\", \"?\"):.2f}')
print(f'Instruction-following: {s.get(\"instruction_following\", \"?\"):.2f}')
print(f'Readability:           {s.get(\"readability\", \"?\"):.2f}')
" 2>/dev/null || cat "$RESULTS_DIR/${RESULTS_NAME}_score.json"
fi

echo ""
echo "[DONE] Score saved to $RESULTS_DIR/${RESULTS_NAME}_score.json"
