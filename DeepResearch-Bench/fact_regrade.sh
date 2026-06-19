#!/bin/bash
# FACT-only regrade (grade.sh has no --skip-race). Runs the FACT pipeline
# extract -> dedup -> scrape -> validate -> stat for one or more result sets,
# leaving any existing RACE results untouched. Keys load from .env; free-tier
# throttle by default (the Mistral key is rate-limited). Self-contained so it can
# be launched detached via run_durable.sh and survive a session teardown.
#
# Usage:  bash fact_regrade.sh [name ...]        (default: fork-research original-research)
#         bash run_durable.sh --name fact -- bash fact_regrade.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }
export LLM_BACKEND="${LLM_BACKEND:-mistral}"
export FACT_MODEL="${FACT_MODEL:-mistral-small-latest}"
export RACE_MODEL="${RACE_MODEL:-mistral-large-latest}"
export N_WORKERS="${N_WORKERS:-1}"
export LLM_MIN_INTERVAL="${LLM_MIN_INTERVAL:-1.1}"   # ~1 req/s for the rate-limited key

PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY=python3
cd "$HERE/deep_research_bench"
QF=data/prompt_data/query.jsonl

NAMES=("$@"); [ ${#NAMES[@]} -gt 0 ] || NAMES=(fork-research original-research)

for v in "${NAMES[@]}"; do
    FO="results/fact/$v"; mkdir -p "$FO"
    RAW="data/test_data/raw_data/$v.jsonl"
    if [ ! -f "$RAW" ]; then echo "[skip] $v: no raw_data at $RAW"; continue; fi
    echo ">>> [$v] extract  @ $(date +%H:%M:%S)"
    "$PY" -u -m utils.extract     --raw_data_path "$RAW"                 --output_path "$FO/extracted.jsonl"    --query_data_path "$QF" --n_total_process 1
    echo ">>> [$v] dedup    @ $(date +%H:%M:%S)"
    "$PY" -u -m utils.deduplicate --raw_data_path "$FO/extracted.jsonl"  --output_path "$FO/deduplicated.jsonl" --query_data_path "$QF" --n_total_process 1
    echo ">>> [$v] scrape   @ $(date +%H:%M:%S)"
    "$PY" -u -m utils.scrape      --raw_data_path "$FO/deduplicated.jsonl" --output_path "$FO/scraped.jsonl"    --n_total_process 1
    echo ">>> [$v] validate @ $(date +%H:%M:%S)"
    "$PY" -u -m utils.validate    --raw_data_path "$FO/scraped.jsonl"    --output_path "$FO/validated.jsonl"    --query_data_path "$QF" --n_total_process 1
    echo ">>> [$v] stat     @ $(date +%H:%M:%S)"
    "$PY" -u -m utils.stat        --input_path "$FO/validated.jsonl"     --output_path "$FO/fact_result.txt"
    echo "--- [$v] fact_result ---"; cat "$FO/fact_result.txt"
done
echo "=== FACT regrade DONE @ $(date +%H:%M:%S) ==="
