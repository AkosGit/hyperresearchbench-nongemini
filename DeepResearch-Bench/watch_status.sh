#!/bin/bash
# One-shot liveness check for benchmark generations — answers "is it ACTUALLY
# alive?" without the forensic archaeology the 2026-06-16 post-mortem needed.
#
# harness.py's heartbeat thread rewrites <run>/harness-status.json every ~20s.
# This reads every such file and classifies each query:
#   ALIVE   state=generating AND heartbeat < 90s old
#   DEAD?   state=generating BUT heartbeat is stale  -> harness process is gone
#   DONE    a report was produced (state=completed)
#   FAILED  timeout / killed_signal_N / exit_N / no_report / error_*
#
# A frozen heartbeat with state still "generating" is the exact signature of the
# original failure — now it's one glance instead of pgrep+pmset+log-show.
#
# Usage:  bash watch_status.sh [runs_dir]        (default: ./runs)
#         watch -n 30 bash watch_status.sh       (live refresh)

set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNS="${1:-$HERE/runs}"

python3 - "$RUNS" <<'PY'
import json, sys, os, glob, time
runs = sys.argv[1]
now = time.time()
files = sorted(glob.glob(os.path.join(runs, "**", "harness-status.json"), recursive=True))
print(f"=== benchmark run status @ {time.strftime('%Y-%m-%d %H:%M:%S')}   ({runs}) ===")
if not files:
    print("  (no harness-status.json yet — nothing generating, or runs/ is empty)")
    sys.exit(0)
for f in files:
    try:
        s = json.load(open(f, encoding="utf-8"))
    except Exception as e:
        print(f"  [BADFILE] {f}: {e}")
        continue
    age = int(now - os.path.getmtime(f))
    st = str(s.get("state"))
    if st == "generating":
        flag = "ALIVE" if age < 90 else "DEAD?"
    elif st == "completed":
        flag = "DONE"
    elif st in ("no_report", "timeout") or st.startswith(("exit_", "killed_", "error")):
        flag = "FAILED"
    else:
        flag = st
    label = os.path.basename(os.path.dirname(f))          # e.g. query_67
    parent = os.path.basename(os.path.dirname(os.path.dirname(f)))  # e.g. fork
    rc = f"Y({s.get('report_chars')}ch)" if s.get("report_found") else "n"
    print(f"  [{flag:6}] {parent}/{label:10} state={st:14} "
          f"elapsed={s.get('elapsed_s')}s hb_age={age}s "
          f"events={s.get('events')} tools={s.get('tool_calls')} "
          f"tok={s.get('prompt_tokens')}p+{s.get('completion_tokens')}c report={rc}")
    if s.get("tool_counts"):
        tc = " ".join(f"{k}:{v}" for k, v in s["tool_counts"].items())
        print(f"           tools: {tc}")
    if st == "generating" and age >= 90:
        print(f"           ^^ heartbeat {age}s stale but state still 'generating' "
              f"=> harness PROCESS LIKELY DEAD (relaunch via run_durable.sh)")
PY
