#!/bin/bash
# Re-run only the FACT validate+stat steps on ALREADY-scraped content
# (results/fact/<name>/scraped.jsonl), skipping extract/dedup/scrape. Useful when
# the scrape already succeeded (cached url_content) but you want to re-judge —
# e.g. after patching the validator to record per-citation reasoning, without
# paying the slow/failure-prone Jina scrape again. Keys load from .env;
# free-tier throttle by default. Self-contained for durable launch.
#
# Usage:  bash fact_revalidate.sh [name ...]   (default: fork-research original-research)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }
export LLM_BACKEND="${LLM_BACKEND:-mistral}"
export FACT_MODEL="${FACT_MODEL:-mistral-small-latest}"
export N_WORKERS="${N_WORKERS:-1}"
export LLM_MIN_INTERVAL="${LLM_MIN_INTERVAL:-1.1}"
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
cd "$HERE/deep_research_bench"
QF=data/prompt_data/query.jsonl
NAMES=("$@"); [ ${#NAMES[@]} -gt 0 ] || NAMES=(fork-research original-research)
for v in "${NAMES[@]}"; do
    FO="results/fact/$v"
    if [ ! -f "$FO/scraped.jsonl" ]; then echo "[skip] $v: no scraped.jsonl"; continue; fi
    echo ">>> [$v] validate (reasoning, from cached scrape) @ $(date +%H:%M:%S)"
    rm -f "$FO/validated.jsonl"
    "$PY" -u -m utils.validate --raw_data_path "$FO/scraped.jsonl" --output_path "$FO/validated.jsonl" --query_data_path "$QF" --n_total_process 1
    echo ">>> [$v] stat @ $(date +%H:%M:%S)"
    "$PY" -u -m utils.stat --input_path "$FO/validated.jsonl" --output_path "$FO/fact_result.txt"
    echo "--- [$v] fact_result ---"; cat "$FO/fact_result.txt"
done
echo "=== revalidate DONE @ $(date +%H:%M:%S) ==="
