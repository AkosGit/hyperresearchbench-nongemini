#!/bin/bash
# Launch a long-running benchmark command so it SURVIVES the two things that
# killed the 2026-06-16 Q67 run:
#
#   1. Parent teardown — the job was a child of the Claude Code agent session
#      and got reaped ~37 min in while the session sat idle. The OS did nothing
#      (no sleep, no reboot, no OOM). A python os.setsid() shim + nohup re-parent
#      the command into its OWN session (new process group, no controlling
#      terminal) so a SIGHUP / parent-process teardown can't cascade into it.
#      (macOS ships no `setsid`, hence the python shim.)
#   2. Idle sleep — caffeinate -dimsu holds off display/idle/disk/system sleep
#      for the whole run (defense in depth; keep the lid open on battery).
#
# A PID file + log file let you (or watch_status.sh) check on it from ANY shell,
# even after the launching session is gone.
#
# Usage:
#   bash run_durable.sh [--name NAME] [--log FILE] -- <command> [args...]
#
# Example (the Q67 fork-vs-original comparison):
#   export MISTRAL_API_KEY=... JINA_API_KEY=...
#   bash run_durable.sh --name q67-compare -- \
#       bash compare.sh /path/to/fork --queries 67 --fresh --yes
#
# Then, from any shell:
#   bash watch_status.sh            # is it alive? per-query heartbeat table
#   tail -f runs/q67-compare-*.log  # full driver output
#   kill -- -$(cat runs/q67-compare.pid)   # stop the whole group

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

NAME="run"
LOG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) NAME="$2"; shift 2 ;;
        --log)  LOG="$2";  shift 2 ;;
        --)     shift; break ;;
        -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
        *) echo "[ERROR] unknown arg: $1 (did you forget '--' before the command?)"; exit 1 ;;
    esac
done

if [ $# -lt 1 ]; then
    echo "[ERROR] no command given. Usage: bash run_durable.sh [--name N] -- <command...>"
    exit 1
fi

mkdir -p "$HERE/runs"
TS="$(date +%Y%m%d-%H%M%S)"
[ -n "$LOG" ] || LOG="$HERE/runs/${NAME}-${TS}.log"
PIDFILE="$HERE/runs/${NAME}.pid"

if ! command -v caffeinate >/dev/null 2>&1; then
    echo "[WARN] caffeinate not found (non-macOS?) — launching detached without sleep-guard."
    CAFF=()
else
    CAFF=(caffeinate -dimsu)
fi

{
    echo "================================================================"
    echo "[durable] $(date '+%Y-%m-%d %H:%M:%S')  starting: $*"
    echo "================================================================"
} >> "$LOG"

# Detach into a BRAND-NEW session so a parent teardown / SIGHUP / process-group
# kill (what reaped the original run) cannot cascade in. macOS has no `setsid`,
# so a tiny python shim does os.setsid() then execs the (caffeinated) command.
# nohup ignores SIGHUP; </dev/null + >>LOG 2>&1 detaches stdio so the parent
# closing its end can't EOF the job.
PYDETACH='import os,sys
try:
    os.setsid()
except OSError:
    pass
os.execvp(sys.argv[1], sys.argv[1:])'
nohup python3 -c "$PYDETACH" "${CAFF[@]}" "$@" </dev/null >>"$LOG" 2>&1 &
CHILD=$!
echo "$CHILD" > "$PIDFILE"
disown "$CHILD" 2>/dev/null || true

echo "[durable] launched : $*"
echo "[durable] pid      : $CHILD   (saved to $PIDFILE)"
echo "[durable] log      : $LOG"
echo "[durable] watch    : bash $HERE/watch_status.sh"
echo "[durable] tail     : tail -f \"$LOG\""
echo "[durable] stop     : kill -- -$CHILD    # whole group; harness's own claude"
echo "[durable]            subgroup may need: pkill -f 'claude -p'"
echo "[durable] NOTE: keep the Mac plugged in with the lid OPEN (caffeinate"
echo "[durable]       does not stop clamshell/lid-close sleep on battery)."
