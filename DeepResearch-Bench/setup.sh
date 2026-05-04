#!/bin/bash
# One-command setup: install hyperresearch globally, clone the upstream
# DeepResearch-Bench repo (for the RACE evaluator), install Gemini eval
# deps, and download the 100 benchmark queries.
#
# Prerequisites:
#   - Python 3.11, 3.12, or 3.13 (NOT 3.14 — Crawl4AI's lxml pin)
#   - Claude Code CLI installed and authenticated (`claude --version`)
#
# Usage:
#   bash setup.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DRB_REPO="$HERE/deep_research_bench"
DRB_URL="https://github.com/Ayanami0730/deep_research_bench.git"

echo "=== hyperresearchbench setup ==="
echo ""

# Step 0: Verify Python version
PY_MAJ=$(python -c "import sys; print(sys.version_info.major)")
PY_MIN=$(python -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJ" -ne 3 ] || [ "$PY_MIN" -lt 11 ] || [ "$PY_MIN" -ge 14 ]; then
    echo "[ERROR] Python ${PY_MAJ}.${PY_MIN} not supported. Use 3.11, 3.12, or 3.13."
    echo "        (Crawl4AI's lxml~=5.3 pin has no cp314 wheels yet.)"
    exit 1
fi
echo "[OK] Python ${PY_MAJ}.${PY_MIN}"

# Step 1: Install hyperresearch + run global install (skill + agents → ~/.claude/)
echo ""
echo "[PIP] Installing hyperresearch..."
python -m pip install --upgrade pip >/dev/null
python -m pip install --upgrade hyperresearch

echo ""
echo "[INSTALL] Registering /hyperresearch skill globally..."
hyperresearch install --global

# Step 2: Clone upstream DRB if not present (RACE evaluator + reference data)
echo ""
if [ -d "$DRB_REPO" ]; then
    echo "[OK] Upstream DRB already cloned at $DRB_REPO"
else
    echo "[FETCH] Cloning $DRB_URL"
    git clone --depth 1 "$DRB_URL" "$DRB_REPO"
fi

# Step 3: Install Gemini eval deps
echo ""
echo "[PIP] Installing eval dependencies..."
python -m pip install --upgrade \
    google-generativeai \
    requests \
    tqdm \
    beautifulsoup4 \
    lxml >/dev/null

# Step 4: Download benchmark queries
echo ""
echo "[FETCH] Downloading 100 benchmark queries..."
python "$HERE/harness.py" --setup

echo ""
echo "=== Setup complete ==="
echo ""
echo "Verify hyperresearch:"
echo "  hyperresearch --version       (should be 0.8.5+)"
echo ""
echo "Smoke test (one query, ~1.5-2.5 hours, ~\$60-120):"
echo "  python harness.py --query 67"
echo "  export GEMINI_API_KEY=<your-key>"
echo "  bash grade.sh --limit 1"
echo ""
echo "Full run (100 queries):"
echo "  python harness.py             (resume-safe; restartable)"
echo "  bash grade.sh                 (RACE + FACT)"
echo ""
echo "See README.md for details."
