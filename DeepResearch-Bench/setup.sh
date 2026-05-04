#!/bin/bash
# One-time setup: clone the upstream DeepResearch-Bench repo (for the RACE
# evaluator + reference data), install eval Python deps, and download the
# 100 benchmark queries.
#
# Usage:
#   bash setup.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DRB_REPO="$HERE/deep_research_bench"
DRB_URL="https://github.com/Ayanami0730/deep_research_bench.git"

echo "=== DeepResearch-Bench setup ==="
echo ""

# Step 1: Clone upstream DRB if not present
if [ -d "$DRB_REPO" ]; then
    echo "[OK] Upstream DRB already cloned at $DRB_REPO"
else
    echo "[FETCH] Cloning $DRB_URL"
    git clone --depth 1 "$DRB_URL" "$DRB_REPO"
fi

# Step 2: Install Python deps for the evaluator
echo ""
echo "[PIP] Installing eval dependencies..."
python -m pip install --upgrade pip >/dev/null
python -m pip install \
    google-generativeai \
    requests \
    tqdm \
    beautifulsoup4 \
    lxml

# Step 3: Download benchmark queries (used by harness.py)
echo ""
echo "[FETCH] Downloading 100 benchmark queries..."
python "$HERE/harness.py" --setup

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Configure your Claude Code project with whatever research workflow you want to test."
echo "  2. Smoke-test with one query: python harness.py --limit 1"
echo "  3. Grade it:                   bash grade.sh --limit 1"
echo ""
echo "For the full 100-query run, see README.md."
