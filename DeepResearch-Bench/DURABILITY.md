# Durability & live monitoring

Why this exists: on 2026-06-16 a `compare.sh` Q67 run (fork-vs-original) died
**~38 minutes in** — partway through the fork's pipeline (195 notes fetched,
through source-tensions; no draft, no `final_report`, nothing scoreable) — and
the failure was **invisible**: the log just stopped, looking identical to a
long-running step.

## Post-mortem

**What was ruled out (all checked against system logs):**

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Machine reboot | ❌ | `uptime` = 83 days |
| Claude app restart | ❌ | app PID unchanged since the launch day |
| Lid-close / idle sleep at death | ❌ | `lidopen` assertion incremented continuously across the death; no Sleep/Wake events |
| OOM / jetsam kill | ❌ | no `jetsam`/`memorystatus` in the unified log at the death minute |
| Per-query timeout | ❌ | died at ~38 min, far under the 3 h cap; whole tree died at once, not just `claude` |

**Root cause:** the generation was launched as a **background child of the
Claude Code agent session**. While the session sat idle, the runtime reaped the
background process group — taking `compare.sh`, `harness.py`, and `claude` with
it. The OS did nothing. So the fix is to (a) **decouple the job from the
session** and (b) **make liveness observable** so a stall/death is obvious in
seconds, not after forensic archaeology.

## What changed

**`harness.py` — real-time observability (was: `subprocess.run(capture_output=True)`, which buffered ALL output until exit):**
- Streams `claude` `stream-json` line-by-line to **`<run>/harness-query.log`** (timestamped: `tool_use`, `RESULT`, `END`).
- A heartbeat thread rewrites **`<run>/harness-status.json`** every ~20 s with `state`, `elapsed_s`, `heartbeat_iso`, event/tool counts, token totals, and `report_found`. A frozen `heartbeat_iso` = the harness is dead; a fresh heartbeat with a frozen `last_event_iso` = `claude` is alive but stalled.
- Persists the full raw stream to **`<run>/claude-stream.jsonl`** for post-hoc debugging + token accounting.
- Timeouts now **kill the whole `claude` process group** (subagents included, via `start_new_session=True` + `killpg`) and are recorded as a distinct `state=timeout`.
- Output is line-buffered (`reconfigure(line_buffering=True)` + `python -u` in `compare.sh`) so a redirected log is real-time.
- Bugfix: a run where **no** query produced a report no longer crashes the final summary with `FileNotFoundError` (which, under `compare.sh`'s `set -e`, would have aborted the whole comparison right after a timeout).

**`run_durable.sh` — survives session teardown + idle sleep:**
Detaches the command into a **brand-new session** (python `os.setsid()` shim — macOS has no `setsid`) under `caffeinate -dimsu`, with a PID file + log. The job no longer dies when the launching session goes away.

**`watch_status.sh` — one-command liveness check:**
Reads every `harness-status.json` and classifies each query `ALIVE / DEAD? / DONE / FAILED`. A stale heartbeat with `state=generating` is flagged as "harness process likely DEAD" — the exact signature of the original failure, now a one-glance check.

**`compare.sh` — defense in depth:** holds its own `caffeinate` for the run's lifetime and timestamps each phase.

## How to run a durable comparison

```bash
cd DeepResearch-Bench
source .venv/bin/activate                 # python 3.11–3.13 with the eval deps
export MISTRAL_API_KEY=...  JINA_API_KEY=...

# Launch detached + caffeinated — survives the launching shell/session dying:
bash run_durable.sh --name q67-compare -- \
    bash compare.sh /path/to/fork --queries 67 --fresh --yes

# Check liveness any time, from any shell:
bash watch_status.sh
#   [ALIVE ] fork/query_67  state=generating elapsed=412s hb_age=8s events=337 ...
#   [DEAD? ] ...            ^^ heartbeat 240s stale but state still 'generating'

tail -f runs/q67-compare-*.log            # full driver output
kill -- -$(cat runs/q67-compare.pid)      # stop the whole group
```

**Still required:** keep the Mac **plugged in with the lid open** — `caffeinate`
holds off *idle* sleep but not clamshell/lid-close sleep on battery.

## Second post-mortem: FACT grading aborted the comparison

After the durability fix, a re-run's fork **generated fine** (94 min, 68 KB report) but the comparison still died — this time in **grading**, before the original ran:

- **Symptom:** `[FACT 1/5] Extract citations` → `JSONDecodeError ... char 30216` (×3) → `extracted.jsonl` never written → `[FACT 2/5]` `FileNotFoundError` → under `set -e`, the whole `compare.sh` aborted.
- **Root cause:** the citation extractor (`mistral-small-latest`) truncated its JSON list at the 8192-token output cap on a 68 KB report. The upstream `extract.py` then (a) re-parsed the **same** truncated text 3× (the model is called once, outside the retry loop), and (b) **wrote no record on failure**, so the next step had no input file.

**Fixes:**
- `extract.py` (snapshot in `patches/extract_robust.py`, applied by `setup.sh`): `_parse_citations()` strict-parses, else **salvages every complete `{...}` object before the truncation**, and the per-article path **always writes a record** (empty citations rather than nothing). Unit-tested on valid / truncated / fenced / empty / garbage input.
- `grade.sh`: the FACT chain runs with errexit relaxed (`fact_ok` flag) so a FACT failure **never aborts grading — RACE scores are always preserved**.
- `compare.sh`: the `grade.sh` call is `|| `-guarded so a grading failure on one version **can't stop the other version** from running.

**Validated:** re-grading the existing fork report ran clean end-to-end — FACT recovered 79 citations (34 valid, rate 0.43) instead of crashing.

**Note on the rate-limited Mistral key:** grading logs were full of `429 — retry` backoffs. Use `--free-tier` (sets `N_WORKERS=1`, `LLM_MIN_INTERVAL=1.1`) on `compare.sh`/`grade.sh` to stay under 1 req/s and avoid wasted retries.
