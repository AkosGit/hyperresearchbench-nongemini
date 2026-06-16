#!/bin/bash
# Benchmark YOUR fork of hyperresearch against the ORIGINAL on N benchmark
# queries, score both with the Mistral RACE + FACT evaluator, and diff.
#
# How the comparison works
# ------------------------
# The hyperresearch pipeline under test is whatever package is installed
# GLOBALLY (`hyperresearch install --global` copies the skill + agent prompts
# into ~/.claude/). The fork's changes ("accuracy levers") live in those
# bundled skill/agent .md files. So this script, for each version:
#   1. pip-installs that version, then `hyperresearch install --global`
#   2. generates the reports for each query via harness.py
#   3. grades them with grade.sh (Mistral)
# Because step 1 mutates global ~/.claude state, the two versions are run
# STRICTLY SEQUENTIALLY — never in parallel.
#
# Cost / time warning
# -------------------
# Each query is a full-tier hyperresearch run: ~1.5–2.5 h and ~$60–120 of
# Anthropic API. The default (2 queries × 2 versions = 4 runs) is roughly
# 6–10 h wall-clock and ~$240–480. Use --yes to skip the confirmation.
#
# Run isolation
# -------------
# Each query generates in its OWN dir (runs/<label>/query_<id>/) with its OWN
# fresh hyperresearch vault + SQLite DB, so fork/original and Q67/Q52 never
# share state within a run. BUT the harness reuses an existing run dir if one
# is present (it does NOT wipe it), so leftovers from a PRIOR benchmark would be
# picked up — the agent finds the existing .hyperresearch/ DB (sources, notes,
# drafts) and reuses it. Pass --fresh to wipe each version's runs/ dir AND its
# results JSONL first, so every report starts from an empty vault/DB.
#
# Rate limits
# -----------
# The Mistral client retries on 429/5xx with backoff (honoring Retry-After), so
# any tier finishes without dropping RACE scores or FACT citations. On the free
# tier (1 req/s) add --free-tier (sets N_WORKERS=1 + LLM_MIN_INTERVAL=1.1) to
# stay under the limit and avoid wasted 429 round-trips.
#
# Prerequisites:
#   bash setup.sh
#   export MISTRAL_API_KEY=<key>          # RACE + FACT judge
#   export JINA_API_KEY=<key>             # FACT only (else pass --skip-fact)
#   claude --version                      # authenticated Claude Code CLI
#
# Usage:
#   bash compare.sh /path/to/your/hyperresearch
#   bash compare.sh /path/to/fork --original https://github.com/jordan-gibbs/hyperresearch.git
#   bash compare.sh /path/to/fork --queries "67 52" --skip-fact --yes
#   bash compare.sh /path/to/fork --fresh        # wipe prior runs first (clean DB)
#   bash compare.sh /path/to/fork --free-tier    # Mistral free tier: throttle to 1 req/s

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# Load secrets (MISTRAL_API_KEY, JINA_API_KEY) from a gitignored .env if present,
# so keys never have to be passed inline (and never land in the repo / on GitHub).
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

# ── Defaults ───────────────────────────────────────────────────────
FORK_PATH="$(cd "$HERE/../../hyperresearch" 2>/dev/null && pwd || true)"
ORIGINAL="https://github.com/jordan-gibbs/hyperresearch.git"
QUERIES="67 52"
TIMEOUT=10800
MODEL="opus"
SKIP_FACT=""
ASSUME_YES=false
FRESH=false
FREE_TIER=false

# ── Parse args ─────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --original)  ORIGINAL="$2"; shift ;;
        --queries)   QUERIES="$2"; shift ;;
        --timeout)   TIMEOUT="$2"; shift ;;
        --model)     MODEL="$2"; shift ;;
        --skip-fact) SKIP_FACT="--skip-fact" ;;
        --fresh)     FRESH=true ;;
        --free-tier) FREE_TIER=true ;;
        --yes|-y)    ASSUME_YES=true ;;
        --help|-h)   sed -n '2,51p' "$0"; exit 0 ;;
        -*)          echo "Unknown option: $1"; exit 1 ;;
        *)           FORK_PATH="$1" ;;
    esac
    shift
done

if [ -z "${FORK_PATH:-}" ] || [ ! -d "$FORK_PATH" ]; then
    echo "[ERROR] Fork path not found. Pass it explicitly:"
    echo "        bash compare.sh /path/to/your/hyperresearch"
    exit 1
fi
FORK_PATH="$(cd "$FORK_PATH" && pwd)"

# ── Sanity: keys + tools ───────────────────────────────────────────
if [ -z "${MISTRAL_API_KEY:-}" ]; then
    echo "[ERROR] export MISTRAL_API_KEY first (RACE + FACT judge)."; exit 1
fi
if [ -z "$SKIP_FACT" ] && [ -z "${JINA_API_KEY:-}" ]; then
    echo "[WARN] JINA_API_KEY unset → running RACE only (use --skip-fact to silence)."
    SKIP_FACT="--skip-fact"
fi
command -v claude >/dev/null || { echo "[ERROR] claude CLI not found / authenticated."; exit 1; }

# Free tier: serialize to one request/second so RACE + FACT never trip the limit.
# (The Mistral client also retries on 429 regardless, so this just avoids waste.)
if [ "$FREE_TIER" = true ]; then
    export N_WORKERS="${N_WORKERS:-1}"
    export LLM_MIN_INTERVAL="${LLM_MIN_INTERVAL:-1.1}"
    echo "[FREE-TIER] N_WORKERS=$N_WORKERS  LLM_MIN_INTERVAL=${LLM_MIN_INTERVAL}s  (stays under 1 req/s)"
fi

echo "=== hyperresearch fork-vs-original comparison ==="
echo "Fork:      $FORK_PATH"
echo "Original:  $ORIGINAL"
echo "Queries:   $QUERIES"
echo "Model:     $MODEL    Timeout: ${TIMEOUT}s/query    FACT: $([ -n "$SKIP_FACT" ] && echo off || echo on)    Fresh: $([ "$FRESH" = true ] && echo on || echo off)"
echo ""

NUM_Q=$(echo $QUERIES | wc -w | tr -d ' ')
echo "This will run $((NUM_Q * 2)) full-tier generations (~$((NUM_Q*2*2))–$((NUM_Q*2*3)) h, ~\$$((NUM_Q*2*60))–$((NUM_Q*2*120)))."
if [ "$ASSUME_YES" != true ]; then
    read -r -p "Proceed? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# Hold off idle sleep for the whole comparison (tied to this script's PID, so it
# releases automatically when we exit). Idle sleep was a secondary risk; the
# PRIMARY durability fix is launching this script via run_durable.sh, which
# detaches it from the parent session/process-group. Belt and suspenders.
if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -dimsu -w "$$" &
    echo "[CAFFEINATE] idle sleep held off for the comparison (pid $$)"
fi

# Clone the original into a temp checkout if it's a git URL
ORIG_SPEC="$ORIGINAL"
if [[ "$ORIGINAL" == http*://* || "$ORIGINAL" == git@* ]]; then
    ORIG_DIR="$HERE/.original_checkout"
    if [ -d "$ORIG_DIR/.git" ]; then
        echo "[OK] Original already cloned at $ORIG_DIR"
    else
        echo "[FETCH] Cloning original → $ORIG_DIR"
        rm -rf "$ORIG_DIR"
        git clone --depth 1 "$ORIGINAL" "$ORIG_DIR"
    fi
    ORIG_SPEC="$ORIG_DIR"
fi

# ── Run one version end-to-end ─────────────────────────────────────
# $1 = label, $2 = pip install spec (path or package)
run_version () {
    local label="$1" spec="$2"
    echo ""
    echo "############################################################"
    echo "# VERSION: $label   ($spec)"
    echo "############################################################"

    echo "[PIP] Installing $label …"
    python -m pip install --upgrade --force-reinstall "$spec"
    echo "[INSTALL] hyperresearch install --global"
    hyperresearch install --global
    hyperresearch --version || true

    # Fresh start: wipe this version's run dirs (vaults + SQLite DBs) and its
    # results JSONL so every report regenerates from an empty vault.
    if [ "$FRESH" = true ]; then
        echo "[FRESH] Wiping runs/${label}/ and results/${label}-research.jsonl"
        rm -rf "$HERE/runs/${label}"
        rm -f "$HERE/results/${label}-research.jsonl"
    fi

    # Clean, .claude-free project root so the GLOBAL skill drives the run
    local proot="$HERE/.compare_root_${label}"
    mkdir -p "$proot"

    for q in $QUERIES; do
        echo ""
        echo "=== [$label] generating query $q  @ $(date '+%Y-%m-%d %H:%M:%S') ==="
        echo "    (live: bash $HERE/watch_status.sh  |  log: runs/${label}/query_${q}/harness-query.log)"
        # -u: unbuffered, so progress reaches the log in real time even when this
        # script's stdout is redirected to a file (block buffering previously hid
        # the entire generation — and its death — behind an empty log).
        python -u "$HERE/harness.py" \
            --query "$q" \
            --output "${label}-research" \
            --runs-dir "$HERE/runs/${label}" \
            --project-root "$proot" \
            --model "$MODEL" \
            --timeout "$TIMEOUT" \
            --resume
        echo "=== [$label] query $q generation returned  @ $(date '+%Y-%m-%d %H:%M:%S') ==="
    done

    echo ""
    echo "=== [$label] grading with Mistral  @ $(date '+%Y-%m-%d %H:%M:%S') ==="
    # On a fresh run, force re-grading so RACE doesn't reuse stale raw_results.jsonl
    local force_arg=""
    [ "$FRESH" = true ] && force_arg="--force"
    # Grading must never abort the comparison: if this version's grade fails,
    # log it and continue so the OTHER version still generates + grades. (RACE
    # results are preserved even when FACT fails — see grade.sh.)
    bash "$HERE/grade.sh" "${label}-research" --only-en $SKIP_FACT $force_arg \
        || echo "[WARN] grading failed for ${label} — continuing to the next version."
}

run_version "fork" "$FORK_PATH"
run_version "original" "$ORIG_SPEC"

# ── Side-by-side summary ───────────────────────────────────────────
echo ""
echo "############################################################"
echo "# COMPARISON SUMMARY"
echo "############################################################"
python3 - "$HERE/results" <<'PY'
import os, sys, re
base = sys.argv[1]
def parse(path):
    d = {}
    if not os.path.exists(path): return d
    for line in open(path, encoding="utf-8"):
        m = re.match(r'\s*([A-Za-z _]+?):\s*([\d.]+)', line)
        if m: d[m.group(1).strip()] = float(m.group(2))
    return d

fork_race = parse(f"{base}/fork-research_score/race_result.txt")
orig_race = parse(f"{base}/original-research_score/race_result.txt")
fork_fact = parse(f"{base}/fork-research_score/fact_result.txt")
orig_fact = parse(f"{base}/original-research_score/fact_result.txt")

rows = ["Comprehensiveness","Insight","Instruction Following","Readability","Overall Score"]
print(f"\n{'RACE metric':<24}{'fork':>10}{'original':>10}{'Δ (fork-orig)':>16}")
print("-"*60)
for r in rows:
    f, o = fork_race.get(r), orig_race.get(r)
    if f is None and o is None: continue
    delta = (f-o) if (f is not None and o is not None) else float('nan')
    print(f"{r:<24}{(f if f is not None else float('nan')):>10.4f}{(o if o is not None else float('nan')):>10.4f}{delta:>16.4f}")

if fork_fact or orig_fact:
    print(f"\n{'FACT metric':<24}{'fork':>10}{'original':>10}{'Δ (fork-orig)':>16}")
    print("-"*60)
    for r in ["total_citations","total_valid_citations","valid_rate"]:
        f, o = fork_fact.get(r), orig_fact.get(r)
        if f is None and o is None: continue
        delta = (f-o) if (f is not None and o is not None) else float('nan')
        print(f"{r:<24}{(f if f is not None else float('nan')):>10.4f}{(o if o is not None else float('nan')):>10.4f}{delta:>16.4f}")

fo, oo = fork_race.get("Overall Score"), orig_race.get("Overall Score")
if fo is not None and oo is not None:
    verdict = "FORK wins" if fo>oo else ("ORIGINAL wins" if oo>fo else "TIE")
    print(f"\nVerdict (RACE overall): {verdict}  ({fo:.4f} vs {oo:.4f})")
print(f"\nPer-query detail: {base}/fork-research_score/  and  {base}/original-research_score/")
PY
echo ""
echo "[DONE] Comparison complete."
