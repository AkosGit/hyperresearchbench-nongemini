#!/bin/bash
# One-command setup: install hyperresearch globally, clone the upstream
# DeepResearch-Bench repo (RACE + FACT evaluator), swap its LLM client over to
# Mistral, install eval deps, and download the 100 benchmark queries.
#
# The RACE + FACT evaluation runs on MISTRAL models (not Gemini):
#   - RACE  (report-quality pairwise judge): mistral-large-latest
#   - FACT  (citation accuracy checker):      mistral-small-latest
# This is done by copying patches/api_mistral.py over the upstream
# deep_research_bench/utils/api.py after cloning, so the swap survives a fresh
# clone. Mistral's API is OpenAI-compatible, so only the LLM client changes.
#
# Prerequisites:
#   - Python 3.11, 3.12, or 3.13 (NOT 3.14 — Crawl4AI's lxml pin) for the
#     hyperresearch *generation* half. (The eval half alone runs on 3.10+.)
#   - Claude Code CLI installed and authenticated (`claude --version`)
#
# Usage:
#   bash setup.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DRB_REPO="$HERE/deep_research_bench"
DRB_URL="https://github.com/Ayanami0730/deep_research_bench.git"
MISTRAL_PATCH="$HERE/patches/api_mistral.py"
EXTRACT_PATCH="$HERE/patches/extract_robust.py"

echo "=== hyperresearchbench setup (Mistral evaluator) ==="
echo ""

# Step 0: Verify Python version
PY_MAJ=$(python -c "import sys; print(sys.version_info.major)")
PY_MIN=$(python -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJ" -ne 3 ] || [ "$PY_MIN" -lt 11 ] || [ "$PY_MIN" -ge 14 ]; then
    echo "[ERROR] Python ${PY_MAJ}.${PY_MIN} not supported for generation. Use 3.11, 3.12, or 3.13."
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

# Step 2: Clone upstream DRB if not present (RACE + FACT evaluator + reference data)
echo ""
if [ -d "$DRB_REPO" ]; then
    echo "[OK] Upstream DRB already cloned at $DRB_REPO"
else
    echo "[FETCH] Cloning $DRB_URL"
    git clone --depth 1 "$DRB_URL" "$DRB_REPO"
fi

# Step 3: Swap the evaluator's LLM client over to Mistral
echo ""
if [ ! -f "$MISTRAL_PATCH" ]; then
    echo "[ERROR] Mistral patch not found at $MISTRAL_PATCH"
    exit 1
fi
echo "[PATCH] Installing Mistral LLM client → deep_research_bench/utils/api.py"
cp "$MISTRAL_PATCH" "$DRB_REPO/utils/api.py"
echo "[OK] RACE=mistral-large-latest  FACT=mistral-small-latest (override via RACE_MODEL / FACT_MODEL)"

# Robust FACT citation extractor: tolerates truncated-JSON model output (the
# 8192-token cap can cut off a long report's citation list) and ALWAYS writes a
# record, so a bad extraction can't crash the FACT pipeline under `set -e`.
if [ -f "$EXTRACT_PATCH" ]; then
    echo "[PATCH] Installing robust FACT extractor → deep_research_bench/utils/extract.py"
    cp "$EXTRACT_PATCH" "$DRB_REPO/utils/extract.py"
fi

# Step 4: Install eval dependencies (Mistral client uses plain `requests`)
echo ""
echo "[PIP] Installing eval dependencies..."
python -m pip install --upgrade \
    requests \
    tqdm \
    beautifulsoup4 \
    lxml >/dev/null

# Step 5: Download benchmark queries
echo ""
echo "[FETCH] Downloading 100 benchmark queries..."
python "$HERE/harness.py" --setup

echo ""
echo "=== Setup complete ==="
echo ""
echo "Verify the Mistral client:"
echo "  LLM_BACKEND=mistral MISTRAL_API_KEY=\$MISTRAL_API_KEY python $DRB_REPO/utils/api.py"
echo ""
echo "Verify hyperresearch:"
echo "  hyperresearch --version       (should be 0.8.5+)"
echo ""
echo "Smoke test (one query, ~1.5-2.5 hours, ~\$60-120 to generate):"
echo "  python harness.py --query 67"
echo "  export MISTRAL_API_KEY=<your-key>     # https://console.mistral.ai/api-keys"
echo "  export JINA_API_KEY=<your-key>        # only needed for FACT; https://jina.ai/reader"
echo "  bash grade.sh --limit 1"
echo ""
echo "Fork-vs-original comparison on 2 reports:"
echo "  bash compare.sh /path/to/your/hyperresearch    (see README.md)"
echo ""
echo "See README.md for details."
